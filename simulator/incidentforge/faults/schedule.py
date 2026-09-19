"""Fault scheduling: turning declared faults into per-minute effects.

A scenario declares *what* goes wrong and *when*. This module turns that into
the concrete effects acting on the simulation at a given minute: how healthy
the compressor is, whether a door is open, how much the sensor is lying by,
and which fault codes the unit is reporting.

Faults ramp rather than switching on. A compressor that fails instantly is
easy to detect and uninteresting; one that degrades over an hour produces the
slow, in-specification drift that the whole product exists to catch.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from simulator.incidentforge.scenarios import FaultType, InjectedFault

__all__ = ["FaultEffects", "compute_fault_effects", "severity_at"]

#: Fault codes a refrigeration unit raises, by condition.
#:
#: Note that no sensor fault produces a code. A drifting thermistor reports a
#: plausible number and the unit has no way to know it is wrong. That silence
#: is exactly why the sensor-drift scenario needs a photograph of the panel to
#: resolve, and why telemetry alone cannot distinguish it from a real excursion.
FAULT_CODE_COMPRESSOR = "AL17"
FAULT_CODE_DOOR = "AL02"
FAULT_CODE_LOW_FUEL = "AL31"

#: Compressor degradation is only severe enough to register a code once it has
#: lost a meaningful fraction of capacity. Below this it is invisible to the
#: unit's own diagnostics, which is the window the prediction is competing in.
_COMPRESSOR_CODE_THRESHOLD = 0.25

_LOW_FUEL_CODE_THRESHOLD_PCT = 15.0


@dataclass(frozen=True, slots=True)
class FaultEffects:
    """Everything the injected faults are doing at one moment."""

    #: 1.0 is a healthy unit; 0.0 is complete failure.
    compressor_health: float = 1.0

    #: Added to the *reported* cargo temperature. The true temperature is
    #: unaffected: this is the instrument lying, not the cargo warming.
    sensor_offset_c: float = 0.0

    #: When set, the sensor reports this fixed value regardless of reality.
    sensor_stuck_at_c: float | None = None

    door_open: bool = False

    #: Added to the ambient profile, representing conditions hotter than the
    #: seasonal norm the profile describes.
    ambient_bonus_c: float = 0.0

    route_delay_min: float = 0.0

    #: Multiplies reefer fuel burn. Above 1.0 represents a leak or a unit
    #: working far harder than its duty cycle suggests.
    fuel_drain_multiplier: float = 1.0

    gps_valid: bool = True

    active_codes: frozenset[str] = field(default_factory=frozenset)

    @property
    def sensor_is_faulty(self) -> bool:
        return self.sensor_offset_c != 0.0 or self.sensor_stuck_at_c is not None


def severity_at(fault: InjectedFault, minute: float) -> float:
    """How far a fault has progressed at a given minute.

    Returns 0.0 before the fault starts, ramps linearly to its declared
    severity over ``ramp_min``, then holds. A fault with a ``duration_min``
    stops abruptly at the end of that window, which models a door being closed
    or traffic clearing.
    """
    if minute < fault.start_min:
        return 0.0

    if fault.duration_min is not None and minute >= fault.start_min + fault.duration_min:
        return 0.0

    elapsed = minute - fault.start_min
    if fault.ramp_min <= 0:
        return fault.severity

    progress = min(1.0, elapsed / fault.ramp_min)
    return fault.severity * progress


def compute_fault_effects(faults: list[InjectedFault], minute: float) -> FaultEffects:
    """Combine every active fault into the effects acting at this minute.

    Where faults overlap, the more severe one wins for a given channel rather
    than the effects summing. Two independent causes of compressor degradation
    do not make a unit twice as broken.
    """
    compressor_health = 1.0
    sensor_offset = 0.0
    sensor_stuck_at: float | None = None
    door_open = False
    ambient_bonus = 0.0
    route_delay = 0.0
    fuel_multiplier = 1.0
    gps_valid = True
    codes: set[str] = set()

    for fault in faults:
        severity = severity_at(fault, minute)
        if severity <= 0.0:
            continue

        match fault.type:
            case FaultType.COMPRESSOR_DEGRADATION:
                compressor_health = min(compressor_health, 1.0 - severity)
                if severity >= _COMPRESSOR_CODE_THRESHOLD:
                    codes.add(FAULT_CODE_COMPRESSOR)

            case FaultType.SENSOR_DRIFT:
                # Severity 1.0 corresponds to a 6 K error, which is far outside
                # any plausible calibration tolerance and unambiguously a fault.
                sensor_offset = max(sensor_offset, severity * 6.0)

            case FaultType.SENSOR_STUCK:
                # The reading freezes at the scenario's declared value, or at a
                # comfortable mid-range number if none was given.
                sensor_stuck_at = fault.params.get("stuck_at_c", 5.0)

            case FaultType.DOOR_LEFT_OPEN:
                door_open = True
                codes.add(FAULT_CODE_DOOR)

            case FaultType.EXTREME_AMBIENT:
                # Severity 1.0 is 10 K above the seasonal profile: a heatwave,
                # not merely a warm afternoon.
                ambient_bonus = max(ambient_bonus, severity * 10.0)

            case FaultType.ROUTE_DELAY:
                route_delay = max(route_delay, fault.params.get("delay_min", 30.0))

            case FaultType.REEFER_FUEL_EXHAUSTION:
                fuel_multiplier = max(fuel_multiplier, 1.0 + severity * 8.0)

            case FaultType.GPS_DROPOUT:
                gps_valid = False

    return FaultEffects(
        compressor_health=compressor_health,
        sensor_offset_c=sensor_offset,
        sensor_stuck_at_c=sensor_stuck_at,
        door_open=door_open,
        ambient_bonus_c=ambient_bonus,
        route_delay_min=route_delay,
        fuel_drain_multiplier=fuel_multiplier,
        gps_valid=gps_valid,
        active_codes=frozenset(codes),
    )


def low_fuel_code(reefer_fuel_pct: float) -> frozenset[str]:
    """The low-fuel code, raised from the fuel state rather than a fault.

    Fuel exhaustion is a consequence of running hard, so the code follows the
    tank level rather than the declared fault, and appears even in scenarios
    that never injected a fuel fault.
    """
    if reefer_fuel_pct <= _LOW_FUEL_CODE_THRESHOLD_PCT:
        return frozenset({FAULT_CODE_LOW_FUEL})
    return frozenset()
