"""The whole loop, end to end, with no language model in it.

`poe demo` replays the flagship scenario through every layer built so far:
evidence, reconciliation, prediction, risk, policy, decision, approval,
execution and outcome verification, finishing by verifying the audit chain.

**Why this exists before the LLM does.** Two reasons, and the second is the
one that matters.

1. It is the proof that the deterministic core is a system rather than a pile
   of services with tests. Everything here is wired the way the application
   wires it; nothing is stubbed.
2. **It is the `rules_only` ablation arm for claim C5**, obtained for free by
   building rules-first. When B8 adds the model, the question "what did the
   LLM actually contribute?" has a baseline to answer against that was not
   reconstructed afterwards from memory. An ablation arm built after the fact
   is an argument; one that has been running in CI since before the treatment
   existed is a measurement.

Offline in the sense that matters: no network, no model, no spend. It does
need the local Postgres and the seeded SQL Server, because the claims it
demonstrates are about real engines.
"""

from __future__ import annotations

import asyncio
import json
import sys
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.actions.executor import ActionExecutor, ExecutionRefusedError
from backend.app.approvals.binding import ApprovalContext
from backend.app.approvals.service import (
    ApprovalDecision,
    ApprovalRefusal,
    ApprovalService,
)
from backend.app.audit.service import AuditService
from backend.app.db.app.models import ActionCandidate, Recommendation
from backend.app.db.app.repositories import EvidenceRepository, IncidentRepository
from backend.app.db.app.repositories.risk import RiskRepository
from backend.app.db.app.session import build_engine
from backend.app.db.legacy.repository import LegacyRepository
from backend.app.decision.catalogue import FacilityOption
from backend.app.decision.engine import rank_options
from backend.app.decision.feasibility import assess_feasibility
from backend.app.domain.enums import ActionType, IncidentStatus
from backend.app.domain.evidence import Evidence
from backend.app.incidents.detection import PredictiveDetector
from backend.app.incidents.lifecycle import IncidentService
from backend.app.incidents.replay import ScenarioReplay
from backend.app.policies.engine import ACTION_REQUIREMENTS, Principal, Role, evaluate
from backend.app.risk.service import SlopeRiskService
from backend.app.verification.service import VerificationService

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCENARIO = "compressor_degradation_pharma_01"
SHIPMENT_ID = "SH-2041"
VEHICLE_ID = "AX-042"

DISPATCHER = Principal(subject="j.okafor@axon.example", role=Role.DISPATCHER)
FLEET_MANAGER = Principal(subject="r.castellanos@axon.example", role=Role.FLEET_MANAGER)

#: How far each seeded facility is off the planned route, in minutes.
#:
#: **Scenario data, not an ERP fact, and deliberately not computed.** The
#: facilities view carries coordinates but the vehicle's live position is not
#: exposed anywhere, so a real detour needs a position and a routing engine -
#: neither of which Phase 1 has. Deriving minutes from a straight line between
#: two points would look like a measurement and be a guess, and it feeds
#: `MAX_DETOUR_MINUTES`, so a wrong guess silently changes which options are
#: feasible. Everything else about these facilities - capability, free slots -
#: is read live from the ERP, including CS-13 being full.
DETOUR_MINUTES = {"CS-11": 45.0, "CS-12": 95.0, "CS-13": 60.0}

TOTAL_STEPS = 13

#: The minute the threshold alarm fires on this recording - the first reading
#: outside the 2-8 C envelope.
#:
#: A constant because the baseline detector is not run in this demo; the
#: predictive arm is. It is the flagship's declared `breach_at_min`, asserted
#: against the physics by `poe forge verify` and against both detectors by
#: AxonBench's lead-time grader, so it cannot drift here unnoticed.
BASELINE_ALARM_MINUTE = 137

#: Where `--json` writes when given no path. Served read-only by the API, so
#: the control tower shows the last run that actually happened rather than a
#: fixture checked in beside it.
DEFAULT_TRACE_PATH = PROJECT_ROOT / "data" / "generated" / "demo-trace.json"


