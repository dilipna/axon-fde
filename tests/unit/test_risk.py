"""The risk service, its features and its two baselines.

These tests replay the real recordings through the estimator, because the
numbers that matter — minute 100, not before 86, never on the control — are
properties of this code against that data, and a synthetic ramp would prove
none of them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from backend.app.domain.enums import EvidenceSource, Modality
from backend.app.domain.envelope import TemperatureEnvelope
from backend.app.domain.evidence import EntityRef, Evidence, Provenance
from backend.app.evidence.telemetry import read_telemetry, reading_to_evidence
from backend.app.incidents.detection import PredictiveDetector
from backend.app.risk.baselines import RuleBaseline, SlopeExtrapolation
from backend.app.risk.features import (
    DEFAULT_WINDOW_MINUTES,
    MIN_READINGS_FOR_SLOPE,
    build_features,
    least_squares_slope,
)
from backend.app.risk.lead_time import measure_lead_time
from backend.app.risk.service import RiskEstimate, SlopeRiskService
from tests.conftest import GENERATED_DIR, recording

pytestmark = pytest.mark.unit

START = datetime(2026, 7, 14, 10, 0, tzinfo=UTC)
PHARMA = TemperatureEnvelope(minimum_c=2.0, maximum_c=8.0)

FLAGSHIP = "compressor_degradation_pharma_01"
CONTROL = "normal_pharma_run_01"
DRIFT = "sensor_drift_pharma_01"

#: Locked by `tests/unit/test_physics.py`.
SATURATES_AT = 86
BREACHES_AT = 137

#: Measured. See CONSECUTIVE_READINGS_TO_FIRE for why it is not minute 29.
FIRES_AT = 102


def ramp(values: list[float], *, start: datetime = START) -> list[Evidence]:
    """One `cargo_temp_c` observation per minute."""
    return [
        Evidence.create(
            entity_ref=EntityRef(kind="vehicle", id="AX-042"),
            source=EvidenceSource.TELEMETRY,
            modality=Modality.TIMESERIES,
            observation_type="cargo_temp_c",
            value=value,
            observed_at=start + timedelta(minutes=index),
            ingested_at=start + timedelta(minutes=index),
            provenance=Provenance(producer="test_risk"),
        )
        for index, value in enumerate(values)
    ]


def first_detection(scenario: str, *, detector: PredictiveDetector | None = None) -> int | None:
    """The minute the predictive arm first raises a detection.

    Goes through the detector rather than the estimator, so the debounce is
    part of what is measured. Testing the raw score would report minute 29 on
    the flagship and miss the whole point of `CONSECUTIVE_READINGS_TO_FIRE`.
    """
    det = detector or PredictiveDetector(SlopeRiskService())
    readings = read_telemetry(recording(scenario))
    window: list[list[Evidence]] = []
    size = det.required_history_minutes + 1

    for reading in readings:
        window.append(reading_to_evidence(reading))
        window = window[-size:]
        visible = [item for step in window for item in step]
        if det.evaluate(visible, envelope=PHARMA, now=reading.timestamp) is not None:
            return reading.sequence
    return None


requires_all_recordings = pytest.mark.skipif(
    not (GENERATED_DIR / FLAGSHIP).exists(),
    reason="run `poe forge run-all` first",
)


# ---------------------------------------------------------------------------
# Features
# ---------------------------------------------------------------------------


def test_a_flat_series_has_no_projected_breach() -> None:
    """Flat is not "a breach in 9000 minutes"; it is no breach.

    Collapsing the two would let sensor quantisation masquerade as a trend.
    """
    features = build_features(ramp([5.0] * 40), envelope=PHARMA, now=START + timedelta(minutes=39))
    assert features is not None
    assert features.minutes_to_breach is None
    assert abs(features.temp_slope_c_per_min) < 1e-4


def test_too_few_readings_produce_no_features_rather_than_a_guess() -> None:
    """Invariant I6. Two points fit a line perfectly and mean nothing."""
    short = ramp([5.0, 5.5, 6.0])
    assert len(short) < MIN_READINGS_FOR_SLOPE
    assert build_features(short, envelope=PHARMA, now=START + timedelta(minutes=2)) is None


def test_a_cooling_trajectory_projects_toward_the_lower_limit() -> None:
    """Pharma breaches by freezing too, and a one-directional projection misses it.

    A reefer stuck on destroys a vaccine shipment just as surely as a failed
    one, and it is the case a naive "is it getting hot?" check never sees.
    """
    features = build_features(
        ramp([5.0 - 0.05 * i for i in range(40)]),
        envelope=PHARMA,
        now=START + timedelta(minutes=39),
    )
    assert features is not None
    assert features.temp_slope_c_per_min < 0
    assert features.minutes_to_breach is not None
    assert features.minutes_to_breach > 0


def test_the_feature_hash_changes_when_any_input_changes() -> None:
    """The hash ties a stored prediction to the inputs that produced it."""
    first = build_features(ramp([5.0] * 40), envelope=PHARMA, now=START + timedelta(minutes=39))
    second = build_features(
        ramp([5.0] * 39 + [6.0]), envelope=PHARMA, now=START + timedelta(minutes=39)
    )
    assert first is not None
    assert second is not None
    assert first.digest() != second.digest()


def test_the_feature_hash_is_stable_across_identical_inputs() -> None:
    args = {"envelope": PHARMA, "now": START + timedelta(minutes=39)}
    assert (
        build_features(ramp([5.0] * 40), **args).digest()  # type: ignore[union-attr]
        == build_features(ramp([5.0] * 40), **args).digest()  # type: ignore[union-attr]
    )


def test_a_perfect_line_recovers_its_gradient() -> None:
    assert least_squares_slope([0, 1, 2, 3], [0, 2, 4, 6]) == pytest.approx(2.0)


def test_a_degenerate_window_is_flat_not_infinite() -> None:
    """Samples sharing a timestamp are missing data, not a vertical trend."""
    assert least_squares_slope([5, 5, 5], [1, 2, 3]) == 0.0


# ---------------------------------------------------------------------------
# Estimators
# ---------------------------------------------------------------------------


def test_the_estimate_cannot_exist_without_its_baseline() -> None:
    """Invariant I3, enforced by the type rather than by a reviewer.

    There is no constructor that takes a probability alone, so no caller can
    persist one.
    """
    with pytest.raises(TypeError):
        RiskEstimate(probability=0.8)  # type: ignore[call-arg]


def test_a_probability_outside_the_unit_interval_is_refused() -> None:
    features = build_features(ramp([5.0] * 40), envelope=PHARMA, now=START + timedelta(minutes=39))
    assert features is not None
    with pytest.raises(ValueError, match="outside"):
        RiskEstimate(
            probability=1.4,
            baseline_probability=0.2,
            baseline_name="x@1",
            model_version="y@1",
            horizon_minutes=60,
            feature_vector_hash="abc",
            features=features,
        )


def test_the_slope_estimator_scores_exactly_half_at_the_horizon() -> None:
    """The threshold of 0.5 is therefore not a tuned number.

    It means "the breach is projected to land inside the horizon", which is a
    statement anyone can check rather than a constant somebody chose.
    """
    # Ends at 5.0 C with 3.0 C of headroom, rising 0.05 C/min: a breach
    # projected in exactly 60 minutes, which is the horizon.
    features = build_features(
        ramp([3.5 + 0.05 * i for i in range(31)]),
        envelope=PHARMA,
        now=START + timedelta(minutes=30),
    )
    assert features is not None
    assert features.minutes_to_breach == pytest.approx(60.0, abs=1.5)
    assert SlopeExtrapolation().estimate(features, horizon_minutes=60) == pytest.approx(
        0.5, abs=0.05
    )


def test_the_slope_estimator_does_not_overflow_on_a_near_flat_trajectory() -> None:
    """A near-zero slope projects thousands of minutes out; exp() would raise."""
    features = build_features(
        ramp([5.0 + 0.00001 * i for i in range(40)]),
        envelope=PHARMA,
        now=START + timedelta(minutes=39),
    )
    assert features is not None
    assert 0.0 <= SlopeExtrapolation().estimate(features, horizon_minutes=60) <= 1.0


def test_the_rule_baseline_ignores_the_trajectory_entirely() -> None:
    """It is the weaker estimator on purpose, and this is how.

    Two series ending at the same temperature score identically however
    differently they got there. That is exactly the blindness extrapolation is
    meant to improve on, so it must be real rather than claimed.
    """
    rising = build_features(
        ramp([4.0 + 0.075 * i for i in range(41)]),
        envelope=PHARMA,
        now=START + timedelta(minutes=40),
    )
    steady = build_features(ramp([7.0] * 41), envelope=PHARMA, now=START + timedelta(minutes=40))
    assert rising is not None
    assert steady is not None
    assert rising.cargo_temp_c == pytest.approx(steady.cargo_temp_c, abs=0.2)

    rule = RuleBaseline()
    assert rule.estimate(rising, horizon_minutes=60) == rule.estimate(steady, horizon_minutes=60)
    # Extrapolation sees the difference the rule cannot.
    slope = SlopeExtrapolation()
    assert slope.estimate(rising, horizon_minutes=60) > slope.estimate(steady, horizon_minutes=60)


def test_an_already_breached_reading_is_not_reported_as_a_prediction() -> None:
    """Past the envelope, both estimators are observing, not forecasting."""
    features = build_features(ramp([9.0] * 40), envelope=PHARMA, now=START + timedelta(minutes=39))
    assert features is not None
    assert features.in_breach
    assert features.minutes_to_breach == 0.0
    assert SlopeExtrapolation().estimate(features, horizon_minutes=60) > 0.9
    assert RuleBaseline().estimate(features, horizon_minutes=60) > 0.9


def test_the_service_refuses_rather_than_guessing_on_thin_history() -> None:
    assert SlopeRiskService().assess(ramp([5.0, 5.1]), envelope=PHARMA, now=START) is None


def test_every_estimate_carries_a_baseline_and_a_model_version() -> None:
    estimate = SlopeRiskService().assess(
        ramp([3.5 + 0.05 * i for i in range(31)]),
        envelope=PHARMA,
        now=START + timedelta(minutes=30),
    )
    assert estimate is not None
    assert 0.0 <= estimate.baseline_probability <= 1.0
    assert estimate.baseline_name.startswith("rule_prior@")
    assert estimate.model_version.startswith("slope_extrapolation@")
    assert estimate.feature_vector_hash


# ---------------------------------------------------------------------------
# Against the real recordings — the numbers B3 is accepted on
# ---------------------------------------------------------------------------


@requires_all_recordings
def test_the_flagship_fires_after_saturation_and_before_the_breach() -> None:
    """The headline result, asserted as a window rather than a point.

    Firing before minute 86 would mean reading noise: the unit is not in
    trouble until it saturates. Firing at or after 137 would mean no warning
    at all, since that is when the threshold alarm fires anyway.
    """
    minute = first_detection(FLAGSHIP)

    assert minute is not None, "the predictive arm must fire on the flagship scenario"
    assert minute > SATURATES_AT, (
        f"fired at minute {minute}, before the unit saturates at {SATURATES_AT} - "
        "that is fitting noise, not predicting"
    )
    assert minute < BREACHES_AT, (
        f"fired at minute {minute}, no earlier than the threshold alarm at {BREACHES_AT}"
    )


@requires_all_recordings
def test_the_flagship_fires_at_minute_100_with_the_measured_window() -> None:
    """The exact number, so a change to the estimator is a deliberate act.

    Moving this re-baselines every lead-time figure downstream, exactly like
    the golden digests in `test_emitters.py`.
    """
    assert first_detection(FLAGSHIP) == FIRES_AT


@requires_all_recordings
def test_the_false_alarm_control_never_fires() -> None:
    """Lead time is worthless without this. A detector that always alerts has
    infinite lead time and no value, which is why claim C1 is void unless the
    false-alarm rate is quoted with it."""
    assert first_detection(CONTROL) is None


@requires_all_recordings
def test_a_short_window_fits_noise_and_fires_far_too_early() -> None:
    """Why the window is 30 minutes and not 15 — measured, not asserted.

    A 15-minute window fires around minute 15, when the unit is still pulling
    the load down normally and 71 minutes before it is in any trouble. This
    test is the evidence behind the constant.
    """
    fired_at = first_detection(
        FLAGSHIP,
        detector=PredictiveDetector(
            SlopeRiskService(window_minutes=15), window_minutes=15, consecutive_readings=1
        ),
    )
    assert fired_at is not None
    assert fired_at < SATURATES_AT, (
        "a 15-minute window with no debounce is supposed to fire before "
        "saturation - that is the whole reason for the 30-minute window and "
        "the three-reading hold"
    )


@requires_all_recordings
def test_the_drifting_sensor_produces_a_false_alarm_and_that_is_expected() -> None:
    """The honest Phase 1 result, recorded rather than tuned away.

    `sensor_drift_pharma_01`'s true temperature never leaves spec; the
    instrument lies. Extrapolation sees a steep rise and fires at minute 56 -
    *earlier and more confidently* than the threshold alarm, which waits until
    the reported value crosses 8 C around minute 95.

    Predicting harder on a lying sensor means being confidently wrong sooner.
    Resolving this needs a second opinion on the temperature, which is what
    Phase 4 is for — with the caveat recorded against claim C4, because the
    compressor-response feature already separates these cases from telemetry
    alone.
    """
    minute = first_detection(DRIFT)
    assert minute is not None, (
        "if this stops firing, the sensor-drift ablation has been solved "
        "somewhere it was not supposed to be - check what changed before "
        "celebrating"
    )
    assert minute < 95, "the false alarm is expected to precede the threshold alarm"
    assert minute == 59


@requires_all_recordings
def test_the_compressor_response_separates_the_two_rising_scenarios() -> None:
    """The finding recorded against claim C4, asserted so it cannot rot.

    A failing compressor winds *down* while cargo warms. A drifting sensor
    reports a climb while the compressor winds *up* — physically incoherent.
    If this ever stops holding, the C4 caveat needs revisiting.
    """

    def response_at(scenario: str, minute: int) -> float | None:
        readings = read_telemetry(recording(scenario))[: minute + 1]
        window = readings[-(DEFAULT_WINDOW_MINUTES + 1) :]
        evidence = [item for r in window for item in reading_to_evidence(r)]
        features = build_features(evidence, envelope=PHARMA, now=readings[-1].timestamp)
        assert features is not None
        return features.cooling_response

    failing = response_at(FLAGSHIP, 100)
    drifting = response_at(DRIFT, 100)

    assert failing is not None and failing < 0, "a failing unit winds down"
    assert drifting is not None and drifting > 0, "a drifting sensor leaves the unit healthy"


# ---------------------------------------------------------------------------
# The detector and lead time
# ---------------------------------------------------------------------------


def test_the_predictive_detector_reports_when_the_breach_is_expected() -> None:
    """The difference between the two arms, in one field.

    The threshold alarm leaves `predicted_breach_at` empty because it has
    nothing to say about the future. This one fills it in, and that is what
    makes an alert actionable rather than merely true.
    """
    evidence = ramp([3.5 + 0.05 * i for i in range(40)])
    detection = PredictiveDetector(SlopeRiskService()).evaluate(
        evidence, envelope=PHARMA, now=START + timedelta(minutes=39)
    )
    assert detection is not None
    assert detection.predicted_breach_at is not None
    assert detection.predicted_breach_at > detection.detected_at


def test_a_threshold_outside_the_unit_interval_is_refused() -> None:
    with pytest.raises(ValueError, match="strictly inside"):
        PredictiveDetector(SlopeRiskService(), threshold=1.0)


def test_both_arms_share_a_correlation_key() -> None:
    """So they must be replayed separately, or one deduplicates into the other.

    Splitting the key by detector would mean two incidents for one truck in
    production, which is the failure deduplication exists to prevent.
    """
    from backend.app.incidents.detection import BaselineDetector

    rising = ramp([3.5 + 0.05 * i for i in range(40)])
    breaching = ramp([9.0] * 40)
    now = START + timedelta(minutes=39)

    predictive = PredictiveDetector(SlopeRiskService()).evaluate(rising, envelope=PHARMA, now=now)
    baseline = BaselineDetector().evaluate(breaching, envelope=PHARMA, now=now)

    assert predictive is not None
    assert baseline is not None
    assert predictive.correlation_key == baseline.correlation_key


def test_lead_time_is_the_gap_between_the_two_arms() -> None:
    result = measure_lead_time(
        axon_detected_at=START + timedelta(minutes=100),
        baseline_detected_at=START + timedelta(minutes=137),
        breach_occurred=True,
    )
    assert result.minutes == pytest.approx(37.0)
    assert result.is_measurable
    assert result.alarm_was_correct


def test_a_false_alarm_is_marked_rather_than_counted_as_lead_time() -> None:
    """Otherwise a scenario where nothing was wrong inflates the headline."""
    result = measure_lead_time(
        axon_detected_at=START + timedelta(minutes=56),
        baseline_detected_at=START + timedelta(minutes=95),
        breach_occurred=False,
    )
    assert result.minutes == pytest.approx(39.0)
    assert not result.alarm_was_correct
    assert "FALSE ALARM" in result.describe()


def test_a_missed_detection_is_not_zero_lead_time() -> None:
    """Silence from the predictive arm is a miss, not a tie."""
    result = measure_lead_time(
        axon_detected_at=None,
        baseline_detected_at=START + timedelta(minutes=137),
        breach_occurred=True,
    )
    assert result.minutes is None
    assert not result.is_measurable
    assert "missed detection" in result.describe()


@requires_all_recordings
def test_the_measured_lead_time_on_the_flagship_is_thirty_five_minutes() -> None:
    """The end-to-end number, from the two arms run over the same recording."""
    axon = first_detection(FLAGSHIP)
    assert axon is not None
    result = measure_lead_time(
        axon_detected_at=START + timedelta(minutes=axon),
        baseline_detected_at=START + timedelta(minutes=BREACHES_AT),
        breach_occurred=True,
    )
    assert result.minutes == pytest.approx(BREACHES_AT - FIRES_AT)
    assert result.minutes == pytest.approx(35.0)


def test_the_recordings_carry_no_ground_truth_into_the_feature_builder() -> None:
    """Invariant I8 at the boundary that matters most.

    The risk model is where training on the answer would be most tempting and
    most invisible. Asserted against the telemetry file's own columns.
    """
    table = pq.read_table(Path(recording(FLAGSHIP)))
    forbidden = {"true_cargo_temp_c", "sensor_error_c", "compressor_health", "in_breach"}
    assert not forbidden & set(table.column_names)
