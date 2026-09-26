"""Train, calibrate and judge the risk model - out of regime, against baselines.

`poe train-risk`. Everything the plan asks of B12 in one deterministic run.

**The split is by regime, and it is leave-one-regime-out.** For each of the
pack's fault regimes, a model is trained on the others and scored on the one it
never saw. Pooling those out-of-regime predictions gives every scenario a score
from a model that had no example of its regime, which is the honest reading of
"does this generalise to a fault it was not trained on". A row-level split would
leak through the ODE's autocorrelation: adjacent minutes are near-copies.

**Calibration has its own split.** Within the training regimes, every fourth
scenario (sorted by id) is held out from the booster and used only to fit the
isotonic map. It is disjoint by scenario, not by regime; the regime shift is
then paid at test time, where it belongs.

**Nothing is tuned.** The hyperparameters below are fixed, were set before any
result was seen, and no run changes them. The plan's stop condition allows two
feature iterations; a hyperparameter search against the same held-out regimes
would spend the test set.

**Baselines are mandatory and share the exact rows**: the current-margin rule,
slope extrapolation, and logistic regression on the model's own features. The
logistic baseline is what tells you whether the trees found anything a linear
model on the same inputs could not.

**Limitation, in the same breath as the metric:** trained and evaluated on
synthetic data from a documented lumped-capacitance thermal model. Real-world
generalisation is unvalidated.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import lightgbm as lgb
import numpy as np
from numpy.typing import NDArray
from sklearn.impute import SimpleImputer
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from backend.app.risk.baselines import RuleBaseline, SlopeExtrapolation
from backend.app.risk.trained import (
    ARTIFACT_FORMAT,
    FEATURE_SETS,
    MODEL_HORIZON_MINUTES,
)
from benchmarks.axonbench.provenance import git_sha
from benchmarks.riskmodel.dataset import Row, ScenarioMeta, build_dataset
from benchmarks.riskmodel.evaluate import (
    TARGET_FAR,
    auc_pr,
    bootstrap_difference,
    brier,
    ece,
    lead_time_at_far,
    reliability,
)

__all__ = ["LGBM_PARAMS", "STOP_CONDITION_MINUTES", "main", "pipeline_to_weights", "run"]

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ARTIFACT_DIR = PROJECT_ROOT / "data" / "models" / "risk_v1"

#: Fixed before any result was seen. See the module docstring.
LGBM_PARAMS: dict[str, Any] = {
    "objective": "binary",
    "learning_rate": 0.05,
    "num_leaves": 15,
    "min_data_in_leaf": 200,
    "feature_fraction": 0.9,
    "lambda_l2": 1.0,
    "seed": 0,
    "deterministic": True,
    "force_row_wise": True,
    "num_threads": 1,
    "verbosity": -1,
}
N_ROUNDS = 200

#: The plan's stop condition: LightGBM must beat slope extrapolation by this
#: much on lead time at the fixed false-alarm rate, or the baseline ships.
#: The register says "points"; the metric is minutes of median lead time, so
#: 5 points is read as 5 minutes. Stated here so the reading is not chosen
#: after the result.
STOP_CONDITION_MINUTES = 5.0


def _matrix(rows: list[Row], feature_set: str) -> NDArray[np.float64]:
    return np.array([r.vectors[feature_set] for r in rows], dtype=float)


def _labels(rows: list[Row]) -> NDArray[np.int_]:
    return np.array([r.label for r in rows], dtype=int)


def calibration_scenarios(scenario_ids: list[str]) -> set[str]:
    """Every fourth scenario, by sorted id. Deterministic and result-independent."""
    return {s for i, s in enumerate(sorted(scenario_ids)) if i % 4 == 3}


def _fit(rows: list[Row], feature_set: str) -> tuple[lgb.Booster, IsotonicRegression]:
    held = calibration_scenarios(sorted({r.scenario_id for r in rows}))
    fit_rows = [r for r in rows if r.scenario_id not in held]
    cal_rows = [r for r in rows if r.scenario_id in held]

    booster = lgb.train(
        LGBM_PARAMS,
        lgb.Dataset(
            _matrix(fit_rows, feature_set),
            label=_labels(fit_rows),
            feature_name=list(FEATURE_SETS[feature_set]),
        ),
        num_boost_round=N_ROUNDS,
    )
    raw = booster.predict(_matrix(cal_rows, feature_set))
    isotonic = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
    isotonic.fit(raw, _labels(cal_rows))
    return booster, isotonic


def _metrics(labels: NDArray[np.int_], scores: NDArray[np.float64]) -> dict[str, float]:
    return {
        "auc_pr": auc_pr(labels, scores),
        "brier": brier(labels, scores),
        "ece": ece(labels, scores),
    }


def run(feature_set: str = "v2") -> dict[str, Any]:
    """The whole evaluation. Returns a JSON-serialisable report."""
    rows, meta = build_dataset()
    regimes = sorted({r.regime for r in rows})
    labels = _labels(rows)

    scores: dict[str, NDArray[np.float64]] = {
        "rule_margin": np.array(
            [
                RuleBaseline().estimate(r.features, horizon_minutes=MODEL_HORIZON_MINUTES)
                for r in rows
            ]
        ),
        "slope_extrapolation": np.array(
            [
                SlopeExtrapolation().estimate(r.features, horizon_minutes=MODEL_HORIZON_MINUTES)
                for r in rows
            ]
        ),
        "logistic_regression": np.zeros(len(rows)),
        "lightgbm_raw": np.zeros(len(rows)),
        "lightgbm_isotonic": np.zeros(len(rows)),
    }

    for regime in regimes:
        test_idx = [i for i, r in enumerate(rows) if r.regime == regime]
        train_rows = [r for r in rows if r.regime != regime]
        test_rows = [rows[i] for i in test_idx]

        booster, isotonic = _fit(train_rows, feature_set)
        raw = booster.predict(_matrix(test_rows, feature_set))
        scores["lightgbm_raw"][test_idx] = raw
        scores["lightgbm_isotonic"][test_idx] = isotonic.predict(raw)

        logistic = _fit_logistic(train_rows, feature_set)
        scores["logistic_regression"][test_idx] = logistic.predict_proba(
            _matrix(test_rows, feature_set)
        )[:, 1]

    arms: dict[str, Any] = {}
    for name, s in scores.items():
        lead = lead_time_at_far(rows, s, meta)
        arms[name] = {
            **_metrics(labels, s),
            "lead_time_at_far": {
                "target_far": TARGET_FAR,
                "achieved_far": lead.false_alarm_rate,
                "detection_rate": lead.detection_rate,
                "median_lead_min": lead.median_lead_min,
                "median_lead_detected_min": lead.median_lead_detected_min,
                "threshold": lead.threshold,
            },
        }
    arms["logistic_regression"]["reliability"] = reliability(labels, scores["logistic_regression"])
    arms["lightgbm_isotonic"]["reliability"] = reliability(labels, scores["lightgbm_isotonic"])
    arms["lightgbm_raw"]["reliability"] = reliability(labels, scores["lightgbm_raw"])
    arms["slope_extrapolation"]["reliability"] = reliability(labels, scores["slope_extrapolation"])

    per_regime: dict[str, Any] = {}
    for regime in regimes:
        idx = [i for i, r in enumerate(rows) if r.regime == regime]
        y = labels[idx]
        if y.sum() == 0 or y.sum() == len(y):
            per_regime[regime] = {"rows": len(idx), "positives": int(y.sum()), "auc_pr": None}
            continue
        per_regime[regime] = {
            "rows": len(idx),
            "positives": int(y.sum()),
            "auc_pr": {n: auc_pr(y, scores[n][idx]) for n in scores},
        }

    # Is the gap noise? Scenarios are resampled, not rows, because rows within a
    # scenario are near-copies. Each candidate is compared with slope extrapolation.
    slope_threshold = arms["slope_extrapolation"]["lead_time_at_far"]["threshold"]
    bootstrap = {
        name: bootstrap_difference(
            rows,
            meta,
            scores[name],
            scores["slope_extrapolation"],
            labels,
            threshold_a=arms[name]["lead_time_at_far"]["threshold"],
            threshold_b=slope_threshold,
        )
        for name in ("logistic_regression", "lightgbm_isotonic")
    }

    model_lead = arms["lightgbm_isotonic"]["lead_time_at_far"]["median_lead_min"]
    slope_lead = arms["slope_extrapolation"]["lead_time_at_far"]["median_lead_min"]
    improvement = model_lead - slope_lead
    return {
        "horizon_minutes": MODEL_HORIZON_MINUTES,
        "rows": len(rows),
        "positive_rate": float(labels.mean()),
        "scenarios": len(meta),
        "regimes": regimes,
        "feature_set": feature_set,
        "features": list(FEATURE_SETS[feature_set]),
        "lgbm_params": LGBM_PARAMS | {"n_rounds": N_ROUNDS},
        "arms": arms,
        "per_regime_auc_pr": per_regime,
        "bootstrap_vs_slope_extrapolation": bootstrap,
        "stop_condition": {
            "rule": "lightgbm_isotonic must beat slope_extrapolation on median lead time "
            f"at FAR<={TARGET_FAR} by >= {STOP_CONDITION_MINUTES} min",
            "model_lead_min": model_lead,
            "slope_lead_min": slope_lead,
            "improvement_min": improvement,
            "passed": improvement >= STOP_CONDITION_MINUTES,
        },
        "limitation": (
            "Trained and evaluated on synthetic data from a documented lumped-capacitance "
            "thermal model; real-world generalisation is unvalidated."
        ),
        "git_sha": git_sha(),
    }


def _fit_logistic(rows: list[Row], feature_set: str) -> Any:
    pipeline = make_pipeline(
        SimpleImputer(strategy="median", keep_empty_features=True),
        StandardScaler(),
        LogisticRegression(C=1.0, max_iter=2000),
    )
    pipeline.fit(_matrix(rows, feature_set), _labels(rows))
    return pipeline


def pipeline_to_weights(pipeline: Any) -> dict[str, Any]:
    """A fitted imputer-scaler-logistic pipeline, as the plain numbers serving needs."""
    imputer, scaler, model = (step for _, step in pipeline.steps)
    return {
        # An all-missing column is filled with 0 by the imputer.
        "medians": [0.0 if np.isnan(v) else float(v) for v in imputer.statistics_],
        "means": [float(v) for v in scaler.mean_],
        "scales": [float(v) for v in scaler.scale_],
        "coefficients": [float(v) for v in model.coef_[0]],
        "intercept": float(model.intercept_[0]),
    }


def write_artifact(
    directory: Path, meta: dict[str, ScenarioMeta], rows: list[Row], feature_set: str
) -> None:
    """Fit the logistic model on every regime and store it as plain weights.

    The shipped model has seen every regime, so the performance to quote for it
    is the out-of-regime estimate in the report, **not** anything measured on
    its own training data. Serving needs no scikit-learn: the artifact is the
    imputation medians, the scaling, the coefficients and the intercept.
    """
    weights = pipeline_to_weights(_fit_logistic(rows, feature_set))
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "weights.json").write_text(json.dumps(weights, indent=2) + "\n", encoding="utf-8")
    training_hash = hashlib.sha256(
        json.dumps(sorted(meta), separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    (directory / "manifest.json").write_text(
        json.dumps(
            {
                "format": ARTIFACT_FORMAT,
                "version": f"logistic-stack-{feature_set}-{training_hash}",
                "model": "logistic_regression",
                "features": list(FEATURE_SETS[feature_set]),
                "feature_set": feature_set,
                "horizon_minutes": MODEL_HORIZON_MINUTES,
                "training_scenarios_sha": training_hash,
                "rows": len(rows),
                "git_sha": git_sha(),
                "limitation": "synthetic data; real-world generalisation is unvalidated",
                "selection_caveat": (
                    "chosen after the held-out regimes had been consulted twice (two feature "
                    "iterations); LightGBM, the pre-registered model, failed its stop condition"
                ),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Train and judge the risk model.")
    parser.add_argument("--feature-set", choices=sorted(FEATURE_SETS), default="v2")
    parser.add_argument("--write-artifact", action="store_true")
    parser.add_argument("--out", type=Path, default=None, help="Write the report JSON here.")
    args = parser.parse_args(argv)

    report = run(args.feature_set)
    print(f"rows={report['rows']} positives={report['positive_rate']:.3f} horizon=60min")
    print(
        f"{'arm':<22}{'AUC-PR':>8}{'Brier':>8}{'ECE':>8}{'FAR':>7}{'det':>6}{'lead*':>7}{'lead':>6}"
    )
    for name, arm in report["arms"].items():
        lead = arm["lead_time_at_far"]
        print(
            f"{name:<22}{arm['auc_pr']:>8.3f}{arm['brier']:>8.3f}{arm['ece']:>8.3f}"
            f"{lead['achieved_far']:>7.2f}{lead['detection_rate']:>6.2f}"
            f"{lead['median_lead_min']:>7.1f}{lead['median_lead_detected_min']:>6.1f}"
        )
    print("  lead* = median over all breaches, a miss counting 0; lead = detected only")
    stop = report["stop_condition"]
    print(
        f"LightGBM stop condition (+{STOP_CONDITION_MINUTES:g} min over slope): "
        f"{stop['improvement_min']:+.1f} min -> {'PASSED' if stop['passed'] else 'FAILED'}"
    )
    for name, diff in report["bootstrap_vs_slope_extrapolation"].items():
        print(f"  {name} minus slope (95% over scenarios):")
        for metric, ci in diff.items():
            low, high = ci["ci95_low"], ci["ci95_high"]
            print(f"    {metric:<18}{ci['mean']:>+8.3f}  [{low:>+8.3f}, {high:>+8.3f}]")
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"report: {args.out}")
    if args.write_artifact:
        rows, meta = build_dataset()
        write_artifact(ARTIFACT_DIR, meta, rows, args.feature_set)
        print(f"artifact: {ARTIFACT_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
