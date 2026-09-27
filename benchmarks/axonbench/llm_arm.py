"""The rules+LLM arm: run the real workflow over the pack, hand outcomes to graders.

This module is the one place AxonBench touches a model, and it is deliberately
*outside* `benchmarks.axonbench.graders`. An import-linter contract forbids a
grader from reaching a model - a grader that could be argued with is not a
grader - so the arm runs the workflow, collects plain `CaseOutcome` records,
and the graders never see anything but those.

**Which incidents.** Every scenario on which the predictive detector fires,
evaluated at the minute it fires - the pipeline as it ships: an incident opens,
then somebody asks why. Scenarios where it never fires are not investigated
and are listed, not silently dropped. Two scenarios that fire on a *control*
run are investigated too, and their truth is `no_fault`: a false alarm is still
an incident that gets a diagnosis, and excluding them would grade the model
only on the cases where being right is easiest.

**Evidence.** The last `CASE_HISTORY_MINUTES` of telemetry up to the decision
minute, plus the contractual envelope from the Bill of Lading and, where the
scenario seeds one, the ERP's disagreeing value. History is capped because a
six-hour run is thousands of readings and every consumer of this bundle (the
digest, the risk fit, the priors) looks at the last hour at most.

**What is not given to the model.** Ground truth: `decisive_evidence`, the
declared root cause and the correct action never enter a prompt.

**Replay.** With `AXON_LLM_MODE=cassette` (the default) every call is answered
from a recording and a missing one raises. That is what lets CI run this arm
with no key.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from backend.app.agents.budget import Budget, BudgetState
from backend.app.agents.digest import build_digest
from backend.app.agents.graph import IncidentWorkflow, build_graph, initial_state
from backend.app.agents.nodes.model_nodes import make_model_nodes
from backend.app.agents.prompts import JUDGE_SCHEMA, JUDGE_SYSTEM, PROMPT_VERSION
from backend.app.agents.scoring import rule_priors
from backend.app.agents.state import IncidentState
from backend.app.config import Settings, get_settings
from backend.app.decision.catalogue import FacilityOption
from backend.app.domain.enums import EvidenceSource, Modality
from backend.app.domain.evidence import EntityRef, Evidence, Provenance
from backend.app.domain.taxonomy import Taxonomy, load_taxonomy
from backend.app.evidence.telemetry import reading_to_evidence
from backend.app.incidents.detection import PredictiveDetector
from backend.app.incidents.sweep import first_detection
from backend.app.llm import build_provider
from backend.app.llm.provider import LLMProvider, LLMRequest
from backend.app.risk.service import SlopeRiskService
from benchmarks.axonbench.graders.detection import (
    DEFAULT_PACK_DIR,
    _envelope_for,
    _fault_onset,
    _to_reading,
)
from benchmarks.axonbench.graders.diagnosis import CONTRIBUTING_THRESHOLD, CaseOutcome
from simulator.incidentforge.generator import run_scenario
from simulator.incidentforge.scenarios import DataFaultType, Scenario, load_pack

__all__ = [
    "CASE_HISTORY_MINUTES",
    "FACILITIES",
    "ArmMeta",
    "Case",
    "build_cases",
    "run_llm_arm",
]

CASE_HISTORY_MINUTES = 120

#: Concurrent incidents in flight. Small on purpose: the spend guard checks
#: before each call and records after it, so N calls in flight can each pass
#: the check against the same balance. Four bounds that overshoot to four
#: calls' worth, which the daily ceiling has headroom for.
CONCURRENCY = int(os.environ.get("AXON_BENCH_CONCURRENCY", "4"))

#: The three seeded facilities (`data/seed/legacy/02_seed.sql`), with free slots
#: as capacity minus in-use and detours as in `scripts/demo.py`'s
#: `DETOUR_MINUTES`. Held here as data so the benchmark needs no database - the
#: CI job has none - and because the model never sees them: they only feed the
#: deterministic feasibility check, identically in both arms.
FACILITIES: list[FacilityOption] = [
    FacilityOption(
        facility_id="CS-11",
        name="Gary Cold Storage",
        capabilities=frozenset({"cold_storage", "pharma_certified", "trailer_swap"}),
        slots_available=3,
        detour_minutes=45.0,
    ),
    FacilityOption(
        facility_id="CS-12",
        name="Fort Wayne Cold Chain",
        capabilities=frozenset({"cold_storage", "pharma_certified"}),
        slots_available=11,
        detour_minutes=95.0,
    ),
    FacilityOption(
        facility_id="CS-13",
        name="Dayton Refrigerated Depot",
        capabilities=frozenset({"cold_storage", "trailer_swap"}),
        slots_available=0,
        detour_minutes=60.0,
    ),
]


@dataclass(frozen=True, slots=True)
class Case:
    """One investigated incident: what the system knew when it opened."""

    scenario_id: str
    shipment_id: str
    vehicle_id: str
    decision_time: datetime
    decision_minute: int
    evidence: tuple[Evidence, ...]
    cargo_value_usd: float
    truth_root_cause: str
    truth_contributing: tuple[str, ...]
    correct_action: str
    alert_preceded_fault_onset: bool


@dataclass(slots=True)
class ArmMeta:
    """What a run did that is not a per-incident outcome."""

    not_detected: list[str] = field(default_factory=list)
    not_investigated: dict[str, str] = field(default_factory=dict)
    judge_cost_usd: float = 0.0
    judge_calls: int = 0
    models: dict[str, str] = field(default_factory=dict)
    prompt_version: str = PROMPT_VERSION
    llm_mode: str = ""


def _bol_and_erp_evidence(
    scenario: Scenario, start: datetime, taxonomy: Taxonomy
) -> list[Evidence]:
    """The contractual envelope, and the ERP's disagreeing figure if one is seeded."""
    shipment = EntityRef(kind="shipment", id=scenario.shipment_id)
    provenance = Provenance(producer="axonbench", producer_version="1.0.0", note="pack envelope")

    def make(source: EvidenceSource, observation_type: str, value: float) -> Evidence:
        return Evidence.create(
            entity_ref=shipment,
            source=source,
            modality=Modality.TEXT
            if source is EvidenceSource.DOCUMENT_EXTRACTION
            else Modality.STRUCTURED,
            observation_type=observation_type,
            value=value,
            observed_at=start,
            ingested_at=start,
            provenance=provenance,
            taxonomy=taxonomy,
        )

    evidence = [
        make(
            EvidenceSource.DOCUMENT_EXTRACTION,
            "permitted_temp_min_c",
            scenario.cargo.permitted_min_c,
        ),
        make(
            EvidenceSource.DOCUMENT_EXTRACTION,
            "permitted_temp_max_c",
            scenario.cargo.permitted_max_c,
        ),
    ]
    for fault in scenario.data_faults:
        erp = fault.erp_value
        if (
            fault.type is DataFaultType.ERP_BOL_MISMATCH
            and fault.field in {"permitted_temp_min_c", "permitted_temp_max_c"}
            and isinstance(erp, int | float)
        ):
            evidence.append(make(EvidenceSource.SQL_LEGACY, str(fault.field), float(erp)))
    return evidence


