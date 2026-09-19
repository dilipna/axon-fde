"""The telemetry adapter: readings in, typed evidence out.

These are unit tests over a generated Parquet file rather than integration
tests, because the adapter touches no database. What they protect is the
mapping itself, which is the kind of code that looks obviously correct and
quietly drops a column.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from backend.app.domain.enums import EvidenceSource, Modality
from backend.app.evidence.telemetry import (
    TELEMETRY_COLUMNS,
    UnknownTelemetryColumnError,
    read_telemetry,
    reading_to_evidence,
)
from tests.conftest import recording

pytestmark = pytest.mark.unit

SCENARIO = "compressor_degradation_pharma_01"


@pytest.fixture
def flagship() -> Path:
    """The generated flagship recording, or a skip that CI turns into a failure."""
    return recording(SCENARIO)


def _row(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "vehicle_id": "AX-042",
        "shipment_id": "SH-2041",
        "timestamp": "2026-07-14T10:00:00Z",
        "sequence": 0,
        "cargo_temp_c": 4.97,
        "ambient_temp_c": 29.18,
        "humidity_pct": 57.3,
        "compressor_rpm": 0.0,
        "reefer_status": "standby",
        "reefer_fuel_pct": 95.0,
        "truck_fuel_pct": 77.98,
        "door_state": "closed",
        "speed_kph": 95.2,
        "route_delay_min": 0.0,
        "minutes_to_destination": 240.0,
        "latitude": 41.8781,
        "longitude": -87.6298,
        "fault_codes": [],
    }
    base.update(overrides)
    return base


def _write(tmp_path: Path, rows: list[dict[str, object]]) -> Path:
    path = tmp_path / "telemetry.parquet"
    columns = {key: [row[key] for row in rows] for key in rows[0]}
    pq.write_table(pa.table(columns), path)
    return path


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def test_every_telemetry_column_has_an_observation_type_or_is_ignored(tmp_path: Path) -> None:
    """A column with no mapping must fail loudly, not vanish.

    A silently dropped column is a sensor whose readings never become
    evidence. That surfaces much later as a detector that inexplicably never
    fires, and by then nobody is looking at the adapter.
    """
    path = _write(tmp_path, [_row(cargo_door_ajar_sensor=1.0)])
    with pytest.raises(UnknownTelemetryColumnError, match="cargo_door_ajar_sensor"):
        read_telemetry(path)


def test_readings_come_back_in_sequence_order(tmp_path: Path) -> None:
    """Phase 2 delivers these from a topic, which guarantees no ordering.

    Supersession depends on knowing which reading is newer, so the adapter
    sorts rather than trusting whatever order the file happened to hold.
    """
    path = _write(
        tmp_path,
        [
            _row(sequence=2, timestamp="2026-07-14T10:02:00Z"),
            _row(sequence=0, timestamp="2026-07-14T10:00:00Z"),
            _row(sequence=1, timestamp="2026-07-14T10:01:00Z"),
        ],
    )
    assert [reading.sequence for reading in read_telemetry(path)] == [0, 1, 2]


def test_a_naive_timestamp_is_refused(tmp_path: Path) -> None:
    """A reading with no offset corrupts every freshness comparison after it."""
    path = _write(tmp_path, [_row(timestamp="2026-07-14T10:00:00")])
    with pytest.raises(ValueError, match="no timezone"):
        read_telemetry(path)


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------


def test_one_reading_becomes_one_observation_per_measured_column(tmp_path: Path) -> None:
    path = _write(tmp_path, [_row()])
    evidence = reading_to_evidence(read_telemetry(path)[0])

    assert len(evidence) == len(TELEMETRY_COLUMNS)
    assert {item.observation_type for item in evidence} == set(TELEMETRY_COLUMNS.values())
    assert all(item.source is EvidenceSource.TELEMETRY for item in evidence)
    assert all(item.modality is Modality.TIMESERIES for item in evidence)


def test_no_telemetry_observation_is_ever_authored_by_a_model(tmp_path: Path) -> None:
    """Invariant I1 at the adapter boundary.

    The enum makes an LLM source unrepresentable, but the adapter is where an
    inference could plausibly be dressed up as a reading, so it is asserted
    here as well as in the type.
    """
    path = _write(tmp_path, [_row()])
    for item in reading_to_evidence(read_telemetry(path)[0]):
        assert item.source is EvidenceSource.TELEMETRY
        assert "llm" not in item.source.value


def test_an_empty_fault_code_list_is_recorded_rather_than_dropped(tmp_path: Path) -> None:
    """ "No active faults" is an observation, and it is the decisive one.

    The sensor-drift scenario turns on a reported breach arriving with *no*
    fault code. If empty lists were dropped as uninteresting, the absence
    would be indistinguishable from never having looked, and the case that
    separates a drifting sensor from a failing compressor would disappear.
    """
    path = _write(tmp_path, [_row(fault_codes=[])])
    evidence = reading_to_evidence(read_telemetry(path)[0])

    faults = [item for item in evidence if item.observation_type == "fault_code"]
    assert len(faults) == 1
    assert faults[0].value == []


def test_fault_codes_survive_as_a_list_of_strings(tmp_path: Path) -> None:
    path = _write(tmp_path, [_row(fault_codes=["AL17", "AL02"])])
    evidence = reading_to_evidence(read_telemetry(path)[0])

    faults = next(item for item in evidence if item.observation_type == "fault_code")
    assert faults.value == ["AL17", "AL02"]
    assert all(isinstance(code, str) for code in faults.value)


def test_confidence_is_computed_and_never_uniform(tmp_path: Path) -> None:
    """Invariant I2: the adapter supplies no confidence of its own.

    Different observation types declare different telemetry reliabilities, so
    a single reading must produce a spread of confidences. All-equal values
    would mean the taxonomy was bypassed and a constant substituted.
    """
    path = _write(tmp_path, [_row()])
    confidences = {item.confidence for item in reading_to_evidence(read_telemetry(path)[0])}
    assert len(confidences) > 1
    assert all(0.0 <= value <= 1.0 for value in confidences)


def test_replaying_an_old_recording_does_not_mark_everything_stale(tmp_path: Path) -> None:
    """Ingestion defaults to the reading's own time, not the wall clock.

    A scenario recorded months ago must replay with fresh readings. Dating
    ingestion to now would age every observation past its TTL and quietly
    degrade the confidence of an entire benchmark run.
    """
    path = _write(tmp_path, [_row()])
    evidence = reading_to_evidence(read_telemetry(path)[0])
    assert not any(item.is_stale for item in evidence)


def test_a_late_arriving_reading_is_aged_by_its_ingestion_time(tmp_path: Path) -> None:
    """Freshness must reflect delivery lag when there genuinely is one."""
    path = _write(tmp_path, [_row()])
    reading = read_telemetry(path)[0]
    delayed = reading_to_evidence(reading, ingested_at=reading.timestamp + timedelta(hours=3))
    temperature = next(item for item in delayed if item.observation_type == "cargo_temp_c")
    assert temperature.is_stale


# ---------------------------------------------------------------------------
# Against the real flagship recording
# ---------------------------------------------------------------------------


def test_the_flagship_recording_converts_without_loss(flagship: Path) -> None:
    """Every reading in the real scenario produces a full evidence set."""
    readings = read_telemetry(flagship)
    assert len(readings) == 240
    assert readings[0].vehicle_id == "AX-042"
    assert readings[0].shipment_id == "SH-2041"

    produced = [reading_to_evidence(reading) for reading in readings]
    assert all(len(item) == len(TELEMETRY_COLUMNS) for item in produced)


def test_the_flagship_recording_carries_no_ground_truth_columns(flagship: Path) -> None:
    """Invariant I8, checked where the application actually reads.

    `ground_truth.parquet` is a separate file, but the guarantee that matters
    is that nothing the adapter reads exposes the answer. Asserted against the
    adapter's own column vocabulary rather than against the file, so a
    ground-truth column added upstream would fail here too.
    """
    forbidden = {
        "true_cargo_temp_c",
        "sensor_error_c",
        "compressor_health",
        "in_breach",
        "saturated",
        "root_cause",
    }
    assert not forbidden & set(pq.read_table(flagship).column_names)
    assert not forbidden & set(TELEMETRY_COLUMNS)


def test_the_flagship_recording_starts_inside_its_envelope(flagship: Path) -> None:
    """The scenario is only meaningful if it begins in spec and degrades."""
    readings = read_telemetry(flagship)
    first = readings[0].measurements["cargo_temp_c"]
    assert 2.0 <= first <= 8.0

    last = readings[-1].measurements["cargo_temp_c"]
    assert last > 8.0, "the flagship scenario must end outside its envelope"


def test_the_recording_is_one_reading_per_minute(flagship: Path) -> None:
    """Minute offsets are used as lead-time units, so the spacing must hold."""
    readings = read_telemetry(flagship)
    start = readings[0].timestamp
    assert start == datetime(2026, 7, 14, 10, 0, tzinfo=UTC)
    for reading in readings:
        assert reading.timestamp - start == timedelta(minutes=reading.sequence)
