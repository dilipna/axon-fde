"""The rules+LLM arm: the digest, the two model nodes, and the graders over them.

**Nothing here calls a model.** The provider is a scripted stand-in that
answers from what it is shown. That tests the machinery - what reaches the
prompt, what is done with the answer, how the outcome is graded - and says
nothing about what a real model would produce. That is measured by recording
cassettes, and until that happens `C3`/`C5`/`C10`/`C12` for the LLM arm have no
numbers; these tests are why they will mean something when they do.

**Every grader is shown to fail**, as everywhere in this package. A grader
that returns 1.0 for anything is worse than none because it gets quoted.
"""

from __future__ import annotations

import random
import re
from dataclasses import replace
from typing import Any

import pytest

from backend.app.agents.digest import build_digest
from backend.app.agents.scoring import MAX_SUPPORTING_EVIDENCE, rule_priors
from backend.app.domain.enums import RootCause
from backend.app.llm.provider import LLMRequest, LLMResponse, TokenUsage
from benchmarks.axonbench.claims import CLAIMS, ClaimStatus
from benchmarks.axonbench.graders.diagnosis import (
    CaseOutcome,
    CostLatencyGrader,
    DiagnosisGrader,
    GroundingGrader,
    LlmValueGrader,
)
from benchmarks.axonbench.llm_arm import build_cases, run_llm_arm, run_rules_arm

FLAGSHIP = "compressor_degradation_pharma_01"
DOOR = "door_open_pharma_01"


@pytest.fixture(scope="module")
def flagship_case():
    cases, _ = build_cases(only=frozenset({FLAGSHIP}))
    return cases[0]


# ---------------------------------------------------------------------------
# A provider that answers from what it is shown
# ---------------------------------------------------------------------------


def _handle(text: str, observation_type: str, source: str | None = None) -> str:
    """The digest handle of a `[E07] <type> = ...` line."""
    for line in text.splitlines():
        match = re.match(rf"\[(E\d+)\] {observation_type} = .*source {source or ''}", line)
        if match:
            return match.group(1)
    raise AssertionError(f"no {observation_type} line in the digest")


class ScriptedProvider:
    """Answers the three prompts. Records every request it was sent."""

    mode = "scripted"

    def __init__(self, *, links=None, narrative=None, score=4) -> None:
        self.requests: list[LLMRequest] = []
        self._links = links
        self._narrative = narrative
        self._score = score

    def _respond(self, request: LLMRequest, parsed: dict[str, Any]) -> LLMResponse:
        return LLMResponse(
            text="{}",
            usage=TokenUsage(input_tokens=1000, output_tokens=200, cache_read_tokens=0),
            model=request.model,
            parsed=parsed,
            latency_ms=1500,
        )

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        text = request.messages[0]["content"]
        if request.node == "propose_links":
            links = (
                self._links(text)
                if self._links
                else [
                    {
                        "root_cause": "compressor_degradation",
                        "evidence_handles": [
                            _handle(text, "fault_code"),
                            _handle(text, "compressor_rpm"),
                        ],
                        "rationale": "an active fault and a falling compressor",
                    }
                ]
            )
            return self._respond(request, {"links": links})
        if request.node == "narrate":
            body = (
                self._narrative(text)
                if self._narrative
                else {
                    "narrative": "The cargo is warming and the unit reports a fault.",
                    "cited_handles": [_handle(text, "cargo_temp_c")],
                }
            )
            return self._respond(request, body)
        return self._respond(request, {"score": self._score, "reason": "fine"})


# ---------------------------------------------------------------------------
# The digest
# ---------------------------------------------------------------------------