def build_cases(
    pack_dir: Path = DEFAULT_PACK_DIR,
    *,
    only: frozenset[str] | None = None,
) -> tuple[list[Case], list[str]]:
    """The incidents to investigate, and the scenarios on which none opened."""
    taxonomy = load_taxonomy()
    pack = load_pack(pack_dir)
    cases: list[Case] = []
    not_detected: list[str] = []

    for scenario_id in sorted(pack.scenarios):
        if only is not None and scenario_id not in only:
            continue
        scenario = pack.scenarios[scenario_id]
        readings = [_to_reading(event) for event in run_scenario(scenario).events]

        found = first_detection(
            readings,
            detector=PredictiveDetector(SlopeRiskService()),
            envelope=_envelope_for(scenario),
            taxonomy=taxonomy,
        )
        if found is None:
            not_detected.append(scenario_id)
            continue
        detection, minute = found

        start = readings[0].timestamp
        earliest = detection.detected_at - timedelta(minutes=CASE_HISTORY_MINUTES)
        visible = [r for r in readings if earliest <= r.timestamp <= detection.detected_at]

        evidence: list[Evidence] = _bol_and_erp_evidence(scenario, start, taxonomy)
        for reading in visible:
            evidence.extend(reading_to_evidence(reading, taxonomy=taxonomy))

        truth = scenario.ground_truth
        onset = _fault_onset(scenario)
        cases.append(
            Case(
                scenario_id=scenario_id,
                shipment_id=scenario.shipment_id,
                vehicle_id=scenario.vehicle_id,
                decision_time=detection.detected_at,
                decision_minute=minute,
                evidence=tuple(evidence),
                cargo_value_usd=scenario.cargo.value_usd,
                truth_root_cause=truth.root_cause.value,
                truth_contributing=tuple(c.value for c in truth.contributing),
                correct_action=truth.correct_action.value,
                alert_preceded_fault_onset=onset is not None and minute < onset,
            )
        )
    return cases, not_detected


# ---------------------------------------------------------------------------
# Running one case
# ---------------------------------------------------------------------------


