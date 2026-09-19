"""Turning a telemetry stream into typed evidence.

This is the first adapter, and it sets the pattern the others follow: a
source-specific reader that knows one input format, and produces nothing but
``Evidence`` built through ``Evidence.create()``. Everything downstream -
reconciliation, detection, risk, the narrative - sees only that shape, which
is what makes a photographed gauge comparable to a database row.

**Why it reads Parquet rather than importing IncidentForge.** The simulator is
a separate package with its own contract, and an import-linter rule already
keeps it from depending on the application. Depending on it in the other
direction would be just as wrong: in Phase 2 these readings arrive from a
Redpanda topic, and the adapter must not need rewriting when they do. What is
stable is the column vocabulary, not the producer.

**Why not every column becomes evidence.** ``latitude`` and ``longitude`` have
no observation type: they are context for a map, not something that can be
corroborated or contradicted, and inventing a type for them would put
un-reconcilable values into a store whose whole purpose is reconciliation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from backend.app.domain.enums import EvidenceSource, Modality
from backend.app.domain.evidence import EntityRef, Evidence, ObservationValue, Provenance
from backend.app.domain.taxonomy import Taxonomy, load_taxonomy

__all__ = [
    "TELEMETRY_COLUMNS",
    "TelemetryReading",
    "read_telemetry",
    "reading_to_evidence",
]

PRODUCER = "telemetry_adapter"
PRODUCER_VERSION = "1.0.0"

#: Telemetry column -> observation type. The two disagree in one place
#: (`fault_codes` carries the type `fault_code`), which is exactly why this is
#: an explicit table rather than an assumption that the names match.
TELEMETRY_COLUMNS: dict[str, str] = {
    "cargo_temp_c": "cargo_temp_c",
    "ambient_temp_c": "ambient_temp_c",
    "humidity_pct": "humidity_pct",
    "compressor_rpm": "compressor_rpm",
    "reefer_status": "reefer_status",
    "reefer_fuel_pct": "reefer_fuel_pct",
    "truck_fuel_pct": "truck_fuel_pct",
    "door_state": "door_state",
    "speed_kph": "speed_kph",
    "route_delay_min": "route_delay_min",
    "minutes_to_destination": "minutes_to_destination",
    "fault_codes": "fault_code",
}

#: Columns that identify the reading rather than being one. Listed so that a
#: new column added upstream is noticed rather than silently dropped.
_IDENTITY_COLUMNS = frozenset({"vehicle_id", "shipment_id", "timestamp", "sequence"})

#: Deliberately not evidence: position is context for a map, not a claim that
#: another source could corroborate or contradict.
_IGNORED_COLUMNS = frozenset({"latitude", "longitude"})


class TelemetryReading:
    """One telemetry sample, decoupled from whoever produced it.

    A plain container rather than a Pydantic model: it exists only to carry a
    row from the reader to the converter, and validation happens where it
    matters, in ``Evidence.create()``.
    """

    __slots__ = ("measurements", "sequence", "shipment_id", "timestamp", "vehicle_id")

    def __init__(
        self,
        *,
        vehicle_id: str,
        shipment_id: str,
        timestamp: datetime,
        sequence: int,
        measurements: dict[str, Any],
    ) -> None:
        self.vehicle_id = vehicle_id
        self.shipment_id = shipment_id
        self.timestamp = timestamp
        self.sequence = sequence
        self.measurements = measurements


class UnknownTelemetryColumnError(ValueError):
    """A column arrived that the adapter has no mapping for.

    Fatal rather than ignored. A silently dropped column is a sensor whose
    readings never become evidence, which shows up much later as a detector
    that mysteriously never fires.
    """


def _parse_timestamp(raw: object) -> datetime:
    """Read a timestamp from Parquet, whatever shape it arrives in.

    The emitter writes ISO-8601 strings, but a Parquet timestamp column reads
    back as a `datetime`. Both are accepted; neither is allowed to be naive,
    because a naive reading would corrupt every freshness and conflict-window
    comparison downstream.
    """
    if isinstance(raw, datetime):
        moment = raw
    elif isinstance(raw, str):
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    else:
        raise TypeError(f"cannot read a timestamp from {type(raw).__name__}: {raw!r}")

    if moment.tzinfo is None:
        raise ValueError(
            f"telemetry timestamp {moment.isoformat()} has no timezone; the "
            "producer must emit an explicit offset"
        )
    return moment.astimezone(UTC)


def read_telemetry(path: Path) -> list[TelemetryReading]:
    """Load a telemetry file into readings, in stream order.

    Raises:
        UnknownTelemetryColumnError: The file carries a column this adapter
            does not know how to interpret.
    """
    table = pq.read_table(path)
    unknown = set(table.column_names) - set(TELEMETRY_COLUMNS) - _IDENTITY_COLUMNS
    unknown -= _IGNORED_COLUMNS
    if unknown:
        raise UnknownTelemetryColumnError(
            f"{path.name} carries columns with no observation type: "
            f"{sorted(unknown)}. Add them to TELEMETRY_COLUMNS or to the "
            "ignore list deliberately; dropping them silently would mean a "
            "sensor whose readings never become evidence."
        )

    readings: list[TelemetryReading] = []
    for row in table.to_pylist():
        readings.append(
            TelemetryReading(
                vehicle_id=row["vehicle_id"],
                shipment_id=row["shipment_id"],
                timestamp=_parse_timestamp(row["timestamp"]),
                sequence=int(row["sequence"]),
                measurements={column: row[column] for column in TELEMETRY_COLUMNS if column in row},
            )
        )
    # Sorted by sequence rather than trusting file order: a replay from an
    # event stream in Phase 2 offers no ordering guarantee, and the
    # supersession rules depend on knowing which reading is newer.
    readings.sort(key=lambda reading: reading.sequence)
    return readings


def reading_to_evidence(
    reading: TelemetryReading,
    *,
    ingested_at: datetime | None = None,
    taxonomy: Taxonomy | None = None,
    scenario_run_id: str | None = None,
) -> list[Evidence]:
    """Convert one reading into one piece of evidence per measurement.

    An empty ``fault_codes`` list is still recorded. "No active faults" is an
    observation that a sensor genuinely made, and it is what distinguishes the
    sensor-drift scenario - a reported breach with no fault code - from a real
    compressor failure. Dropping empty lists would erase exactly the signal
    that case turns on.
    """
    tax = taxonomy or load_taxonomy()
    entity = EntityRef(kind="vehicle", id=reading.vehicle_id)
    # Defaults to the reading's own timestamp rather than to the wall clock, so
    # that replaying a scenario recorded last month does not mark every
    # observation as stale on arrival.
    arrival = ingested_at or reading.timestamp

    provenance = Provenance(
        producer=PRODUCER,
        producer_version=PRODUCER_VERSION,
        note=f"sequence {reading.sequence}" + (f" of {scenario_run_id}" if scenario_run_id else ""),
    )

    evidence: list[Evidence] = []
    for column, observation_type in TELEMETRY_COLUMNS.items():
        if column not in reading.measurements:
            continue
        raw = reading.measurements[column]
        if raw is None:
            # An absent reading is represented by its absence. Substituting a
            # default here would fabricate an observation, which is the one
            # thing this system must never do.
            continue

        evidence.append(
            Evidence.create(
                entity_ref=entity,
                source=EvidenceSource.TELEMETRY,
                modality=Modality.TIMESERIES,
                observation_type=observation_type,
                value=_coerce(raw),
                observed_at=reading.timestamp,
                ingested_at=arrival,
                provenance=provenance,
                taxonomy=tax,
            )
        )
    return evidence


def _coerce(raw: Any) -> ObservationValue:
    """Normalise a Parquet value into an observation value.

    Lists are materialised as `list[str]` because pyarrow hands back a plain
    list whose members may be any Arrow scalar type, and the taxonomy's
    set-valued shape check requires strings.
    """
    if isinstance(raw, list):
        return [str(item) for item in raw]
    return raw  # type: ignore[no-any-return]
