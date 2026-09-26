"""A trained risk estimator: logistic regression over the features and the baselines' scores.

It implements the same `RiskEstimator` protocol as the two baselines, so it is
a drop-in `primary` for `SlopeRiskService` - and when it is, slope extrapolation
becomes the stored baseline.

**Why a logistic regression and not the LightGBM the plan named.** LightGBM was
trained and evaluated first, out of regime, and failed the plan's pre-registered
stop condition after both allowed feature iterations (57 minutes *worse* than
slope extrapolation on median lead time at the same false-alarm rate, 95%
interval entirely below zero). Twelve regimes are effectively sixty independent
scenarios however many rows there are, and the trees fit regime quirks. A linear
model on the same inputs generalised. See `claims.md` C2 for the numbers,
including what was *not* established. No scikit-learn or LightGBM is needed at
serving time: the artifact is plain weights and the maths is a dot product and
a sigmoid.

**One feature builder, shared with training.** The model sees only what
`model_row` extracts from a `RiskFeatures`, and `RiskFeatures` comes from
`build_features`, the same function the detector and the trainer call. A model
trained on rows built one way and served rows built another drifts silently.

**Refuses to load a mismatched artifact.** The manifest records the feature
list it was trained on. If the code's list has since changed, loading raises
instead of feeding the model columns in a different order - which would still
return probabilities, all of them wrong.

**What it is not.** Trained and evaluated on synthetic data from a documented
lumped-capacitance thermal model. It says nothing about real trucks.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from backend.app.risk.baselines import RuleBaseline, SlopeExtrapolation
from backend.app.risk.features import RiskFeatures

__all__ = [
    "ARTIFACT_FORMAT",
    "FEATURE_NAMES",
    "FEATURE_SETS",
    "MODEL_HORIZON_MINUTES",
    "ArtifactMismatchError",
    "LinearRiskEstimator",
    "LinearWeights",
    "model_row",
]

ARTIFACT_FORMAT = 1

#: The one horizon this model answers. Trained on labels "leaves the envelope
#: within the next 60 minutes"; asking it about 30 would return a number that
#: means something else, so `estimate` refuses.
MODEL_HORIZON_MINUTES = 60

#: Iteration 1: what `RiskFeatures` carries, and nothing derived. **Deliberately
#: without the raw envelope or raw cargo temperature.** Those identify the cargo
#: class (frozen vs pharma), which correlates with which faults the pack
#: injects; the permitted maximum enters only through `headroom_c`, as the
#: plan's leakage controls require.
FEATURES_V1: tuple[str, ...] = (
    "headroom_c",
    "temp_slope_c_per_min",
    "minutes_to_breach",
    "ambient_temp_c",
    "compressor_rpm",
    "compressor_rpm_slope",
    "fault_code_count",
    "has_prior_maintenance_warning",
    "minutes_to_destination",
    "door_open",
)

#: Iteration 2 adds three derived inputs. Recorded with the reason, because
#: iteration 1 lost to slope extrapolation on lead time and this was chosen
#: *after* seeing that, on the per-regime table: the trees were failing to
#: transfer the trajectory physics across regimes that slope extrapolation
#: handles within any of them. So the incumbent's own score is given to the
#: model, which can then learn corrections to it (notably the cooling-response
#: signal that separates a lying sensor from a failing unit) instead of
#: relearning extrapolation from scratch. All three are functions of
#: `RiskFeatures`, so serving computes them exactly as training does.
FEATURES_V2: tuple[str, ...] = (
    *FEATURES_V1,
    "slope_extrapolation_score",
    "rule_margin_score",
    "cooling_response",
)

FEATURE_SETS: dict[str, tuple[str, ...]] = {"v1": FEATURES_V1, "v2": FEATURES_V2}

#: The set the shipped artifact is trained on. Changed only by a deliberate act
#: that also changes what `TrainedRiskEstimator.load` will accept.
FEATURE_NAMES: tuple[str, ...] = FEATURES_V2


def _nan(value: float | None) -> float:
    return float("nan") if value is None else float(value)


def model_row(features: RiskFeatures, feature_set: str = "v2") -> list[float]:
    """The feature vector, as numbers, in the named set's order.

    Absence is `nan`, which LightGBM treats as missing rather than as a value.
    That is invariant I6 reaching the model: an unknown compressor slope is not
    a slope of zero.
    """
    row = [
        features.headroom_c,
        features.temp_slope_c_per_min,
        _nan(features.minutes_to_breach),
        _nan(features.ambient_temp_c),
        _nan(features.compressor_rpm),
        _nan(features.compressor_rpm_slope),
        float(len(features.fault_codes)),
        float(features.has_prior_maintenance_warning),
        _nan(features.minutes_to_destination),
        float(features.door_open),
    ]
    if feature_set == "v1":
        return row
    return [
        *row,
        SlopeExtrapolation().estimate(features, horizon_minutes=MODEL_HORIZON_MINUTES),
        RuleBaseline().estimate(features, horizon_minutes=MODEL_HORIZON_MINUTES),
        _nan(features.cooling_response),
    ]


class ArtifactMismatchError(RuntimeError):
    """The stored model was trained on different inputs than this code builds."""


def _sigmoid(z: float) -> float:
    return 1.0 / (1.0 + float(np.exp(-z)))


@dataclass(frozen=True, slots=True)
class LinearWeights:
    """Everything the model is: imputation, scaling, coefficients, intercept."""

    #: Median used to fill a missing input. Fitted on training data only.
    medians: tuple[float, ...]
    means: tuple[float, ...]
    scales: tuple[float, ...]
    coefficients: tuple[float, ...]
    intercept: float

    def probability(self, row: list[float]) -> float:
        x = np.array(row, dtype=float)
        x = np.where(np.isnan(x), np.array(self.medians), x)
        z = (x - np.array(self.means)) / np.array(self.scales)
        return _sigmoid(float(np.dot(z, np.array(self.coefficients)) + self.intercept))


class LinearRiskEstimator:
    """A logistic model, calibrated by construction, loaded from a versioned artifact."""

    name = "logistic_stack"

    def __init__(self, weights: LinearWeights, *, version: str, feature_set: str = "v2") -> None:
        self._weights = weights
        self.version = version
        self._feature_set = feature_set

    @classmethod
    def load(cls, directory: Path) -> LinearRiskEstimator:
        """Load an artifact directory.

        Raises:
            ArtifactMismatchError: The format or feature list does not match
                what this code builds.
        """
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        if manifest["format"] != ARTIFACT_FORMAT:
            raise ArtifactMismatchError(
                f"artifact format {manifest['format']} != supported {ARTIFACT_FORMAT}"
            )
        feature_set = str(manifest["feature_set"])
        expected = FEATURE_SETS.get(feature_set)
        if expected is None or tuple(manifest["features"]) != expected:
            raise ArtifactMismatchError(
                "the model was trained on features "
                f"{manifest['features']} but this code builds {list(expected or ())}; "
                "loading it would feed the model columns in a different order "
                "and still return a probability. Retrain."
            )
        w = json.loads((directory / "weights.json").read_text(encoding="utf-8"))
        return cls(
            LinearWeights(
                medians=tuple(w["medians"]),
                means=tuple(w["means"]),
                scales=tuple(w["scales"]),
                coefficients=tuple(w["coefficients"]),
                intercept=float(w["intercept"]),
            ),
            version=str(manifest["version"]),
            feature_set=feature_set,
        )

    def estimate(self, features: RiskFeatures, *, horizon_minutes: int) -> float:
        if horizon_minutes != MODEL_HORIZON_MINUTES:
            raise ValueError(
                f"this model answers a {MODEL_HORIZON_MINUTES}-minute horizon, not "
                f"{horizon_minutes}; its probabilities would mean something else"
            )
        return self._weights.probability(model_row(features, self._feature_set))
