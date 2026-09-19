"""Writing simulation output to disk.

Three artifacts per run, kept deliberately separate:

``telemetry.parquet``    What the application may read. Nothing else.
``ground_truth.parquet`` What actually happened. Benchmark only.
``manifest.json``        Provenance and a content digest.

The separation is enforced by directory layout and by convention here, and by
`test_telemetry_events_carry_no_ground_truth` in the test suite. If ground
truth reached the feature builder, the risk model would train on the answer and
every accuracy number downstream would be meaningless.

The manifest's digest is what makes reproducibility checkable rather than
asserted: the same scenario and seed must produce the same digest on any
machine, and a golden-file test holds the flagship scenario to it.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from simulator.incidentforge.generator import SimulationResult

__all__ = [
    "RunArtifacts",
    "compute_digest",
    "write_run",
]

TELEMETRY_FILE = "telemetry.parquet"
GROUND_TRUTH_FILE = "ground_truth.parquet"
MANIFEST_FILE = "manifest.json"


class RunArtifacts:
    """Where a run's output landed."""

    __slots__ = ("digest", "ground_truth_path", "manifest_path", "telemetry_path")

    def __init__(
        self,
        telemetry_path: Path,
        ground_truth_path: Path,
        manifest_path: Path,
        digest: str,
    ) -> None:
        self.telemetry_path = telemetry_path
        self.ground_truth_path = ground_truth_path
        self.manifest_path = manifest_path
        self.digest = digest


def compute_digest(result: SimulationResult) -> str:
    """A content hash over the telemetry stream.

    Covers only the application-visible events, because that is what
    determinism must hold for: a change in ground-truth bookkeeping that leaves
    telemetry identical does not invalidate a stored benchmark result.

    Floats are serialised through Python's repr via ``json.dumps`` with sorted
    keys, which is stable across platforms for IEEE-754 doubles.
    """
    hasher = hashlib.sha256()
    for event in result.events:
        payload = event.model_dump(mode="json")
        hasher.update(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    return hasher.hexdigest()


def _to_table(rows: list[dict[str, Any]]) -> pa.Table:
    if not rows:
        raise ValueError("refusing to write an empty table")
    columns = {key: [row[key] for row in rows] for key in rows[0]}
    return pa.table(columns)


def write_run(
    result: SimulationResult,
    out_dir: Path,
    *,
    pack_version: str,
) -> RunArtifacts:
    """Write a run's telemetry, ground truth and manifest.

    Output lands in ``out_dir/<scenario_id>/``. Re-running overwrites, which is
    safe because the run is deterministic: identical inputs rewrite identical
    bytes.
    """
    run_dir = out_dir / result.scenario_id
    run_dir.mkdir(parents=True, exist_ok=True)

    telemetry_path = run_dir / TELEMETRY_FILE
    ground_truth_path = run_dir / GROUND_TRUTH_FILE
    manifest_path = run_dir / MANIFEST_FILE

    pq.write_table(
        _to_table([e.model_dump(mode="json") for e in result.events]),
        telemetry_path,
        compression="snappy",
    )
    pq.write_table(
        _to_table([f.model_dump(mode="json") for f in result.ground_truth]),
        ground_truth_path,
        compression="snappy",
    )

    digest = compute_digest(result)

    manifest = {
        "scenario_id": result.scenario_id,
        "pack_version": pack_version,
        "seed": result.seed,
        "event_count": len(result.events),
        "telemetry_digest_sha256": digest,
        # Outcomes, recorded so a benchmark can be graded without re-running.
        "actual_breach_minute": result.actual_breach_minute,
        "reported_breach_minute": result.reported_breach_minute,
        "first_saturation_minute": result.first_saturation_minute,
        "sensor_lied": result.sensor_lied,
        "generated_at": datetime.now(UTC).isoformat(),
        # Deliberately recorded: the ground-truth file must never be read by
        # the application, and naming it here makes that boundary explicit to
        # anyone inspecting the output.
        "application_may_read": [TELEMETRY_FILE],
        "benchmark_only": [GROUND_TRUTH_FILE],
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    return RunArtifacts(telemetry_path, ground_truth_path, manifest_path, digest)