class TestTheDigest:
    def test_it_is_identical_whatever_order_the_evidence_arrives_in(self, flagship_case) -> None:
        """A cassette is keyed on the prompt, so the prompt must not depend on
        iteration order - or a replay would miss for no reason anyone could see."""
        shuffled = list(flagship_case.evidence)
        random.Random(7).shuffle(shuffled)
        assert build_digest(shuffled).text == build_digest(flagship_case.evidence).text

    def test_it_carries_no_evidence_uuid(self, flagship_case) -> None:
        """Ids are random per run. One in the prompt would make every replay a miss."""
        digest = build_digest(flagship_case.evidence)
        for evidence in flagship_case.evidence[:200]:
            assert str(evidence.id) not in digest.text
        assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-", digest.text)

    def test_every_handle_resolves_to_a_real_observation(self, flagship_case) -> None:
        digest = build_digest(flagship_case.evidence)
        known = {str(e.id) for e in flagship_case.evidence}
        assert digest.handles
        assert set(digest.handles.values()) <= known

    def test_two_sources_reporting_one_type_are_both_shown(self, flagship_case) -> None:
        """The flagship's ERP says 10 C and the signed BOL says 8 C. Showing
        only whichever sorted last put the wrong ceiling in front of the model."""
        text = build_digest(flagship_case.evidence).text
        assert re.search(r"permitted_temp_max_c = 8\.00 \(source document_extraction", text)
        assert re.search(r"permitted_temp_max_c = 10\.00 \(source sql_legacy", text)

    def test_it_never_contains_ground_truth(self, flagship_case) -> None:
        text = build_digest(flagship_case.evidence).text
        for leaked in (flagship_case.scenario_id, flagship_case.correct_action, "decisive"):
            assert leaked not in text

    def test_no_evidence_is_said_plainly(self) -> None:
        digest = build_digest([])
        assert digest.handles == {}
        assert "No observations" in digest.text


# ---------------------------------------------------------------------------
# The model nodes
# ---------------------------------------------------------------------------