def _trace_path(args: list[str]) -> Path | None:
    """The ``--json [PATH]`` destination, or None when the flag is absent."""
    if "--json" not in args:
        return None
    index = args.index("--json")
    following = args[index + 1] if index + 1 < len(args) else None
    if following is None or following.startswith("--"):
        return DEFAULT_TRACE_PATH
    return Path(following)


# ---------------------------------------------------------------------------
# Presentation
# ---------------------------------------------------------------------------


class Narrator(Protocol):
    """Where the loop's narration goes.

    The loop takes one of these rather than printing, so the terminal demo and
    the control-tower UI run **the same code over the same database** and
    cannot drift into telling different stories. A UI fed from a second
    implementation of the loop would be a mock with extra steps, and the first
    thing an interviewer asks a dashboard is whether the numbers are real.
    """

    def step(self, title: str) -> None: ...
    def say(self, line: str = "") -> None: ...
    def good(self, line: str) -> None: ...
    def refused(self, line: str) -> None: ...
    def note(self, line: str) -> None: ...
    def banner(self, line: str) -> None: ...
    def fact(self, key: str, value: object) -> None: ...


class Console:
    """Numbered steps, so the demo reads as a narrative rather than a log."""

    def __init__(self) -> None:
        self._step = 0

    def step(self, title: str) -> None:
        self._step += 1
        print(f"\n\033[1m[{self._step}/{TOTAL_STEPS}] {title}\033[0m")

    def say(self, line: str = "") -> None:
        print(f"      {line}" if line else "")

    def good(self, line: str) -> None:
        print(f"      \033[32m✓\033[0m {line}")

    def refused(self, line: str) -> None:
        print(f"      \033[33m⛔\033[0m {line}")

    def note(self, line: str) -> None:
        print(f"      \033[2m{line}\033[0m")

    def banner(self, line: str) -> None:
        print(f"\n\033[1m{line}\033[0m")
        print("=" * len(line))

    def fact(self, key: str, value: object) -> None:
        """Deliberately ignored: the prose above already states these."""


class Recorder:
    """Captures the same narration as structured steps, and also prints it.

    It prints as well as records for one reason: a trace written by a run
    nobody watched is indistinguishable from a trace written by a run that
    failed halfway. The terminal stays the source of truth about whether the
    demo worked; the JSON is what the UI reads.

    The `kind` on each line is the whole vocabulary the UI needs - `good` and
    `refused` are what let it show a refusal as a refusal rather than as
    another grey line of log, and a governance demo whose refusals do not look
    different from its successes has buried its own point.
    """

    def __init__(self) -> None:
        self._console = Console()
        self.steps: list[dict[str, Any]] = []
        self.banners: list[str] = []
        self.facts: dict[str, object] = {}

    def _line(self, kind: str, text: str) -> None:
        if not self.steps:
            # Narration before the first step - the opening banner's notes.
            self.steps.append({"number": 0, "title": "", "lines": []})
        self.steps[-1]["lines"].append({"kind": kind, "text": text})

    def step(self, title: str) -> None:
        self._console.step(title)
        # Numbered by titled steps, not by list length: narration before the
        # first step occupies entry 0, and counting entries made step one
        # render as step two in the UI while the terminal said step one.
        number = sum(1 for entry in self.steps if entry["title"]) + 1
        self.steps.append({"number": number, "title": title, "lines": []})

    def say(self, line: str = "") -> None:
        self._console.say(line)
        self._line("say", line)

    def good(self, line: str) -> None:
        self._console.good(line)
        self._line("good", line)

    def refused(self, line: str) -> None:
        self._console.refused(line)
        self._line("refused", line)

    def note(self, line: str) -> None:
        self._console.note(line)
        self._line("note", line)

    def banner(self, line: str) -> None:
        self._console.banner(line)
        self.banners.append(line)

    def fact(self, key: str, value: object) -> None:
        """Record a number the UI needs to place, rather than print it.

        The chart has to mark the minute the detector fired and the minute the
        envelope was breached. The first version of the UI recovered those by
        regex over the narration, which found one of the two and silently drew
        a chart missing its most important mark - the comparison the whole
        lead-time claim rests on. Prose is for people; this is for the chart.
        """
        self.facts[key] = value

    def as_payload(self, *, exit_code: int, elapsed_seconds: float) -> dict[str, Any]:
        return {
            "scenario_id": SCENARIO,
            "shipment_id": SHIPMENT_ID,
            "vehicle_id": VEHICLE_ID,
            "arm": "rules_only",
            "model_id": "none",
            "recorded_at": datetime.now(UTC).isoformat(),
            "elapsed_seconds": round(elapsed_seconds, 2),
            "exit_code": exit_code,
            "total_steps": TOTAL_STEPS,
            "banners": self.banners,
            "facts": self.facts,
            # Numbered from 0 when the run narrated before its first step, so
            # a UI can render the preamble without inventing a step for it.
            "steps": [s for s in self.steps if s["title"] or s["lines"]],
        }