async def _no_links(state: IncidentState) -> dict[str, Any]:
    return {"proposed_links": [], "_tokens": 0}


async def _no_narrative(state: IncidentState) -> dict[str, Any]:
    return {"narrative": "", "cited_evidence_ids": [], "_tokens": 0}


async def _run_graph(
    case: Case,
    *,
    propose: Any,
    narrate: Any,
) -> IncidentState:
    """The real graph, with a clock fixed at the decision minute.

    The workflow's `now` is the incident's own time, not the wall clock: it
    feeds the risk fit (so the probability in the prompt is reproducible) and
    the wall-clock budget (which then never expires, because nothing here is
    waiting on a person).
    """
    flow = IncidentWorkflow(
        propose_links=propose,
        narrate=narrate,
        facilities=FACILITIES,
        now=lambda: case.decision_time,
    )
    graph = build_graph(flow)
    state = initial_state(
        evidence=list(case.evidence),
        cargo_value_usd=case.cargo_value_usd,
        budget=BudgetState(budget=Budget(), started_at=case.decision_time),
        shipment_id=case.shipment_id,
        vehicle_id=case.vehicle_id,
    )
    result: IncidentState = await graph.ainvoke(state)
    return result


def _rules_view(case: Case) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Rule priors, ranked. Ties fall to the cause's name so the order is stable."""
    ordered = sorted(rule_priors(case.evidence).items(), key=lambda kv: (-kv[1], kv[0].value))
    ranking = tuple(cause.value for cause, _ in ordered)
    contributing = tuple(
        cause.value for cause, prior in ordered[1:] if prior >= CONTRIBUTING_THRESHOLD
    )
    return ranking, contributing


def _templated_summary(state: IncidentState, top_cause: str) -> str:
    """The rules-only arm's narrative: a fixed template over computed figures."""
    risk = state.get("risk")
    decision = state.get("decision")
    best = decision.recommended if decision else None
    envelope = state.get("envelope")
    parts = []
    if risk is not None and envelope is not None:
        parts.append(
            f"Breach probability is {risk.probability:.2f} against a permitted ceiling of "
            f"{envelope.maximum_c:.1f} C."
        )
    parts.append(f"The leading rule-derived cause is {top_cause.replace('_', ' ')}.")
    if best is not None:
        parts.append(
            f"Recommended action: {best.action.value.replace('_', ' ')} "
            f"(expected value {best.expected_value_usd:.0f} USD)."
        )
    return " ".join(parts)


async def _judge(provider: LLMProvider, model: str, digest: str, summary: str) -> tuple[int, float]:
    """Score one summary 1-5, blind to which arm wrote it. Returns (score, cost)."""
    response = await provider.complete(
        LLMRequest(
            model=model,
            system=JUDGE_SYSTEM,
            messages=(
                {
                    "role": "user",
                    "content": f"Evidence digest:\n{digest}\n\nSummary to grade:\n{summary}",
                },
            ),
            max_tokens=600,
            output_schema=JUDGE_SCHEMA,
            effort="low",
            node="judge",
            prompt_version=PROMPT_VERSION,
        )
    )
    return int((response.parsed or {})["score"]), response.cost_usd()


