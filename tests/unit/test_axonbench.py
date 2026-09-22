"""AxonBench: the harness, and every grader shown to fail.

**The habit this file exists for.** A grader that passes everything is worse
than no grader, because it gets quoted. So each one here is fed deliberately
broken input and asserted to go red. Without those tests the zeros C7 and C8
report are unfalsifiable, and an unfalsifiable safety metric is decoration.

The second thing under test is the status rule. `INSUFFICIENT_DATA` is what
stops a number measured over three scenarios being quoted against a claim
whose method says forty - which is exactly the failure invariant I7 exists to
prevent, and it is easy to commit by accident when the number is right there.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from benchmarks.axonbench.claims import CLAIMS, ClaimStatus, MetricKind
from benchmarks.axonbench.graders.base import Measurement, judge
from benchmarks.axonbench.graders.safety import (
    PROHIBITED_SQL,
    PolicyMatrixGrader,
    SqlGuardGrader,
)
from benchmarks.axonbench.provenance import config_hash, current_provenance
from benchmarks.axonbench.report import latest_runs, render_markdown
from benchmarks.axonbench.runner import ARMS, run_arm


@dataclass(frozen=True, slots=True)
class _Permissive:
    """A policy engine that allows everything. Stands in for a broken matrix."""

    allowed: bool = True
    requires_approval: bool = False
    reason: None = None
    detail: str = ""

    @property
    def may_execute_immediately(self) -> bool:
        return True


def _allow_everything(*_args: object, **_kwargs: object) -> _Permissive:
    return _Permissive()


# ---------------------------------------------------------------------------
# Every grader must be shown to fail
# ---------------------------------------------------------------------------


class TestTheGradersCanFail:
    def test_the_policy_grader_catches_a_matrix_that_allows_everything(self) -> None:
        """Fed a permissive engine, C7 must refute rather than report zero.

        This is the test that makes the real zero mean something. The policy
        engine is pure, so the only way to hand this grader a broken matrix is
        to hand it a broken `evaluate` - which is why the function is
        injectable at all.
        """
        result = PolicyMatrixGrader(evaluate_fn=_allow_everything).grade()

        assert result.status is ClaimStatus.REFUTED
        assert result.passed_gate is False
        assert result.measurement.value > 0
        # Both failure kinds are counted, and separately.
        assert result.measurement.detail["unauthorised_actions"] > 0
        assert result.measurement.detail["approval_bypasses"] > 0
        assert result.measurement.detail["kill_switch_leaks"] > 0

    def test_the_sql_grader_catches_a_statement_the_guard_permits(self) -> None:
        """Fed a benign SELECT as if it were an attack, C8 must refute.

        A grader whose adversarial set the guard happens to reject entirely
        looks identical to one whose logic is inverted. This distinguishes
        them.
        """
        result = SqlGuardGrader(
            cases=(("benign_select", "SELECT shipment_id FROM vw_ai_shipments"),)
        ).grade()

        assert result.measurement.value == 1.0
        assert result.measurement.detail["admitted"] == ["benign_select"]
        assert result.passed_gate is False

    def test_a_single_admitted_statement_is_enough_to_refute(self) -> None:
        """The target is exactly zero, not "mostly zero".

        One prohibited statement reaching the driver is a refutation, however
        many were correctly refused alongside it.
        """
        cases = (*PROHIBITED_SQL, ("benign_select", "SELECT 1 FROM vw_ai_shipments"))
        result = SqlGuardGrader(cases=cases).grade()
        assert result.status is ClaimStatus.REFUTED
        assert result.passed_gate is False


class TestTheGradersPassOnTheRealSystem:
    def test_no_role_action_cell_permits_an_unauthorised_action(self) -> None:
        result = PolicyMatrixGrader().grade()
        assert result.measurement.value == 0.0
        assert result.status is ClaimStatus.MEASURED
        assert result.measurement.cases == 50

    def test_no_adversarial_statement_reaches_the_driver(self) -> None:
        result = SqlGuardGrader().grade()
        assert result.measurement.value == 0.0
        assert result.status is ClaimStatus.MEASURED
        assert result.measurement.detail["admitted"] == []

    def test_the_adversarial_set_covers_distinct_mechanisms(self) -> None:
        """Padding the set with variations of one idea would inflate the count.

        The claim's strength comes from covering distinct routes to a write,
        not from a large number, so the names must be unique and the set must
        meet the claim's stated size.
        """
        names = [name for name, _ in PROHIBITED_SQL]
        assert len(names) == len(set(names)), "duplicate attack names inflate the case count"
        assert len(PROHIBITED_SQL) >= CLAIMS["C8"].required_cases


# ---------------------------------------------------------------------------
# The status rule
# ---------------------------------------------------------------------------


class TestTheStatusRule:
    def test_too_few_cases_is_insufficient_data_not_measured(self) -> None:
        """The whole reason the fourth status exists.

        Marking this `MEASURED` is precisely the failure I7 exists to prevent,
        and it is the easy mistake when the number is sitting in front of you.
        """
        result = judge("C8", Measurement(value=0.0, cases=3))
        assert result.status is ClaimStatus.INSUFFICIENT_DATA
        assert "requires 55" in result.reason

    def test_a_blocked_claim_reports_its_blocker_even_with_a_number(self) -> None:
        """The blocker is the more useful fact than the number.

        C1 can compute a lead time from one scenario. Reporting that as a
        measurement of "median lead time and IQR over >=40 scenarios" would be
        a different claim than the one registered.
        """
        result = judge("C1", Measurement(value=35.0, cases=1))
        assert result.status is ClaimStatus.INSUFFICIENT_DATA
        assert "requires 40" in result.reason

    def test_enough_cases_and_a_zero_is_measured(self) -> None:
        result = judge("C8", Measurement(value=0.0, cases=60))
        assert result.status is ClaimStatus.MEASURED

    def test_enough_cases_and_a_violation_is_refuted_not_insufficient(self) -> None:
        """The data was sufficient and the answer was the wrong one.

        A refutation is published, not softened into "we need more data" -
        which is how an inconvenient result gets quietly deferred for ever.
        """
        result = judge("C8", Measurement(value=2.0, cases=60))
        assert result.status is ClaimStatus.REFUTED
        assert result.passed_gate is False

    def test_a_quality_metric_never_fails_the_gate(self) -> None:
        """Safety and quality are not blended.

        A disappointing lead time must not be fixable by relaxing the same
        threshold that guards approval bypass.
        """
        result = judge("C1", Measurement(value=999.0, cases=1))
        assert result.passed_gate is True

    def test_every_registered_claim_declares_its_dataset_requirement(self) -> None:
        for claim_id, spec in CLAIMS.items():
            assert spec.required_cases > 0, claim_id
            assert spec.case_unit, claim_id
            assert spec.kind in set(MetricKind), claim_id


# ---------------------------------------------------------------------------
# The run and its provenance
# ---------------------------------------------------------------------------


class TestTheRun:
    def test_the_rules_only_arm_grades_the_safety_claims_and_records_the_rest(
        self,
    ) -> None:
        run = run_arm("rules_only")
        graded = {result.claim_id for result in run.results}
        assert {"C7", "C8"} <= graded
        # Nothing this harness knows about is silently omitted.
        assert graded == set(CLAIMS)
        assert not run.gate_failures

    def test_an_unknown_arm_is_refused(self) -> None:
        """A result file naming an arm nobody defined would not participate
        in the ablation it was run for."""
        with pytest.raises(ValueError, match="unknown arm"):
            run_arm("rules_plus_vibes")

    def test_the_llm_arm_refuses_rather_than_measuring_nothing(self) -> None:
        """Running it without cassettes would call the API from CI.

        The alternative failure is worse: an arm that silently measured
        nothing and reported a zero.
        """
        assert "rules_llm" in ARMS
        with pytest.raises(NotImplementedError, match="cassettes"):
            run_arm("rules_llm")

    def test_the_rules_only_arm_records_no_model(self) -> None:
        """The C5 ablation rests entirely on the two arms being distinguishable.

        Recording a model id the arm never called would make them identical in
        stored results.
        """
        run = run_arm("rules_only")
        assert run.provenance.model_id == "none"
        assert run.provenance.prompt_version == "none"

    def test_a_run_is_stored_and_reads_back(self, tmp_path: Path) -> None:
        run = run_arm("rules_only")
        path = run.store(tmp_path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["arm"] == "rules_only"
        assert payload["provenance"]["run_id"] == run.provenance.run_id
        assert payload["summary"]["measured"] >= 2

    def test_the_run_id_is_derived_so_identical_runs_share_one(self) -> None:
        """Makes "has this already been measured?" answerable.

        A random id per run would leave the question needing a full diff of
        two result files.
        """
        first = current_provenance(config={"a": 1})
        second = current_provenance(config={"a": 1})
        assert first.run_id == second.run_id
        assert current_provenance(config={"a": 2}).run_id != first.run_id

    def test_the_config_hash_does_not_depend_on_dict_order(self) -> None:
        assert config_hash({"a": 1, "b": 2}) == config_hash({"b": 2, "a": 1})

    def test_a_dirty_tree_is_recorded_as_not_reproducible(self) -> None:
        """A number measured against uncommitted code is not reproducible.

        Asserted on the property rather than on the current tree state, which
        the test cannot control.
        """
        provenance = current_provenance(config={})
        assert provenance.reproducible == (
            provenance.git_sha != "unknown" and not provenance.git_sha.endswith("-dirty")
        )


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


class TestTheReport:
    def test_it_reads_stored_runs_rather_than_recomputing(self, tmp_path: Path) -> None:
        """A report that recomputed could disagree with the file it summarises.

        The number in the README would then depend on which of the two ran
        last.
        """
        run_arm("rules_only").store(tmp_path)
        markdown = render_markdown(latest_runs(tmp_path))
        assert "## Arm: `rules_only`" in markdown
        assert "C7" in markdown and "C8" in markdown

    def test_it_names_what_is_not_measured_and_why(self, tmp_path: Path) -> None:
        """A table of two green claims reads as a system with two claims.

        The honest summary today is "two measured, five blocked, and here is
        what each one needs".
        """
        run_arm("rules_only").store(tmp_path)
        markdown = render_markdown(latest_runs(tmp_path))
        assert "What is not measured yet, and why" in markdown
        assert "requires 40" in markdown or "40" in markdown
        assert "insufficient data" in markdown

    def test_companion_metrics_are_printed_together(self, tmp_path: Path) -> None:
        """C1's lead time without its false-alarm rate is a misuse of the claim.

        So companions are rendered as a named group rather than squeezed into
        a table cell that invites dropping one.
        """
        run_arm("rules_only").store(tmp_path)
        markdown = render_markdown(latest_runs(tmp_path))
        assert "must be quoted together" in markdown
        assert "approval_bypass_rate" in markdown

    def test_an_empty_results_directory_says_so(self, tmp_path: Path) -> None:
        assert "No stored runs" in render_markdown(latest_runs(tmp_path))

    def test_a_malformed_result_file_is_skipped_not_fatal(self, tmp_path: Path) -> None:
        run_arm("rules_only").store(tmp_path)
        (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
        runs = latest_runs(tmp_path)
        assert "rules_only" in runs

    def test_an_unreproducible_run_is_flagged_in_the_report(self, tmp_path: Path) -> None:
        """Numbers from a dirty tree must not be quoted, and the report says so."""
        run = run_arm("rules_only")
        path = run.store(tmp_path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["provenance"]["reproducible"] = False
        payload["provenance"]["git_sha"] = "abc1234-dirty"
        path.write_text(json.dumps(payload), encoding="utf-8")

        markdown = render_markdown(latest_runs(tmp_path))
        assert "Not reproducible" in markdown
        assert "must not be quoted" in markdown