@dataclass
class Wiring:
    """The services, built exactly as the application builds them."""

    session: Any
    audit: AuditService
    evidence: EvidenceRepository
    incidents: IncidentRepository
    lifecycle: IncidentService
    approvals: ApprovalService
    executor: ActionExecutor
    verification: VerificationService
    risk_repo: RiskRepository


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


async def run_demo(*, keep: bool = False, narrator: Narrator | None = None) -> int:
    console: Narrator = narrator or Console()
    console.banner("AxonFDE - rules-only closed loop (no language model)")
    console.note(
        "Every number below is computed. Nothing is narrated by a model, and nothing is stubbed."
    )

    engine = build_engine()
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    try:
        async with session_factory() as session:
            audit = AuditService(session)
            evidence_repo = EvidenceRepository(session)
            incidents_repo = IncidentRepository(session)
            lifecycle = IncidentService(incidents_repo, evidence_repo, audit)
            approvals = ApprovalService(session, audit)
            wiring = Wiring(
                session=session,
                audit=audit,
                evidence=evidence_repo,
                incidents=incidents_repo,
                lifecycle=lifecycle,
                approvals=approvals,
                executor=ActionExecutor(session, audit, approvals),
                verification=VerificationService(session, audit, lifecycle),
                risk_repo=RiskRepository(session),
            )
            code = await _loop(console, wiring)

            # **The demo rolls back by default**, and that is not timidity.
            # One transaction for the whole run means it either leaves a
            # complete, internally consistent incident behind or nothing at
            # all - and leaving nothing makes the demo repeatable. A committing
            # demo is a demo that works once: the second run finds the first
            # run's incident still open on the same correlation key,
            # deduplicates into it, and fails on a transition that was already
            # made. That is exactly how this was found.
            #
            # `--keep` commits, for when the point is to inspect the rows
            # afterwards.
            if keep and code == 0:
                await session.commit()
                console.note("Committed. `poe demo` without --keep leaves no residue.")
            else:
                await session.rollback()
                if keep:
                    console.note("Rolled back: the run did not complete, so nothing is kept.")
                else:
                    console.note("Rolled back. Pass --keep to inspect the rows afterwards.")
            return code
    finally:
        await engine.dispose()


