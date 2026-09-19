"""Typed reads from the legacy ERP, and the evidence they produce.

The six ``vw_ai_*`` views are the entire surface the application is allowed to
touch, and the read-only login can reach nothing else. This module turns them
into two things: plain records for context, and ``Evidence`` for anything that
could be corroborated or contradicted by another source.

**Not every ERP field becomes evidence.** A customer name is context; it
cannot be in conflict with a photograph. The permitted temperature range can,
and is - the flagship scenario turns on the ERP saying one thing and the
signed shipping document saying another. The rule applied here is: a field
becomes evidence when a second source could plausibly disagree with it, and
stays a plain record otherwise.

**Why the queries are literal.** Every statement here is a fixed string with
bound parameters, never assembled from caller input. The SQL guard exists for
statements the model proposes; this module does not need it, and routing
hand-written queries through a guard designed for generated ones would blur
which path is which.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pyodbc

from backend.app.config import Settings
from backend.app.db.legacy.session import legacy_connection, run_in_thread
from backend.app.domain.enums import EvidenceSource, Modality
from backend.app.domain.evidence import EntityRef, Evidence, Provenance
from backend.app.domain.taxonomy import Taxonomy, load_taxonomy

__all__ = [
    "CargoRequirement",
    "LegacyRepository",
    "MaintenanceEvent",
    "ShipmentRecord",
]

PRODUCER = "legacy_erp_adapter"
PRODUCER_VERSION = "1.0.0"


@dataclass(frozen=True, slots=True)
class CargoRequirement:
    """The contractual temperature envelope, as the ERP holds it."""

    shipment_id: str
    cargo_class: str
    permitted_temp_min_c: float
    permitted_temp_max_c: float
    special_handling: str | None


@dataclass(frozen=True, slots=True)
class ShipmentRecord:
    """A shipment and the context around it."""

    shipment_id: str
    vehicle_id: str
    customer_name: str
    customer_tier: str
    origin: str
    destination: str
    status: str
    cargo_value_usd: float
    planned_arrival: datetime | None


@dataclass(frozen=True, slots=True)
class MaintenanceEvent:
    """One entry from the vehicle's service history."""

    event_id: str
    vehicle_id: str
    occurred_at: datetime
    event_type: str
    fault_codes: tuple[str, ...]
    severity: str
    days_ago: int
    technician_notes: str | None


def _as_utc(value: datetime | str | None) -> datetime | None:
    """Normalise a SQL Server timestamp, whichever ODBC driver returned it.

    Two things have to be absorbed here, and both were found by a test rather
    than anticipated.

    **The driver decides the Python type.** `ODBC Driver 18` hands back a
    `datetime`; the legacy `SQL Server` driver that ships with Windows - the
    only one installed on some development machines - hands back a string.
    The same code therefore runs against two different types depending on the
    machine, and CI would never have caught it because CI installs 18.

    **`DATETIME2` carries no offset.** The seed data is written in UTC, so UTC
    is attached explicitly rather than letting a naive value reach the evidence
    model, which would reject it with a message about a producer three layers
    away.

    SQL Server renders `DATETIME2` with seven fractional digits, which is not
    valid ISO-8601. Python 3.11 and later tolerate it; earlier versions do not,
    which is worth knowing if this ever has to run on an older runtime.
    """
    if value is None:
        return None
    moment = datetime.fromisoformat(value) if isinstance(value, str) else value
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _split_fault_codes(raw: str | None) -> tuple[str, ...]:
    """The ERP stores fault codes as a comma-separated string."""
    if not raw:
        return ()
    return tuple(part.strip() for part in raw.split(",") if part.strip())


