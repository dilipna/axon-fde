"""Tests for the thermal model and scenario execution.

Two things are being protected.

First, **determinism**. A benchmark built on a non-reproducible simulator
produces numbers nobody can check. Same seed must mean byte-identical output.

Second, **the agreement between ground truth and physics**. Each scenario
declares when a breach occurs; the simulation must actually produce it at that
minute. If the two drift apart, every metric computed from the scenario is
silently wrong while every test still passes. These tests make that loud.
"""

from __future__ import annotations

import itertools
from pathlib import Path

import pytest

from simulator.incidentforge.faults.schedule import (
    FAULT_CODE_COMPRESSOR,
    compute_fault_effects,
    severity_at,
)
from simulator.incidentforge.generator import run_scenario
from simulator.incidentforge.physics.ambient import ambient_temperature_c
from simulator.incidentforge.physics.params import params_for_cargo_class
from simulator.incidentforge.physics.thermal import (
    ThermalState,
    equilibrium_temperature_c,
    step_thermal,
)
from simulator.incidentforge.scenarios import (
    AmbientProfile,
    InjectedFault,
    load_pack,
)

pytestmark = pytest.mark.unit

PACK_DIR = Path(__file__).resolve().parents[2] / "data" / "scenarios" / "pack_v1"


@pytest.fixture(scope="module")
def pack():
    return load_pack(PACK_DIR)


# ---------------------------------------------------------------------------
# Ground truth must match what the physics actually does
# ---------------------------------------------------------------------------


def test_every_scenario_reproduces_its_declared_breach(pack):
    """The check that keeps the benchmark honest.

    A scenario whose declared breach minute drifts from what the simulation
    produces would corrupt lead-time and risk metrics while every other test
    continued to pass.
    """
    for scenario in pack.scenarios.values():
        result = run_scenario(scenario)
        declared = scenario.ground_truth.breach_at_min
        assert result.actual_breach_minute == declared, (
            f"{scenario.scenario_id}: declares breach at {declared} but the "
            f"simulation breaches at {result.actual_breach_minute}"
        )


def test_every_scenario_reproduces_its_breach_flag(pack):
    for scenario in pack.scenarios.values():
        result = run_scenario(scenario)
        assert (result.actual_breach_minute is not None) == (scenario.ground_truth.breach_occurs), (
            scenario.scenario_id
        )


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_same_seed_produces_identical_output(pack):
    """Same seed, byte-identical stream. Without this nothing is reproducible."""
    scenario = pack.scenarios["compressor_degradation_pharma_01"]
    first = run_scenario(scenario)
    second = run_scenario(scenario)

    assert [e.model_dump() for e in first.events] == [e.model_dump() for e in second.events]
    assert [f.model_dump() for f in first.ground_truth] == [
        f.model_dump() for f in second.ground_truth
    ]


def test_different_seeds_produce_different_noise(pack):
    """Seeding must actually vary the run, or the seed is decorative."""
    scenario = pack.scenarios["compressor_degradation_pharma_01"]
    baseline = run_scenario(scenario)
    reseeded = run_scenario(scenario.model_copy(update={"seed": 999}))

    assert [e.cargo_temp_c for e in baseline.events] != [e.cargo_temp_c for e in reseeded.events]
    # Physics is unchanged, so the breach lands in the same place.
    assert reseeded.actual_breach_minute == baseline.actual_breach_minute


def test_event_stream_is_complete_and_ordered(pack):
    for scenario in pack.scenarios.values():
        result = run_scenario(scenario)
        assert len(result.events) == scenario.duration_minutes
        assert [e.sequence for e in result.events] == list(range(scenario.duration_minutes))
        timestamps = [e.timestamp for e in result.events]
        assert timestamps == sorted(timestamps)


# ---------------------------------------------------------------------------
# The flagship scenario's defining properties
# ---------------------------------------------------------------------------