async def _loop(console: Narrator, wiring: Wiring) -> int:
    recording = PROJECT_ROOT / "data" / "generated" / SCENARIO / "telemetry.parquet"
    if not recording.exists():
        console.refused(f"{recording} is missing. Run `uv run poe forge run-all` first.")
        return 1

    legacy = LegacyRepository()
    risk_service = SlopeRiskService()

    # -- 1 --------------------------------------------------------------
    console.step("Read the shipment from the legacy ERP")
    shipment = await legacy.shipment(SHIPMENT_ID)
    if shipment is None:
        console.refused(f"{SHIPMENT_ID} is not in the ERP. Run `uv run poe seed` first.")
        return 1
    cargo_value = float(shipment.cargo_value_usd)
    console.good(
        f"{SHIPMENT_ID} — {shipment.customer_name}, {shipment.origin} -> "
        f"{shipment.destination}, ${cargo_value:,.0f}"
    )
    console.note("Read through six read-only views. The AI cannot write here (I4).")

    # -- 2 --------------------------------------------------------------
    console.step("Replay 240 minutes of telemetry through the predictive detector")
    replay = ScenarioReplay(
        evidence=wiring.evidence,
        incidents=wiring.incidents,
        audit=wiring.audit,
        legacy=legacy,
        detector=PredictiveDetector(risk_service),
    )
    result = await replay.run(
        recording,
        shipment_id=SHIPMENT_ID,
        scenario_run_id=f"demo-{SCENARIO}",
        extraction_dir=PROJECT_ROOT / "data" / "documents" / "extractions",
        # The whole recording, not just up to the first detection. The
        # detector still reports minute 102 - the loop keeps the first
        # detection and deduplicates the rest - but outcome verification needs
        # readings from *after* the action, and stopping at detection leaves
        # its window empty. An empty window grades `inconclusive`, which is
        # correct and demonstrates nothing.
        stop_at_first_detection=False,
    )
    console.good(
        f"{result.readings_replayed} readings replayed, "
        f"{result.evidence_persisted} pieces of evidence stored"
    )

    # -- 3 --------------------------------------------------------------
    console.step("Reconcile the sources that disagree")
    if result.conflicts:
        for conflict in result.conflicts:
            console.refused(
                f"conflict on {conflict.observation_type}"
                + (" (safety critical)" if conflict.safety_critical else "")
            )
    else:
        console.say("no conflicts")
    console.note(
        "The ERP says the cargo may reach 10.0 C; the signed Bill of Lading says "
        "8.0 C. The document wins on authority, and the disagreement stays visible."
    )

    # -- 4 --------------------------------------------------------------
    console.step("Resolve the envelope this cargo is judged against")
    envelope = result.envelope
    if envelope is None:
        console.refused("no envelope could be resolved — the system declines to guess (I6)")
        return 1
    console.good(f"{envelope.minimum_c} to {envelope.maximum_c} C, from the Bill of Lading")
    console.note("Judged against the ERP's 10 C, this load never looks at risk at all.")

    # -- 5 --------------------------------------------------------------
    console.step("Detect")
    if result.detection is None or result.incident_id is None:
        console.refused("no detection — nothing further to demonstrate")
        return 1
    console.good(
        f"incident opened at minute {result.detected_at_minute} "
        f"by the {result.detection.detected_by.value} detector"
    )
    console.note(
        f"The threshold baseline does not fire until minute {BASELINE_ALARM_MINUTE}, when the "
        "cargo is already out of spec. That gap is the lead time."
    )
    # Recorded as data, not only as prose. The control tower marks both minutes
    # on its chart, and recovering them by reading the sentences back is how
    # the first version came to draw the detection line and silently omit the
    # breach line it is measured against.
    console.fact("detected_at_minute", result.detected_at_minute)
    console.fact("baseline_alarm_minute", BASELINE_ALARM_MINUTE)
    console.fact("lead_time_minutes", BASELINE_ALARM_MINUTE - (result.detected_at_minute or 0))
    incident = await wiring.incidents.get(result.incident_id)
    assert incident is not None

    detected_at = result.detection.detected_at
    # Guarded rather than assumed. A detection that deduplicated into a live
    # incident finds it already past `detected`, and an unguarded move would
    # raise on a perfectly normal path.
    if IncidentStatus(incident.status) is IncidentStatus.DETECTED:
        await wiring.lifecycle.transition(
            incident,
            IncidentStatus.INVESTIGATING,
            actor="axon.workflow",
            at=detected_at,
            reason="predictive detection opened for assessment",
        )

    # -- 6 --------------------------------------------------------------
    console.step("Estimate risk, and store it beside its baseline")
    # Queried by entity, not by incident. The replay stores every reading but
    # attaches only the detection's own evidence to the incident, so
    # `for_incident` returns a handful of rows and the trajectory cannot be
    # fitted from them - which the risk service correctly reports as a refusal
    # rather than a zero. The assessment needs the vehicle's history.
    bundle = await wiring.evidence.active_observations(entity_kind="vehicle", entity_id=VEHICLE_ID)
    window = _recent(bundle, before=detected_at, minutes=45)
    estimate = risk_service.assess(
        window, envelope=envelope, now=detected_at, context=result.context_evidence
    )
    if estimate is None:
        console.refused("the trajectory could not be fitted — a refusal, not a zero (I6)")
        return 1
    await wiring.risk_repo.record(incident.id, estimate)
    console.good(f"p(breach within {estimate.horizon_minutes} min) = {estimate.probability:.3f}")
    console.say(
        f"baseline ({estimate.baseline_name}) = {estimate.baseline_probability:.3f}, "
        f"uplift {estimate.uplift_over_baseline:+.3f}"
    )
    console.note(
        "The baseline is a NOT NULL column, not a notebook cell (I3). A prediction "
        "cannot be stored without the number it has to beat."
    )

    # -- 7 --------------------------------------------------------------
    console.step("Check which interventions are actually possible")
    facilities = await _facilities(legacy)
    for facility in facilities:
        state = (
            f"{facility.slots_available} slots"
            if facility.slots_available
            else "\033[33mfull\033[0m"
        )
        console.say(
            f"{facility.facility_id} {facility.name}: {state}, "
            f"{facility.detour_minutes:.0f} min detour"
        )
    # Derived from the ERP's planned arrival and the reading clock, and
    # discarded when the two disagree implausibly - the recording and the
    # seeded shipment come from separate runs, so the subtraction can produce
    # a journey that ended last week. An implausible number is reported as
    # *unknown* rather than passed on: feasibility would otherwise rule out
    # options on the strength of arithmetic nobody checked (I6).
    remaining = _minutes_to_destination(shipment.planned_arrival, detected_at)
    console.say(
        f"time to destination: {remaining:.0f} min"
        if remaining is not None
        else "time to destination: unknown (the ERP arrival and the recording "
        "clock are from different runs, so it is not derivable)"
    )
    feasibility = assess_feasibility(
        facilities=facilities,
        requires_pharma_certification=True,
        minutes_to_destination=remaining,
    )
    blocked = {action: verdict for action, verdict in feasibility.items() if not verdict.feasible}
    for action, verdict in sorted(blocked.items(), key=lambda pair: pair[0].value):
        console.refused(f"{action.value}: {verdict.reason}")

    # -- 8 --------------------------------------------------------------
    console.step("Rank every option by expected value, including doing nothing")
    permitted = {
        action: (
            evaluate(DISPATCHER, action).requires_approval,
            requirement.approver_role.value if requirement.approver_role else None,
        )
        for action, requirement in ACTION_REQUIREMENTS.items()
    }
    decision = rank_options(
        probability=estimate.probability,
        cargo_value_usd=cargo_value,
        feasibility=feasibility,
        permitted=permitted,
    )
    for option in decision.options[:4]:
        mark = ">" if option is decision.recommended else " "
        console.say(
            f"{mark} {option.action.value:<28} EV ${option.expected_value_usd:>12,.0f}"
            + ("" if option.feasible else "   (infeasible)")
        )
    recommended = decision.recommended
    if recommended is None:
        console.refused("no feasible option")
        return 1
    console.good(f"recommended: {recommended.action.value}")
    if decision.flip is not None:
        console.note(decision.flip.describe())

    # -- 9 --------------------------------------------------------------
    console.step("Request approval, bound to exactly what the approver is shown")
    candidate, recommendation = await _persist_recommendation(
        wiring, incident.id, recommended.action, decision, estimate
    )
    # Built from the assessment window, not the whole bundle. The recording
    # now runs to minute 240, and binding an approval to readings that had not
    # arrived when it was requested would bind it to a world the approver
    # could not have seen - the precise thing the hash exists to prevent.
    context = _context(incident.id, recommended.action, window, estimate)
    approval = await wiring.approvals.request(
        recommendation_id=recommendation.id,
        action_candidate_id=candidate.id,
        context=context,
        required_role=Role.FLEET_MANAGER,
        requested_by=DISPATCHER.subject,
        requested_at=detected_at,
    )
    await wiring.lifecycle.transition(
        incident,
        IncidentStatus.AWAITING_APPROVAL,
        actor=DISPATCHER.subject,
        at=detected_at,
        reason=f"{recommended.action.value} requires fleet manager sign-off",
    )
    console.good(f"approval {str(approval.id)[:8]} requested of {approval.required_role}")
    console.say(f"bound context hash {approval.bound_context_hash[:16]}...")
    console.note(
        f"Covers {len(context.evidence_hashes)} evidence hashes, the risk probability "
        f"({estimate.probability:.3f}) AND its baseline ({estimate.baseline_probability:.3f}), "
        "the action and its target."
    )

    # -- 10 -------------------------------------------------------------
    console.step("A fleet manager grants it")
    await wiring.approvals.decide(
        approval,
        principal=FLEET_MANAGER,
        decision=ApprovalDecision.GRANTED,
        context=context,
        at=detected_at + timedelta(minutes=2),
        rationale="Gary has pharma-certified capacity and the trend is unambiguous.",
    )
    console.good(f"granted by {approval.decided_by}")

    # -- 11 -------------------------------------------------------------
    console.step("The world moves before the action is taken")
    # Four more minutes of real telemetry, not a fabricated hash. The readings
    # below genuinely arrived after the signature, so the staleness this
    # demonstrates is the one that happens in production rather than one
    # staged for the demo.
    later = detected_at + timedelta(minutes=4)
    fresh = _recent(bundle, before=later, minutes=45)
    moved_estimate = risk_service.assess(
        fresh, envelope=envelope, now=later, context=result.context_evidence
    )
    moved = _context(incident.id, recommended.action, fresh, moved_estimate or estimate)
    # Counted by timestamp, not by differencing the two window sizes. Both
    # windows are 45 minutes wide and the later one has slid forward, so the
    # difference in their lengths is how many readings *left* the window minus
    # how many entered - which printed "-48 new readings arrived".
    arrived = sum(1 for item in fresh if item.observed_at > detected_at)
    console.say(
        f"{arrived} new readings arrived; "
        f"p(breach) moved {estimate.probability:.3f} -> "
        f"{(moved_estimate or estimate).probability:.3f}"
    )
    try:
        await wiring.executor.execute(
            principal=FLEET_MANAGER,
            context=moved,
            request={"vehicle_id": VEHICLE_ID, "facility_id": "CS-11"},
            approval=approval,
            at=later,
        )
    except ExecutionRefusedError as refusal:
        console.refused(f"execution refused: {refusal.refusal.value}")
        check = wiring.approvals.check(approval, moved, now=later, granted_context=context)
        console.say(f"changed since the signature: {', '.join(check.changed_fields)}")
        console.note(
            "The approval is live and correctly signed. It is refused anyway, because "
            "it was bound to a world that no longer exists — which is invariant I5. "
            "Expiry would have been a different code with a different remedy."
        )
        if refusal.refusal is not ApprovalRefusal.APPROVAL_STALE:
            console.refused(f"expected approval_stale, got {refusal.refusal.value}")
            return 1
    else:
        console.refused("the stale approval was NOT refused — invariant I5 is broken")
        return 1

    # -- 12 -------------------------------------------------------------
    console.step("Re-seek approval against the world as it now is, then act")
    reapproval = await wiring.approvals.request(
        recommendation_id=recommendation.id,
        action_candidate_id=candidate.id,
        context=moved,
        required_role=Role.FLEET_MANAGER,
        requested_by=DISPATCHER.subject,
        requested_at=later,
    )
    await wiring.approvals.decide(
        reapproval,
        principal=FLEET_MANAGER,
        decision=ApprovalDecision.GRANTED,
        context=moved,
        at=later + timedelta(seconds=30),
        rationale="Re-confirmed against the updated readings.",
    )
    await wiring.lifecycle.transition(
        incident,
        IncidentStatus.ACTING,
        actor=FLEET_MANAGER.subject,
        at=later,
        reason="approval re-granted against current evidence",
    )
    outcome = await wiring.executor.execute(
        principal=FLEET_MANAGER,
        context=moved,
        request={"vehicle_id": VEHICLE_ID, "facility_id": "CS-11"},
        approval=reapproval,
        at=later + timedelta(minutes=1),
    )
    console.good(outcome.summary)

    replayed = await wiring.executor.execute(
        principal=FLEET_MANAGER,
        context=moved,
        request={"vehicle_id": VEHICLE_ID, "facility_id": "CS-11"},
        approval=reapproval,
        at=later + timedelta(minutes=2),
    )
    console.good(
        f"retried: no second truck dispatched (attempt {replayed.attempts}, "
        f"same execution {str(replayed.execution.id)[:8]})"
    )
    console.note("Idempotency is keyed on the approval. A second signature would act again.")

    # -- 13 -------------------------------------------------------------
    console.step("Verify the outcome, then verify the record of all of it")
    assert outcome.expected_effect is not None
    verification = await wiring.verification.schedule(
        incident=incident,
        execution=outcome.execution,
        effect=outcome.expected_effect,
        actor=FLEET_MANAGER.subject,
        at=later + timedelta(minutes=1),
    )
    console.say(
        f"claim: {outcome.expected_effect.kind.value} within "
        f"{outcome.expected_effect.within_minutes} minutes"
    )

    graded = await wiring.verification.verify(
        verification,
        incident=incident,
        evidence=bundle,
        envelope=envelope,
        actor="scheduler",
        at=verification.window_end + timedelta(minutes=1),
    )
    console.say(f"verdict: {graded.result.verdict.value} - {graded.result.detail}")
    if graded.reopened:
        console.refused(f"incident reopened: {graded.incident_status.value}")
    console.note(
        "Graded against the recording, which is the trajectory of a truck that was "
        "NOT rerouted: IncidentForge does not yet simulate post-action physics. So a "
        "failure here is the honest result, and it shows the reopen path against "
        "real readings. Synthesising a recovery to make the demo end happily would "
        "be fabricating an observation, which is the one thing the system refuses "
        "to do anywhere else."
    )

    report = await wiring.audit.verify()
    console.say()
    if report.valid:
        console.good(f"audit chain verified: {report.events_checked} events intact")
    else:
        console.refused(report.describe())
        return 1
    console.note(
        "Keyed with HMAC when AXON_AUDIT_HMAC_KEY is set; unkeyed here, which "
        "detects partial tampering only. The chain says which guarantee it makes."
    )

    console.banner("Closed loop complete - no model was consulted at any point")
    return 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


