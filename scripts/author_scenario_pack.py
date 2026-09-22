"""Emit the scenario-pack YAML files from an explicit regime table.

`uv run python -m scripts.author_scenario_pack`

**Why a script and not sixty hand-written files.** Claim C1's method requires
at least forty true-breach scenarios and C6's requires twenty seeded conflicts.
Hand-deriving forty breach minutes from the thermal model is not something a
person does reliably, and a single transposed digit would silently corrupt
every lead-time number computed from the pack. So the *design* is authored --
the table below names every fault, severity, ambient profile, cargo class and
seed -- and the *consequence* is read out of the simulator.

**What is authored and what is measured, stated plainly.** Every row of the
table was chosen for a dataset property: which generative regime it exercises,
which cargo class carries it, and whether a breach should occur at all. Rows
were adjusted -- durations lengthened for high-thermal-mass cargo, severities
raised -- until the intended breach or non-breach materialised. **No row was
ever adjusted after looking at what a detector did with it.** That distinction
is the difference between a dataset and a result, and a pack tuned on detector
output would make C1 a measurement of this script.

**Regenerating is a re-baselining act, not a fix.** The golden digests in
`tests/unit/test_emitters.py` and the stored benchmark runs under
`benchmarks/results/published/` are computed from these files. If the physics
changes, re-running this script would quietly move every declared
`breach_at_min` to follow it, and `poe forge verify` -- whose whole job is to
catch exactly that drift -- would then pass. Change the physics and the
digests go red first; that is the intended order.

The three pack v1 scenarios are not emitted here. They are hand-written, they
are referenced by name in tests and documentation, and the flagship's ground
truth is quoted in the architecture notes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from simulator.incidentforge.generator import run_scenario
from simulator.incidentforge.scenarios import Scenario

PACK_DIR = Path(__file__).resolve().parents[1] / "data" / "scenarios" / "pack_v1"
PACK_VERSION = "1.1.0"

# ---------------------------------------------------------------------------
# Cargo and environment vocabulary
# ---------------------------------------------------------------------------

#: Contractual envelope and declared value per cargo class. The envelope is the
#: *authoritative* one -- what the signed Bill of Lading says -- because that is
#: what `resolve_envelope` returns once reconciliation has picked a winner, and
#: judging the physics against anything else would measure a different system.
CARGO: dict[str, tuple[float, float, float]] = {
    "pharma_2_8": (2.0, 8.0, 184000.0),
    "vaccine_2_8": (2.0, 8.0, 310000.0),
    "fresh_0_4": (0.0, 4.0, 48000.0),
    "frozen_minus_18": (-20.0, -15.0, 96000.0),
}

#: Run length per cargo class. Produce and frozen loads carry an order of
#: magnitude more thermal mass than a few pallets of vaccine, so a 300-minute
#: run cannot show an excursion developing at all -- the first grid attempt
#: produced zero breaches for both classes for exactly this reason. Fifteen
#: hours is a long haul, which is when these loads actually get into trouble.
DURATION: dict[str, int] = {
    "pharma_2_8": 300,
    "vaccine_2_8": 300,
    "fresh_0_4": 900,
    "frozen_minus_18": 900,
}

AMBIENT: dict[str, dict[str, Any]] = {
    "cool": {"type": "cold", "peak_c": 8.0, "min_c": 1.0},
    "mild": {"type": "diurnal_mild", "peak_c": 24.0, "min_c": 14.0},
    "hot": {"type": "diurnal_hot", "peak_c": 34.0, "min_c": 21.0},
    "vhot": {"type": "diurnal_hot", "peak_c": 38.0, "min_c": 26.0},
    "gulf": {"type": "diurnal_hot", "peak_c": 40.0, "min_c": 28.0},
    "scorch": {"type": "diurnal_hot", "peak_c": 42.0, "min_c": 30.0},
    "desert": {"type": "constant", "peak_c": 40.0, "min_c": 40.0},
}


# ---------------------------------------------------------------------------
# Fault constructors, so a table row reads as a sentence
# ---------------------------------------------------------------------------


def compressor(severity: float, ramp: int, start: int = 45) -> dict[str, Any]:
    return {
        "type": "compressor_degradation",
        "start_min": start,
        "severity": severity,
        "ramp_min": ramp,
    }


def door(start: int, minutes: int) -> dict[str, Any]:
    return {
        "type": "door_left_open",
        "start_min": start,
        "severity": 1.0,
        "duration_min": minutes,
    }


def heat(severity: float, start: int) -> dict[str, Any]:
    return {"type": "extreme_ambient", "start_min": start, "severity": severity}


def fuel(severity: float, start: int) -> dict[str, Any]:
    return {"type": "reefer_fuel_exhaustion", "start_min": start, "severity": severity}


def delay(minutes: float, start: int = 60) -> dict[str, Any]:
    return {
        "type": "route_delay",
        "start_min": start,
        "severity": 1.0,
        "params": {"delay_min": minutes},
    }


def drift(severity: float, start: int = 60, ramp: int = 45) -> dict[str, Any]:
    return {"type": "sensor_drift", "start_min": start, "severity": severity, "ramp_min": ramp}


def stuck(at_c: float, start: int = 60) -> dict[str, Any]:
    return {
        "type": "sensor_stuck",
        "start_min": start,
        "severity": 1.0,
        "params": {"stuck_at_c": at_c},
    }


# ---------------------------------------------------------------------------
# Seeded cross-source conflicts
# ---------------------------------------------------------------------------


def erp_bol(field_name: str, erp: float | str, bol: float | str) -> dict[str, Any]:
    """The ERP and the signed shipping document disagree about a contract term."""
    return {
        "type": "erp_bol_mismatch",
        "field": field_name,
        "erp_value": erp,
        "document_value": bol,
    }


def panel(field_name: str, telemetry: float | str, photographed: float | str) -> dict[str, Any]:
    """The telemetry feed and a photographed control panel disagree."""
    return {
        "type": "sensor_panel_disagreement",
        "field": field_name,
        "telemetry_value": telemetry,
        "panel_value": photographed,
    }


# ---------------------------------------------------------------------------
# The regime table
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Row:
    """One scenario's design. Everything here is authored; nothing is measured."""

    scenario_id: str
    cargo_class: str
    ambient: str
    faults: list[dict[str, Any]]
    root_cause: str
    correct_action: str
    risk: str
    #: Whether this row is meant to breach. Checked against the simulator, and
    #: a disagreement stops the emit rather than being written down.
    expect_breach: bool
    note: str
    contributing: list[str] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    duration: int | None = None
    allowed: list[str] = field(default_factory=list)
    forbidden: list[str] = field(default_factory=list)


