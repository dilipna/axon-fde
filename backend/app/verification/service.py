"""Scheduling and running outcome verification.

This is what closes the loop. An action is taken, a window opens, and when it
closes the readings inside it are graded against the claim the action made.
The incident then moves on its own: confirmed resolves it, failed reopens it.

**A failed verification reopens the incident rather than closing it.** The
transition ``VERIFYING -> INVESTIGATING`` already exists in the lifecycle
table for exactly this. Without it, the only path out of an action is
resolution, and the resolution rate becomes a count of interventions attempted
rather than of problems fixed - which is the metric most likely to be quoted
and least likely to be true.

**An inconclusive verification does not move the incident at all.** Not
forward, because nothing was established; not back, because nothing failed.
It sits in ``VERIFYING`` with ``follow_up_required`` set, which is a queue a
human works. The alternative - resolving on missing data - is the failure that
invariant I6 exists to prevent, wearing a different hat.

The scheduler itself is deliberately not here. ``due()`` returns what is ready
and the caller decides when to ask; wiring APScheduler into the service would
make every test either sleep or reach into a scheduler's internals, and the
interesting behaviour is the grading, not the cron.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.actions.effects import EffectKind, ExpectedEffect
from backend.app.audit.service import AuditService
from backend.app.db.app.models import ActionExecution, Incident, OutcomeVerification
from backend.app.db.app.repositories.verification import VerificationRepository
from backend.app.domain.enums import IncidentStatus
from backend.app.domain.envelope import TemperatureEnvelope
from backend.app.domain.evidence import Evidence
from backend.app.incidents.lifecycle import IncidentService
from backend.app.verification.outcome import Verdict, VerificationResult, evaluate_effect

__all__ = [
    "CARGO_TEMPERATURE_OBSERVATION",
    "VerificationOutcome",
    "VerificationService",
    "readings_from_evidence",
]

#: The observation type outcome verification reads. Named here rather than
#: inlined so that renaming it in the taxonomy breaks one place loudly instead
#: of silently returning no readings - which would grade every action
#: `inconclusive` and look like a telemetry fault.
CARGO_TEMPERATURE_OBSERVATION = "cargo_temp_c"


def readings_from_evidence(evidence: Sequence[Evidence]) -> list[tuple[datetime, float]]:
    """Pull ``(observed_at, temperature)`` pairs out of an evidence bundle.

    Non-numeric values are dropped rather than coerced. A cargo temperature
    that is not a number is a broken adapter, and turning it into one would
    put an invented reading in front of a grader.
    """
    readings: list[tuple[datetime, float]] = []
    for item in evidence:
        if item.observation_type != CARGO_TEMPERATURE_OBSERVATION:
            continue
        if isinstance(item.value, bool) or not isinstance(item.value, int | float):
            continue
        readings.append((item.observed_at, float(item.value)))
    return readings


@dataclass(frozen=True, slots=True)
class VerificationOutcome:
    """The graded verification and what it did to the incident."""

    verification: OutcomeVerification
    result: VerificationResult
    #: The status the incident ended in. Unchanged from its previous value
    #: when the verdict could not settle anything.
    incident_status: IncidentStatus
    reopened: bool


class VerificationService:
    """Opens verification windows and grades them when they close."""

    def __init__(
        self,
        session: AsyncSession,
        audit: AuditService,
        incidents: IncidentService,
    ) -> None:
        self._repo = VerificationRepository(session)
        self._audit = audit
        self._incidents = incidents

    async def schedule(
        self,
        *,
        incident: Incident,
        execution: ActionExecution,
        effect: ExpectedEffect,
        actor: str,
        at: datetime | None = None,
    ) -> OutcomeVerification:
        """Open a verification window for an action that has just run.

        Moves the incident into ``VERIFYING`` if it is not already there. An
        incident with an open verification window that was not marked as being
        verified would be invisible to anything listing what the system is
        waiting on.
        """
        moment = at or datetime.now(UTC)
        if moment.tzinfo is None:
            raise ValueError("at must be timezone-aware")

        window_end = moment + timedelta(minutes=effect.within_minutes)
        verification = await self._repo.schedule(
            incident_id=incident.id,
            execution_id=execution.id,
            expected_effect=effect.as_payload(),
            window_start=moment,
            window_end=window_end,
        )

        if IncidentStatus(incident.status) is not IncidentStatus.VERIFYING:
            await self._incidents.transition(
                incident,
                IncidentStatus.VERIFYING,
                actor=actor,
                at=moment,
                reason=f"awaiting outcome of {execution.action_type}",
            )

        await self._audit.append(
            actor=actor,
            action="verification.scheduled",
            subject_kind="outcome_verification",
            subject_id=str(verification.id),
            incident_id=incident.id,
            occurred_at=moment,
            payload={
                "execution_id": str(execution.id),
                "action": execution.action_type,
                "expected_effect": effect.as_payload(),
                "window_start": moment.isoformat(),
                "window_end": window_end.isoformat(),
            },
        )
        return verification

    async def verify(
        self,
        verification: OutcomeVerification,
        *,
        incident: Incident,
        evidence: Sequence[Evidence],
        envelope: TemperatureEnvelope,
        actor: str = "scheduler",
        at: datetime | None = None,
    ) -> VerificationOutcome:
        """Grade a closed window and move the incident accordingly.

        Raises:
            ValueError: The verification already has a verdict. Re-grading
                would overwrite the record of what was concluded at the time,
                and an outcome that can be revised quietly is not evidence.
        """
        moment = at or datetime.now(UTC)
        if moment.tzinfo is None:
            raise ValueError("at must be timezone-aware")
        if verification.verdict is not None:
            raise ValueError(
                f"verification {verification.id} already concluded "
                f"{verification.verdict}; re-grading would overwrite the record"
            )

        effect = _effect_from_payload(verification.expected_effect)
        result = evaluate_effect(
            effect,
            readings=readings_from_evidence(evidence),
            envelope=envelope,
            window_start=verification.window_start,
            window_end=verification.window_end,
        )

        verification.verdict = result.verdict.value
        verification.observed_effect = result.observed
        verification.follow_up_required = result.requires_follow_up

        status = IncidentStatus(incident.status)
        reopened = False
        if result.verdict is Verdict.CONFIRMED:
            incident = await self._incidents.transition(
                incident,
                IncidentStatus.RESOLVED,
                actor=actor,
                at=moment,
                reason=result.detail,
            )
            status = IncidentStatus.RESOLVED
        elif result.verdict is Verdict.FAILED:
            # Back for another look rather than closed on the strength of
            # having done something.
            incident = await self._incidents.transition(
                incident,
                IncidentStatus.INVESTIGATING,
                actor=actor,
                at=moment,
                reason=result.detail,
            )
            status = IncidentStatus.INVESTIGATING
            reopened = True

        await self._audit.append(
            actor=actor,
            action="verification.completed",
            subject_kind="outcome_verification",
            subject_id=str(verification.id),
            incident_id=incident.id,
            occurred_at=moment,
            payload={
                "verdict": result.verdict.value,
                "detail": result.detail,
                "observed": result.observed,
                "expected_effect": verification.expected_effect,
                "follow_up_required": result.requires_follow_up,
                "incident_status": status.value,
            },
        )
        return VerificationOutcome(
            verification=verification,
            result=result,
            incident_status=status,
            reopened=reopened,
        )

    async def due(self, now: datetime | None = None) -> list[OutcomeVerification]:
        """Verifications whose window has closed and which nobody has graded."""
        return await self._repo.due(now or datetime.now(UTC))


def _effect_from_payload(payload: dict[str, object]) -> ExpectedEffect:
    """Rebuild the stored claim.

    Read back from the row rather than recomputed from the action's current
    spec, deliberately. Grading a year-old action against today's spec table
    would silently re-target a claim that was made under different rules, and
    the stored row is the only record of what was actually promised.
    """
    kind = payload.get("kind")
    within = payload.get("within_minutes")
    if not isinstance(kind, str) or not isinstance(within, int):
        raise ValueError(f"stored expected_effect is not gradeable: {payload!r}")
    detail = {key: value for key, value in payload.items() if key not in {"kind", "within_minutes"}}
    return ExpectedEffect(kind=EffectKind(kind), within_minutes=within, detail=detail)