#: Longer than this and the "remaining journey" is arithmetic on two clocks
#: that were never synchronised, not a fact about a truck.
_MAX_PLAUSIBLE_JOURNEY_MINUTES = 24 * 60


def _minutes_to_destination(planned_arrival: datetime | None, now: datetime) -> float | None:
    """How long is left, or None when that cannot be honestly said."""
    if planned_arrival is None:
        return None
    if planned_arrival.tzinfo is None:
        planned_arrival = planned_arrival.replace(tzinfo=UTC)
    minutes = (planned_arrival - now).total_seconds() / 60.0
    if minutes <= 0 or minutes > _MAX_PLAUSIBLE_JOURNEY_MINUTES:
        return None
    return minutes


def _recent(bundle: list[Evidence], *, before: datetime, minutes: int) -> list[Evidence]:
    """The evidence a detector would have had at a given moment.

    Filtered by ``observed_at`` rather than by insertion order, because the
    replay stores everything and an assessment made "at minute 102" must not
    see minute 103.
    """
    since = before - timedelta(minutes=minutes)
    return [item for item in bundle if since <= item.observed_at <= before]


def _context(
    incident_id: Any,
    action: ActionType,
    bundle: list[Evidence],
    estimate: Any,
) -> ApprovalContext:
    """Build the binding from the same objects the screen would render from."""
    hashes = [item.content_hash for item in bundle if item.observation_type == "cargo_temp_c"]
    return ApprovalContext(
        incident_id=incident_id,
        action=action,
        target_ref="CS-11",
        evidence_hashes=hashes[-20:],
        risk_probability=estimate.probability,
        baseline_probability=estimate.baseline_probability,
        baseline_name=estimate.baseline_name,
        model_version=estimate.model_version,
        horizon_minutes=estimate.horizon_minutes,
        degraded=estimate.degraded,
    )


