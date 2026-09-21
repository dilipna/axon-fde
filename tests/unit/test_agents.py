"""Budgets, scoring and grounding - the three deterministic guards around the model.

The load-bearing property here is that **the model cannot raise a
hypothesis above its rule prior**. That is invariant I2 made operational: the
model proposes links, and a deterministic function owns every number.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from backend.app.agents.budget import Budget, BudgetKind, BudgetState
from backend.app.agents.grounding import (
    GroundingFailure,
    PermittedValues,
    check_grounding,
)
from backend.app.agents.scoring import (
    MAX_SUPPORTING_EVIDENCE,
    PRIOR_OBSERVATION_TYPES,
    ProposedLink,
    rule_priors,
    score_hypotheses,
)
from backend.app.domain.enums import EvidenceSource, Modality, RootCause
from backend.app.domain.evidence import EntityRef, Evidence, Provenance
from backend.app.domain.taxonomy import load_taxonomy

START = datetime(2026, 9, 20, 14, 0, tzinfo=UTC)


def observation(
    observation_type: str = "cargo_temp_c",
    value: float | str | bool | list[str] = 7.9,
    *,
    at: datetime | None = None,
) -> Evidence:
    moment = at or START
    return Evidence.create(
        entity_ref=EntityRef(kind="vehicle", id="AX-042"),
        source=EvidenceSource.TELEMETRY,
        modality=Modality.TIMESERIES,
        observation_type=observation_type,
        value=value,
        observed_at=moment,
        ingested_at=moment + timedelta(seconds=5),
        provenance=Provenance(producer="test_agents", producer_version="1.0.0", note="fixture"),
    )


# ---------------------------------------------------------------------------
# Budgets
# ---------------------------------------------------------------------------


class TestBudgets:
    def test_each_limit_is_enforced_independently(self) -> None:
        """Four limits, because they fail in different ways.

        A single combined limit would let three of them run away: one node can
        make forty tool calls inside one step, and a run blocked on a slow
        external system consumes neither.
        """
        budget = Budget(max_steps=5, max_tool_calls=5, max_wall_clock_seconds=60, max_tokens=100)
        base = BudgetState(budget=budget, started_at=START)

        assert base.advance(steps=5).check(START).primary is BudgetKind.STEPS
        assert base.advance(steps=0, tool_calls=5).check(START).primary is BudgetKind.TOOL_CALLS
        assert base.advance(steps=0, tokens=100).check(START).primary is BudgetKind.TOKENS
        assert base.check(START + timedelta(seconds=60)).primary is BudgetKind.WALL_CLOCK

    def test_a_run_inside_every_limit_may_continue(self) -> None:
        state = BudgetState(budget=Budget(), started_at=START).advance(steps=3, tokens=500)
        assert state.check(START + timedelta(seconds=5)).may_continue

    def test_every_exhausted_budget_is_reported_not_just_the_first(self) -> None:
        """Otherwise an operator raises one limit and rediscovers the next.

        The same argument as approval refusals: one situation should cost one
        round trip through a human, not three.
        """
        budget = Budget(max_steps=2, max_tool_calls=2, max_wall_clock_seconds=10, max_tokens=10)
        state = BudgetState(budget=budget, started_at=START).advance(
            steps=2, tool_calls=2, tokens=10
        )
        verdict = state.check(START + timedelta(seconds=30))
        assert set(verdict.exhausted) == set(BudgetKind)

    def test_spending_a_budget_does_not_raise(self) -> None:
        """Exhaustion escalates; it does not throw.

        A workflow that raised would lose everything it had established, and
        the dispatcher would get a stack trace where they needed a partial
        answer and a human.
        """
        budget = Budget(max_steps=1, max_tool_calls=1, max_wall_clock_seconds=1, max_tokens=1)
        verdict = BudgetState(budget=budget, started_at=START).advance(steps=99).check(START)
        assert verdict.may_continue is False
        assert "exhausted" in verdict.describe()

    def test_state_is_immutable_so_a_resumed_run_cannot_double_count(self) -> None:
        """The graph checkpoints after every node.

        A mutable counter shared across a resumed run would re-add whatever
        happened before the checkpoint, and the budget would expire early for
        reasons nobody could reproduce.
        """
        first = BudgetState(budget=Budget(), started_at=START)
        second = first.advance(steps=3)
        assert first.steps == 0
        assert second.steps == 3

    def test_a_zero_budget_is_refused_at_construction(self) -> None:
        with pytest.raises(ValueError, match="can never start"):
            Budget(max_steps=0)

    def test_a_naive_timestamp_is_refused(self) -> None:
        state = BudgetState(budget=Budget(), started_at=START)
        with pytest.raises(ValueError, match="timezone-aware"):
            state.check(datetime(2026, 9, 20, 14, 5))  # noqa: DTZ001 - the point


# ---------------------------------------------------------------------------
# Scoring - invariant I2
# ---------------------------------------------------------------------------


class TestTheModelCannotSetAConfidence:
    def test_a_proposed_link_has_nowhere_to_put_a_number(self) -> None:
        """Structural, not behavioural.

        Adding a `confidence` field to `ProposedLink` is the single change
        that would break I2, so the absence is asserted rather than left to be
        noticed in review.
        """
        fields = set(ProposedLink.__dataclass_fields__)
        assert "confidence" not in fields
        assert "score" not in fields
        assert "probability" not in fields

    def test_linking_more_evidence_never_raises_a_hypothesis_above_its_prior(self) -> None:
        """The property that makes this safe rather than merely tidy.

        A model that linked forty high-confidence readings to the wrong cause
        is still capped by what the rules think plausible. The most it can do
        is fail to raise something that deserved raising - visible in the
        output, rather than silently persuasive.
        """
        evidence = [observation(at=START + timedelta(minutes=i)) for i in range(40)]
        priors = {RootCause.COMPRESSOR_DEGRADATION: 0.30}
        link = ProposedLink(
            root_cause=RootCause.COMPRESSOR_DEGRADATION,
            evidence_ids=tuple(str(item.id) for item in evidence),
            rationale="Everything supports this.",
        )
        scored = score_hypotheses([link], evidence=evidence, priors=priors)
        assert scored[0].confidence <= 0.30
        assert scored[0].uplift_over_prior <= 0.0

    def test_repeating_one_observation_does_not_manufacture_corroboration(self) -> None:
        """A thousand readings from one thermometer are one thermometer."""
        item = observation()
        once = score_hypotheses(
            [ProposedLink(RootCause.COMPRESSOR_DEGRADATION, (str(item.id),))],
            evidence=[item],
            priors={RootCause.COMPRESSOR_DEGRADATION: 0.8},
        )
        repeated = score_hypotheses(
            [ProposedLink(RootCause.COMPRESSOR_DEGRADATION, (str(item.id),) * 10)],
            evidence=[item],
            priors={RootCause.COMPRESSOR_DEGRADATION: 0.8},
        )
        assert once[0].confidence == repeated[0].confidence
        assert len(repeated[0].supporting_evidence_ids) == 1

    def test_support_is_capped_so_a_long_replay_cannot_inflate_a_hypothesis(self) -> None:
        evidence = [observation(at=START + timedelta(minutes=i)) for i in range(20)]
        link = ProposedLink(
            RootCause.COMPRESSOR_DEGRADATION, tuple(str(item.id) for item in evidence)
        )
        scored = score_hypotheses(
            [link], evidence=evidence, priors={RootCause.COMPRESSOR_DEGRADATION: 0.9}
        )
        assert len(scored[0].supporting_evidence_ids) == MAX_SUPPORTING_EVIDENCE

    def test_a_link_to_evidence_that_does_not_exist_is_dropped_not_guessed_at(self) -> None:
        """The second layer under the grounding check.

        A scorer that silently accepted an unknown id would make the first
        layer optional, and layers that are optional are the ones that get
        skipped.
        """
        item = observation()
        scored = score_hypotheses(
            [ProposedLink(RootCause.COMPRESSOR_DEGRADATION, (str(uuid4()), str(item.id)))],
            evidence=[item],
            priors={RootCause.COMPRESSOR_DEGRADATION: 0.8},
        )
        assert scored[0].supporting_evidence_ids == (str(item.id),)

    def test_a_hypothesis_with_no_usable_evidence_collapses(self) -> None:
        """An unsupported hypothesis is not as good as an unexamined one."""
        item = observation()
        scored = score_hypotheses(
            [ProposedLink(RootCause.DOOR_LEFT_OPEN, (str(uuid4()),))],
            evidence=[item],
            priors={RootCause.DOOR_LEFT_OPEN: 0.9},
        )
        assert scored[0].confidence == 0.0

    def test_a_link_citing_nothing_is_refused_at_construction(self) -> None:
        with pytest.raises(ValueError, match="cites no evidence"):
            ProposedLink(RootCause.COMPRESSOR_DEGRADATION, ())

    def test_scoring_is_deterministic(self) -> None:
        evidence = [observation(at=START + timedelta(minutes=i)) for i in range(5)]
        link = ProposedLink(
            RootCause.COMPRESSOR_DEGRADATION, tuple(str(item.id) for item in evidence)
        )
        first = score_hypotheses([link], evidence=evidence)
        second = score_hypotheses([link], evidence=evidence)
        assert [h.explain() for h in first] == [h.explain() for h in second]


class TestRulePriors:
    def test_every_type_the_rules_read_is_declared_in_the_taxonomy(self) -> None:
        """The test that would have caught the first draft.

        It read `setpoint_temp_c`, `door_open_state` and `reefer_fault_codes`,
        none of which exist. Nothing raised: the lookups missed, every prior
        stayed at its floor, and the linking step would have been capped at
        0.02 on every hypothesis for ever. A rule that reads an undeclared
        type is not a weak rule - it is an absent one, and it fails silently
        in the direction of looking cautious.
        """
        declared = set(load_taxonomy().observations)
        missing = sorted(PRIOR_OBSERVATION_TYPES - declared)
        assert not missing, f"rules read undeclared types: {missing}"

    def test_a_silent_compressor_failure_still_gets_a_real_prior(self) -> None:
        """The flagship scenario has no fault code for over an hour.

        A prior that required one would leave the linking step unable to raise
        the correct hypothesis on the scenario the whole system was built
        around.
        """
        evidence = [
            observation("cargo_temp_c", 7.9),
            observation("permitted_temp_max_c", 8.0),
            observation("reefer_status", "running"),
        ]
        assert rule_priors(evidence)[RootCause.COMPRESSOR_DEGRADATION] >= 0.5

    def test_a_reported_breach_with_a_healthy_unit_raises_sensor_malfunction(self) -> None:
        """The sensor-drift scenario: the instrument is lying.

        This prior is what stops the linking step building a confident
        compressor story on a thermometer's word.
        """
        evidence = [
            observation("cargo_temp_c", 8.6),
            observation("permitted_temp_max_c", 8.0),
            observation("reefer_status", "running"),
            observation("compressor_rpm", 1800.0),
        ]
        assert rule_priors(evidence)[RootCause.SENSOR_MALFUNCTION] > 0.3

    def test_a_fault_code_suppresses_the_sensor_hypothesis(self) -> None:
        """A unit complaining about itself is not a lying thermometer."""
        evidence = [
            observation("cargo_temp_c", 8.6),
            observation("permitted_temp_max_c", 8.0),
            observation("reefer_status", "running"),
            observation("compressor_rpm", 1800.0),
            observation("fault_code", ["AL17"]),
        ]
        priors = rule_priors(evidence)
        assert priors[RootCause.SENSOR_MALFUNCTION] < priors[RootCause.COMPRESSOR_DEGRADATION]

    def test_an_ajar_door_counts_as_a_door_left_open(self) -> None:
        """Treating only `open` as a fault would miss the commonest version."""
        priors = rule_priors([observation("door_state", "ajar")])
        assert priors[RootCause.DOOR_LEFT_OPEN] > 0.5

    def test_the_null_hypothesis_is_not_pinned_at_the_floor(self) -> None:
        """A system whose null hypothesis is always tiny will always find a cause."""
        evidence = [
            observation("cargo_temp_c", 5.2),
            observation("permitted_temp_max_c", 8.0),
            observation("reefer_status", "cycling"),
        ]
        assert rule_priors(evidence)[RootCause.NO_FAULT] > 0.5

    def test_a_missing_ceiling_leaves_the_temperature_priors_alone(self) -> None:
        """No envelope, no judgement (I6).

        Assuming a ceiling nobody stated is how a prior becomes an opinion.
        """
        priors = rule_priors([observation("cargo_temp_c", 9.9)])
        assert priors[RootCause.COMPRESSOR_DEGRADATION] < 0.1
        assert priors[RootCause.NO_FAULT] < 0.1

    def test_no_prior_is_ever_zero(self) -> None:
        """A zero prior is a veto: no amount of evidence could ever raise it."""
        assert all(value > 0.0 for value in rule_priors([]).values())


# ---------------------------------------------------------------------------
# Grounding
# ---------------------------------------------------------------------------


class TestGrounding:
    def test_a_fabricated_citation_is_rejected(self) -> None:
        """The B9 acceptance criterion.

        A narrative citing an observation that does not exist claims a basis
        it does not have, and it reads identically to one that does.
        """
        item = observation()
        report = check_grounding(
            "The cargo reached 7.9 C.",
            cited_evidence_ids=[str(item.id), str(uuid4())],
            evidence=[item],
        )
        assert report.grounded is False
        assert GroundingFailure.FABRICATED_CITATION in report.failures
        assert len(report.fabricated_ids) == 1

    def test_a_narrative_quoting_a_real_reading_is_grounded(self) -> None:
        item = observation("cargo_temp_c", 7.9)
        report = check_grounding(
            "The cargo reached 7.9 C against an 8.0 C ceiling.",
            cited_evidence_ids=[str(item.id)],
            evidence=[item],
            permitted=PermittedValues.of(8.0),
        )
        assert report.grounded, report.describe()

    def test_an_invented_temperature_is_caught(self) -> None:
        """The number that matters most in a cold-chain narrative.

        An earlier draft ignored every figure below 10, which exempted every
        temperature in the system while diligently checking the dollar
        figures. This test is why that was found.
        """
        item = observation("cargo_temp_c", 7.9)
        report = check_grounding(
            "The cargo reached 9.4 C.",
            cited_evidence_ids=[str(item.id)],
            evidence=[item],
        )
        assert GroundingFailure.UNSUPPORTED_NUMBER in report.failures
        assert report.unsupported_numbers == (9.4,)

    def test_a_percentage_matches_the_probability_it_refers_to(self) -> None:
        """ "78%" legitimately refers to a stored 0.78.

        Offering only one reading would reject correct prose; reporting both
        as separate figures - which the first draft did - marked a correct
        narrative as half fabricated.
        """
        item = observation("cargo_temp_c", 7.9)
        report = check_grounding(
            "Breach probability is 78% within the hour.",
            cited_evidence_ids=[str(item.id)],
            evidence=[item],
            permitted=PermittedValues.of(0.78, 7.9),
        )
        assert report.grounded, report.describe()

    def test_small_counts_in_prose_are_not_treated_as_measurements(self) -> None:
        """Otherwise a real fabrication is buried under false positives.

        A check nobody reads is a check that does not exist.
        """
        item = observation("cargo_temp_c", 7.9)
        report = check_grounding(
            "Across 3 readings and 2 sources, the cargo reached 7.9 C.",
            cited_evidence_ids=[str(item.id)],
            evidence=[item],
        )
        assert report.grounded, report.describe()

    def test_a_dollar_figure_with_separators_is_checked(self) -> None:
        item = observation("cargo_temp_c", 7.9)
        report = check_grounding(
            "The consignment is worth $184,000.",
            cited_evidence_ids=[str(item.id)],
            evidence=[item],
            permitted=PermittedValues.of(184_000.0),
        )
        assert report.grounded, report.describe()

        invented = check_grounding(
            "The consignment is worth $250,000.",
            cited_evidence_ids=[str(item.id)],
            evidence=[item],
            permitted=PermittedValues.of(184_000.0),
        )
        assert GroundingFailure.UNSUPPORTED_NUMBER in invented.failures

    def test_a_non_numeric_observation_licenses_no_figures(self) -> None:
        """A categorical reading is not a number, however it is stored."""
        door = observation("door_state", "open")
        report = check_grounding(
            "The manifest lists 1487 units.",
            cited_evidence_ids=[str(door.id)],
            evidence=[door],
        )
        assert GroundingFailure.UNSUPPORTED_NUMBER in report.failures

    def test_an_uncited_recommendation_is_an_opinion(self) -> None:
        item = observation()
        report = check_grounding("Reroute the vehicle.", cited_evidence_ids=[], evidence=[item])
        assert GroundingFailure.NO_CITATIONS in report.failures

    def test_the_report_is_storable_and_says_what_it_checked(self) -> None:
        """`grounding_check` is a NOT NULL column on every recommendation.

        A report that said only "failed" would make the column decorative.
        """
        item = observation("cargo_temp_c", 7.9)
        payload = check_grounding(
            "The cargo reached 7.9 C.",
            cited_evidence_ids=[str(item.id)],
            evidence=[item],
        ).as_payload()
        assert payload["grounded"] is True
        assert payload["checked_citations"] == 1
        assert payload["checked_numbers"] >= 1