def test_flagship_stays_in_specification_for_over_two_hours(pack):
    """The premise of the entire product: a threshold alarm sees nothing.

    If this scenario breached early, a threshold detector would catch it and
    there would be no lead time to claim.
    """
    scenario = pack.scenarios["compressor_degradation_pharma_01"]
    result = run_scenario(scenario)
    assert result.actual_breach_minute is not None
    assert result.actual_breach_minute > 120


def test_flagship_temperature_climbs_monotonically_once_saturated(pack):
    """The signature a slope-based detector can see and a threshold cannot."""
    scenario = pack.scenarios["compressor_degradation_pharma_01"]
    result = run_scenario(scenario)

    saturation = result.first_saturation_minute
    assert saturation is not None

    # From saturation to breach, true temperature only rises.
    window = result.ground_truth[saturation : result.actual_breach_minute]
    temps = [f.true_cargo_temp_c for f in window]
    assert all(b >= a for a, b in itertools.pairwise(temps))
    assert temps[-1] > temps[0]


def test_saturation_precedes_breach_with_usable_warning(pack):
    """Saturation is the physical moment the outcome becomes near-inevitable.

    The gap between it and the breach is the window any predictive system is
    competing for. A small gap would mean the scenario offers nothing to win.
    """
    scenario = pack.scenarios["compressor_degradation_pharma_01"]
    result = run_scenario(scenario)

    assert result.first_saturation_minute is not None
    assert result.actual_breach_minute is not None
    warning_minutes = result.actual_breach_minute - result.first_saturation_minute
    assert warning_minutes >= 30, f"only {warning_minutes} minutes of warning"


def test_flagship_raises_the_compressor_fault_code(pack):
    scenario = pack.scenarios["compressor_degradation_pharma_01"]
    result = run_scenario(scenario)
    assert any(FAULT_CODE_COMPRESSOR in e.fault_codes for e in result.events)


# ---------------------------------------------------------------------------
# The sensor-drift trap
# ---------------------------------------------------------------------------


def test_sensor_drift_reports_a_breach_that_never_happens(pack):
    """The scenario that justifies multimodality.

    The cargo stays in specification throughout. The instrument says otherwise.
    A telemetry-only system reroutes a healthy load; the panel photograph is
    the only thing that contradicts the sensor.
    """
    scenario = pack.scenarios["sensor_drift_pharma_01"]
    result = run_scenario(scenario)

    assert result.actual_breach_minute is None, "the cargo must never truly breach"
    assert result.reported_breach_minute is not None, "the sensor must report one"
    assert result.sensor_lied


def test_sensor_drift_produces_no_fault_code(pack):
    """A drifting thermistor reports a plausible number and the unit cannot
    tell it is wrong. That silence is why telemetry alone cannot resolve this.
    """
    scenario = pack.scenarios["sensor_drift_pharma_01"]
    result = run_scenario(scenario)
    assert all(not e.fault_codes for e in result.events)


def test_sensor_drift_keeps_the_refrigeration_unit_nominal(pack):
    """Compressor and reefer status stay healthy, which is the evidence that
    points at the instrument rather than a thermal fault."""
    scenario = pack.scenarios["sensor_drift_pharma_01"]
    result = run_scenario(scenario)
    assert all(f.compressor_health == 1.0 for f in result.ground_truth)
    assert all(e.reefer_status != "fault" for e in result.events)


# ---------------------------------------------------------------------------
# The control scenario
# ---------------------------------------------------------------------------


def test_healthy_scenario_never_breaches_or_saturates(pack):
    scenario = pack.scenarios["normal_pharma_run_01"]
    result = run_scenario(scenario)

    assert result.actual_breach_minute is None
    assert result.reported_breach_minute is None
    assert result.first_saturation_minute is None


def test_healthy_scenario_stays_well_inside_its_envelope(pack):
    scenario = pack.scenarios["normal_pharma_run_01"]
    result = run_scenario(scenario)
    temps = [f.true_cargo_temp_c for f in result.ground_truth]
    assert min(temps) >= scenario.cargo.permitted_min_c
    assert max(temps) <= scenario.cargo.permitted_max_c


