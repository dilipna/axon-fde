"""Physical parameters for the simulated fleet.

The thermal model is a lumped-capacitance approximation: the cargo is treated
as a single thermal mass exchanging heat with ambient through the trailer
envelope, opposed by the refrigeration unit.

These parameters are plausible rather than measured. They are stated here with
their reasoning so a reader can judge them, and they are calibrated so that the
simulation reproduces each scenario's declared ground truth (a test asserts
this). Nothing in this project claims they describe a specific real trailer.

Orders of magnitude, for orientation:

- A reefer trailer envelope has a heat transfer coefficient around 30-90 W/K
  depending on insulation condition, door seals and age.
- A high-value pharmaceutical load is physically small: a few pallets, not a
  full 20-tonne cargo. That gives it far less thermal inertia than a produce
  load, which is exactly why pharma excursions develop in hours rather than days.
- A trailer refrigeration unit delivers roughly 3-8 kW of cooling.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["TRAILER_CLASSES", "ThermalParams", "params_for_cargo_class"]


@dataclass(frozen=True, slots=True)
class ThermalParams:
    """Lumped-capacitance parameters for one trailer and cargo configuration."""

    #: Thermal mass of cargo plus interior air, in joules per kelvin.
    #: Equivalently `mass_kg * specific_heat_j_per_kg_k`. Larger means slower.
    thermal_mass_j_per_k: float

    #: Envelope heat transfer coefficient, watts per kelvin. Higher means a
    #: leakier trailer: more heat in per degree of difference with ambient.
    ua_w_per_k: float

    #: Refrigeration capacity at full health, in watts.
    cooling_capacity_w: float

    #: Multiplier applied to `ua_w_per_k` while a door is open. An open door
    #: adds convective infiltration far exceeding conduction through walls.
    door_open_ua_multiplier: float

    #: Proportional controller gain, duty cycle per kelvin of error. The unit
    #: ramps toward full output as cargo temperature rises above setpoint.
    controller_gain_per_k: float

    #: Duty cycle below which the unit cycles off rather than running
    #: continuously, which is what produces the characteristic saw-tooth.
    min_duty_cycle: float

    #: Compressor speed at full duty, used to derive the `compressor_rpm`
    #: telemetry channel.
    compressor_rpm_at_full_duty: float

    #: Reefer fuel consumption at full duty, in percent of tank per hour.
    fuel_burn_pct_per_hour_at_full_duty: float

    def time_constant_seconds(self) -> float:
        """Open-loop thermal time constant, C/UA.

        Useful as a sanity check: an integration step must be far smaller than
        this, and a scenario shorter than a fraction of it cannot show much
        thermal movement.
        """
        return self.thermal_mass_j_per_k / self.ua_w_per_k


#: Per-cargo-class parameters.
#:
#: The pharma figures are calibrated so that the flagship scenario breaches at
#: its declared minute; `tests/unit/test_physics.py` asserts that the
#: calibration still holds, so a parameter change that silently invalidates
#: every stored benchmark result fails the build instead.
TRAILER_CLASSES: dict[str, ThermalParams] = {
    # Small, high-value load. Low thermal mass, so it moves quickly once
    # cooling becomes insufficient. A degraded (not failed) unit produces the
    # slow ~1 K/hour climb that a threshold alarm cannot see coming.
    "pharma_2_8": ThermalParams(
        thermal_mass_j_per_k=2.4e6,  # ~700 kg of water-like product
        ua_w_per_k=95.0,  # an older trailer with tired door seals
        cooling_capacity_w=3200.0,
        door_open_ua_multiplier=6.0,
        controller_gain_per_k=0.55,
        min_duty_cycle=0.12,
        compressor_rpm_at_full_duty=2400.0,
        fuel_burn_pct_per_hour_at_full_duty=3.5,
    ),
    "vaccine_2_8": ThermalParams(
        thermal_mass_j_per_k=1.8e6,  # smaller still: very high value per kg
        ua_w_per_k=90.0,
        cooling_capacity_w=3200.0,
        door_open_ua_multiplier=6.0,
        controller_gain_per_k=0.6,
        min_duty_cycle=0.12,
        compressor_rpm_at_full_duty=2400.0,
        fuel_burn_pct_per_hour_at_full_duty=3.5,
    ),
    # Produce fills the trailer. Large thermal mass, so excursions develop
    # over many hours: forgiving, and correspondingly less dramatic.
    "fresh_0_4": ThermalParams(
        thermal_mass_j_per_k=2.8e7,
        ua_w_per_k=70.0,
        cooling_capacity_w=5000.0,
        door_open_ua_multiplier=5.0,
        controller_gain_per_k=0.4,
        min_duty_cycle=0.15,
        compressor_rpm_at_full_duty=2600.0,
        fuel_burn_pct_per_hour_at_full_duty=4.2,
    ),
    # Frozen runs a much larger gradient to ambient, so it loses ground fast
    # when the unit degrades, despite substantial thermal mass.
    "frozen_minus_18": ThermalParams(
        thermal_mass_j_per_k=2.2e7,
        ua_w_per_k=75.0,
        cooling_capacity_w=7000.0,
        door_open_ua_multiplier=5.0,
        controller_gain_per_k=0.35,
        min_duty_cycle=0.20,
        compressor_rpm_at_full_duty=2800.0,
        fuel_burn_pct_per_hour_at_full_duty=5.5,
    ),
}


def params_for_cargo_class(cargo_class: str) -> ThermalParams:
    try:
        return TRAILER_CLASSES[cargo_class]
    except KeyError:
        raise KeyError(
            f"No thermal parameters for cargo class {cargo_class!r}. "
            f"Known classes: {', '.join(sorted(TRAILER_CLASSES))}"
        ) from None