# fmt: off
# The table below is formatted as a table on purpose: one scenario per
# three or four lines, so the spread of regimes, cargo classes and ambient
# profiles can be read down the columns. Black-style formatting expands each
# Row to fifteen lines and the pack's shape stops being visible, which is the
# only thing this file is for. Everything outside these markers is formatted
# normally.
_REROUTE = ["reroute_to_cold_storage", "trailer_swap", "contact_driver"]
_NO_BREACH_OK = ["continue_route", "do_nothing"]
_SENSOR = ["mark_for_inspection", "inspect_refrigeration", "contact_driver"]
_MAINT = ["escalate_maintenance", "inspect_refrigeration", "contact_driver"]

# --- Family A: compressor degradation (11 breach with the flagship, 2 control)
FAMILY_A: list[Row] = [
    Row("compressor_degradation_produce_01", "fresh_0_4", "hot", [compressor(0.75, 120)],
        "compressor_degradation", "reroute_to_cold_storage", "high", True,
        "A produce load loses cooling slowly; the thermal mass hides it for hours.",
        allowed=_REROUTE, forbidden=["continue_route", "do_nothing"]),
    Row("compressor_degradation_produce_02", "fresh_0_4", "vhot", [compressor(0.90, 90)],
        "compressor_degradation", "reroute_to_cold_storage", "high", True,
        "Severe capacity loss on produce under a heatwave.",
        conflicts=[erp_bol("permitted_temp_max_c", 6.0, 4.0)],
        allowed=_REROUTE, forbidden=["continue_route", "do_nothing"]),
    Row("compressor_degradation_frozen_01", "frozen_minus_18", "hot", [compressor(0.70, 120)],
        "compressor_degradation", "reroute_to_cold_storage", "high", True,
        "Frozen cargo runs a 50 K gradient, so lost capacity shows quickly despite the mass.",
        conflicts=[erp_bol("permitted_temp_min_c", -22.0, -20.0)],
        allowed=_REROUTE, forbidden=["continue_route", "do_nothing"]),
    Row("compressor_degradation_frozen_02", "frozen_minus_18", "vhot", [compressor(0.85, 90)],
        "compressor_degradation", "reroute_to_cold_storage", "critical", True,
        "Near-total capacity loss on frozen cargo in extreme ambient.",
        allowed=_REROUTE, forbidden=["continue_route", "do_nothing"]),
    Row("compressor_degradation_pharma_02", "pharma_2_8", "vhot",
        [compressor(0.55, 45), delay(40.0, 70)],
        "compressor_degradation", "reroute_to_cold_storage", "critical", True,
        "Fast degradation plus traffic. The margin that would have saved it is spent waiting.",
        contributing=["route_delay"],
        conflicts=[erp_bol("permitted_temp_max_c", 10.0, 8.0)],
        allowed=_REROUTE, forbidden=["continue_route", "do_nothing"]),
    Row("compressor_degradation_pharma_03", "pharma_2_8", "hot",
        [compressor(0.50, 60), delay(25.0, 90)],
        "compressor_degradation", "reroute_to_cold_storage", "high", True,
        "The flagship's regime at a different severity and a shorter delay.",
        contributing=["route_delay"],
        allowed=_REROUTE, forbidden=["continue_route", "do_nothing"]),
    Row("compressor_degradation_vaccine_01", "vaccine_2_8", "hot", [compressor(0.40, 60)],
        "compressor_degradation", "reroute_to_cold_storage", "high", True,
        "A vaccine load has less mass than pharma, so the same fault bites sooner.",
        conflicts=[panel("cargo_temp_c", 6.8, 5.1)],
        allowed=_REROUTE, forbidden=["continue_route", "do_nothing"]),
    # 0.85, not the 0.60 first written here: at 0.60 on a mild day the unit
    # still holds, which is the design of the control row further down. The two
    # were the same scenario with opposite labels until the emitter refused.
    Row("compressor_degradation_vaccine_02", "vaccine_2_8", "mild", [compressor(0.85, 30)],
        "compressor_degradation", "reroute_to_cold_storage", "high", True,
        "A sharp failure on a mild day: no environmental help, the unit simply cannot cope.",
        allowed=_REROUTE, forbidden=["continue_route", "do_nothing"]),
    Row("compressor_degradation_vaccine_03", "vaccine_2_8", "vhot", [compressor(0.30, 100)],
        "compressor_degradation", "reroute_to_cold_storage", "high", True,
        "The subtlest degradation in the pack, carried by a long ramp and hot ambient.",
        conflicts=[erp_bol("permitted_temp_min_c", 0.0, 2.0)],
        allowed=_REROUTE, forbidden=["continue_route", "do_nothing"]),
    Row("compressor_degradation_vaccine_04", "vaccine_2_8", "hot",
        [compressor(0.52, 50), delay(35.0, 80)],
        "compressor_degradation", "reroute_to_cold_storage", "critical", True,
        "Degradation and delay together on the highest-value load in the pack.",
        contributing=["route_delay"],
        allowed=_REROUTE, forbidden=["continue_route", "do_nothing"]),
    Row("compressor_wear_pharma_control_01", "pharma_2_8", "mild", [compressor(0.45, 70)],
        "compressor_degradation", "continue_route", "moderate", False,
        "Real wear the unit absorbs. Nothing leaves the envelope, and acting costs money.",
        allowed=[*_NO_BREACH_OK, "mark_for_inspection"],
        forbidden=["reroute_to_cold_storage", "trailer_swap"]),
    Row("compressor_wear_vaccine_control_01", "vaccine_2_8", "mild", [compressor(0.60, 30)],
        "compressor_degradation", "escalate_maintenance", "moderate", False,
        "The unit saturates and holds. A maintenance call is right; a reroute is not.",
        allowed=[*_MAINT, "continue_route"],
        forbidden=["reroute_to_cold_storage", "trailer_swap"]),
]

