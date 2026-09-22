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
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pytest

from backend.app.domain.enums import DetectedBy, IncidentSeverity
from backend.app.domain.envelope import TemperatureEnvelope
from backend.app.domain.evidence import Evidence
from backend.app.incidents.detection import Detection
from benchmarks.axonbench.claims import CLAIMS, ClaimStatus, MetricKind
from benchmarks.axonbench.graders.base import Measurement, judge
from benchmarks.axonbench.graders.detection import ConflictGrader, LeadTimeGrader
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


class _AlwaysFires:
    """A detector that raises an incident on the very first reading.

    The degenerate maximiser of lead time: it is never late, because it is
    always alarming. Stands in for a detector whose threshold has been tuned
    until C1's headline looks good.
    """

    name = "always_fires"

    def evaluate(
        self,
        evidence: list[Evidence],
        *,
        envelope: TemperatureEnvelope,
        now: datetime,
    ) -> Detection | None:
        readings = [e for e in evidence if e.observation_type == "cargo_temp_c"]
        if not readings:
            return None
        latest = max(readings, key=lambda item: item.observed_at)
        return Detection(
            incident_type="thermal_excursion",
            entity_kind=latest.entity_ref.kind,
            entity_id=latest.entity_ref.id,
            detected_by=DetectedBy.AXON,
            detected_at=latest.observed_at,
            severity=IncidentSeverity.SEV3,
            evidence_ids=(str(latest.id),),
            detail="always",
        )


class _NeverFires:
    """A detector with nothing to say, ever."""

    name = "never_fires"

    def evaluate(
        self,
        evidence: list[Evidence],
        *,
        envelope: TemperatureEnvelope,
        now: datetime,
    ) -> Detection | None:
        return None


@dataclass(frozen=True, slots=True)
class _Reconciled:
    conflicts: tuple[object, ...] = ()


@dataclass(frozen=True, slots=True)
class _Conflict:
    observation_type: str


def _finds_no_conflicts(evidence: list[Evidence], **_kwargs: object) -> _Reconciled:
    return _Reconciled()


def _finds_every_conflict(evidence: list[Evidence], **_kwargs: object) -> _Reconciled:
    return _Reconciled(conflicts=tuple(_Conflict(e.observation_type) for e in evidence))


@pytest.fixture(scope="module")
def rules_only_run():
    """One `rules_only` run, shared by every test that reads one.

    `run_arm` is deterministic and writes nothing until `.store()` is called,
    so a shared run is the same object each test would have built privately.
    It is a fixture because C1 now sweeps two detectors over sixty scenarios -
    about sixteen seconds - and eight private copies of that is two minutes of
    CI spent recomputing an identical answer.
    """
    return run_arm("rules_only")


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

    def test_the_lead_time_grader_notices_a_detector_that_alerts_constantly(self) -> None:
        """The failure C1's pairing rule exists to catch, made to happen.

        A detector that fires on the first reading has enormous lead time and
        no value whatever. Reporting only the median would score it *better*
        than the real detector, which is precisely why `claims.md` calls
        quoting lead time without the false-alarm rate a misuse. The rate must
        go to 1.0 and be visible.
        """
        result = LeadTimeGrader(axon_factory=_AlwaysFires).grade()

        assert result.measurement.companions["false_alarm_rate"] == 1.0
        assert result.measurement.companions["axon_detection_rate"] == 1.0
        # And the tell that the lead time is worthless: every alert landed
        # before the fault that would justify it had started.
        assert result.measurement.companions["alerts_preceding_fault_onset_rate"] > 0.9
        assert len(result.measurement.detail["controls_with_a_false_alarm"]) == 20

    def test_the_lead_time_grader_notices_a_detector_that_never_fires(self) -> None:
        """Silence is not a perfect score, and must not be reported as one.

        With no alerts there is no lead time to take a median of. A grader that
        judged by case count alone would see forty breach scenarios, find the
        dataset requirement met, and publish `MEASURED 0 minutes` - which reads
        as "no advantage over the baseline" rather than "nothing was measured".
        """
        result = LeadTimeGrader(axon_factory=_NeverFires).grade()

        assert result.status is ClaimStatus.INSUFFICIENT_DATA
        assert "no true-breach scenario" in result.reason
        assert result.measurement.companions["lead_time_sample_size"] == 0.0

    def test_the_conflict_grader_catches_a_reconciler_that_finds_nothing(self) -> None:
        """Fed a reconciler that never raises a conflict, C6's recall goes to 0."""
        result = ConflictGrader(reconcile_fn=_finds_no_conflicts).grade()

        assert result.measurement.companions["recall"] == 0.0
        assert result.measurement.detail["true_positives"] == 0
        assert len(result.measurement.detail["missed_conflicts"]) == result.measurement.cases

    def test_the_conflict_grader_catches_a_reconciler_that_flags_everything(self) -> None:
        """Recall alone cannot tell a detector from a stuck alarm.

        A reconciler that calls every pair a conflict scores perfect recall. It
        is precision that refuses it, which is why C6 is never quoted without
        both - the same shape of mistake as C1's lead time without its
        false-alarm rate.
        """
        result = ConflictGrader(reconcile_fn=_finds_every_conflict).grade()

        assert result.measurement.companions["recall"] == 1.0
        assert result.measurement.companions["precision"] < 1.0
        assert result.measurement.detail["false_positives"] > 0

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