async def _facilities(legacy: LegacyRepository) -> list[FacilityOption]:
    """Capabilities and free slots from the ERP; detour from the scenario.

    Split deliberately. Which facilities are certified and how many slots are
    free are facts the ERP holds and that change hour by hour, so they are
    read live. How far each one is off the route depends on where the vehicle
    is right now, which nothing exposes - so it is declared rather than
    invented. See ``DETOUR_MINUTES``.
    """
    rows = await asyncio.to_thread(
        legacy._fetch,
        "SELECT facility_id, name, capabilities, slots_available FROM dbo.vw_ai_facilities",
    )
    options: list[FacilityOption] = []
    for row in rows:
        facility_id = str(row["facility_id"])
        options.append(
            FacilityOption(
                facility_id=facility_id,
                name=str(row["name"]),
                capabilities=frozenset(
                    part.strip() for part in str(row["capabilities"]).split(",") if part.strip()
                ),
                slots_available=int(row["slots_available"]),
                detour_minutes=DETOUR_MINUTES.get(facility_id, 999.0),
            )
        )
    return options


async def _persist_recommendation(
    wiring: Wiring,
    incident_id: Any,
    action: ActionType,
    decision: Any,
    estimate: Any,
) -> tuple[ActionCandidate, Recommendation]:
    """Store what was considered, not only what was chosen.

    Every candidate is written, including the rejected ones with their
    reasons. "We considered a trailer swap and Dayton had no slots" is the
    answer to the question an auditor actually asks.
    """
    candidates: dict[ActionType, ActionCandidate] = {}
    for option in decision.options:
        row = ActionCandidate(
            incident_id=incident_id,
            action_type=option.action.value,
            target_ref="CS-11" if option.action is action else None,
            feasible=option.feasible,
            infeasibility_reason=option.feasibility.reason or None,
            est_cost_usd=option.direct_cost_usd,
            est_delay_min=option.delay_minutes,
            predicted_risk_after=option.residual_risk,
            ev_total=option.expected_value_usd,
            ev_breakdown=option.breakdown(),
            assumptions=option.assumptions,
            required_approval_role=option.approver_role,
        )
        wiring.session.add(row)
        candidates[option.action] = row
    await wiring.session.flush()

    recommendation = Recommendation(
        incident_id=incident_id,
        selected_action_id=candidates[action].id,
        narrative=(
            f"Reroute to pharma-certified cold storage. Projected breach probability "
            f"{estimate.probability:.0%} within {estimate.horizon_minutes} minutes "
            f"against a {estimate.baseline_probability:.0%} rule baseline."
        ),
        cited_evidence_ids=[],
        flip_sensitivity=(
            {"probability": decision.flip.probability, "distance": decision.flip.distance}
            if decision.flip is not None
            else None
        ),
        # No model produced this narrative, so there is nothing to ground-check.
        # Recorded as such rather than left to look like a check that passed.
        grounding_check={"applicable": False, "reason": "rules_only arm: no model output"},
    )
    wiring.session.add(recommendation)
    await wiring.session.flush()
    return candidates[action], recommendation