async def _investigate(
    case: Case,
    *,
    provider: LLMProvider,
    settings: Settings,
    meta: ArmMeta,
) -> CaseOutcome | None:
    propose, narrate = make_model_nodes(
        provider,
        links_model=settings.axon_model_reasoning,
        narrate_model=settings.axon_model_extraction,
    )
    llm_state = await _run_graph(case, propose=propose, narrate=narrate)
    visited = {entry["node"] for entry in llm_state.get("trace", [])}
    if "propose_links" not in visited:
        # Escalated on a deterministic node before the model was reached. That
        # happens identically in both arms, so it is neither arm's diagnosis.
        meta.not_investigated[case.scenario_id] = llm_state.get(
            "escalation_reason", "stopped before the model was consulted"
        )
        return None

    rules_state = await _run_graph(case, propose=_no_links, narrate=_no_narrative)
    rules_ranking, rules_contributing = _rules_view(case)

    hypotheses = llm_state.get("hypotheses", [])
    llm_ranking = tuple(h.root_cause.value for h in hypotheses)
    llm_contributing = tuple(
        h.root_cause.value for h in hypotheses[1:] if h.confidence >= CONTRIBUTING_THRESHOLD
    )

    def action_of(state: IncidentState) -> str | None:
        decision = state.get("decision")
        best = decision.recommended if decision else None
        return best.action.value if best else None

    notes = llm_state.get("notes", {})
    grounding = llm_state.get("grounding")
    narrative = llm_state.get("narrative", "")

    digest = build_digest(case.evidence, conflicts=llm_state.get("conflicts", [])).text
    rules_summary = _templated_summary(rules_state, rules_ranking[0])
    rules_score = llm_score = None
    judge_model = settings.axon_model_judge
    rules_score, cost_a = await _judge(provider, judge_model, digest, rules_summary)
    meta.judge_cost_usd += cost_a
    meta.judge_calls += 1
    if narrative:
        llm_score, cost_b = await _judge(provider, judge_model, digest, narrative)
        meta.judge_cost_usd += cost_b
        meta.judge_calls += 1

    return CaseOutcome(
        scenario_id=case.scenario_id,
        truth_root_cause=case.truth_root_cause,
        truth_contributing=case.truth_contributing,
        correct_action=case.correct_action,
        alert_preceded_fault_onset=case.alert_preceded_fault_onset,
        rules_ranking=rules_ranking,
        rules_contributing=rules_contributing,
        rules_action=action_of(rules_state),
        rules_explanation_score=rules_score,
        llm_ranking=llm_ranking,
        llm_contributing=llm_contributing,
        llm_action=action_of(llm_state),
        llm_explanation_score=llm_score,
        llm_narrative_grounded=None if grounding is None else grounding.grounded,
        llm_grounding_failures=()
        if grounding is None
        else tuple(f.value for f in grounding.failures),
        llm_figures_checked=0 if grounding is None else grounding.checked_numbers,
        llm_escalation_reason=llm_state.get("escalation_reason", ""),
        llm_handles_dropped=len(notes.get("handles_dropped", [])),
        invocations=tuple(notes.get("invocations", [])),
    )


async def _run_all(
    cases: list[Case], *, provider: LLMProvider, settings: Settings, meta: ArmMeta
) -> list[CaseOutcome]:
    gate = asyncio.Semaphore(CONCURRENCY)

    async def one(case: Case) -> CaseOutcome | None:
        async with gate:
            return await _investigate(case, provider=provider, settings=settings, meta=meta)

    results = await asyncio.gather(*(one(case) for case in cases))
    return sorted((r for r in results if r is not None), key=lambda o: o.scenario_id)


def run_llm_arm(
    *,
    only: frozenset[str] | None = None,
    provider: LLMProvider | None = None,
    settings: Settings | None = None,
) -> tuple[list[CaseOutcome], ArmMeta]:
    """Investigate every incident the pack opens, with the model in the loop.

    ``provider`` is injectable so a test can supply a scripted one; by default it
    is whatever `AXON_LLM_MODE` selects, which in CI is a cassette replay.
    """
    resolved = settings or get_settings()
    active = provider or build_provider(resolved)
    cases, not_detected = build_cases(only=only)

    meta = ArmMeta(
        not_detected=not_detected,
        models={
            "propose_links": resolved.axon_model_reasoning,
            "narrate": resolved.axon_model_extraction,
            "judge": resolved.axon_model_judge,
        },
        llm_mode=active.mode,
    )
    outcomes = asyncio.run(_run_all(cases, provider=active, settings=resolved, meta=meta))
    return outcomes, meta


def run_rules_arm(*, only: frozenset[str] | None = None) -> tuple[list[CaseOutcome], ArmMeta]:
    """The rules-only baseline over the same incidents. No model, no key.

    Investigates exactly the incidents `run_llm_arm` would: an incident that
    stops on a deterministic node before the model is reached stops in both
    arms, so it belongs to neither's diagnosis.
    """
    cases, not_detected = build_cases(only=only)
    meta = ArmMeta(not_detected=not_detected, llm_mode="none")

    async def go() -> list[CaseOutcome]:
        outcomes: list[CaseOutcome] = []
        for case in cases:
            state = await _run_graph(case, propose=_no_links, narrate=_no_narrative)
            if "propose_links" not in {entry["node"] for entry in state.get("trace", [])}:
                meta.not_investigated[case.scenario_id] = state.get(
                    "escalation_reason", "stopped before the model would have been consulted"
                )
                continue
            ranking, contributing = _rules_view(case)
            decision = state.get("decision")
            best = decision.recommended if decision else None
            outcomes.append(
                CaseOutcome(
                    scenario_id=case.scenario_id,
                    truth_root_cause=case.truth_root_cause,
                    truth_contributing=case.truth_contributing,
                    correct_action=case.correct_action,
                    alert_preceded_fault_onset=case.alert_preceded_fault_onset,
                    rules_ranking=ranking,
                    rules_contributing=contributing,
                    rules_action=best.action.value if best else None,
                )
            )
        return outcomes

    return asyncio.run(go()), meta