# --- Family B: door left open (10 breach, 2 control)
FAMILY_B: list[Row] = [
    Row("door_open_pharma_01", "pharma_2_8", "hot", [door(70, 40)],
        "door_left_open", "contact_driver", "high", True,
        "A door left open at a stop. Fast, obvious, and a reroute is the wrong answer.",
        allowed=["contact_driver", "inspect_refrigeration", "reroute_to_cold_storage"],
        forbidden=["do_nothing", "trailer_swap"]),
    Row("door_open_pharma_02", "pharma_2_8", "vhot", [door(90, 25)],
        "door_left_open", "contact_driver", "high", True,
        "Twenty-five minutes is enough in a heatwave.",
        conflicts=[panel("reefer_status", "running", "fault")],
        allowed=["contact_driver", "inspect_refrigeration", "reroute_to_cold_storage"],
        forbidden=["do_nothing", "trailer_swap"]),
    Row("door_open_pharma_03", "pharma_2_8", "mild", [door(60, 55)],
        "door_left_open", "contact_driver", "high", True,
        "A long opening on a mild day still breaches: infiltration dwarfs conduction.",
        allowed=["contact_driver", "inspect_refrigeration", "reroute_to_cold_storage"],
        forbidden=["do_nothing", "trailer_swap"]),
    Row("door_open_vaccine_01", "vaccine_2_8", "hot", [door(60, 35)],
        "door_left_open", "contact_driver", "critical", True,
        "The lowest thermal mass in the pack meets the largest heat ingress.",
        conflicts=[erp_bol("cargo_class", "pharma_2_8", "vaccine_2_8")],
        allowed=["contact_driver", "inspect_refrigeration", "reroute_to_cold_storage"],
        forbidden=["do_nothing", "trailer_swap"]),
    Row("door_open_vaccine_02", "vaccine_2_8", "mild", [door(100, 30)],
        "door_left_open", "contact_driver", "high", True,
        "A late opening leaves little run left to recover in.",
        conflicts=[panel("reefer_status", "running", "fault")],
        allowed=["contact_driver", "inspect_refrigeration", "reroute_to_cold_storage"],
        forbidden=["do_nothing", "trailer_swap"]),
    Row("door_open_vaccine_03", "vaccine_2_8", "vhot", [door(80, 22)],
        "door_left_open", "contact_driver", "critical", True,
        "Twenty-two minutes. The shortest breaching opening in the pack.",
        allowed=["contact_driver", "inspect_refrigeration", "reroute_to_cold_storage"],
        forbidden=["do_nothing", "trailer_swap"]),
    Row("door_open_produce_01", "fresh_0_4", "vhot", [door(120, 240)],
        "door_left_open", "contact_driver", "high", True,
        "A produce trailer needs hours of open door to breach, and gets them.",
        conflicts=[panel("cargo_temp_c", 3.1, 4.9)],
        allowed=["contact_driver", "inspect_refrigeration", "reroute_to_cold_storage"],
        forbidden=["do_nothing", "trailer_swap"]),
    Row("door_open_produce_02", "fresh_0_4", "hot", [door(150, 300)],
        "door_left_open", "contact_driver", "high", True,
        "A door seal failure rather than a forgotten latch: five hours of it.",
        allowed=["contact_driver", "inspect_refrigeration", "reroute_to_cold_storage"],
        forbidden=["do_nothing", "trailer_swap"]),
    Row("door_open_frozen_01", "frozen_minus_18", "hot", [door(120, 240)],
        "door_left_open", "contact_driver", "critical", True,
        "Frozen cargo and an open door: the gradient does the damage.",
        allowed=["contact_driver", "inspect_refrigeration", "reroute_to_cold_storage"],
        forbidden=["do_nothing", "trailer_swap"]),
    Row("door_open_frozen_02", "frozen_minus_18", "vhot", [door(150, 200)],
        "door_left_open", "contact_driver", "critical", True,
        "The same failure under a heatwave, and it arrives sooner.",
        conflicts=[erp_bol("permitted_temp_max_c", -12.0, -15.0)],
        allowed=["contact_driver", "inspect_refrigeration", "reroute_to_cold_storage"],
        forbidden=["do_nothing", "trailer_swap"]),
    Row("door_open_pharma_control_01", "pharma_2_8", "mild", [door(50, 15)],
        "door_left_open", "continue_route", "moderate", False,
        "A normal loading stop. Fifteen minutes, recovered, no breach.",
        allowed=[*_NO_BREACH_OK, "contact_driver"],
        forbidden=["reroute_to_cold_storage", "trailer_swap"]),
    Row("door_open_vaccine_control_01", "vaccine_2_8", "cool", [door(110, 12)],
        "door_left_open", "continue_route", "low", False,
        "A brief opening on a cold day. The unit never even works hard.",
        allowed=[*_NO_BREACH_OK, "contact_driver"],
        forbidden=["reroute_to_cold_storage", "trailer_swap"]),
]

