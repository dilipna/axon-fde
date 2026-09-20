"""Two non-ML estimators, and an honest account of what they are.

These exist for three reasons, in ascending order of importance.

1. Something must serve predictions before there is a trained model.
2. Every stored prediction carries a baseline beside it (invariant I3), so
   there has to be a baseline to store.
3. **The trained model has to beat one of them to ship.** B12's stop
   condition is that LightGBM must improve on ``SlopeExtrapolation`` by at
   least 5 points of lead-time-at-fixed-false-alarm-rate after two feature
   iterations, or the baseline ships and the negative result is published. A
   weak baseline makes that comparison meaningless, so these are written to be
   as good as their form allows rather than as foils.

**These are not calibrated probabilities.** Both return a value in [0, 1] that
is *monotone in risk*, and that is all. A returned 0.7 does not mean seven
runs in ten breach; nobody has checked, and the curve below is chosen, not
fitted. Calibration is B12's isotonic regression and its reliability diagram,
and claim C2 stays ``PLACEHOLDER`` until that exists. Multiplying one of these
by a cargo value to get an expected loss would be the single easiest way to
ship a misleading number from this project.
"""

from __future__ import annotations

import math
from typing import Protocol

from backend.app.risk.features import RiskFeatures

__all__ = [
    "DEFAULT_HORIZON_MINUTES",
    "RiskEstimator",
    "RuleBaseline",
    "SlopeExtrapolation",
]

#: The question every estimate answers: will this cargo leave its envelope
#: within the next hour? An hour is roughly the time needed to reroute a
#: trailer to cold storage, so it is the horizon over which a warning is
#: actionable rather than merely alarming.
DEFAULT_HORIZON_MINUTES = 60


class RiskEstimator(Protocol):
    """Anything that turns features into a risk score."""

    name: str
    version: str

    def estimate(self, features: RiskFeatures, *, horizon_minutes: int) -> float: ...


class RuleBaseline:
    """A static prior from the current state. No trajectory, no arithmetic.

    The "what would a sensible rule say?" floor: it looks at how much headroom
    is left and whether the unit is complaining, and has no concept of where
    the temperature is heading. Deliberately the weaker of the two - its job
    is to show that extrapolation earns its keep, and later that a model earns
    its keep over extrapolation.

    The bands are **priors, not fitted values**. They encode a belief about
    how alarming a given headroom is and have never been checked against an
    outcome frequency. That is exactly why nothing multiplies this by a cargo
    value.
    """

    name = "rule_prior"
    version = "1"

    #: Headroom in degrees -> prior probability of leaving the envelope.
    #: Coarse on purpose: a finer ladder would imply a precision a rule with
    #: no trend information does not have.
    _BANDS: tuple[tuple[float, float], ...] = ((0.5, 0.65), (1.0, 0.40), (2.0, 0.18), (4.0, 0.06))

    #: An active fault code raises the prior. A prior warning on the vehicle
    #: raises it less: it says this unit has a history, not that it is in
    #: trouble now.
    _FAULT_CODE_UPLIFT = 0.15
    _PRIOR_WARNING_UPLIFT = 0.05

    #: Never certain from a rule. Returning 1.0 would claim to know an outcome
    #: from a snapshot.
    _CEILING = 0.95
    _FLOOR = 0.02

    def estimate(self, features: RiskFeatures, *, horizon_minutes: int) -> float:
        """Score the current state.

        ``horizon_minutes`` is accepted and ignored, which is the honest
        signature: this estimator has no trajectory and cannot distinguish one
        horizon from another. Dropping the parameter would break the protocol;
        pretending to use it would be worse.
        """
        if features.in_breach:
            # Already outside the envelope. An observation, not a prediction.
            return self._CEILING

        probability = self._FLOOR
        for threshold, prior in self._BANDS:
            if features.headroom_c < threshold:
                probability = prior
                break

        if features.fault_codes:
            probability += self._FAULT_CODE_UPLIFT
        if features.has_prior_maintenance_warning:
            probability += self._PRIOR_WARNING_UPLIFT

        return min(self._CEILING, max(self._FLOOR, probability))


class SlopeExtrapolation:
    """Fit the recent trajectory, project it to the envelope, score the gap.

    The estimator B12 has to beat, and it is genuinely reasonable: on the
    flagship recording the detector built on it fires at minute 102 - 16
    minutes after the unit saturates, 35 before the envelope is breached - and
    never fires on the false-alarm control.

    **What it cannot do**, and what the trained model is being asked to fix:

    - It is blind to *why* the temperature is rising. A drifting sensor and a
      failing compressor produce the same slope and score identically. The
      features already carry the signal that separates them
      (``RiskFeatures.cooling_response``); this estimator does not look at it,
      because a linear fit is a linear fit. On `sensor_drift_pharma_01` it
      therefore false-alarms at minute 59 - earlier and more confidently than
      the threshold baseline, which waits until the reported value crosses
      8 C around minute 95. Predicting harder on a lying sensor means being
      confidently wrong sooner.
    - It assumes the trajectory continues straight. A saturating unit's curve
      is not a straight line, so projecting one mis-times the crossing -
      conservatively here, which is the safe direction, but wrongly either way.
    """

    name = "slope_extrapolation"
    version = "1"

    #: How sharply the score rises as the projected breach comes inside the
    #: horizon. At 15, a breach projected exactly at the horizon scores 0.5,
    #: one 30 minutes inside it ~0.88, one 30 minutes beyond it ~0.12. Chosen
    #: so the interesting range spans about an hour either side of the horizon
    #: rather than switching like a step function.
    _STEEPNESS_MINUTES = 15.0

    _CEILING = 0.97
    _FLOOR = 0.01

    def estimate(self, features: RiskFeatures, *, horizon_minutes: int) -> float:
        """Score how close the projected breach is to the horizon."""
        if features.in_breach:
            return self._CEILING

        projected = features.minutes_to_breach
        if projected is None:
            # Not heading out of spec at all. Not "very unlikely" - the
            # trajectory does not reach the envelope, and the floor represents
            # residual uncertainty about the fit, not a computed likelihood.
            return self._FLOOR

        exponent = (horizon_minutes - projected) / self._STEEPNESS_MINUTES
        # Guards against OverflowError on a trajectory projected thousands of
        # minutes out, which a near-flat slope produces routinely.
        if exponent < -60:
            return self._FLOOR
        if exponent > 60:
            return self._CEILING
        return min(self._CEILING, max(self._FLOOR, 1.0 / (1.0 + math.exp(-exponent))))
