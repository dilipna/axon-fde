"""C11: post-action trajectories, and the grader that scores the verifier on them.

Method pre-registered in claims.md before any trajectory was generated.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.app.verification.outcome import Verdict
from benchmarks.axonbench.graders.verification import (
    Trajectory,
    VerificationGrader,
    measure_trajectories,
)
from simulator.incidentforge.generator import Intervention, run_scenario
from simulator.incidentforge.scenarios import load_scenario

PACK = Path(__file__).resolve().parents[2] / "data" / "scenarios" / "pack_v1"
FLAGSHIP = load_scenario(PACK / "compressor_degradation_pharma_01.yaml")


class TestTheInterventionHook:
    def test_no_intervention_is_the_same_run(self) -> None:
        """The pack's golden digests enforce this too; asserted here directly."""
        assert run_scenario(FLAGSHIP).events == run_scenario(FLAGSHIP, intervention=None).events

    def test_cold_storage_really_cools_the_cargo(self) -> None:
        """An intervention that did nothing would make every label 'failed'."""
        untreated = run_scenario(FLAGSHIP)
        treated = run_scenario(
            FLAGSHIP, intervention=Intervention(kind="cold_storage", effective_minute=100)
        )
        end = FLAGSHIP.duration_minutes - 1
        assert treated.ground_truth[end].true_cargo_temp_c < 8.0
        assert untreated.ground_truth[end].true_cargo_temp_c > 8.0
        # Nothing before the intervention moved.
        assert treated.events[:100] == untreated.events[:100]

    def test_the_sensor_is_not_repaired_by_the_action(self) -> None:
        stuck = load_scenario(PACK / "stuck_sensor_real_breach_01.yaml")
        treated = run_scenario(
            stuck, intervention=Intervention(kind="unit_swap", effective_minute=60)
        )
        tail = [event.cargo_temp_c for event in treated.events[-10:]]
        assert len(set(tail)) == 1, "a stuck sensor must stay stuck after the action"


def test_the_measured_result_matches_the_preregistered_prediction() -> None:
    """High agreement on honest sensors; false confirmations only from lying ones."""
    grader = VerificationGrader()
    rows, _ = grader.trajectories()
    measurement = measure_trajectories(rows)
    assert measurement.cases >= 20
    assert measurement.companions["honest_sensor_agreement"] == 1.0
    lying = {r.scenario_id for r in rows if r.sensor_lied}
    for label in measurement.detail["false_confirmations"]:
        assert label.split("/")[0] in lying


class TestTheArithmeticCanFail:
    @staticmethod
    def _row(reported: Verdict, truth: Verdict, *, lied: bool = False) -> Trajectory:
        return Trajectory("s", "reroute_to_cold_storage", lied, reported, truth)

    def test_a_false_confirmation_is_counted_as_one(self) -> None:
        rows = [self._row(Verdict.CONFIRMED, Verdict.FAILED)] + [
            self._row(Verdict.FAILED, Verdict.FAILED) for _ in range(3)
        ]
        m = measure_trajectories(rows)
        assert m.value == pytest.approx(0.75)
        assert m.companions["false_confirmations"] == 1.0
        assert m.companions["false_confirmation_rate"] == pytest.approx(0.25)

    def test_inconclusive_is_not_agreement(self) -> None:
        m = measure_trajectories([self._row(Verdict.INCONCLUSIVE, Verdict.CONFIRMED)])
        assert m.value == 0.0
        assert m.companions["missed_recoveries"] == 1.0
