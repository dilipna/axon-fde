"""Scenario execution: turning a scenario definition into a telemetry stream.

The critical separation in this module is between what is **true** and what is
**reported**.

``TelemetryEvent`` contains only what a real sensor would emit. It is what the
application sees. ``GroundTruthFrame`` contains the true state of the simulated
world and is written to a separate file that only the benchmark reads.

That separation is not tidiness. If true cargo temperature leaked into the
telemetry stream, the risk model would train on the answer, and every
subsequent accuracy number would be meaningless. Under a sensor fault the two
diverge by design, and the application must be fooled exactly as it would be
in reality.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from simulator.incidentforge.faults.schedule import (
    compute_fault_effects,
    low_fuel_code,
)
from simulator.incidentforge.physics.ambient import ambient_temperature_c
from simulator.incidentforge.physics.params import params_for_cargo_class
from simulator.incidentforge.physics.thermal import (
    ThermalState,
    step_thermal,
)
from simulator.incidentforge.scenarios import Scenario

__all__ = [
    "GroundTruthFrame",
    "SimulationResult",
    "TelemetryEvent",
    "run_scenario",
]

#: One sample per minute. Matches the cadence the risk model's features are
#: built at, and is far below the thermal time constant, so forward Euler is
#: comfortably stable.
_TIMESTEP_SECONDS = 60.0

#: Runs start at a fixed wall-clock instant so that a given seed produces
#: byte-identical timestamps. A real clock would make runs unreproducible.
_EPOCH = datetime(2026, 7, 14, 10, 0, 0, tzinfo=UTC)

#: Sensor noise, one standard deviation in kelvin. Small enough not to obscure
#: the trend, large enough that a naive single-reading threshold is brittle and
#: a slope estimated over a window is genuinely more informative.
_SENSOR_NOISE_K = 0.06

_START_LAT, _START_LON = 41.8781, -87.6298  # Chicago
_END_LAT, _END_LON = 39.9612, -82.9988  # Columbus

_NOMINAL_SPEED_KPH = 92.0


class TelemetryEvent(BaseModel):
    """One telemetry sample, as the application receives it.

    Contains no ground truth. Everything here is something a sensor on a real
    trailer could plausibly report, including when it is wrong.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    vehicle_id: str
    shipment_id: str
    timestamp: datetime
    sequence: int = Field(ge=0, description="Minutes since the run began")

    cargo_temp_c: float = Field(description="As reported; may be wrong under a sensor fault")
    ambient_temp_c: float
    humidity_pct: float
    compressor_rpm: float
    reefer_status: str
    reefer_fuel_pct: float
    truck_fuel_pct: float
    door_state: str
    speed_kph: float
    route_delay_min: float
    minutes_to_destination: float
    latitude: float | None
    longitude: float | None
    fault_codes: list[str]