class TestTheModelNodes:
    def test_a_normal_answer_becomes_a_scored_hypothesis(self) -> None:
        provider = ScriptedProvider()
        outcomes, meta = run_llm_arm(only=frozenset({FLAGSHIP}), provider=provider)
        outcome = outcomes[0]
        assert outcome.llm_ranking and outcome.llm_ranking[0] == "compressor_degradation"
        # Two model calls in the workflow, and both were recorded.
        assert [c["node"] for c in outcome.invocations] == ["propose_links", "narrate"]
        # The judge scored both summaries, and is kept out of the incident's cost.
        assert meta.judge_calls == 2
        assert {r.node for r in provider.requests} == {"propose_links", "narrate", "judge"}

    def test_a_fabricated_handle_is_dropped_and_counted(self) -> None:
        def links(text: str) -> list[dict[str, Any]]:
            return [
                {
                    "root_cause": "compressor_degradation",
                    "evidence_handles": [_handle(text, "fault_code"), "E99"],
                    "rationale": "x",
                }
            ]

        outcomes, _ = run_llm_arm(
            only=frozenset({FLAGSHIP}), provider=ScriptedProvider(links=links)
        )
        assert outcomes[0].llm_handles_dropped == 1
        assert outcomes[0].llm_ranking == ("compressor_degradation",)

    def test_a_link_citing_nothing_real_is_dropped_not_guessed(self) -> None:
        """`ProposedLink` refuses an empty citation list; the node must drop the
        link rather than let the constructor raise or invent a citation."""

        def links(text: str) -> list[dict[str, Any]]:
            return [
                {
                    "root_cause": "door_left_open",
                    "evidence_handles": ["E98", "E99"],
                    "rationale": "x",
                }
            ]

        outcomes, _ = run_llm_arm(
            only=frozenset({FLAGSHIP}), provider=ScriptedProvider(links=links)
        )
        assert outcomes[0].llm_ranking == ()

    async def test_the_model_cannot_raise_a_hypothesis_above_its_rule_prior(
        self, flagship_case
    ) -> None:
        """Invariant I2, through the real nodes.

        The model links a cause the rules gave the floor prior (there is no fuel
        rule), citing four observations. Its confidence is still capped by that
        prior, so the model cannot talk its way to a confident wrong answer.
        """
        from backend.app.agents.nodes.model_nodes import make_model_nodes
        from benchmarks.axonbench.llm_arm import _run_graph

        def links(text: str) -> list[dict[str, Any]]:
            handles = [
                _handle(text, t)
                for t in ("cargo_temp_c", "compressor_rpm", "fault_code", "reefer_fuel_pct")
            ]
            return [
                {
                    "root_cause": "reefer_fuel_exhaustion",
                    "evidence_handles": handles,
                    "rationale": "trust me",
                }
            ]

        propose, narrate = make_model_nodes(
            ScriptedProvider(links=links), links_model="gpt-5", narrate_model="gpt-5-mini"
        )
        state = await _run_graph(flagship_case, propose=propose, narrate=narrate)
        top = state["hypotheses"][0]
        floor = rule_priors(flagship_case.evidence)[RootCause.REEFER_FUEL_EXHAUSTION]
        assert top.root_cause is RootCause.REEFER_FUEL_EXHAUSTION
        assert len(top.supporting_evidence_ids) == MAX_SUPPORTING_EVIDENCE
        assert top.confidence <= top.prior == floor
        assert floor < 0.05  # the rules really did think it implausible

    def test_a_narrative_citing_an_unknown_handle_fails_grounding(self) -> None:
        """The node passes an unknown handle through as a non-bundle id, so the
        deterministic check sees the fabrication. Filtering it here would hide
        from the check the one thing it exists to catch."""
        narrative = lambda text: {  # noqa: E731
            "narrative": "The cargo is warming.",
            "cited_handles": ["E99"],
        }
        outcomes, _ = run_llm_arm(
            only=frozenset({FLAGSHIP}), provider=ScriptedProvider(narrative=narrative)
        )
        assert outcomes[0].llm_narrative_grounded is False
        assert "fabricated_citation" in outcomes[0].llm_grounding_failures
        assert outcomes[0].llm_escalation_reason

    def test_no_prompt_carries_ground_truth(self) -> None:
        provider = ScriptedProvider()
        run_llm_arm(only=frozenset({FLAGSHIP}), provider=provider)
        for request in provider.requests:
            body = request.messages[0]["content"]
            assert FLAGSHIP not in body
            assert "reroute_to_cold_storage" not in body.split("Interventions ranked")[0]

    def test_the_prompts_are_byte_stable_across_runs(self) -> None:
        """Replay depends on it: a different prompt is a different cassette key."""
        first, second = ScriptedProvider(), ScriptedProvider()
        run_llm_arm(only=frozenset({FLAGSHIP}), provider=first)
        run_llm_arm(only=frozenset({FLAGSHIP}), provider=second)
        from backend.app.llm.cassettes import cassette_key

        assert sorted(cassette_key(r) for r in first.requests) == sorted(
            cassette_key(r) for r in second.requests
        )


# ---------------------------------------------------------------------------
# Building the incidents
# ---------------------------------------------------------------------------


class TestTheIncidents:
    def test_the_rules_baseline_needs_no_model_and_covers_the_same_incidents(self) -> None:
        outcomes, meta = run_rules_arm(only=frozenset({FLAGSHIP, DOOR}))
        assert {o.scenario_id for o in outcomes} == {FLAGSHIP, DOOR}
        assert all(o.llm_ranking is None for o in outcomes)
        assert meta.llm_mode == "none"

    def test_a_scenario_that_never_opens_an_incident_is_named_not_dropped(self) -> None:
        cases, not_detected = build_cases(only=frozenset({"normal_pharma_run_01", FLAGSHIP}))
        assert [c.scenario_id for c in cases] == [FLAGSHIP]
        assert not_detected == ["normal_pharma_run_01"]

    def test_the_flagship_is_investigated_at_the_minute_the_detector_fired(
        self, flagship_case
    ) -> None:
        """102 is the figure the demo, the claim register and the UI all quote."""
        assert flagship_case.decision_minute == 102
        assert flagship_case.truth_root_cause == "compressor_degradation"


# ---------------------------------------------------------------------------
# Graders, each shown to fail
# ---------------------------------------------------------------------------