# --- Family C: environmental heat (6 breach, 4 control)
FAMILY_C: list[Row] = [
    Row("ambient_heat_pharma_01", "pharma_2_8", "scorch", [heat(1.0, 40)],
        "environmental_heat", "reroute_to_cold_storage", "high", True,
        "A 42 C day with a heatwave on top. The unit is healthy and still loses.",
        allowed=_REROUTE, forbidden=["continue_route", "do_nothing"]),
    Row("ambient_heat_pharma_02", "pharma_2_8", "desert", [heat(1.0, 45)],
        "environmental_heat", "reroute_to_cold_storage", "high", True,
        "Constant 40 C with no overnight relief.",
        conflicts=[erp_bol("permitted_temp_max_c", 9.0, 8.0)],
        allowed=_REROUTE, forbidden=["continue_route", "do_nothing"]),
    Row("ambient_heat_pharma_03", "pharma_2_8", "gulf", [heat(0.9, 50), delay(45.0, 70)],
        "environmental_heat", "reroute_to_cold_storage", "high", True,
        "Heat plus a delay: the load spends its margin sitting still.",
        contributing=["route_delay"],
        allowed=_REROUTE, forbidden=["continue_route", "do_nothing"]),
    Row("ambient_heat_vaccine_01", "vaccine_2_8", "scorch", [heat(1.0, 45)],
        "environmental_heat", "reroute_to_cold_storage", "critical", True,
        "The same conditions against the pack's most valuable load.",
        conflicts=[panel("cargo_temp_c", 7.9, 6.2)],
        allowed=_REROUTE, forbidden=["continue_route", "do_nothing"]),
    Row("ambient_heat_vaccine_02", "vaccine_2_8", "gulf", [heat(1.0, 55)],
        "environmental_heat", "reroute_to_cold_storage", "high", True,
        "A Gulf-coast afternoon and a healthy unit that cannot keep up.",
        conflicts=[erp_bol("permitted_temp_max_c", 10.0, 8.0)],
        allowed=_REROUTE, forbidden=["continue_route", "do_nothing"]),
    Row("ambient_heat_vaccine_03", "vaccine_2_8", "vhot", [heat(0.9, 50), delay(30.0, 80)],
        "environmental_heat", "reroute_to_cold_storage", "high", True,
        "Heat and traffic, the most common real-world pairing in the customer brief.",
        contributing=["route_delay"],
        allowed=_REROUTE, forbidden=["continue_route", "do_nothing"]),
    Row("ambient_heat_pharma_control_01", "pharma_2_8", "hot", [heat(0.8, 50)],
        "environmental_heat", "continue_route", "moderate", False,
        "A hot afternoon the unit handles. The saturation is real; the breach is not.",
        allowed=_NO_BREACH_OK, forbidden=["reroute_to_cold_storage", "trailer_swap"]),
    Row("ambient_heat_produce_control_01", "fresh_0_4", "scorch", [heat(1.0, 60)],
        "environmental_heat", "continue_route", "low", False,
        "Produce has the mass and the unit has the capacity. Fifteen hours change nothing.",
        allowed=_NO_BREACH_OK, forbidden=["reroute_to_cold_storage", "trailer_swap"]),
    Row("ambient_heat_frozen_control_01", "frozen_minus_18", "scorch", [heat(1.0, 60)],
        "environmental_heat", "continue_route", "low", False,
        "A frozen unit carries enough capacity that ambient alone cannot reach it.",
        allowed=_NO_BREACH_OK, forbidden=["reroute_to_cold_storage", "trailer_swap"]),
    Row("ambient_heat_vaccine_control_01", "vaccine_2_8", "cool", [heat(0.4, 70)],
        "environmental_heat", "continue_route", "low", False,
        "A warm spell on a cold day. The quietest scenario in the pack.",
        allowed=_NO_BREACH_OK, forbidden=["reroute_to_cold_storage", "trailer_swap"]),
]

