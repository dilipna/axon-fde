"""Turning the scenario pack into labelled feature rows.

One row per scenario per minute, built by the **same** `build_features` the
detector and the serving path use. Labels come from the simulator's ground
truth and only ever from there: `GroundTruthFrame` is benchmark-only (I8), and
this is benchmark code.

**Labels are counterfactual-clean.** The simulator applies no intervention, so
"leaves its envelope within the next 60 minutes" describes what happens if
nobody acts - which is the question the risk number answers.

**Which rows exist.**

- Rows start once a full feature window exists (minute 30). Before that
  `build_features` returns `None`, and this module does not invent a row.
- A row is dropped once the cargo has *truly* left its envelope: at that point
  the label is trivially 1 and the model would be graded on reading a
  thermometer, not on predicting anything.
- On a scenario that never breaches, every row is label 0. The run is complete,
  so there is no censoring: nothing further happens after the last minute.

**Regime.** The scenario's set of injected fault types. It is the unit of the
train/test split: the plan's rule is that a row-level split leaks through the
ODE's autocorrelation (adjacent minutes are near-copies), so whole regimes are
held out.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.app.domain.envelope import TemperatureEnvelope
from backend.app.domain.evidence import Evidence
from backend.app.domain.taxonomy import load_taxonomy
from backend.app.evidence.telemetry import TelemetryReading, reading_to_evidence
from backend.app.risk.features import DEFAULT_WINDOW_MINUTES, RiskFeatures, build_features
from backend.app.risk.trained import FEATURE_SETS, MODEL_HORIZON_MINUTES, model_row
from benchmarks.axonbench.graders.detection import DEFAULT_PACK_DIR, _to_reading
from simulator.incidentforge.generator import run_scenario
from simulator.incidentforge.scenarios import ScenarioPack, load_pack

__all__ = ["Row", "ScenarioMeta", "build_dataset", "regime_of"]

#: The columns the risk features read. Converting only these is a speed choice
#: with no effect on the result: the other columns are never looked at by
#: `build_features`.
_NEEDED_COLUMNS = (
    "cargo_temp_c",
    "compressor_rpm",
    "ambient_temp_c",
    "fault_codes",
    "minutes_to_destination",
    "door_state",
)


def regime_of(injected_fault_types: list[str]) -> str:
    return "+".join(sorted(injected_fault_types)) or "none"


@dataclass(frozen=True, slots=True)
class Row:
    scenario_id: str
    regime: str
    minute: int
    label: int
    features: RiskFeatures
    #: Model inputs by feature-set name, so one dataset serves every iteration.
    vectors: dict[str, list[float]]


@dataclass(frozen=True, slots=True)
class ScenarioMeta:
    scenario_id: str
    regime: str
    #: First minute the *true* temperature left the envelope, or ``None``.
    breach_minute: int | None
    duration_minutes: int


def _first_true_breach(scenario_frames: list[object]) -> int | None:
    for index, frame in enumerate(scenario_frames):
        if frame.in_breach:  # type: ignore[attr-defined]
            return index
    return None


def build_dataset(
    pack: ScenarioPack | None = None,
    *,
    horizon_minutes: int = MODEL_HORIZON_MINUTES,
    window_minutes: int = DEFAULT_WINDOW_MINUTES,
) -> tuple[list[Row], dict[str, ScenarioMeta]]:
    """Every labelled row in the pack, and per-scenario metadata."""
    active = pack or load_pack(DEFAULT_PACK_DIR)
    taxonomy = load_taxonomy()
    rows: list[Row] = []
    meta: dict[str, ScenarioMeta] = {}

    for scenario_id in sorted(active.scenarios):
        scenario = active.scenarios[scenario_id]
        result = run_scenario(scenario)
        regime = regime_of([fault.type.value for fault in scenario.injected_faults])
        breach = _first_true_breach(list(result.ground_truth))
        meta[scenario_id] = ScenarioMeta(scenario_id, regime, breach, scenario.duration_minutes)

        envelope = TemperatureEnvelope(
            minimum_c=scenario.cargo.permitted_min_c, maximum_c=scenario.cargo.permitted_max_c
        )
        per_minute: list[list[Evidence]] = []
        readings: list[TelemetryReading] = []
        for event in result.events:
            full = _to_reading(event)
            reading = TelemetryReading(
                vehicle_id=full.vehicle_id,
                shipment_id=full.shipment_id,
                timestamp=full.timestamp,
                sequence=full.sequence,
                measurements={c: full.measurements[c] for c in _NEEDED_COLUMNS},
            )
            readings.append(reading)
            per_minute.append(reading_to_evidence(reading, taxonomy=taxonomy))

        for minute in range(window_minutes, len(readings)):
            if breach is not None and minute >= breach:
                break
            window = [
                item for chunk in per_minute[minute - window_minutes : minute + 1] for item in chunk
            ]
            features = build_features(
                window,
                envelope=envelope,
                now=readings[minute].timestamp,
                window_minutes=window_minutes,
            )
            if features is None:
                continue
            label = int(breach is not None and breach - minute <= horizon_minutes)
            rows.append(
                Row(
                    scenario_id=scenario_id,
                    regime=regime,
                    minute=minute,
                    label=label,
                    features=features,
                    vectors={name: model_row(features, name) for name in FEATURE_SETS},
                )
            )
    return rows, meta