def outcome(
    scenario_id: str = "s",
    *,
    truth: str = "compressor_degradation",
    contributing: tuple[str, ...] = (),
    rules: tuple[str, ...] = ("compressor_degradation",),
    llm: tuple[str, ...] | None = ("compressor_degradation",),
    early: bool = False,
    **extra: Any,
) -> CaseOutcome:
    return CaseOutcome(
        scenario_id=scenario_id,
        truth_root_cause=truth,
        truth_contributing=contributing,
        correct_action="reroute_to_cold_storage",
        alert_preceded_fault_onset=early,
        rules_ranking=rules,
        rules_contributing=(),
        rules_action="reroute_to_cold_storage",
        llm_ranking=llm,
        llm_action="reroute_to_cold_storage",
        **extra,
    )


class TestC3TheDiagnosisGrader:
    def test_all_right_scores_one_and_all_wrong_scores_zero(self) -> None:
        right = tuple(outcome(f"r{i}") for i in range(4))
        wrong = tuple(outcome(f"w{i}", rules=("no_fault",), llm=("no_fault",)) for i in range(4))
        assert DiagnosisGrader(right, arm="rules").measure().value == 1.0
        assert DiagnosisGrader(wrong, arm="rules").measure().value == 0.0

    def test_an_empty_ranking_is_wrong_not_a_crash_or_a_free_pass(self) -> None:
        empty = (outcome(llm=()), outcome("b", llm=None))
        assert DiagnosisGrader(empty, arm="llm").measure().value == 0.0

    def test_top3_counts_a_correct_second_choice(self) -> None:
        o = (outcome(rules=("no_fault", "compressor_degradation", "door_left_open")),)
        m = DiagnosisGrader(o, arm="rules").measure()
        assert m.value == 0.0
        assert m.companions["top3_accuracy"] == 1.0

    def test_contributing_f1_is_computed_over_sets(self) -> None:
        # predicted {a,b} vs true {a,c}: tp=1 fp=1 fn=1 -> F1 = 2/4
        o = (
            replace(
                outcome(contributing=("environmental_heat", "route_delay")),
                rules_contributing=("environmental_heat", "door_left_open"),
            ),
        )
        assert DiagnosisGrader(o, arm="rules").measure().companions["contributing_cause_f1"] == 0.5

    def test_incidents_opened_before_the_fault_are_split_out(self) -> None:
        o = (outcome("a"), outcome("b", rules=("no_fault",), early=True))
        m = DiagnosisGrader(o, arm="rules").measure()
        assert m.value == 0.5
        assert m.companions["top1_accuracy_after_fault_onset"] == 1.0
        assert m.companions["incidents_before_fault_onset"] == 1.0

    def test_below_the_required_count_it_is_insufficient_not_measured(self) -> None:
        few = tuple(outcome(f"s{i}") for i in range(5))
        result = DiagnosisGrader(few, arm="rules").grade()
        assert result.status is ClaimStatus.INSUFFICIENT_DATA
        assert str(CLAIMS["C3"].required_cases) in result.reason

    def test_at_the_required_count_it_is_measured(self) -> None:
        enough = tuple(outcome(f"s{i}") for i in range(CLAIMS["C3"].required_cases))
        assert DiagnosisGrader(enough, arm="rules").grade().status is ClaimStatus.MEASURED


class TestC5TheAblation:
    def test_it_reports_a_negative_delta_rather_than_only_a_positive_one(self) -> None:
        """The register says C5 may be refuted. A grader that could only report
        a gain would be a claim, not a measurement."""
        worse = tuple(outcome(f"s{i}", llm=("no_fault",)) for i in range(4))
        assert LlmValueGrader(worse).measure().value == -1.0

    def test_it_reports_a_gain(self) -> None:
        better = tuple(outcome(f"s{i}", rules=("no_fault",)) for i in range(4))
        m = LlmValueGrader(better).measure()
        assert m.value == 1.0
        assert m.detail["llm_wins"] == ["s0", "s1", "s2", "s3"]

    def test_the_action_delta_is_measured_and_moves_when_the_actions_differ(self) -> None:
        """Action selection is unchanged by construction. This is the check
        that the code still says so: it goes red the day a hypothesis reaches
        the decision engine."""
        differing = tuple(replace(outcome(f"s{i}"), llm_action="do_nothing") for i in range(4))
        m = LlmValueGrader(differing).measure()
        assert m.companions["delta_correct_action_rate"] == -1.0
        assert m.companions["incidents_with_identical_action"] == 0.0

    def test_explanation_delta_uses_only_incidents_both_arms_were_judged_on(self) -> None:
        o = (
            outcome("a", rules_explanation_score=2, llm_explanation_score=4),
            outcome("b", rules_explanation_score=2),
        )
        m = LlmValueGrader(o).measure()
        assert m.companions["explanations_judged"] == 1.0
        assert m.companions["delta_explanation_score"] == 2.0


