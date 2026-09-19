"""Health and dependency probes.

Two distinct endpoints, because they answer different questions:

``/health``       Is this process alive and correctly configured?
                  Used by load balancers and container orchestrators. Must not
                  depend on anything external, or a database blip cycles every
                  healthy instance.

``/health/deps``  Which dependencies are actually reachable?
                  Used by humans and by the bootstrap script to diagnose a
                  half-started environment. Never gates traffic.
"""

from __future__ import annotations

import asyncio
import contextlib
from enum import StrEnum
from typing import Literal
from urllib.parse import urlparse

from fastapi import APIRouter
from pydantic import BaseModel, Field

from backend.app.config import Settings, get_settings

router = APIRouter(tags=["health"])

#: A probe that takes longer than this is reported as unreachable. Kept short
#: because this endpoint is used interactively while bringing an environment up.
_PROBE_TIMEOUT_SECONDS = 2.0


class DependencyStatus(StrEnum):
    REACHABLE = "reachable"
    UNREACHABLE = "unreachable"
    NOT_CONFIGURED = "not_configured"


class DependencyReport(BaseModel):
    name: str
    status: DependencyStatus
    target: str = Field(description="Host and port probed, never credentials")
    detail: str | None = None
    latency_ms: float | None = None


class HealthResponse(BaseModel):
    status: Literal["ok"]
    environment: str
    llm_mode: str
    tracing_enabled: bool


class DependencyResponse(BaseModel):
    status: Literal["ok", "degraded"]
    dependencies: list[DependencyReport]


async def _probe_tcp(name: str, host: str, port: int) -> DependencyReport:
    """Check that something is listening, without authenticating.

    A TCP probe deliberately does not verify credentials or schema. It answers
    "is the container up", which is the question being asked while bringing an
    environment up, and it cannot be fooled into logging a password.
    """
    target = f"{host}:{port}"
    loop = asyncio.get_running_loop()
    started = loop.time()
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=_PROBE_TIMEOUT_SECONDS
        )
    except TimeoutError:
        return DependencyReport(
            name=name,
            status=DependencyStatus.UNREACHABLE,
            target=target,
            detail=f"no response within {_PROBE_TIMEOUT_SECONDS}s",
        )
    except OSError as exc:
        return DependencyReport(
            name=name,
            status=DependencyStatus.UNREACHABLE,
            target=target,
            detail=exc.strerror or str(exc),
        )

    writer.close()
    # The peer closing first is not a failure of the probe.
    with contextlib.suppress(OSError):
        await writer.wait_closed()

    return DependencyReport(
        name=name,
        status=DependencyStatus.REACHABLE,
        target=target,
        latency_ms=round((loop.time() - started) * 1000, 2),
    )


async def _probe_all(settings: Settings) -> list[DependencyReport]:
    probes = [
        _probe_tcp("postgres", settings.postgres_host, settings.postgres_port),
        _probe_tcp("mssql_legacy", settings.mssql_host, settings.mssql_port),
    ]

    if settings.s3_endpoint_url:
        parsed = urlparse(settings.s3_endpoint_url)
        if parsed.hostname:
            probes.append(_probe_tcp("object_store", parsed.hostname, parsed.port or 80))

    return list(await asyncio.gather(*probes))


@router.get("/health", response_model=HealthResponse, summary="Liveness probe")
async def health() -> HealthResponse:
    settings = get_settings()
    return HealthResponse(
        status="ok",
        environment=settings.axon_env.value,
        llm_mode=settings.axon_llm_mode.value,
        tracing_enabled=settings.tracing_enabled,
    )


@router.get(
    "/health/deps",
    response_model=DependencyResponse,
    summary="Dependency reachability",
)
async def health_dependencies() -> DependencyResponse:
    reports = await _probe_all(get_settings())
    degraded = any(r.status is DependencyStatus.UNREACHABLE for r in reports)
    return DependencyResponse(
        status="degraded" if degraded else "ok",
        dependencies=reports,
    )
