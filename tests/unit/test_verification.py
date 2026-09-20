"""Grading an action against the claim it made.

The verdicts that matter most here are the two that decline to conclude
anything. A verification window with no data in it is not a pass, and an
action that changes a record rather than a temperature was never gradeable.
Both of those are easy to collapse into "success", and both would inflate
every outcome metric the system reports.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from backend.app.actions.effects import MIN_SLOPE_WINDOW_MINUTES, EffectKind, ExpectedEffect
from backend.app.domain.envelope import TemperatureEnvelope
from backend.app.verification.outcome import (
    RECOVERY_HOLD_READINGS,
    STOPPED_RISING_SLOPE_C_PER_MIN,
    Verdict,
    evaluate_effect,
)
from tests.conftest import recording

START = datetime(2026, 9, 19, 14, 0, tzinfo=UTC)
#: The flagship shipment's contractual envelope, from the signed Bill of
#: Lading rather than the ERP - the conflict the pipeline resolves.
ENVELOPE = TemperatureEnvelope(minimum_c=2.0, maximum_c=8.0)


def series(values: list[float], *, every_minutes: int = 1) -> list[tuple[datetime, float]]:
    return [(START + timedelta(minutes=i * every_minutes), v) for i, v in enumerate(values)]


def recovery(minutes: int = 90) -> ExpectedEffect:
    return ExpectedEffect(kind=EffectKind.TEMPERATURE_WITHIN_ENVELOPE, within_minutes=minutes)


def stops_rising() -> ExpectedEffect:
    return ExpectedEffect(
        kind=EffectKind.TEMPERATURE_STOPS_RISING, within_minutes=MIN_SLOPE_WINDOW_MINUTES
    )


def grade(effect: ExpectedEffect, readings: list[tuple[datetime, float]]):
    return evaluate_effect(
        effect,
        readings=readings,
        envelope=ENVELOPE,
        window_start=START,
        window_end=START + timedelta(minutes=effect.within_minutes),
    )


class TestMissingDataIsNeverSuccess:
    def test_a_window_with_no_readings_is_inconclusive(self) -> None:
        """Invariant I6, in the place it is most tempting to break.

        A fleet with a dead telemetry link would otherwise report a hundred
        percent intervention success rate, because every window would be
        empty and every empty window would pass.
        """
        result = grade(recovery(), [])
        assert result.verdict is Verdict.INCONCLUSIVE
        assert result.observed["reading_count"] == 0

    def test_readings_outside_the_window_do_not_count(self) -> None:
        """The window is the claim's deadline, not a hint.

        Readings from before the action was taken describe the problem, not
        the fix, and letting them in would grade a reroute against the
        temperatures that caused it.
        """
        before = [(START - timedelta(minutes=i), 5.0) for i in range(1, 10)]
        assert grade(recovery(), before).verdict is Verdict.INCONCLUSIVE

    def test_one_reading_back_in_spec_is_not_a_recovery(self) -> None:
        """A thermometer twitching, not a trend.

        The same lesson the predictive detector learned: a one-reading trigger
        fires on transients, and the corrected answer needed a hold.
        """
        result = grade(recovery(), series([7.5, 7.6]))
        assert result.verdict is Verdict.INCONCLUSIVE
        assert str(RECOVERY_HOLD_READINGS) in result.detail

    def test_a_slope_fitted_to_a_fraction_of_the_window_is_inconclusive(self) -> None:
        """A minimum count is not a window.

        Five readings satisfy a count four minutes into a ninety-minute
        window, and the fit then describes four minutes of whatever was
        happening at the start.
        """
        result = grade(stops_rising(), series([6.0, 6.1, 6.2, 6.3, 6.4]))
        assert result.verdict is Verdict.INCONCLUSIVE
        assert result.observed["span_minutes"] < result.observed["required_span_minutes"]


class TestRecovery:
    def test_cargo_back_inside_the_envelope_and_holding_is_confirmed(self) -> None:
        result = grade(recovery(), series([9.0, 8.5, 7.9, 7.4, 7.0, 6.8]))
        assert result.verdict is Verdict.CONFIRMED
        assert result.requires_follow_up is False

    def test_cargo_still_outside_the_envelope_is_a_failure(self) -> None:
        result = grade(recovery(), series([9.0, 9.2, 9.4, 9.6, 9.8, 10.0]))
        assert result.verdict is Verdict.FAILED
        assert result.requires_follow_up is True

    def test_a_dip_into_spec_that_does_not_hold_is_a_failure(self) -> None:
        """Ending outside the envelope fails, however good the middle looked."""
        result = grade(recovery(), series([9.0, 7.5, 7.4, 7.6, 8.4, 8.9]))
        assert result.verdict is Verdict.FAILED

    def test_the_envelope_boundary_is_inclusive(self) -> None:
        """8.0 C against a contractual maximum of 8.0 C is in spec.

        Off by one in the exclusive direction would fail a reroute that landed
        the cargo exactly on its limit, and our verdict would disagree with
        the customer's on the boundary case most likely to be disputed.
        """
        assert grade(recovery(), series([8.0, 8.0, 8.0])).verdict is Verdict.CONFIRMED


class TestTheRiseStopping:
    def test_a_flat_trace_confirms_that_the_rise_stopped(self) -> None:
        flat = series([6.0 + 0.001 * i for i in range(MIN_SLOPE_WINDOW_MINUTES + 1)])
        result = grade(stops_rising(), flat)
        assert result.verdict is Verdict.CONFIRMED
        assert result.observed["slope_c_per_min"] <= STOPPED_RISING_SLOPE_C_PER_MIN

    def test_a_trace_still_climbing_at_a_failing_compressor_rate_fails(self) -> None:
        """0.023 C/min is the flagship scenario's measured gradient."""
        climbing = series([6.0 + 0.023 * i for i in range(MIN_SLOPE_WINDOW_MINUTES + 1)])
        result = grade(stops_rising(), climbing)
        assert result.verdict is Verdict.FAILED

    def test_the_threshold_is_not_zero(self) -> None:
        """A healthy trailer drifts. Zero would fail every intervention.

        Sensor quantisation and ordinary ambient variation put a working unit
        at a few thousandths of a degree per minute, so "any positive slope is
        still rising" would mark every successful action as a failure.
        """
        assert STOPPED_RISING_SLOPE_C_PER_MIN > 0.0
        drifting = series([6.0 + 0.004 * i for i in range(MIN_SLOPE_WINDOW_MINUTES + 1)])
        assert grade(stops_rising(), drifting).verdict is Verdict.CONFIRMED


