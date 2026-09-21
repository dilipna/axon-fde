"""Replaying a scenario through the evidence pipeline.

This is the first place the pieces meet: telemetry, the ERP, a signed
document, reconciliation, detection and persistence. It exists so that the
claim "the system detects this" is something that can be run rather than
argued about.

**The order matters, and it is not arbitrary.** Context evidence - the
contractual envelope, the maintenance history - is loaded and reconciled
*before* the telemetry replay starts, because the envelope is what the
readings are judged against. Loading it afterwards would mean the early
readings were compared to nothing.

**Why the envelope comes out of reconciliation rather than configuration.**
For the flagship shipment the ERP says the cargo may reach 10 C and the signed
Bill of Lading says 8 C. Those are two sources disagreeing about the same
typed observation, which is exactly what the taxonomy's authority order is
for: the document wins, the disagreement stays visible as a conflict, and the
detector judges against 8 C. Hard-coding either number would delete the part
of this scenario that makes it worth running.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from uuid import UUID

from backend.app.audit.service import AuditService
from backend.app.db.app.repositories.evidence import EvidenceRepository
from backend.app.db.app.repositories.incident import IncidentRepository
from backend.app.db.legacy.repository import LegacyRepository
from backend.app.domain.envelope import TemperatureEnvelope
from backend.app.domain.evidence import Evidence
from backend.app.domain.taxonomy import Taxonomy, load_taxonomy
from backend.app.evidence.documents import extraction_to_evidence, load_extractions
from backend.app.evidence.envelope import resolve_envelope
from backend.app.evidence.reconciliation import Conflict, reconcile
from backend.app.evidence.telemetry import read_telemetry, reading_to_evidence
from backend.app.incidents.detection import BaselineDetector, Detection, Detector
from backend.app.incidents.lifecycle import IncidentService
from backend.app.risk.features import DEFAULT_WINDOW_MINUTES

__all__ = ["ReplayResult", "ScenarioReplay", "resolve_envelope"]

#: Readings per database round trip. Large enough that a 240-minute scenario
#: is a handful of flushes rather than 240, small enough that a fleet-scale
#: replay does not build an unbounded list in memory first.
_BATCH_READINGS = 30


@dataclass(slots=True)
class ReplayResult:
    """What a replay produced."""

    scenario_run_id: str
    readings_replayed: int = 0
    evidence_persisted: int = 0
    envelope: TemperatureEnvelope | None = None
    conflicts: tuple[Conflict, ...] = ()
    detection: Detection | None = None
    incident_id: UUID | None = None

    #: Minute offset at which the detector fired, counted from the first
    #: reading. This is the number the lead-time claim is built from, so it is
    #: computed here rather than left to whoever reads the result.
    detected_at_minute: int | None = None

    context_evidence: list[Evidence] = field(default_factory=list)

    @property
    def detected(self) -> bool:
        return self.detection is not None


class ScenarioReplay:
    """Runs a recorded scenario through the full evidence pipeline."""

    def __init__(
        self,
        *,
        evidence: EvidenceRepository,
        incidents: IncidentRepository,
        audit: AuditService,
        legacy: LegacyRepository | None = None,
        detector: Detector | None = None,
        taxonomy: Taxonomy | None = None,
    ) -> None:
        self._evidence = evidence
        self._incidents = incidents
        self._audit = audit
        self._legacy = legacy
        self._detector = detector or BaselineDetector()
        self._taxonomy = taxonomy or load_taxonomy()
        self._lifecycle = IncidentService(incidents, evidence, audit)

    async def load_context(
        self,
        *,
        shipment_id: str,
        vehicle_id: str,
        observed_at: datetime,
        extraction_dir: Path | None = None,
    ) -> list[Evidence]:
        """Gather everything known about the shipment before it moved.

        The ERP is optional because it is a system we do not control. When it
        is unreachable the replay continues on whatever else is available, and
        the missing envelope surfaces as an absent envelope rather than as a
        default - which is invariant I6 in practice rather than in principle.
        """
        context: list[Evidence] = []

        if self._legacy is not None:
            context.extend(
                await self._legacy.cargo_requirement_evidence(
                    shipment_id, observed_at=observed_at, taxonomy=self._taxonomy
                )
            )
            context.extend(
                await self._legacy.maintenance_evidence(
                    vehicle_id, observed_at=observed_at, taxonomy=self._taxonomy
                )
            )

        if extraction_dir is not None and extraction_dir.exists():
            for extraction in load_extractions(extraction_dir, shipment_id=shipment_id):
                context.extend(extraction_to_evidence(extraction, taxonomy=self._taxonomy))

        return context

    async def run(
        self,
        telemetry_path: Path,
        *,
        shipment_id: str,
        scenario_run_id: str,
        extraction_dir: Path | None = None,
        stop_at_first_detection: bool = True,
    ) -> ReplayResult:
        """Replay a telemetry file, persisting evidence and detecting as it goes.

        Nothing is committed here. The caller owns the transaction, so a
        replay that fails halfway leaves no partial incident behind.
        """
        readings = read_telemetry(telemetry_path)
        if not readings:
            raise ValueError(f"{telemetry_path} contains no readings")

        started_at = readings[0].timestamp
        vehicle_id = readings[0].vehicle_id
        result = ReplayResult(scenario_run_id=scenario_run_id)

        context = await self.load_context(
            shipment_id=shipment_id,
            vehicle_id=vehicle_id,
            observed_at=started_at,
            extraction_dir=extraction_dir,
        )
        if context:
            await self._evidence.add_many(context)
            result.evidence_persisted += len(context)
        result.context_evidence = context

        # Reconciled before the replay begins: the conflict between the ERP
        # and the shipping document is a fact about the shipment, not
        # something a temperature reading reveals.
        reconciliation = reconcile(context, taxonomy=self._taxonomy)
        result.conflicts = reconciliation.conflicts
        for link in reconciliation.links:
            await self._evidence.add_link(link)

        envelope = resolve_envelope(context, taxonomy=self._taxonomy)
        result.envelope = envelope
        if envelope is None:
            # No envelope, no judgement. Reported rather than assumed.
            return result

        await self._audit.append(
            actor="scenario_replay",
            action="evidence.context_loaded",
            subject_kind="shipment",
            subject_id=shipment_id,
            occurred_at=started_at,
            payload={
                "scenario_run_id": scenario_run_id,
                "context_evidence": len(context),
                "conflicts": [
                    {
                        "observation_type": conflict.observation_type,
                        "safety_critical": conflict.safety_critical,
                    }
                    for conflict in reconciliation.conflicts
                ],
                "envelope_min_c": envelope.minimum_c,
                "envelope_max_c": envelope.maximum_c,
            },
        )

        batch: list[Evidence] = []
        pending_readings = 0

        # A rolling window of recent evidence, handed to the detector on every
        # reading. Both arms receive the identical window: `BaselineDetector`
        # ignores everything but the latest reading by design, so giving it
        # history costs nothing and removes any question about whether one arm
        # was fed better data than the other. One extra reading of slack, so a
        # least-squares fit over N minutes has N+1 samples to work with.
        # Asked for, not assumed: a predictive detector needs its fit window
        # *plus* the debounce lookback, and handing it only the fit window
        # makes it silently never fire.
        required = getattr(self._detector, "required_history_minutes", DEFAULT_WINDOW_MINUTES)
        window: deque[list[Evidence]] = deque(maxlen=int(required) + 1)

        async def flush() -> None:
            nonlocal batch, pending_readings
            if batch:
                await self._evidence.add_many(batch)
                result.evidence_persisted += len(batch)
                batch = []
            pending_readings = 0

        for reading in readings:
            produced = reading_to_evidence(
                reading,
                taxonomy=self._taxonomy,
                scenario_run_id=scenario_run_id,
            )
            result.readings_replayed += 1
            window.append(produced)

            visible = [item for step in window for item in step]
            detection = self._detector.evaluate(visible, envelope=envelope, now=reading.timestamp)

            if detection is None or result.detection is not None:
                batch.extend(produced)
                pending_readings += 1
                if pending_readings >= _BATCH_READINGS:
                    await flush()
                continue

            # This reading's evidence is deliberately not added to the batch.
            # The incident claims it, stamping each row with the incident id,
            # and inserting it here as well would be the same rows twice.
            await flush()
            result.detection = detection
            result.detected_at_minute = round(
                (detection.detected_at - started_at).total_seconds() / 60
            )
            outcome = await self._lifecycle.record(
                detection,
                evidence=produced,
                scenario_run_id=scenario_run_id,
            )
            result.evidence_persisted += outcome.evidence_attached
            result.incident_id = outcome.incident.id
            if stop_at_first_detection:
                break

        await flush()
        return result
