"""The risk service: one probability, always beside its baseline.

Invariant I3 says a risk probability is computed outside the LLM and always
stored beside its non-ML baseline. That is enforced structurally here: the
only thing this module returns is a ``RiskEstimate``, and a ``RiskEstimate``
cannot be constructed without both numbers. There is no code path that
produces a prediction alone, so there is nothing to remember to do.

**What serves today.** ``SlopeExtrapolation`` is primary and ``RuleBaseline``
is the comparison. When B12 lands a trained model, the model becomes primary
and *slope extrapolation becomes the baseline* - which is the comparison its
stop condition is written against. ``baseline_name`` is stored per assessment
precisely so that shift is visible in the data rather than buried in a
changelog.

**Degradation.** When the features cannot be built - too few readings, no
envelope - the service returns ``None``. It does not return a default, a
prior, or a cheerful low number. An invented risk score is acted upon exactly
like a real one (invariant I6).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from backend.app.domain.envelope import TemperatureEnvelope
from backend.app.domain.evidence import Evidence
from backend.app.risk.baselines import (
    DEFAULT_HORIZON_MINUTES,
    RiskEstimator,
    RuleBaseline,
    SlopeExtrapolation,
)
from backend.app.risk.features import DEFAULT_WINDOW_MINUTES, RiskFeatures, build_features

__all__ = [
    "RiskEstimate",
    "RiskService",
    "SlopeRiskService",
]


@dataclass(frozen=True, slots=True)
class RiskEstimate:
    """A probability, its baseline, and everything needed to reproduce it.

    Both numbers are required fields. That is the enforcement of I3: you
    cannot build one of these with a prediction and no baseline, so no caller
    can persist one either.
    """

    probability: float
    baseline_probability: float
    baseline_name: str
    model_version: str
    horizon_minutes: int
    feature_vector_hash: str
    features: RiskFeatures

    #: True when the primary estimator was unavailable and the baseline was
    #: used in its place. The system says so rather than pretending.
    degraded: bool = False

    def __post_init__(self) -> None:
        for name, value in (
            ("probability", self.probability),
            ("baseline_probability", self.baseline_probability),
        ):
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name}={value} is outside [0, 1]")

    @property
    def minutes_to_breach(self) -> float | None:
        return self.features.minutes_to_breach

    @property
    def uplift_over_baseline(self) -> float:
        """How much the primary estimator moved the number.

        Persistently near zero across a benchmark run means the primary is
        contributing nothing and the baseline should ship instead - which is
        a result, not a bug.
        """
        return self.probability - self.baseline_probability


class RiskService(Protocol):
    """What every risk service must look like."""

    def assess(
        self,
        evidence: Sequence[Evidence],
        *,
        envelope: TemperatureEnvelope,
        now: datetime,
        horizon_minutes: int = ...,
        context: Sequence[Evidence] = ...,
    ) -> RiskEstimate | None: ...


class SlopeRiskService:
    """Trajectory extrapolation primary, rule prior as the stored baseline."""

    def __init__(
        self,
        *,
        primary: RiskEstimator | None = None,
        baseline: RiskEstimator | None = None,
        window_minutes: int = DEFAULT_WINDOW_MINUTES,
    ) -> None:
        self._primary: RiskEstimator = primary or SlopeExtrapolation()
        self._baseline: RiskEstimator = baseline or RuleBaseline()
        self._window_minutes = window_minutes

    @property
    def model_version(self) -> str:
        return f"{self._primary.name}@{self._primary.version}"

    def assess(
        self,
        evidence: Sequence[Evidence],
        *,
        envelope: TemperatureEnvelope,
        now: datetime,
        horizon_minutes: int = DEFAULT_HORIZON_MINUTES,
        context: Sequence[Evidence] = (),
    ) -> RiskEstimate | None:
        """Estimate the probability of leaving the envelope within the horizon.

        Returns ``None`` when the trajectory cannot be fitted. See the module
        docstring: that is a refusal, not a zero.
        """
        features = build_features(
            evidence,
            envelope=envelope,
            now=now,
            window_minutes=self._window_minutes,
            context=context,
        )
        if features is None:
            return None

        return RiskEstimate(
            probability=self._primary.estimate(features, horizon_minutes=horizon_minutes),
            baseline_probability=self._baseline.estimate(features, horizon_minutes=horizon_minutes),
            baseline_name=f"{self._baseline.name}@{self._baseline.version}",
            model_version=self.model_version,
            horizon_minutes=horizon_minutes,
            feature_vector_hash=features.digest(),
            features=features,
            degraded=False,
        )