class LegacyRepository:
    """Read-only access to the approved ERP views.

    Every call runs the blocking pyodbc work in a worker thread. There is no
    async ODBC driver, and doing this anywhere else would put a database the
    application does not control onto the event loop.
    """

    def __init__(self, *, settings: Settings | None = None) -> None:
        self._settings = settings

    # ------------------------------------------------------------------
    # Raw reads
    # ------------------------------------------------------------------

    def _fetch(self, sql: str, *params: Any) -> list[dict[str, Any]]:
        with legacy_connection(readonly=True, settings=self._settings) as connection:
            cursor = connection.cursor()
            cursor.execute(sql, *params)
            columns = [column[0] for column in cursor.description]
            return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]

    async def cargo_requirement(self, shipment_id: str) -> CargoRequirement | None:
        """The contractual envelope for one shipment."""
        rows = await run_in_thread(
            self._fetch,
            "SELECT shipment_id, cargo_class, permitted_temp_min_c, "
            "permitted_temp_max_c, special_handling "
            "FROM dbo.vw_ai_cargo_requirements WHERE shipment_id = ?",
            shipment_id,
        )
        if not rows:
            return None
        row = rows[0]
        return CargoRequirement(
            shipment_id=row["shipment_id"],
            cargo_class=row["cargo_class"],
            permitted_temp_min_c=float(row["permitted_temp_min_c"]),
            permitted_temp_max_c=float(row["permitted_temp_max_c"]),
            special_handling=row["special_handling"],
        )

    async def shipment(self, shipment_id: str) -> ShipmentRecord | None:
        rows = await run_in_thread(
            self._fetch,
            "SELECT shipment_id, vehicle_id, customer_name, customer_tier, origin, "
            "destination, status, cargo_value_usd, planned_arrival "
            "FROM dbo.vw_ai_shipments WHERE shipment_id = ?",
            shipment_id,
        )
        if not rows:
            return None
        row = rows[0]
        return ShipmentRecord(
            shipment_id=row["shipment_id"],
            vehicle_id=row["vehicle_id"],
            customer_name=row["customer_name"],
            customer_tier=row["customer_tier"],
            origin=row["origin"],
            destination=row["destination"],
            status=row["status"],
            cargo_value_usd=float(row["cargo_value_usd"]),
            planned_arrival=_as_utc(row["planned_arrival"]),
        )

    async def maintenance_history(
        self, vehicle_id: str, *, limit: int = 20
    ) -> list[MaintenanceEvent]:
        """Service history for one vehicle, most recent first.

        The prior AL17 warning on AX-042 lives here. It is what lets a fault
        code observed today be read as a known degrading compressor rather
        than as a novel event.
        """
        rows = await run_in_thread(
            self._fetch,
            "SELECT TOP (?) event_id, vehicle_id, occurred_at, event_type, fault_codes, "
            "severity, days_ago, technician_notes "
            "FROM dbo.vw_ai_maintenance WHERE vehicle_id = ? ORDER BY occurred_at DESC",
            limit,
            vehicle_id,
        )
        history: list[MaintenanceEvent] = []
        for row in rows:
            occurred_at = _as_utc(row["occurred_at"])
            if occurred_at is None:
                continue
            history.append(
                MaintenanceEvent(
                    event_id=row["event_id"],
                    vehicle_id=row["vehicle_id"],
                    occurred_at=occurred_at,
                    event_type=row["event_type"],
                    fault_codes=_split_fault_codes(row["fault_codes"]),
                    severity=row["severity"],
                    days_ago=int(row["days_ago"]),
                    technician_notes=row["technician_notes"],
                )
            )
        return history

    # ------------------------------------------------------------------
    # Evidence
    # ------------------------------------------------------------------

    async def cargo_requirement_evidence(
        self,
        shipment_id: str,
        *,
        observed_at: datetime,
        ingested_at: datetime | None = None,
        taxonomy: Taxonomy | None = None,
    ) -> list[Evidence]:
        """The ERP's view of the temperature envelope, as evidence.

        Attached to the *shipment* rather than the vehicle: the envelope is a
        property of what is being carried, and the same trailer carries
        different cargo next week. Getting this wrong would make two unrelated
        shipments look as though they disagreed about the same fact.

        These observations have no TTL. A contractual limit does not become
        less true with age, which is why the taxonomy declares
        `ttl_seconds: null` for them.
        """
        requirement = await self.cargo_requirement(shipment_id)
        if requirement is None:
            return []

        tax = taxonomy or load_taxonomy()
        entity = EntityRef(kind="shipment", id=shipment_id)
        provenance = Provenance(
            producer=PRODUCER,
            producer_version=PRODUCER_VERSION,
            note="dbo.vw_ai_cargo_requirements",
        )
        arrival = ingested_at or observed_at

        return [
            Evidence.create(
                entity_ref=entity,
                source=EvidenceSource.SQL_LEGACY,
                modality=Modality.STRUCTURED,
                observation_type=observation_type,
                value=value,
                observed_at=observed_at,
                ingested_at=arrival,
                provenance=provenance,
                taxonomy=tax,
            )
            for observation_type, value in (
                ("permitted_temp_min_c", requirement.permitted_temp_min_c),
                ("permitted_temp_max_c", requirement.permitted_temp_max_c),
                ("cargo_class", requirement.cargo_class),
            )
        ]

    async def maintenance_evidence(
        self,
        vehicle_id: str,
        *,
        observed_at: datetime,
        ingested_at: datetime | None = None,
        taxonomy: Taxonomy | None = None,
    ) -> list[Evidence]:
        """Prior fault codes and time since service, as evidence.

        Only warnings that are still open are recorded as
        `maintenance_warning`. A fault code from a repair that closed it is
        history, not a live warning, and treating the two the same would mean
        every serviced vehicle carries a permanent warning.
        """
        history = await self.maintenance_history(vehicle_id)
        if not history:
            return []

        tax = taxonomy or load_taxonomy()
        entity = EntityRef(kind="vehicle", id=vehicle_id)
        provenance = Provenance(
            producer=PRODUCER,
            producer_version=PRODUCER_VERSION,
            note="dbo.vw_ai_maintenance",
        )
        arrival = ingested_at or observed_at
        evidence: list[Evidence] = []

        warnings = sorted(
            {
                code
                for event in history
                if event.event_type == "warning"
                for code in event.fault_codes
            }
        )
        if warnings:
            evidence.append(
                Evidence.create(
                    entity_ref=entity,
                    source=EvidenceSource.SQL_LEGACY,
                    modality=Modality.STRUCTURED,
                    observation_type="maintenance_warning",
                    value=warnings,
                    observed_at=observed_at,
                    ingested_at=arrival,
                    provenance=provenance,
                    taxonomy=tax,
                )
            )

        serviced = [event for event in history if event.event_type in ("service", "repair")]
        if serviced:
            most_recent = max(event.occurred_at for event in serviced)
            days = (observed_at - most_recent).total_seconds() / 86400.0
            evidence.append(
                Evidence.create(
                    entity_ref=entity,
                    source=EvidenceSource.SQL_LEGACY,
                    modality=Modality.STRUCTURED,
                    observation_type="days_since_last_service",
                    # Computed against the observation time rather than read
                    # from the view's `days_ago`, which is relative to the
                    # server clock and would make a replayed scenario's
                    # evidence drift every time it ran.
                    value=round(max(0.0, days), 1),
                    observed_at=observed_at,
                    ingested_at=arrival,
                    provenance=provenance,
                    taxonomy=tax,
                )
            )
        return evidence


def is_connection_error(exc: BaseException) -> bool:
    """Whether a failure was the ERP being unreachable rather than a bad query."""
    return isinstance(exc, pyodbc.Error)