def main(argv: list[str] | None = None) -> int:
    # Windows consoles default to cp1252, and a demo that dies printing its own
    # output is a bad demo. The body is ASCII, but scenario text and exception
    # messages are not guaranteed to be, so degrade rather than crash.
    for stream in (sys.stdout, sys.stderr):
        with suppress(AttributeError, ValueError):
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

    args = list(argv if argv is not None else sys.argv[1:])
    keep = "--keep" in args
    trace_path = _trace_path(args)

    # The recorder prints as well as records, so `--json` never turns a visible
    # run into a silent one. A trace file from a run nobody watched looks
    # exactly like a trace from a run that died halfway.
    narrator: Narrator = Recorder() if trace_path is not None else Console()

    started = datetime.now(UTC)
    try:
        code = asyncio.run(run_demo(keep=keep, narrator=narrator))
    except Exception as exc:
        print(f"\n\033[31mThe demo stopped: {type(exc).__name__}: {exc}\033[0m")
        print("Check that `poe up`, `poe migrate`, `poe seed` and `poe forge run-all` have run.")
        return 1

    elapsed = (datetime.now(UTC) - started).total_seconds()
    if trace_path is not None and isinstance(narrator, Recorder):
        # Written only after a completed run. A partial trace served to the UI
        # would render as a demo that simply had fewer steps.
        payload = narrator.as_payload(exit_code=code, elapsed_seconds=elapsed)
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        trace_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
        print(f"\n\033[2mTrace written to {trace_path}\033[0m")

    print(f"\n\033[2mElapsed {elapsed:.1f}s\033[0m")
    return code


if __name__ == "__main__":
    sys.exit(main())
