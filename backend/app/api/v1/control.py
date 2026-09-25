"""What the control tower reads.

Three read-only endpoints behind the UI: the trace of the last closed-loop
run, the measured claim register, and the telemetry the detectors were judged
against.

**Why this router runs nothing.** The loop it displays talks to the seeded SQL
Server through `LegacyRepository`, and an import-linter contract forbids
`backend.app.api` from reaching pyodbc even transitively. The contract is
layering discipline rather than a live hazard - the repository already offloads
every ODBC call with `asyncio.to_thread` - but relaxing a stated invariant to
save a subprocess call is the kind of trade that is cheap once and expensive as
a habit. So `poe demo --json` runs the loop and writes a trace, and this router
serves it.

That split buys something beyond compliance. The demo owns a single
transaction for its whole run and rolls it back at the end, which is what makes
it repeatable; holding that transaction open across an HTTP request, for a
run that takes seconds and touches two databases, would be a worse design than
the one the contract pushed us into.

**Nothing here is a fixture.** Every field the UI renders was computed by a run
against real engines. If no run has happened the endpoints say so - they do not
fall back to sample data, because a dashboard that looks identical whether or
not the system works is worse than one that is plainly empty.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

router = APIRouter(tags=["control"])

PROJECT_ROOT = Path(__file__).resolve().parents[4]
TRACE_PATH = PROJECT_ROOT / "data" / "generated" / "demo-trace.json"
PUBLISHED_DIR = PROJECT_ROOT / "benchmarks" / "results" / "published"
GENERATED_DIR = PROJECT_ROOT / "data" / "generated"


class TraceMissing(BaseModel):
    """Why there is nothing to show, and the command that fixes it."""

    available: bool = False
    reason: str
    fix: str


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


@router.get("/control/trace", summary="The last closed-loop run, step by step")
def demo_trace() -> dict[str, Any]:
    """The narration of the last `poe demo --json`, as structured steps.

    404 rather than an empty shape when no run exists. A UI that renders an
    empty timeline looks like a system with nothing wrong; one that renders an
    error looks like a system that has not been run, which is the truth.
    """
    if not TRACE_PATH.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "available": False,
                "reason": "no closed-loop run has been recorded",
                "fix": "uv run poe demo --json",
            },
        )
    return _read_json(TRACE_PATH)


@router.get("/control/claims", summary="Measured claims from the published run")
def published_claims() -> dict[str, Any]:
    """The newest published benchmark run.

    Read from `benchmarks/results/published/` rather than from the whole
    results directory: those are the runs that back a number somebody can see,
    and they are the only ones eligible to appear in a UI. Exploratory runs
    stay out of it, which is invariant I7 applied to pixels rather than prose.
    """
    runs = sorted(PUBLISHED_DIR.glob("*.json"))
    if not runs:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "available": False,
                "reason": "no published benchmark run",
                "fix": "uv run poe bench --arm rules_only, then copy it into published/",
            },
        )

    newest = max(
        (_read_json(path) for path in runs),
        key=lambda payload: str(payload.get("completed_at", "")),
    )
    # A run measured against an uncommitted tree may be recorded but never
    # quoted, so the UI is told rather than left to infer it from the sha.
    newest.setdefault("provenance", {}).setdefault("reproducible", False)
    return newest


class TelemetryPoint(BaseModel):
    """One minute of the recording, as the application received it."""

    minute: int
    cargo_temp_c: float
    ambient_temp_c: float
    compressor_rpm: float
    # Carried so the chart can mark where the unit started reporting trouble.
    fault_codes: list[str] = Field(default_factory=list)


@router.get("/control/telemetry/{scenario_id}", summary="A scenario's recorded telemetry")
def telemetry(scenario_id: str) -> dict[str, Any]:
    """The reading stream both detectors were judged against.

    **Ground truth is not served here and there is no parameter that would
    serve it.** `GroundTruthFrame` is benchmark-only (invariant I8), and a
    chart that overlaid the true temperature on the reported one would put it
    one fetch away from the application. The sensor-drift scenarios are exactly
    the cases where the difference matters, so this is where that invariant
    would be most tempting to bend.
    """
    # Rejected before touching the filesystem: the id becomes a path segment,
    # and a scenario id is [a-z0-9_] by schema, so anything else is either a
    # typo or a traversal attempt and neither deserves a file read.
    if not scenario_id.replace("_", "").isalnum() or not scenario_id.islower():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{scenario_id!r} is not a scenario id",
        )

    path = GENERATED_DIR / scenario_id / "telemetry.parquet"
    if not path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "available": False,
                "reason": f"no recording for {scenario_id}",
                "fix": "uv run poe forge run-all",
            },
        )

    # Imported here rather than at module scope: pyarrow is a heavy dependency
    # and this is the only endpoint that needs it.
    import pyarrow.parquet as pq

    # Named columns, not the whole file. A `read_table(path)` here would pull
    # every column the emitter writes, and the one guarantee this endpoint
    # makes is about which columns leave it.
    table = pq.read_table(
        path,
        columns=[
            "sequence",
            "cargo_temp_c",
            "ambient_temp_c",
            "compressor_rpm",
            "fault_codes",
        ],
    )
    points = [
        {
            "minute": int(row["sequence"]),
            "cargo_temp_c": round(float(row["cargo_temp_c"]), 3),
            "ambient_temp_c": round(float(row["ambient_temp_c"]), 2),
            "compressor_rpm": round(float(row["compressor_rpm"]), 1),
            "fault_codes": list(row["fault_codes"] or []),
        }
        for row in table.to_pylist()
    ]
    return {"scenario_id": scenario_id, "points": points}
