"""FastAPI application entrypoint.

Deliberately thin. The API layer validates, authorises and delegates; no
business logic lives here. Routers are mounted per capability as each one
lands, so the shape of the system is visible from this file alone.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from backend.app.api.ui import mount_control_tower
from backend.app.api.v1 import control, health
from backend.app.config import Environment, get_settings
from backend.app.domain.taxonomy import load_taxonomy
from backend.app.observability.logging import configure_logging, get_logger

log = get_logger(__name__)

API_V1_PREFIX = "/api/v1"


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Validate configuration and fail fast before accepting any traffic."""
    settings = get_settings()
    configure_logging(
        level=settings.axon_log_level,
        json_output=settings.axon_env is not Environment.LOCAL,
    )

    # Loading the taxonomy here means a malformed declaration stops the process
    # at startup rather than silently weakening conflict detection later.
    taxonomy = load_taxonomy()

    log.info(
        "axonfde.startup",
        environment=settings.axon_env.value,
        llm_mode=settings.axon_llm_mode.value,
        taxonomy_version=taxonomy.version,
        observation_types=len(taxonomy.observations),
        tracing_enabled=settings.tracing_enabled,
        dev_tokens_allowed=settings.allow_dev_tokens,
    )

    yield

    log.info("axonfde.shutdown")


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="AxonFDE",
        description=(
            "Proactive decision intelligence and incident response for cold-chain logistics."
        ),
        version="0.1.0",
        lifespan=lifespan,
        # Interactive docs are useful locally and a needless surface elsewhere.
        docs_url="/docs" if settings.axon_env is not Environment.PRODUCTION else None,
        redoc_url=None,
        openapi_url="/openapi.json",
    )

    app.include_router(health.router, prefix=API_V1_PREFIX)
    app.include_router(control.router, prefix=API_V1_PREFIX)
    mount_control_tower(app)

    return app


app = create_app()
