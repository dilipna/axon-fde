"""C2: are the risk model's probabilities calibrated, out of regime?

The measurement is `benchmarks.riskmodel.train.run`: leave-one-regime-out over
the scenario pack, every candidate scored by a model that never saw the regime
it is scored on, against the three mandatory baselines. This grader takes that
report and turns it into a claim result. It computes nothing itself, so the
number a reader checks and the number the training script prints cannot
disagree.

**What is measured and what is not, said where the number is.** The headline is
the expected calibration error of the candidate that would ship, out of regime.
Two things the register asked for are *not* in it: the isotonic step (the
LightGBM candidate that used it failed its stop condition, and the logistic
candidate is calibrated by its own objective) and an operating threshold chosen
by expected cost (that belongs to the decision layer; this evaluation uses a
false-alarm budget so that candidates are compared at the same operating point).

**Deterministic.** Single-threaded LightGBM with a fixed seed, fixed
hyperparameters, and no tuning. Running it twice gives the same numbers, and a
test holds that.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from benchmarks.axonbench.graders.base import GraderResult, Measurement, judge

__all__ = ["RiskCalibrationGrader"]

#: The candidate whose calibration is the headline.
SHIPPING_CANDIDATE = "logistic_regression"


@dataclass(frozen=True, slots=True)
class RiskCalibrationGrader:
    claim_id: str = "C2"
    feature_set: str = "v2"
    #: Injectable so a test can hand the grader a broken report and watch it
    #: react, without retraining anything.
    report: dict[str, Any] | None = None

    def measure(self) -> Measurement:
        report = self.report
        if report is None:
            from benchmarks.riskmodel.train import run

            report = run(self.feature_set)

        arms = report["arms"]
        candidate = arms[SHIPPING_CANDIDATE]
        slope = arms["slope_extrapolation"]
        bootstrap = report["bootstrap_vs_slope_extrapolation"][SHIPPING_CANDIDATE]
        stop = report["stop_condition"]

        companions: dict[str, float] = {
            "candidate_auc_pr": candidate["auc_pr"],
            "candidate_brier": candidate["brier"],
            "slope_auc_pr": slope["auc_pr"],
            "slope_brier": slope["brier"],
            "slope_ece": slope["ece"],
            "rule_margin_ece": arms["rule_margin"]["ece"],
            "lightgbm_ece": arms["lightgbm_isotonic"]["ece"],
            "candidate_median_lead_min": candidate["lead_time_at_far"]["median_lead_min"],
            "slope_median_lead_min": slope["lead_time_at_far"]["median_lead_min"],
            "target_false_alarm_rate": candidate["lead_time_at_far"]["target_far"],
            "candidate_false_alarm_rate": candidate["lead_time_at_far"]["achieved_far"],
            # The pre-registered stop condition, on the model the plan named.
            "lightgbm_lead_improvement_min": stop["improvement_min"],
            "lightgbm_passed_stop_condition": float(stop["passed"]),
            "delta_ece_ci95_low": bootstrap["ece"]["ci95_low"],
            "delta_ece_ci95_high": bootstrap["ece"]["ci95_high"],
            "delta_auc_pr_ci95_low": bootstrap["auc_pr"]["ci95_low"],
            "delta_auc_pr_ci95_high": bootstrap["auc_pr"]["ci95_high"],
            "delta_lead_min_ci95_low": bootstrap["median_lead_min"]["ci95_low"],
            "delta_lead_min_ci95_high": bootstrap["median_lead_min"]["ci95_high"],
        }
        return Measurement(
            value=float(candidate["ece"]),
            cases=int(report["scenarios"]),
            companions=companions,
            detail={
                "candidate": SHIPPING_CANDIDATE,
                "feature_set": report["feature_set"],
                "horizon_minutes": report["horizon_minutes"],
                "rows": report["rows"],
                "positive_rate": report["positive_rate"],
                "regimes": report["regimes"],
                "arms": {
                    name: {k: v for k, v in arm.items() if k != "reliability"}
                    for name, arm in arms.items()
                },
                "reliability_candidate": candidate.get("reliability", []),
                "reliability_slope_extrapolation": slope.get("reliability", []),
                "bootstrap_vs_slope_extrapolation": report["bootstrap_vs_slope_extrapolation"],
                "per_regime_auc_pr": report["per_regime_auc_pr"],
                "stop_condition": stop,
                "limitation": report["limitation"],
                "selection_caveat": (
                    "the feature set was chosen after the held-out regimes were consulted "
                    "once (iteration 1 -> 2), and the logistic candidate was identified as "
                    "the winner afterwards; treat its advantage as a finding to confirm on "
                    "fresh scenarios, not a settled result"
                ),
            },
        )

    def grade(self) -> GraderResult:
        return judge(self.claim_id, self.measure())