# --- Family D: reefer fuel exhaustion (8 breach, 4 control)
FAMILY_D: list[Row] = [
    Row("fuel_exhaustion_pharma_01", "pharma_2_8", "hot", [fuel(1.0, 30)],
        "reefer_fuel_exhaustion", "reroute_to_cold_storage", "critical", True,
        "The tank runs dry and cooling stops entirely. Nothing about the compressor is wrong.",
        allowed=[*_REROUTE, "escalate_maintenance"], forbidden=["continue_route", "do_nothing"]),
    Row("fuel_exhaustion_pharma_02", "pharma_2_8", "vhot", [fuel(0.9, 20)],
        "reefer_fuel_exhaustion", "reroute_to_cold_storage", "critical", True,
        "A leak from early in the run, under the worst ambient.",
        conflicts=[panel("reefer_status", "running", "off")],
        allowed=[*_REROUTE, "escalate_maintenance"], forbidden=["continue_route", "do_nothing"]),
    Row("fuel_exhaustion_vaccine_01", "vaccine_2_8", "hot", [fuel(1.0, 30)],
        "reefer_fuel_exhaustion", "reroute_to_cold_storage", "critical", True,
        "Fuel exhaustion against the lowest thermal mass in the pack.",
        allowed=[*_REROUTE, "escalate_maintenance"], forbidden=["continue_route", "do_nothing"]),
    Row("fuel_exhaustion_vaccine_02", "vaccine_2_8", "vhot", [fuel(1.0, 20)],
        "reefer_fuel_exhaustion", "reroute_to_cold_storage", "critical", True,
        "The fastest total cooling loss the simulator produces.",
        conflicts=[erp_bol("permitted_temp_min_c", 2.0, 3.0)],
        allowed=[*_REROUTE, "escalate_maintenance"], forbidden=["continue_route", "do_nothing"]),
    Row("fuel_exhaustion_produce_01", "fresh_0_4", "hot", [fuel(1.0, 20)],
        "reefer_fuel_exhaustion", "reroute_to_cold_storage", "high", True,
        "Produce survives a long time without cooling, and then does not.",
        conflicts=[panel("cargo_temp_c", 4.6, 2.9)],
        allowed=[*_REROUTE, "escalate_maintenance"], forbidden=["continue_route", "do_nothing"]),
    Row("fuel_exhaustion_produce_02", "fresh_0_4", "vhot", [fuel(0.95, 30)],
        "reefer_fuel_exhaustion", "reroute_to_cold_storage", "high", True,
        "The same failure with a heatwave shortening every margin.",
        allowed=[*_REROUTE, "escalate_maintenance"], forbidden=["continue_route", "do_nothing"]),
    Row("fuel_exhaustion_frozen_01", "frozen_minus_18", "hot", [fuel(1.0, 20)],
        "reefer_fuel_exhaustion", "reroute_to_cold_storage", "critical", True,
        "A frozen load with no cooling is the most expensive row in this table.",
        conflicts=[panel("cargo_temp_c", -18.2, -16.4)],
        allowed=[*_REROUTE, "escalate_maintenance"], forbidden=["continue_route", "do_nothing"]),
    Row("fuel_exhaustion_frozen_02", "frozen_minus_18", "vhot", [fuel(1.0, 30)],
        "reefer_fuel_exhaustion", "reroute_to_cold_storage", "critical", True,
        "The same, under heat, arriving sooner.",
        allowed=[*_REROUTE, "escalate_maintenance"], forbidden=["continue_route", "do_nothing"]),
    Row("fuel_drain_pharma_control_01", "pharma_2_8", "mild", [fuel(0.5, 60)],
        "reefer_fuel_exhaustion", "contact_driver", "moderate", False,
        "The tank drains faster than it should and still lasts the run. A call, not a reroute.",
        allowed=["contact_driver", "escalate_maintenance", "continue_route"],
        forbidden=["reroute_to_cold_storage", "trailer_swap"]),
    Row("fuel_drain_vaccine_control_01", "vaccine_2_8", "cool", [fuel(0.6, 60)],
        "reefer_fuel_exhaustion", "contact_driver", "moderate", False,
        "A cold day means low duty, so a drain that would be serious in summer is not.",
        allowed=["contact_driver", "escalate_maintenance", "continue_route"],
        forbidden=["reroute_to_cold_storage", "trailer_swap"]),
    Row("fuel_drain_pharma_control_02", "pharma_2_8", "mild", [fuel(0.85, 30)],
        "reefer_fuel_exhaustion", "escalate_maintenance", "moderate", False,
        "A serious leak that a mild day keeps survivable. The margin is luck, not design.",
        allowed=["contact_driver", "escalate_maintenance", "continue_route"],
        forbidden=["reroute_to_cold_storage", "trailer_swap"]),
    Row("fuel_drain_vaccine_control_02", "vaccine_2_8", "mild", [fuel(0.95, 25)],
        "reefer_fuel_exhaustion", "escalate_maintenance", "moderate", False,
        "Nearly the worst leak in the table, on the day that forgives it.",
        allowed=["contact_driver", "escalate_maintenance", "continue_route"],
        forbidden=["reroute_to_cold_storage", "trailer_swap"]),
]