# ---------------------------------------------------------------------------
# Ground truth never leaks into the telemetry stream
# ---------------------------------------------------------------------------


def test_telemetry_events_carry_no_ground_truth(pack):
    """If true temperature reached the feature builder, the risk model would
    train on the answer and every accuracy number would be meaningless."""
    forbidden = {
        "true_cargo_temp_c",
        "sensor_error_c",
        "compressor_health",
        "in_breach",
        "saturated",
        "duty_cycle",
    }
    event_fields = set(
        run_scenario(pack.scenarios["sensor_drift_pharma_01"]).events[0].model_dump()
    )
    assert not (forbidden & event_fields)


def test_reported_temperature_differs_from_truth_under_a_sensor_fault(pack):
    """Confirms the two channels are genuinely independent, not the same value
    copied into both."""
    result = run_scenario(pack.scenarios["sensor_drift_pharma_01"])
    errors = [abs(f.sensor_error_c) for f in result.ground_truth]
    assert max(errors) > 2.0


# ---------------------------------------------------------------------------
# Thermal model unit behaviour
# ---------------------------------------------------------------------------


def test_a_healthy_unit_settles_into_proportional_droop():
    """A healthy unit holds the load, but not exactly at setpoint.

    A proportional controller needs a non-zero error to command output, so the
    load settles slightly above setpoint and cycles there. That droop is real
    behaviour, not a defect: at setpoint the error is zero, the unit switches
    off, and the load drifts up until the controller responds.

    What matters is that it settles well inside the envelope rather than
    climbing away.
    """
    params = params_for_cargo_class("pharma_2_8")
    state = ThermalState(cargo_temp_c=5.0, reefer_fuel_pct=90.0)

    for _ in range(240):
        step = step_thermal(
            state,
            params,
            ambient_temp_c=30.0,
            setpoint_c=5.0,
            compressor_health=1.0,
            door_open=False,
            dt_seconds=60.0,
        )
        state = step.state
        assert not step.saturated

    # Settles into droop above setpoint, comfortably inside a 2-8 C envelope.
    assert 5.0 < state.cargo_temp_c < 7.5


def test_a_failed_unit_loses_ground():
    params = params_for_cargo_class("pharma_2_8")
    state = ThermalState(cargo_temp_c=5.0, reefer_fuel_pct=90.0)
    step = step_thermal(
        state,
        params,
        ambient_temp_c=30.0,
        setpoint_c=5.0,
        compressor_health=0.0,
        door_open=False,
        dt_seconds=60.0,
    )
    assert step.state.cargo_temp_c > 5.0
    assert step.cooling_output_w == 0.0


def test_an_open_door_accelerates_warming():
    params = params_for_cargo_class("pharma_2_8")
    state = ThermalState(cargo_temp_c=6.0, reefer_fuel_pct=90.0)
    common = {
        "ambient_temp_c": 30.0,
        "setpoint_c": 5.0,
        "compressor_health": 1.0,
        "dt_seconds": 60.0,
    }
    closed = step_thermal(state, params, door_open=False, **common)
    opened = step_thermal(state, params, door_open=True, **common)
    assert opened.heat_ingress_w > closed.heat_ingress_w
    assert opened.state.cargo_temp_c > closed.state.cargo_temp_c


def test_an_empty_tank_stops_all_cooling():
    params = params_for_cargo_class("pharma_2_8")
    state = ThermalState(cargo_temp_c=7.0, reefer_fuel_pct=0.0)
    step = step_thermal(
        state,
        params,
        ambient_temp_c=30.0,
        setpoint_c=5.0,
        compressor_health=1.0,
        door_open=False,
        dt_seconds=60.0,
    )
    assert step.cooling_output_w == 0.0
    assert step.state.cargo_temp_c > 7.0


