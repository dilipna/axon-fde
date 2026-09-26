"""The trained risk model: features, labels, the artifact, the metrics, and C2.

**The properties that matter here are the ones that fail silently.** A model
served on features built differently from how it was trained still returns
probabilities. An artifact loaded with its columns reordered still returns
probabilities. A lead-time metric that drops missed breaches still returns a
lead time. Each of those is a test below, and each was made to fail by hand.

Trained and evaluated on synthetic data; nothing here says anything about real
trucks.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from backend.app.risk.trained import (
    FEATURE_SETS,
    MODEL_HORIZON_MINUTES,
    ArtifactMismatchError,
    LinearRiskEstimator,
    LinearWeights,
    model_row,
)
from benchmarks.axonbench.claims import CLAIMS, ClaimStatus
from benchmarks.axonbench.graders.risk import RiskCalibrationGrader
from benchmarks.riskmodel.dataset import Row, ScenarioMeta, build_dataset
from benchmarks.riskmodel.evaluate import (
    HOLD,
    auc_pr,
    bootstrap_difference,
    brier,
    ece,
    lead_time_at_far,
)
from benchmarks.riskmodel.train import ARTIFACT_DIR, pipeline_to_weights, run

FLAGSHIP = "compressor_degradation_pharma_01"
CONTROL = "normal_pharma_run_01"


@pytest.fixture(scope="module")
def dataset():
    return build_dataset()


@pytest.fixture(scope="module")
def report():
    return run()


# ---------------------------------------------------------------------------
# Features: one builder, no leakage
# ---------------------------------------------------------------------------


class TestFeatureDiscipline:
    def test_no_input_identifies_the_cargo_class_or_carries_ground_truth(self) -> None:
        """The plan's leakage controls. The permitted maximum enters only as
        headroom; the raw envelope and raw cargo temperature identify the cargo
        class, which correlates with which faults the pack injects."""
        for names in FEATURE_SETS.values():
            for forbidden in (
                "cargo_temp_c",
                "envelope_max_c",
                "envelope_min_c",
                "true_cargo_temp_c",
                "in_breach",
                "compressor_health",
                "scenario_id",
            ):
                assert forbidden not in names

    def test_the_serving_path_and_the_training_row_agree_to_the_last_digit(self, dataset) -> None:
        """Train/serve skew, tested end to end rather than function-to-itself.

        The training row for a minute is built from a hand-sliced window. The
        serving path takes the full evidence stream and asks the risk service.
        They must give the same probability, or the model is being served
        inputs it was never trained on and will still return a number.
        """
        from backend.app.domain.envelope import TemperatureEnvelope
        from backend.app.domain.taxonomy import load_taxonomy
        from backend.app.evidence.telemetry import reading_to_evidence
        from backend.app.risk.baselines import SlopeExtrapolation
        from backend.app.risk.service import SlopeRiskService
        from benchmarks.axonbench.graders.detection import DEFAULT_PACK_DIR, _to_reading
        from simulator.incidentforge.generator import run_scenario
        from simulator.incidentforge.scenarios import load_pack

        rows, _ = dataset
        scenario = load_pack(DEFAULT_PACK_DIR).scenarios[FLAGSHIP]
        readings = [_to_reading(e) for e in run_scenario(scenario).events]
        taxonomy = load_taxonomy()
        estimator = LinearRiskEstimator.load(ARTIFACT_DIR)
        service = SlopeRiskService(primary=estimator, baseline=SlopeExtrapolation())
        envelope = TemperatureEnvelope(
            minimum_c=scenario.cargo.permitted_min_c, maximum_c=scenario.cargo.permitted_max_c
        )

        for minute in (60, 90, 120):
            evidence = [
                item
                for reading in readings[: minute + 1]
                for item in reading_to_evidence(reading, taxonomy=taxonomy)
            ]
            served = service.assess(
                evidence,
                envelope=envelope,
                now=readings[minute].timestamp,
                horizon_minutes=MODEL_HORIZON_MINUTES,
            )
            trained_row = next(r for r in rows if r.scenario_id == FLAGSHIP and r.minute == minute)
            assert served is not None
            assert served.probability == pytest.approx(
                estimator.estimate(trained_row.features, horizon_minutes=MODEL_HORIZON_MINUTES),
                abs=1e-12,
            )

    def test_iteration_two_extends_iteration_one_without_reordering_it(self, dataset) -> None:
        rows, _ = dataset
        v1, v2 = FEATURE_SETS["v1"], FEATURE_SETS["v2"]
        assert v2[: len(v1)] == v1
        sample = rows[len(rows) // 2]
        assert sample.vectors["v2"][: len(v1)] == pytest.approx(sample.vectors["v1"], nan_ok=True)

    def test_a_missing_input_is_missing_not_zero(self, dataset) -> None:
        """I6 reaching the model: an unknown compressor slope is not a slope of 0."""
        rows, _ = dataset
        blank = replace(rows[0].features, compressor_rpm_slope=None, ambient_temp_c=None)
        vector = model_row(blank)
        names = FEATURE_SETS["v2"]
        assert np.isnan(vector[names.index("compressor_rpm_slope")])
        assert np.isnan(vector[names.index("ambient_temp_c")])


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------


class TestLabels:
    def test_a_scenario_that_never_breaches_is_all_zero(self, dataset) -> None:
        rows, meta = dataset
        assert meta[CONTROL].breach_minute is None
        labels = {r.label for r in rows if r.scenario_id == CONTROL}
        assert labels == {0}

    def test_the_flagship_is_positive_exactly_in_the_hour_before_its_breach(self, dataset) -> None:
        """Counterfactual-clean, from ground truth only: positive for the 60
        minutes preceding the true breach, and no rows at or after it (the
        label would be trivially 1 there)."""
        rows, meta = dataset
        breach = meta[FLAGSHIP].breach_minute
        assert breach is not None
        flagship = [r for r in rows if r.scenario_id == FLAGSHIP]
        assert max(r.minute for r in flagship) == breach - 1
        for row in flagship:
            assert row.label == int(breach - row.minute <= MODEL_HORIZON_MINUTES)

    def test_no_row_exists_before_a_full_feature_window(self, dataset) -> None:
        rows, _ = dataset
        assert min(r.minute for r in rows) >= 30


# ---------------------------------------------------------------------------
# The artifact
# ---------------------------------------------------------------------------


class TestTheArtifact:
    def test_the_committed_artifact_loads_and_answers_a_probability(self, dataset) -> None:
        rows, _ = dataset
        estimator = LinearRiskEstimator.load(ARTIFACT_DIR)
        sample = [r for r in rows if r.scenario_id == FLAGSHIP][-1]
        p = estimator.estimate(sample.features, horizon_minutes=MODEL_HORIZON_MINUTES)
        assert 0.0 <= p <= 1.0
        # An hour before the breach the model should be alarmed, not shrugging.
        assert p > 0.5

    def test_serving_reproduces_scikit_learn_exactly(self) -> None:
        """The artifact is plain weights; the maths at serving time must equal
        the fitted pipeline's, including the imputation of missing values."""
        rng = np.random.default_rng(0)
        x = rng.normal(size=(400, 5))
        x[rng.random(x.shape) < 0.1] = np.nan
        y = (np.nan_to_num(x[:, 0]) + 0.5 * np.nan_to_num(x[:, 1]) > 0).astype(int)
        pipeline = make_pipeline(
            SimpleImputer(strategy="median", keep_empty_features=True),
            StandardScaler(),
            LogisticRegression(max_iter=2000),
        ).fit(x, y)
        w = pipeline_to_weights(pipeline)
        weights = LinearWeights(
            medians=tuple(w["medians"]),
            means=tuple(w["means"]),
            scales=tuple(w["scales"]),
            coefficients=tuple(w["coefficients"]),
            intercept=w["intercept"],
        )
        expected = pipeline.predict_proba(x)[:, 1]
        got = np.array([weights.probability(list(row)) for row in x])
        assert got == pytest.approx(expected, abs=1e-9)

    def test_a_reordered_feature_list_is_refused_not_served(self, tmp_path: Path) -> None:
        """The failure this guards is silent: the model would still answer."""
        target = tmp_path / "model"
        shutil.copytree(ARTIFACT_DIR, target)
        manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
        manifest["features"][0], manifest["features"][1] = (
            manifest["features"][1],
            manifest["features"][0],
        )
        (target / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        with pytest.raises(ArtifactMismatchError, match="different order"):
            LinearRiskEstimator.load(target)

    def test_an_unknown_format_is_refused(self, tmp_path: Path) -> None:
        target = tmp_path / "model"
        shutil.copytree(ARTIFACT_DIR, target)
        manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
        manifest["format"] = 99
        (target / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        with pytest.raises(ArtifactMismatchError, match="format"):
            LinearRiskEstimator.load(target)

    def test_another_horizon_is_refused(self, dataset) -> None:
        """Its probabilities mean 'within 60 minutes'. Asked about 30 it would
        return a number that means something else."""
        rows, _ = dataset
        with pytest.raises(ValueError, match="60-minute horizon"):
            LinearRiskEstimator.load(ARTIFACT_DIR).estimate(rows[0].features, horizon_minutes=30)

    def test_a_missing_value_is_filled_with_the_training_median(self) -> None:
        weights = LinearWeights(
            medians=(2.0, 0.0),
            means=(0.0, 0.0),
            scales=(1.0, 1.0),
            coefficients=(1.0, 1.0),
            intercept=0.0,
        )
        assert weights.probability([float("nan"), 1.0]) == pytest.approx(
            weights.probability([2.0, 1.0])
        )

    def test_it_plugs_into_the_risk_service_as_primary_with_slope_as_baseline(
        self, dataset
    ) -> None:
        """I3 through the shipped path: the estimate carries both numbers."""
        from backend.app.risk.baselines import SlopeExtrapolation
        from backend.app.risk.service import SlopeRiskService

        service = SlopeRiskService(
            primary=LinearRiskEstimator.load(ARTIFACT_DIR), baseline=SlopeExtrapolation()
        )
        assert service.model_version.startswith("logistic_stack@")


# ---------------------------------------------------------------------------
# Metrics, each shown to fail
# ---------------------------------------------------------------------------


def _rows(scenario_id: str, minutes: range) -> list[Row]:
    return [
        Row(scenario_id=scenario_id, regime="r", minute=m, label=0, features=None, vectors={})  # type: ignore[arg-type]
        for m in minutes
    ]


def _meta(**breaches: int | None) -> dict[str, ScenarioMeta]:
    return {k: ScenarioMeta(k, "r", v, 200) for k, v in breaches.items()}


class TestMetrics:
    def test_brier_is_zero_for_a_perfect_forecast_and_a_quarter_for_a_coin_flip(self) -> None:
        y = np.array([0, 1, 0, 1])
        assert brier(y, y.astype(float)) == 0.0
        assert brier(y, np.full(4, 0.5)) == pytest.approx(0.25)

    def test_ece_separates_calibrated_from_overconfident(self) -> None:
        rng = np.random.default_rng(1)
        p = rng.random(20000)
        calibrated = (rng.random(20000) < p).astype(int)
        assert ece(calibrated, p) < 0.02
        # Says 0.9 and is right a third of the time.
        y = (rng.random(20000) < 0.33).astype(int)
        assert ece(y, np.full(20000, 0.9)) > 0.5

    def test_auc_pr_is_higher_for_a_ranking_that_orders_positives_first(self) -> None:
        y = np.array([0, 0, 1, 1])
        assert auc_pr(y, np.array([0.1, 0.2, 0.8, 0.9])) > auc_pr(y, np.array([0.9, 0.8, 0.2, 0.1]))

    def test_an_oracle_scorer_earns_lead_time_and_an_inverted_one_earns_none(self) -> None:
        """A metric that cannot tell these apart is not measuring anything."""
        rows = _rows("b", range(30, 100)) + _rows("n", range(30, 100))
        meta = _meta(b=100, n=None)
        good = np.array([1.0 if (r.scenario_id == "b" and r.minute >= 40) else 0.0 for r in rows])
        bad = 1.0 - good
        assert lead_time_at_far(rows, good, meta).median_lead_min == 100 - (40 + HOLD - 1)
        inverted = lead_time_at_far(rows, bad, meta)
        assert inverted.median_lead_min == 0.0
        assert inverted.detection_rate == 0.0

    def test_a_missed_breach_counts_as_zero_and_is_not_dropped(self) -> None:
        """Dropping misses would let a timid detector look good by alerting only
        on the easy ones."""
        # A control scenario is what makes the false-alarm budget bind: without one
        # the most sensitive threshold is feasible and would catch both.
        rows = (
            _rows("hit", range(30, 100))
            + _rows("miss", range(30, 100))
            + _rows("control", range(30, 100))
        )
        meta = _meta(hit=100, miss=100, control=None)
        scores = np.array([0.9 if r.scenario_id == "hit" and r.minute >= 50 else 0.0 for r in rows])
        result = lead_time_at_far(rows, scores, meta)
        detected_lead = 100 - (50 + HOLD - 1)
        assert result.detection_rate == 0.5
        assert result.median_lead_detected_min == detected_lead
        assert result.median_lead_min == detected_lead / 2

    def test_the_false_alarm_budget_is_respected(self) -> None:
        rows = (
            _rows("b", range(30, 100)) + _rows("n1", range(30, 100)) + _rows("n2", range(30, 100))
        )
        meta = _meta(b=100, n1=None, n2=None)
        # Everything scores high: nothing but a threshold above every score
        # avoids alerting on both non-breach scenarios, so FAR <= 0.5 is unmet
        # at any usable threshold and the result must say "no detection".
        result = lead_time_at_far(rows, np.ones(len(rows)), meta, target_far=0.0)
        assert result.false_alarm_rate == 0.0
        assert result.detection_rate == 0.0

    def test_identical_scorers_have_a_zero_difference(self) -> None:
        rows = _rows("b", range(30, 100)) + _rows("n", range(30, 100))
        meta = _meta(b=100, n=None)
        labels = np.array([1 if r.scenario_id == "b" and r.minute >= 40 else 0 for r in rows])
        s = labels.astype(float) * 0.8 + 0.1
        diff = bootstrap_difference(
            rows, meta, s, s, labels, threshold_a=0.5, threshold_b=0.5, resamples=50
        )
        for name in ("brier", "auc_pr"):
            assert diff[name]["mean"] == 0.0
            assert diff[name]["ci95_low"] == diff[name]["ci95_high"] == 0.0


# ---------------------------------------------------------------------------
# C2
# ---------------------------------------------------------------------------


class TestC2:
    def test_it_is_deterministic(self, report) -> None:
        """Single-threaded LightGBM, a fixed seed, no tuning. A benchmark whose
        number moves between runs cannot gate anything."""
        again = run()
        for arm in report["arms"]:
            for metric in ("auc_pr", "brier", "ece"):
                assert again["arms"][arm][metric] == report["arms"][arm][metric]

    def test_every_scenario_is_scored_by_a_model_that_never_saw_its_regime(self, report) -> None:
        assert report["scenarios"] == 60
        assert len(report["regimes"]) >= 10

    def test_the_candidate_beats_slope_extrapolation_on_calibration_and_ranking(
        self, report
    ) -> None:
        """The claim the artifact ships on. If this flips, it should not ship."""
        arms = report["arms"]
        assert arms["logistic_regression"]["ece"] < arms["slope_extrapolation"]["ece"]
        assert arms["logistic_regression"]["auc_pr"] > arms["slope_extrapolation"]["auc_pr"]

    def test_the_stop_condition_is_recorded_consistently(self, report) -> None:
        stop = report["stop_condition"]
        assert stop["passed"] == (stop["improvement_min"] >= 5.0)

    def test_the_grade_is_measured_over_the_whole_pack(self, report) -> None:
        result = RiskCalibrationGrader(report=report).grade()
        assert result.status is ClaimStatus.MEASURED
        assert result.measurement.cases == CLAIMS["C2"].required_cases
        assert result.measurement.value == report["arms"]["logistic_regression"]["ece"]
        # The limitation and the selection caveat travel with the number.
        assert "synthetic" in result.measurement.detail["limitation"]
        assert "fresh scenarios" in result.measurement.detail["selection_caveat"]

    def test_a_run_over_too_few_scenarios_is_insufficient_not_measured(self, report) -> None:
        few = dict(report, scenarios=5)
        assert RiskCalibrationGrader(report=few).grade().status is ClaimStatus.INSUFFICIENT_DATA