# --- Family E: an instrument that lies while something real happens (5 breach, 6 control)
FAMILY_E: list[Row] = [
    Row("drifting_sensor_real_breach_01", "pharma_2_8", "hot", [compressor(0.55, 60), drift(0.4)],
        "compressor_degradation", "reroute_to_cold_storage", "critical", True,
        "A real excursion the instrument overstates. The alarm is right for the wrong reason.",
        contributing=["sensor_malfunction"],
        conflicts=[panel("cargo_temp_c", 8.4, 6.1)],
        allowed=[*_REROUTE, "mark_for_inspection"], forbidden=["continue_route", "do_nothing"]),
    Row("stuck_sensor_real_breach_01", "pharma_2_8", "vhot", [compressor(0.60, 60), stuck(5.0)],
        "compressor_degradation", "reroute_to_cold_storage", "critical", True,
        "The reading freezes at 5 C while the cargo climbs past 8. A threshold alarm never fires.",
        contributing=["sensor_malfunction"],
        conflicts=[panel("cargo_temp_c", 5.0, 9.3)],
        allowed=[*_REROUTE, "mark_for_inspection"], forbidden=["continue_route", "do_nothing"]),
    Row("drifting_sensor_real_breach_02", "vaccine_2_8", "hot", [compressor(0.50, 60), drift(0.5)],
        "compressor_degradation", "reroute_to_cold_storage", "critical", True,
        "Degradation and drift together: the excursion is real and the number is not.",
        contributing=["sensor_malfunction"],
        allowed=[*_REROUTE, "mark_for_inspection"], forbidden=["continue_route", "do_nothing"]),
    Row("stuck_sensor_real_breach_02", "vaccine_2_8", "mild", [compressor(0.65, 60), stuck(5.0)],
        "compressor_degradation", "reroute_to_cold_storage", "critical", True,
        "A frozen reading on a mild day. Silence from the instrument is the whole problem.",
        contributing=["sensor_malfunction"],
        conflicts=[erp_bol("cargo_class", "fresh_0_4", "vaccine_2_8")],
        allowed=[*_REROUTE, "mark_for_inspection"], forbidden=["continue_route", "do_nothing"]),
    Row("stuck_sensor_real_breach_03", "pharma_2_8", "hot", [compressor(0.58, 50), stuck(6.5)],
        "compressor_degradation", "reroute_to_cold_storage", "critical", True,
        "Stuck at 6.5 C -- inside the envelope, so the reading never even looks wrong.",
        contributing=["sensor_malfunction"],
        allowed=[*_REROUTE, "mark_for_inspection"], forbidden=["continue_route", "do_nothing"]),
    Row("drifting_sensor_control_01", "pharma_2_8", "mild", [drift(0.5)],
        "sensor_malfunction", "mark_for_inspection", "moderate", False,
        "The pack v1 sensor-drift case at a second seed. Flag the sensor, keep the cargo moving.",
        conflicts=[panel("cargo_temp_c", 8.1, 4.4)],
        allowed=_SENSOR, forbidden=["reroute_to_cold_storage", "trailer_swap"]),
    Row("drifting_sensor_control_02", "vaccine_2_8", "hot", [drift(0.6)],
        "sensor_malfunction", "mark_for_inspection", "moderate", False,
        "A larger drift on a hotter day. Still nothing wrong with the cargo.",
        allowed=_SENSOR, forbidden=["reroute_to_cold_storage", "trailer_swap"]),
    Row("drifting_sensor_control_03", "frozen_minus_18", "mild", [drift(0.5)],
        "sensor_malfunction", "mark_for_inspection", "moderate", False,
        "Sensor drift on frozen cargo, where a 3 K error is a smaller fraction of the gradient.",
        conflicts=[panel("cargo_temp_c", -14.1, -17.8)],
        allowed=_SENSOR, forbidden=["reroute_to_cold_storage", "trailer_swap"]),
    Row("stuck_sensor_control_01", "fresh_0_4", "hot", [stuck(2.0)],
        "sensor_malfunction", "mark_for_inspection", "moderate", False,
        "A frozen reading on a healthy load. The silence is the only symptom.",
        allowed=_SENSOR, forbidden=["reroute_to_cold_storage", "trailer_swap"]),
    Row("normal_produce_run_01", "fresh_0_4", "hot", [],
        "no_fault", "continue_route", "low", False,
        "A healthy fifteen-hour produce haul. Nothing happens, and that is the measurement.",
        allowed=_NO_BREACH_OK,
        forbidden=["reroute_to_cold_storage", "trailer_swap", "escalate_maintenance"]),
    Row("normal_frozen_run_01", "frozen_minus_18", "mild", [],
        "no_fault", "continue_route", "low", False,
        "A healthy frozen haul, for the same reason.",
        allowed=_NO_BREACH_OK,
        forbidden=["reroute_to_cold_storage", "trailer_swap", "escalate_maintenance"]),
]