class TestUnobservableActions:
    def test_flagging_a_vehicle_is_graded_not_applicable(self) -> None:
        """Not a pass. There was never anything for a sensor to confirm.

        Counting it as a success would pad the outcome metric with actions
        nobody could check; counting it as a failure would punish an action
        that did exactly what was asked.
        """
        effect = ExpectedEffect(kind=EffectKind.NONE_OBSERVABLE, within_minutes=0)
        result = evaluate_effect(
            effect,
            readings=series([6.0] * 10),
            envelope=ENVELOPE,
            window_start=START,
            window_end=START,
        )
        assert result.verdict is Verdict.NOT_APPLICABLE

    def test_an_unobservable_action_still_needs_a_human(self) -> None:
        """It does not resolve the incident on its own."""
        effect = ExpectedEffect(kind=EffectKind.NONE_OBSERVABLE, within_minutes=0)
        result = evaluate_effect(
            effect,
            readings=[],
            envelope=ENVELOPE,
            window_start=START,
            window_end=START,
        )
        assert result.requires_follow_up is True


class TestAgainstTheRealRecordings:
    """The thresholds were derived from these traces; check they still hold."""

    def test_the_healthy_control_run_reads_as_having_stopped_rising(self) -> None:
        """The false-alarm control, applied to outcome grading.

        A threshold tuned too tight would mark a perfectly normal trailer as
        still warming and reopen an incident that was correctly resolved.
        """
        frame = pd.read_parquet(recording("normal_pharma_run_01"))
        temps = [float(v) for v in frame["cargo_temp_c"].tolist()]
        window = temps[-(MIN_SLOPE_WINDOW_MINUTES + 1) :]
        assert grade(stops_rising(), series(window)).verdict is Verdict.CONFIRMED

    def test_the_degrading_compressor_reads_as_still_rising(self) -> None:
        """And a threshold tuned too loose would resolve a failing one."""
        frame = pd.read_parquet(recording("compressor_degradation_pharma_01"))
        temps = [float(v) for v in frame["cargo_temp_c"].tolist()]
        window = temps[-(MIN_SLOPE_WINDOW_MINUTES + 1) :]
        result = grade(stops_rising(), series(window))
        assert result.verdict is Verdict.FAILED
        assert result.observed["slope_c_per_min"] > STOPPED_RISING_SLOPE_C_PER_MIN

    def test_the_two_recordings_sit_either_side_of_the_threshold_with_margin(self) -> None:
        """The separation the constant was chosen from, asserted rather than assumed.

        This is the test that fails if somebody re-tunes the threshold to fix
        a single scenario: the margin on both sides has to survive, not just
        the verdict on one trace.
        """
        slopes = {}
        for scenario in ("normal_pharma_run_01", "compressor_degradation_pharma_01"):
            frame = pd.read_parquet(recording(scenario))
            temps = [float(v) for v in frame["cargo_temp_c"].tolist()]
            window = temps[-(MIN_SLOPE_WINDOW_MINUTES + 1) :]
            slopes[scenario] = grade(stops_rising(), series(window)).observed["slope_c_per_min"]

        healthy = slopes["normal_pharma_run_01"]
        failing = slopes["compressor_degradation_pharma_01"]
        assert healthy < STOPPED_RISING_SLOPE_C_PER_MIN < failing
        # At least 20% clear on each side, so neither lands on the boundary.
        assert healthy < STOPPED_RISING_SLOPE_C_PER_MIN * 0.8
        assert failing > STOPPED_RISING_SLOPE_C_PER_MIN * 1.2


def test_regrading_is_not_possible_by_reordering_the_readings() -> None:
    """A caller cannot change a verdict by changing its query's ordering."""
    readings = series([9.0, 8.5, 7.9, 7.4, 7.0, 6.8])
    forward = grade(recovery(), readings)
    backward = grade(recovery(), list(reversed(readings)))
    assert forward.verdict is backward.verdict
    assert forward.observed == backward.observed


@pytest.mark.parametrize("minutes", [0, -10])
def test_a_measurable_claim_needs_time_to_come_true(minutes: int) -> None:
    with pytest.raises(ValueError, match=r"leaves no time|verification window"):
        ExpectedEffect(kind=EffectKind.TEMPERATURE_WITHIN_ENVELOPE, within_minutes=minutes)