class TestTheDetectionGradersMeasureTheShippedSystem:
    """A grader is only worth its numbers if it measures what actually runs."""

    def test_the_flagship_reproduces_the_minutes_quoted_everywhere_else(self) -> None:
        """102 and 137, the two numbers the rest of the project is built on.

        `tests/integration/test_scenario_replay.py` asserts the same 137
        against a real database, through `ScenarioReplay`. This asserts it
        in-process through the grader. They share `incidents.sweep`, and this
        is what would catch the day they stop agreeing - a benchmark quietly
        measuring a detector nobody ships is not a failure that announces
        itself.
        """
        measurement = LeadTimeGrader().measure()
        flagship = next(
            row
            for row in measurement.detail["per_scenario"]
            if row["scenario_id"] == "compressor_degradation_pharma_01"
        )

        assert flagship["baseline_min"] == 137
        assert flagship["axon_min"] == 102
        assert flagship["lead_time_min"] == 35

    def test_the_negative_set_is_the_same_in_a_fresh_interpreter(self) -> None:
        """C6's negatives must not depend on PYTHONHASHSEED.

        The first version of the channel rotation used `hash(scenario_id)`.
        Python randomises string hashing per process, so every run built a
        different negative set and the stored precision could not be
        reproduced from its own provenance - the single property the whole
        harness exists to provide. A subprocess is the only honest test of
        this: within one interpreter the broken version looks perfectly
        stable.
        """
        script = (
            "import json;"
            "from benchmarks.axonbench.graders.detection import ConflictGrader;"
            "m = ConflictGrader().measure();"
            # The channel assignment, not the counts. The first version of this
            # test compared the missed and spurious conflict lists and the
            # true-negative count - all three identical whether the rotation is
            # stable or reseeded every run - so it passed against the exact bug
            # it was written to catch. Verified the second time by putting
            # `hash()` back and watching it go red.
            "print(json.dumps(m.detail['negative_channels']))"
        )
        outputs = []
        for seed in ("0", "1", "12345"):
            env = {**os.environ, "PYTHONHASHSEED": seed}
            completed = subprocess.run(
                [sys.executable, "-c", script],
                capture_output=True,
                text=True,
                env=env,
                cwd=Path(__file__).resolve().parents[2],
                check=True,
            )
            outputs.append(completed.stdout.strip())

        assert len(set(outputs)) == 1, f"the negative set moved between runs: {outputs}"


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
        rules_only_run,
    ) -> None:
        run = rules_only_run
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

    def test_the_rules_only_arm_records_no_model(self, rules_only_run) -> None:
        """The C5 ablation rests entirely on the two arms being distinguishable.

        Recording a model id the arm never called would make them identical in
        stored results.
        """
        run = rules_only_run
        assert run.provenance.model_id == "none"
        assert run.provenance.prompt_version == "none"

    def test_a_run_is_stored_and_reads_back(self, rules_only_run, tmp_path: Path) -> None:
        run = rules_only_run
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
    def test_it_reads_stored_runs_rather_than_recomputing(
        self, rules_only_run, tmp_path: Path
    ) -> None:
        """A report that recomputed could disagree with the file it summarises.

        The number in the README would then depend on which of the two ran
        last.
        """
        rules_only_run.store(tmp_path)
        markdown = render_markdown(latest_runs(tmp_path))
        assert "## Arm: `rules_only`" in markdown
        assert "C7" in markdown and "C8" in markdown

    def test_it_names_what_is_not_measured_and_why(self, rules_only_run, tmp_path: Path) -> None:
        """A table of two green claims reads as a system with two claims.

        The honest summary today is "two measured, five blocked, and here is
        what each one needs".
        """
        rules_only_run.store(tmp_path)
        markdown = render_markdown(latest_runs(tmp_path))
        assert "What is not measured yet, and why" in markdown
        assert "requires 40" in markdown or "40" in markdown
        assert "insufficient data" in markdown

    def test_companion_metrics_are_printed_together(self, rules_only_run, tmp_path: Path) -> None:
        """C1's lead time without its false-alarm rate is a misuse of the claim.

        So companions are rendered as a named group rather than squeezed into
        a table cell that invites dropping one.
        """
        rules_only_run.store(tmp_path)
        markdown = render_markdown(latest_runs(tmp_path))
        assert "must be quoted together" in markdown
        assert "approval_bypass_rate" in markdown

    def test_an_empty_results_directory_says_so(self, tmp_path: Path) -> None:
        assert "No stored runs" in render_markdown(latest_runs(tmp_path))

    def test_a_malformed_result_file_is_skipped_not_fatal(
        self, rules_only_run, tmp_path: Path
    ) -> None:
        rules_only_run.store(tmp_path)
        (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
        runs = latest_runs(tmp_path)
        assert "rules_only" in runs

    def test_an_unreproducible_run_is_flagged_in_the_report(
        self, rules_only_run, tmp_path: Path
    ) -> None:
        """Numbers from a dirty tree must not be quoted, and the report says so."""
        run = rules_only_run
        path = run.store(tmp_path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["provenance"]["reproducible"] = False
        payload["provenance"]["git_sha"] = "abc1234-dirty"
        path.write_text(json.dumps(payload), encoding="utf-8")

        markdown = render_markdown(latest_runs(tmp_path))
        assert "Not reproducible" in markdown
        assert "must not be quoted" in markdown