# fmt: on

ROWS: list[Row] = FAMILY_A + FAMILY_B + FAMILY_C + FAMILY_D + FAMILY_E


# ---------------------------------------------------------------------------
# Emission
# ---------------------------------------------------------------------------

#: Tools every scenario's assessment must reach for, whatever the outcome.
#: Concluding that nothing is wrong still requires looking.
_REQUIRED_TOOLS = ["get_shipment", "get_recent_telemetry", "calculate_risk"]
_OPTIONAL_TOOLS = ["get_cargo_requirements", "get_maintenance_history", "get_weather"]
#: The executor is never a tool the agent may call. Execution happens only
#: through an approved action, which is invariant I5.
_FORBIDDEN_TOOLS = ["execute_approved_action"]


def _seed_for(scenario_id: str) -> int:
    """A stable seed derived from the scenario's own name.

    Derived rather than assigned from a counter so that inserting a row in the
    middle of the table does not renumber -- and silently re-record -- every
    scenario after it.
    """
    return 20260000 + sum((i + 1) * ord(ch) for i, ch in enumerate(scenario_id)) % 900000


def _fleet_ids(scenario_id: str) -> tuple[str, str]:
    digest = _seed_for(scenario_id)
    return f"AX-{digest % 900 + 100:03d}", f"SH-{digest % 9000 + 1000:04d}"


