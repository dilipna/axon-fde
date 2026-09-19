"""Tests for simulation output and its reproducibility guarantee.

The golden digests below are the strongest reproducibility claim this project
makes. A change to the physics, the fault schedule, the sensor model or the
event schema will change them, and that is the point: such a change silently
invalidates every stored benchmark result, so it must be a deliberate act that
updates these constants rather than something that slips through unnoticed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from simulator.incidentforge.emitters.files import (
    GROUND_TRUTH_FILE,
    TELEMETRY_FILE,
    compute_digest,
    write_run,
)
from simulator.incidentforge.generator import run_scenario
from simulator.incidentforge.scenarios import load_pack

pytestmark = pytest.mark.unit

PACK_DIR = Path(__file__).resolve().parents[2] / "data" / "scenarios" / "pack_v1"

#: Golden digests. Updating these is a deliberate act, not a fix.
GOLDEN_DIGESTS = {
    "compressor_degradation_pharma_01": (
        "cd8de617ff569bd70ec6a2705c79621a486961f0fca682809bbb47f34583a600"
    ),
    "normal_pharma_run_01": ("819347afb9e5a55858e8e5244b17f6cd4db0a38078dd76d145c00ed9fffc36b5"),
    "sensor_drift_pharma_01": ("cfeab66fb408b14bf06c826335e4a9de5fc48f57990dfda1fd9efbfa097a85df"),
}


@pytest.fixture(scope="module")
def pack():
    return load_pack(PACK_DIR)


# ---------------------------------------------------------------------------
# Reproducibility
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("scenario_id", sorted(GOLDEN_DIGESTS))
def test_scenario_matches_its_golden_digest(pack, scenario_id: str):
    """If this fails, either the simulator changed or the machine disagrees.

    Either way, every benchmark result recorded before the change is no longer
    comparable with results recorded after it.
    """
    digest = compute_digest(run_scenario(pack.scenarios[scenario_id]))
    assert digest == GOLDEN_DIGESTS[scenario_id], (
        f"{scenario_id} digest changed. If this was intentional, update "
        "GOLDEN_DIGESTS and re-baseline any stored benchmark results."
    )


def test_every_scenario_in_the_pack_has_a_golden_digest(pack):
    """A new scenario without a digest would drift unnoticed."""
    assert set(pack.scenarios) == set(GOLDEN_DIGESTS)


def test_digest_covers_telemetry_only(pack):
    """Ground-truth bookkeeping must not affect the digest.

    Telemetry is the contract with the application. A change to internal
    diagnostics that leaves telemetry identical should not invalidate stored
    results.
    """
    result = run_scenario(pack.scenarios["normal_pharma_run_01"])
    mutated = type(result)(
        scenario_id=result.scenario_id,
        seed=result.seed,
        events=result.events,
        ground_truth=[],  # ground truth discarded entirely
        actual_breach_minute=result.actual_breach_minute,
        reported_breach_minute=result.reported_breach_minute,
        first_saturation_minute=result.first_saturation_minute,
    )
    assert compute_digest(mutated) == compute_digest(result)


# ---------------------------------------------------------------------------
# Written artifacts
# ---------------------------------------------------------------------------


def test_write_run_produces_three_separate_artifacts(pack, tmp_path: Path):
    result = run_scenario(pack.scenarios["compressor_degradation_pharma_01"])
    artifacts = write_run(result, tmp_path, pack_version="1.0.0")

    assert artifacts.telemetry_path.is_file()
    assert artifacts.ground_truth_path.is_file()
    assert artifacts.manifest_path.is_file()
    # Separate files, so the application can be given one and not the other.
    assert artifacts.telemetry_path != artifacts.ground_truth_path


def test_written_telemetry_contains_no_ground_truth(pack, tmp_path: Path):
    """The boundary that keeps the risk model from training on the answer."""
    result = run_scenario(pack.scenarios["sensor_drift_pharma_01"])
    artifacts = write_run(result, tmp_path, pack_version="1.0.0")

    columns = set(pq.read_table(artifacts.telemetry_path).column_names)
    forbidden = {
        "true_cargo_temp_c",
        "sensor_error_c",
        "compressor_health",
        "in_breach",
        "saturated",
        "duty_cycle",
        "cooling_output_w",
    }
    assert not (columns & forbidden)


def test_ground_truth_file_retains_the_true_values(pack, tmp_path: Path):
    result = run_scenario(pack.scenarios["sensor_drift_pharma_01"])
    artifacts = write_run(result, tmp_path, pack_version="1.0.0")

    columns = set(pq.read_table(artifacts.ground_truth_path).column_names)
    assert {"true_cargo_temp_c", "sensor_error_c", "in_breach"} <= columns


def test_row_counts_match_the_run(pack, tmp_path: Path):
    scenario = pack.scenarios["normal_pharma_run_01"]
    artifacts = write_run(run_scenario(scenario), tmp_path, pack_version="1.0.0")

    assert pq.read_table(artifacts.telemetry_path).num_rows == scenario.duration_minutes
    assert pq.read_table(artifacts.ground_truth_path).num_rows == scenario.duration_minutes


def test_manifest_records_provenance_and_outcomes(pack, tmp_path: Path):
    result = run_scenario(pack.scenarios["compressor_degradation_pharma_01"])
    artifacts = write_run(result, tmp_path, pack_version="1.0.0")

    manifest = json.loads(artifacts.manifest_path.read_text(encoding="utf-8"))
    assert manifest["scenario_id"] == "compressor_degradation_pharma_01"
    assert manifest["pack_version"] == "1.0.0"
    assert manifest["seed"] == result.seed
    assert manifest["telemetry_digest_sha256"] == artifacts.digest
    assert manifest["actual_breach_minute"] == result.actual_breach_minute


def test_manifest_names_the_application_visible_file(pack, tmp_path: Path):
    """Makes the data boundary explicit to anyone inspecting the output."""
    result = run_scenario(pack.scenarios["normal_pharma_run_01"])
    artifacts = write_run(result, tmp_path, pack_version="1.0.0")

    manifest = json.loads(artifacts.manifest_path.read_text(encoding="utf-8"))
    assert manifest["application_may_read"] == [TELEMETRY_FILE]
    assert manifest["benchmark_only"] == [GROUND_TRUTH_FILE]


def test_rewriting_a_run_is_idempotent(pack, tmp_path: Path):
    """Runs are deterministic, so re-running rewrites identical content."""
    result = run_scenario(pack.scenarios["normal_pharma_run_01"])

    first = write_run(result, tmp_path, pack_version="1.0.0")
    first_bytes = first.telemetry_path.read_bytes()

    second = write_run(
        run_scenario(pack.scenarios["normal_pharma_run_01"]), tmp_path, pack_version="1.0.0"
    )
    assert second.telemetry_path.read_bytes() == first_bytes
    assert second.digest == first.digest
