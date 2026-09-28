"""C11: does the verifier's verdict match what actually happened?

Method pre-registered in `docs/evaluation/claims.md` (C11) before any
post-action trajectory existed.

For every breach scenario and each of three actions, IncidentForge simulates
the trajectory *after* the action (its `Intervention` hook): cold storage,
a unit swap, or the driver shutting the door. The shipped verifier,
`verification.outcome.evaluate_effect`, grades the **reported** readings over
the window the shipped action catalogue declares. The label is the same rule
over the simulator's **true** temperatures. Agreement is the headline; the
error that matters most - confirming an intervention that did not work - is a
separate companion, never averaged into it.

Deterministic, no database, no model. The simulator runs in process, as it
does for C1 and C6.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from backend.app.actions.effects import ExpectedEffect
from backend.app.actions.simulators.coldchain import ACTION_SPECS
from backend.app.domain.enums import ActionType
from backend.app.domain.envelope import TemperatureEnvelope
from backend.app.verification.outcome import Verdict, evaluate_effect
from benchmarks.axonbench.graders.base import GraderResult, Measurement, judge
from benchmarks.axonbench.graders.detection import DEFAULT_PACK_DIR
from simulator.incidentforge.generator import Intervention, SimulationResult, run_scenario
from simulator.incidentforge.scenarios import FaultType, Scenario, load_pack

__all__ = ["ACTIONS", "VerificationGrader"]

#: action -> (physical effect, minutes from the action until it takes hold).
#: 45 is the flagship's detour to the nearest cold store (CS-11); a replacement
#: unit takes longer to arrive; a phone call to the driver is quick.
ACTIONS: dict[ActionType, tuple[str, int]] = {
    ActionType.REROUTE_TO_COLD_STORAGE: ("cold_storage", 45),
    ActionType.TRAILER_SWAP: ("unit_swap", 60),
    ActionType.CONTACT_DRIVER: ("close_door", 20),
}

#: The action is taken this many minutes before the breach is reported.
LEAD_MINUTES = 30
EARLIEST_ACTION_MINUTE = 30


@dataclass(frozen=True, slots=True)
class Trajectory:
    scenario_id: str
    action: str
    sensor_lied: bool
    reported: Verdict
    truth: Verdict


def _readings(result: SimulationResult, *, truth: bool) -> list[tuple[datetime, float]]:
    if truth:
        return [(frame.timestamp, frame.true_cargo_temp_c) for frame in result.ground_truth]
    return [(event.timestamp, event.cargo_temp_c) for event in result.events]


def _trajectories(scenario: Scenario) -> tuple[list[Trajectory], int]:
    """Every graded trajectory for one scenario, and how many were excluded."""
    baseline = run_scenario(scenario)
    anchor = baseline.reported_breach_minute
    if anchor is None:
        anchor = baseline.actual_breach_minute
    if anchor is None:
        return [], len(ACTIONS)
    act_at = max(EARLIEST_ACTION_MINUTE, anchor - LEAD_MINUTES)

    envelope = TemperatureEnvelope(
        minimum_c=scenario.cargo.permitted_min_c, maximum_c=scenario.cargo.permitted_max_c
    )
    # The instrument itself is faulty: it drifts or sticks, and keeps doing so
    # after the action, because it travels with the cargo.
    lying = any(
        fault.type in (FaultType.SENSOR_DRIFT, FaultType.SENSOR_STUCK)
        for fault in scenario.injected_faults
    )
    graded: list[Trajectory] = []
    excluded = 0
    for action, (kind, delay) in ACTIONS.items():
        spec = ACTION_SPECS[action]
        if act_at + spec.window_minutes >= scenario.duration_minutes:
            excluded += 1
            continue
        result = run_scenario(
            scenario,
            intervention=Intervention(kind=kind, effective_minute=act_at + delay),  # type: ignore[arg-type]
        )
        start = result.events[act_at].timestamp
        end = result.events[act_at + spec.window_minutes].timestamp
        effect = ExpectedEffect(kind=spec.effect, within_minutes=spec.window_minutes)
        verdicts = [
            evaluate_effect(
                effect,
                readings=_readings(result, truth=truth),
                envelope=envelope,
                window_start=start,
                window_end=end,
            ).verdict
            for truth in (False, True)
        ]
        graded.append(
            Trajectory(
                scenario_id=scenario.scenario_id,
                action=action.value,
                sensor_lied=lying,
                reported=verdicts[0],
                truth=verdicts[1],
            )
        )
    return graded, excluded


@dataclass(frozen=True, slots=True)
class VerificationGrader:
    pack_dir: Path = DEFAULT_PACK_DIR
    claim_id: str = "C11"

    def trajectories(self) -> tuple[list[Trajectory], int]:
        pack = load_pack(self.pack_dir)
        graded: list[Trajectory] = []
        excluded = 0
        for scenario in pack.scenarios.values():
            if not scenario.ground_truth.breach_occurs:
                continue
            rows, skipped = _trajectories(scenario)
            graded.extend(rows)
            excluded += skipped
        return graded, excluded

    def measure(self) -> Measurement:
        rows, excluded = self.trajectories()
        return measure_trajectories(rows, excluded=excluded)

    def grade(self) -> GraderResult:
        return judge(self.claim_id, self.measure())


def measure_trajectories(rows: list[Trajectory], *, excluded: int = 0) -> Measurement:
    """The arithmetic, separate from the simulation so a test can feed it."""
    cases = len(rows)

    def rate(subset: list[Trajectory]) -> float:
        return sum(r.reported is r.truth for r in subset) / len(subset) if subset else 0.0

    false_confirm = [
        r for r in rows if r.reported is Verdict.CONFIRMED and r.truth is not Verdict.CONFIRMED
    ]
    truly_failed = [r for r in rows if r.truth is not Verdict.CONFIRMED]
    companions: dict[str, float] = {
        "agreement": rate(rows),
        "false_confirmations": float(len(false_confirm)),
        # Of the interventions that did not work, how often the verifier said
        # they did. The error that closes an incident on a load still at risk.
        "false_confirmation_rate": len(false_confirm) / len(truly_failed) if truly_failed else 0.0,
        "missed_recoveries": float(
            sum(r.reported is not Verdict.CONFIRMED and r.truth is Verdict.CONFIRMED for r in rows)
        ),
        "inconclusive": float(sum(r.reported is Verdict.INCONCLUSIVE for r in rows)),
        "truly_worked": float(sum(r.truth is Verdict.CONFIRMED for r in rows)),
        "honest_sensor_agreement": rate([r for r in rows if not r.sensor_lied]),
        "lying_sensor_agreement": rate([r for r in rows if r.sensor_lied]),
        "lying_sensor_trajectories": float(sum(r.sensor_lied for r in rows)),
        "excluded_window_past_end": float(excluded),
    }
    for action in sorted({r.action for r in rows}):
        companions[f"agreement_{action}"] = rate([r for r in rows if r.action == action])
    return Measurement(
        value=companions["agreement"],
        cases=cases,
        detail={
            "false_confirmations": [f"{r.scenario_id}/{r.action}" for r in false_confirm],
            "disagreements": [
                f"{r.scenario_id}/{r.action}: reported {r.reported.value}, truth {r.truth.value}"
                for r in rows
                if r.reported is not r.truth
            ],
        },
        companions=companions,
    )