class TestC10Grounding:
    def test_the_rate_counts_ungrounded_narratives(self) -> None:
        o = (
            outcome("a", llm_narrative_grounded=True),
            outcome(
                "b",
                llm_narrative_grounded=False,
                llm_grounding_failures=("fabricated_citation",),
            ),
        )
        m = GroundingGrader(o).measure()
        assert m.value == 0.5
        assert m.detail["failure_counts"] == {"fabricated_citation": 1}

    def test_a_run_with_no_narratives_has_nothing_to_grade(self) -> None:
        m = GroundingGrader((outcome(),)).measure()
        assert m.cases == 0


class TestC12CostAndLatency:
    @staticmethod
    def call(cost: float, ms: int, node: str = "propose_links") -> dict[str, Any]:
        return {
            "node": node,
            "model": "gpt-5",
            "input_tokens": 900,
            "output_tokens": 100,
            "cache_read_tokens": 100,
            "cost_usd": cost,
            "latency_ms": ms,
        }

    def test_cost_and_latency_are_summed_per_incident(self) -> None:
        o = (
            outcome("a", invocations=(self.call(0.01, 1000), self.call(0.02, 2000, "narrate"))),
            outcome("b", invocations=(self.call(0.05, 3000), self.call(0.05, 3000, "narrate"))),
        )
        m = CostLatencyGrader(o).measure()
        assert m.companions["cost_usd_p50"] == pytest.approx(0.065)
        assert m.companions["cost_usd_total"] == pytest.approx(0.13)
        assert m.companions["latency_s_p50"] == pytest.approx(4.5)
        assert m.companions["model_calls_per_incident"] == 2.0
        assert m.companions["cached_input_share"] == pytest.approx(400 / 4000)

    def test_p95_is_not_the_max_and_not_the_median(self) -> None:
        o = tuple(outcome(f"s{i}", invocations=(self.call(float(i + 1), 1000),)) for i in range(40))
        p95 = CostLatencyGrader(o).measure().companions["cost_usd_p95"]
        assert 37.0 < p95 < 40.0

    def test_incidents_that_made_no_call_are_not_counted_as_free(self) -> None:
        """Averaging zeros in would make an outage look like a saving."""
        o = (outcome("a", invocations=(self.call(0.10, 1000),)), outcome("b"))
        m = CostLatencyGrader(o).measure()
        assert m.cases == 1
        assert m.companions["cost_usd_p50"] == pytest.approx(0.10)

    def test_no_calls_at_all_is_insufficient_not_a_zero_dollar_result(self) -> None:
        result = CostLatencyGrader((outcome(),)).grade()
        assert result.status is ClaimStatus.INSUFFICIENT_DATA


class TestTheRegister:
    def test_c9_is_blocked_with_its_reason_and_is_not_a_zero(self) -> None:
        assert CLAIMS["C9"].blocked_by
        assert "attack pack" in CLAIMS["C9"].blocked_by

    def test_c3_c5_c10_c12_are_registered_with_a_stated_dataset(self) -> None:
        for claim in ("C3", "C5", "C10", "C12"):
            assert CLAIMS[claim].required_cases > 0
            assert not CLAIMS[claim].blocked_by, f"{claim} is reachable in the rules_llm arm"
