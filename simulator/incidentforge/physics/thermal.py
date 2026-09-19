"""The lumped-capacitance thermal model.

One state variable, cargo temperature, driven by three terms:

    dT/dt = [ UA_eff * (T_ambient - T_cargo) - Q_cool + Q_door ] / C

    UA_eff  envelope heat transfer, raised while a door is open
    Q_cool  refrigeration output = capacity x health x duty x fuel_available
    Q_door  handled by raising UA_eff rather than as a separate term
    C       thermal mass of cargo plus interior air

The refrigeration unit runs a proportional controller on the error between
cargo temperature and setpoint. When the unit is healthy, duty settles below
saturation and temperature holds. When health falls far enough that full duty
no longer matches the heat load, duty saturates at 1.0 and temperature climbs.

**That saturation point is the whole mechanism of the flagship scenario.** The
climb is slow, monotonic, and entirely within specification for over two
hours, which is precisely why a threshold alarm cannot see it coming and a
slope can.

This model is deliberately simple. It is not a CFD simulation and does not
model stratification, product core versus surface temperature, defrost cycles
or humidity latent load. It is honest about being a model: nothing in this
project claims it predicts a real trailer.
"""

from __future__ import annotations

from dataclasses import dataclass

from simulator.incidentforge.physics.params import ThermalParams

__all__ = ["ThermalState", "ThermalStep", "step_thermal"]


@dataclass(frozen=True, slots=True)
class ThermalState:
    """The simulated world's true thermal state.

    ``cargo_temp_c`` here is the *true* temperature. What a sensor reports may
    differ, and under a sensor fault it does. The two are kept strictly apart
    so that a faulty instrument can lie to the application exactly as it would
    in reality.
    """

    cargo_temp_c: float
    reefer_fuel_pct: float


@dataclass(frozen=True, slots=True)
class ThermalStep:
    """One integration step's outcome, including its intermediate terms.

    The terms are returned rather than discarded because they are what make a
    scenario explicable: "duty saturated at minute 112" is a far more useful
    statement than a temperature curve alone.
    """

    state: ThermalState
    duty_cycle: float
    cooling_output_w: float
    heat_ingress_w: float
    compressor_rpm: float
    saturated: bool


def step_thermal(
    state: ThermalState,
    params: ThermalParams,
    *,
    ambient_temp_c: float,
    setpoint_c: float,
    compressor_health: float,
    door_open: bool,
    dt_seconds: float,
) -> ThermalStep:
    """Advance the thermal state by one timestep.

    Args:
        state: Current true thermal state.
        params: Trailer and cargo thermal parameters.
        ambient_temp_c: Outside temperature for this step.
        setpoint_c: Temperature the refrigeration unit is controlling to.
        compressor_health: 1.0 is nominal, 0.0 is a completely failed unit.
            Degradation scales available cooling output.
        door_open: Whether a door is open, which raises envelope heat transfer.
        dt_seconds: Integration step. Must be far below the thermal time
            constant; 60 s against a time constant of hours is comfortable.

    Returns:
        The new state plus the intermediate terms that produced it.
    """
    if not 0.0 <= compressor_health <= 1.0:
        raise ValueError(f"compressor_health must be in [0, 1], got {compressor_health}")
    if dt_seconds <= 0:
        raise ValueError(f"dt_seconds must be positive, got {dt_seconds}")

    ua = params.ua_w_per_k
    if door_open:
        ua *= params.door_open_ua_multiplier

    heat_ingress_w = ua * (ambient_temp_c - state.cargo_temp_c)

    # Proportional control on the error above setpoint. Below setpoint the
    # unit idles rather than heating: these trailers cool only.
    error_k = state.cargo_temp_c - setpoint_c
    demanded_duty = max(0.0, error_k * params.controller_gain_per_k)

    # A demand below the cycling threshold means the unit switches off rather
    # than running continuously at a trickle.
    if demanded_duty < params.min_duty_cycle:
        demanded_duty = 0.0

    duty = min(1.0, demanded_duty)

    # An empty reefer tank means no cooling at all, however healthy the unit.
    fuel_available = 1.0 if state.reefer_fuel_pct > 0.0 else 0.0

    cooling_output_w = params.cooling_capacity_w * compressor_health * duty * fuel_available

    # Saturation is the condition that matters: the unit is doing everything
    # it can and is still losing ground.
    saturated = duty >= 1.0 and cooling_output_w < heat_ingress_w

    net_w = heat_ingress_w - cooling_output_w
    delta_t = (net_w / params.thermal_mass_j_per_k) * dt_seconds

    fuel_burn = params.fuel_burn_pct_per_hour_at_full_duty * duty * (dt_seconds / 3600.0)
    new_fuel = max(0.0, state.reefer_fuel_pct - fuel_burn)

    return ThermalStep(
        state=ThermalState(
            cargo_temp_c=state.cargo_temp_c + delta_t,
            reefer_fuel_pct=new_fuel,
        ),
        duty_cycle=duty,
        cooling_output_w=cooling_output_w,
        heat_ingress_w=heat_ingress_w,
        compressor_rpm=params.compressor_rpm_at_full_duty * duty * compressor_health,
        saturated=saturated,
    )


def equilibrium_temperature_c(
    params: ThermalParams,
    *,
    ambient_temp_c: float,
    compressor_health: float,
) -> float:
    """Temperature at which full cooling exactly balances heat ingress.

    Above this, the unit cannot hold the load at any duty. It is the analytic
    form of the saturation point, useful for reasoning about a scenario without
    simulating it, and for asserting in tests that a scenario is capable of
    breaching at all.

    Solves ``UA * (T_ambient - T) = capacity * health`` for ``T``.
    """
    if compressor_health <= 0.0:
        return ambient_temp_c
    max_cooling_w = params.cooling_capacity_w * compressor_health
    return ambient_temp_c - (max_cooling_w / params.ua_w_per_k)