class GroundTruthFrame(BaseModel):
    """The true state of the simulated world at one minute.

    Written to a separate file that only the benchmark reads. Never exposed to
    the application, and never used as a model feature.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    sequence: int
    timestamp: datetime
    true_cargo_temp_c: float
    reported_cargo_temp_c: float
    sensor_error_c: float
    compressor_health: float
    duty_cycle: float
    cooling_output_w: float
    heat_ingress_w: float
    saturated: bool
    in_breach: bool


@dataclass(frozen=True, slots=True)
class SimulationResult:
    """A complete run: what the application sees, and what actually happened."""

    scenario_id: str
    seed: int
    events: list[TelemetryEvent]
    ground_truth: list[GroundTruthFrame]

    #: First minute at which the true cargo temperature left its permitted
    #: envelope, or None if it never did.
    actual_breach_minute: int | None

    #: First minute at which the *reported* temperature left the envelope.
    #: Under a sensor fault this can occur when no real breach happened, which
    #: is the trap the sensor-drift scenario sets.
    reported_breach_minute: int | None

    #: First minute at which the unit ran flat out and still lost ground.
    first_saturation_minute: int | None

    @property
    def sensor_lied(self) -> bool:
        """Whether reported and true breach status ever disagreed."""
        return (self.actual_breach_minute is None) != (self.reported_breach_minute is None)


def _position(fraction: float) -> tuple[float, float]:
    """Linear interpolation along the route. Good enough for a map pin."""
    clamped = min(1.0, max(0.0, fraction))
    return (
        _START_LAT + (_END_LAT - _START_LAT) * clamped,
        _START_LON + (_END_LON - _START_LON) * clamped,
    )


def _reefer_status(duty: float, health: float, fuel_pct: float) -> str:
    if fuel_pct <= 0.0:
        return "off"
    if health < 0.35:
        return "fault"
    if duty <= 0.0:
        return "standby"
    if duty >= 1.0:
        return "running"
    return "cycling"


def run_scenario(scenario: Scenario) -> SimulationResult:
    """Execute a scenario end to end.

    Deterministic: the same scenario and seed produce a byte-identical event
    stream. No wall-clock time, no unseeded randomness, no iteration over an
    unordered collection.

    The run applies **no intervention**. That is deliberate: the risk model's
    labels must describe what happens if nobody acts, so intervention effects
    are generated by separate runs and never enter the training set.
    """
    rng = np.random.default_rng(scenario.seed)
    params = params_for_cargo_class(scenario.cargo.cargo_class)

    setpoint_c = (scenario.cargo.permitted_min_c + scenario.cargo.permitted_max_c) / 2.0

    state = ThermalState(cargo_temp_c=setpoint_c, reefer_fuel_pct=95.0)
    truck_fuel_pct = 78.0

    events: list[TelemetryEvent] = []
    frames: list[GroundTruthFrame] = []

    actual_breach: int | None = None
    reported_breach: int | None = None
    first_saturation: int | None = None

    for minute in range(scenario.duration_minutes):
        effects = compute_fault_effects(scenario.injected_faults, minute)

        ambient = ambient_temperature_c(scenario.ambient, minute) + effects.ambient_bonus_c

        step = step_thermal(
            state,
            params,
            ambient_temp_c=ambient,
            setpoint_c=setpoint_c,
            compressor_health=effects.compressor_health,
            door_open=effects.door_open,
            dt_seconds=_TIMESTEP_SECONDS,
        )

        # Extra fuel drain from a leak or an overworked unit, beyond the burn
        # the thermal step already accounted for.
        extra_drain = (
            (effects.fuel_drain_multiplier - 1.0)
            * params.fuel_burn_pct_per_hour_at_full_duty
            * step.duty_cycle
            / 60.0
        )
        state = ThermalState(
            cargo_temp_c=step.state.cargo_temp_c,
            reefer_fuel_pct=max(0.0, step.state.reefer_fuel_pct - extra_drain),
        )

        true_temp = state.cargo_temp_c

        # --- what the sensor reports -------------------------------------
        if effects.sensor_stuck_at_c is not None:
            reported_temp = effects.sensor_stuck_at_c
        else:
            noise = float(rng.normal(0.0, _SENSOR_NOISE_K))
            reported_temp = true_temp + effects.sensor_offset_c + noise

        # --- breach bookkeeping -------------------------------------------
        lo, hi = scenario.cargo.permitted_min_c, scenario.cargo.permitted_max_c
        truly_in_breach = not (lo <= true_temp <= hi)
        reported_in_breach = not (lo <= reported_temp <= hi)

        if truly_in_breach and actual_breach is None:
            actual_breach = minute
        if reported_in_breach and reported_breach is None:
            reported_breach = minute
        if step.saturated and first_saturation is None:
            first_saturation = minute

        # --- remaining channels -------------------------------------------
        codes = sorted(effects.active_codes | low_fuel_code(state.reefer_fuel_pct))

        planned_arrival = scenario.duration_minutes + effects.route_delay_min
        minutes_remaining = max(0.0, planned_arrival - minute)
        progress = 1.0 - (minutes_remaining / planned_arrival) if planned_arrival else 1.0

        # Stationary while the delay is being incurred, and at the destination.
        delayed_now = effects.route_delay_min > 0.0
        speed = 0.0 if (delayed_now or minutes_remaining <= 0.0) else _NOMINAL_SPEED_KPH
        speed = max(0.0, speed + float(rng.normal(0.0, 3.0)) if speed > 0 else 0.0)

        lat, lon = _position(progress)
        truck_fuel_pct = max(0.0, truck_fuel_pct - 0.02)

        timestamp = _EPOCH + timedelta(minutes=minute)

        events.append(
            TelemetryEvent(
                vehicle_id=scenario.vehicle_id,
                shipment_id=scenario.shipment_id,
                timestamp=timestamp,
                sequence=minute,
                cargo_temp_c=round(reported_temp, 2),
                ambient_temp_c=round(ambient, 2),
                humidity_pct=round(float(np.clip(58.0 + rng.normal(0, 3), 0, 100)), 1),
                compressor_rpm=round(step.compressor_rpm, 1),
                reefer_status=_reefer_status(
                    step.duty_cycle, effects.compressor_health, state.reefer_fuel_pct
                ),
                reefer_fuel_pct=round(state.reefer_fuel_pct, 2),
                truck_fuel_pct=round(truck_fuel_pct, 2),
                door_state="open" if effects.door_open else "closed",
                speed_kph=round(speed, 1),
                route_delay_min=round(effects.route_delay_min, 1),
                minutes_to_destination=round(minutes_remaining, 1),
                latitude=round(lat, 5) if effects.gps_valid else None,
                longitude=round(lon, 5) if effects.gps_valid else None,
                fault_codes=codes,
            )
        )

        frames.append(
            GroundTruthFrame(
                sequence=minute,
                timestamp=timestamp,
                true_cargo_temp_c=round(true_temp, 4),
                reported_cargo_temp_c=round(reported_temp, 4),
                sensor_error_c=round(reported_temp - true_temp, 4),
                compressor_health=round(effects.compressor_health, 4),
                duty_cycle=round(step.duty_cycle, 4),
                cooling_output_w=round(step.cooling_output_w, 1),
                heat_ingress_w=round(step.heat_ingress_w, 1),
                saturated=step.saturated,
                in_breach=truly_in_breach,
            )
        )

    return SimulationResult(
        scenario_id=scenario.scenario_id,
        seed=scenario.seed,
        events=events,
        ground_truth=frames,
        actual_breach_minute=actual_breach,
        reported_breach_minute=reported_breach,
        first_saturation_minute=first_saturation,
    )