def to_payload(row: Row, *, breach_at: int | None) -> dict[str, Any]:
    vehicle_id, shipment_id = _fleet_ids(row.scenario_id)
    lo, hi, value = CARGO[row.cargo_class]
    return {
        "scenario_id": row.scenario_id,
        "pack_version": PACK_VERSION,
        "description": row.note,
        "seed": _seed_for(row.scenario_id),
        "duration_minutes": row.duration or DURATION[row.cargo_class],
        "vehicle_id": vehicle_id,
        "shipment_id": shipment_id,
        "cargo": {
            "cargo_class": row.cargo_class,
            "value_usd": value,
            "permitted_min_c": lo,
            "permitted_max_c": hi,
        },
        "ambient": AMBIENT[row.ambient],
        "injected_faults": row.faults,
        "data_faults": row.conflicts,
        "ground_truth": {
            "root_cause": row.root_cause,
            "contributing": row.contributing,
            "breach_occurs": breach_at is not None,
            "breach_at_min": breach_at,
            "correct_action": row.correct_action,
            "decisive_evidence": [],
        },
        "expected": {
            "risk_category": row.risk,
            "required_tools": list(_REQUIRED_TOOLS),
            "optional_tools": list(_OPTIONAL_TOOLS),
            "forbidden_tools": list(_FORBIDDEN_TOOLS),
            "expected_evidence": [],
            "expected_documents": [],
            "approval_required": breach_at is not None,
            "allowed_actions": row.allowed,
            "forbidden_actions": row.forbidden,
        },
    }


_HEADER = (
    "# Emitted by scripts/author_scenario_pack.py. Edit the table there, not\n"
    "# this file: `breach_at_min` below is read out of the thermal model, and a\n"
    "# hand-edited copy would declare a ground truth the physics does not\n"
    "# produce. Regenerating is a deliberate re-baselining act -- see the\n"
    "# module docstring and the golden digests in tests/unit/test_emitters.py.\n"
    "#\n"
)


def emit() -> int:
    written = 0
    mismatches: list[str] = []
    for row in ROWS:
        payload = to_payload(row, breach_at=None)
        # Built once with no declared breach so the model validates, run, and
        # then rebuilt with whatever the physics actually did. The declared
        # ground truth is never allowed to be an assertion about the physics
        # that the physics has not been asked to confirm.
        probe = Scenario.model_validate(payload)
        result = run_scenario(probe)
        breach_at = result.actual_breach_minute

        if (breach_at is not None) != row.expect_breach:
            # Collected rather than raised on the first one. A design pass that
            # can only learn about one bad row per run turns into a dozen
            # rebuild cycles, and the temptation at cycle nine is to flip
            # `expect_breach` to match instead of fixing the design.
            mismatches.append(
                f"{row.scenario_id}: the table expects "
                f"{'a breach' if row.expect_breach else 'no breach'}; the simulation "
                f"produced {f'one at minute {breach_at}' if breach_at else 'none'}"
            )
            continue

        final = Scenario.model_validate(to_payload(row, breach_at=breach_at))
        body = yaml.safe_dump(
            final.model_dump(mode="json"), sort_keys=False, allow_unicode=True, width=88
        )
        (PACK_DIR / f"{row.scenario_id}.yaml").write_text(
            _HEADER + f"# {row.note}\n\n" + body, encoding="utf-8"
        )
        written += 1

    if mismatches:
        raise SystemExit(
            "The physics disagrees with the table for "
            f"{len(mismatches)} row(s). Fix each row's design; do not write down "
            "the number the physics gave, and do not flip expect_breach to match "
            "it.\n  " + "\n  ".join(mismatches)
        )
    return written


if __name__ == "__main__":
    count = emit()
    breaches = sum(1 for row in ROWS if row.expect_breach)
    conflicts = sum(len(row.conflicts) for row in ROWS)
    print(f"Wrote {count} scenarios to {PACK_DIR}")
    print(f"  {breaches} breach, {count - breaches} control, {conflicts} seeded conflicts")