def test_equilibrium_temperature_marks_the_saturation_point():
    """Above the equilibrium temperature the unit cannot hold at any duty."""
    params = params_for_cargo_class("pharma_2_8")
    healthy = equilibrium_temperature_c(params, ambient_temp_c=34.0, compressor_health=1.0)
    degraded = equilibrium_temperature_c(params, ambient_temp_c=34.0, compressor_health=0.6)

    assert healthy < 8.0, "a healthy unit must be able to hold a pharma envelope"
    assert degraded > 8.0, "a degraded unit must not be able to"


def test_invalid_health_is_rejected():
    params = params_for_cargo_class("pharma_2_8")
    state = ThermalState(cargo_temp_c=5.0, reefer_fuel_pct=90.0)
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        step_thermal(
            state,
            params,
            ambient_temp_c=20.0,
            setpoint_c=5.0,
            compressor_health=1.5,
            door_open=False,
            dt_seconds=60.0,
        )


def test_unknown_cargo_class_is_reported_clearly():
    with pytest.raises(KeyError, match="No thermal parameters"):
        params_for_cargo_class("antimatter")


# ---------------------------------------------------------------------------
# Fault scheduling
# ---------------------------------------------------------------------------


def test_fault_ramps_rather_than_switching_on():
    """An instant failure is easy to detect and uninteresting. The gradual
    ramp is what produces an in-specification drift worth predicting."""
    fault = InjectedFault(type="compressor_degradation", start_min=45, severity=0.4, ramp_min=60)
    assert severity_at(fault, 44) == 0.0
    assert severity_at(fault, 45) == 0.0
    assert severity_at(fault, 75) == pytest.approx(0.2)
    assert severity_at(fault, 105) == pytest.approx(0.4)
    assert severity_at(fault, 200) == pytest.approx(0.4)


def test_a_fault_with_a_duration_ends():
    fault = InjectedFault(type="door_left_open", start_min=30, severity=1.0, duration_min=20)
    assert severity_at(fault, 40) == 1.0
    assert severity_at(fault, 50) == 0.0


def test_compressor_code_appears_only_past_the_diagnostic_threshold():
    """Below the threshold the unit's own diagnostics see nothing, which is
    the window a predictive system competes in."""
    faults = [InjectedFault(type="compressor_degradation", start_min=0, severity=0.4, ramp_min=100)]
    assert FAULT_CODE_COMPRESSOR not in compute_fault_effects(faults, 10).active_codes
    assert FAULT_CODE_COMPRESSOR in compute_fault_effects(faults, 90).active_codes


def test_overlapping_faults_take_the_worst_rather_than_summing():
    """Two causes of degradation do not make a unit twice as broken."""
    faults = [
        InjectedFault(type="compressor_degradation", start_min=0, severity=0.3),
        InjectedFault(type="compressor_degradation", start_min=0, severity=0.6),
    ]
    assert compute_fault_effects(faults, 10).compressor_health == pytest.approx(0.4)


def test_no_faults_means_nominal_effects():
    effects = compute_fault_effects([], 100)
    assert effects.compressor_health == 1.0
    assert not effects.sensor_is_faulty
    assert not effects.door_open
    assert not effects.active_codes


# ---------------------------------------------------------------------------
# Ambient profile
# ---------------------------------------------------------------------------


def test_constant_profile_does_not_vary():
    profile = AmbientProfile(type="constant", peak_c=22.0, min_c=22.0)
    assert ambient_temperature_c(profile, 0) == 22.0
    assert ambient_temperature_c(profile, 500) == 22.0


def test_diurnal_profile_stays_within_its_declared_band():
    profile = AmbientProfile(type="diurnal_hot", peak_c=34.0, min_c=21.0)
    values = [ambient_temperature_c(profile, m) for m in range(0, 1440, 10)]
    assert min(values) >= 21.0 - 1e-9
    assert max(values) <= 34.0 + 1e-9


def test_diurnal_profile_warms_through_the_afternoon():
    """Runs start mid-morning, so ambient should be climbing early on - which
    is what makes a degrading unit fall behind as the run progresses."""
    profile = AmbientProfile(type="diurnal_hot", peak_c=34.0, min_c=21.0)
    assert ambient_temperature_c(profile, 240) > ambient_temperature_c(profile, 0)
