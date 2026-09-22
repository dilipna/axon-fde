"""Scenario definitions for IncidentForge.

A scenario is a complete, seeded description of a world: a shipment, its cargo
contract, an ambient temperature profile, the faults injected into it, and the
ground truth about what actually happened.

Scenarios are the foundation of evaluation. Because the generator knows the
true root cause, whether a breach occurs and which evidence is decisive, root
cause accuracy, lead time and intervention selection become measurable rather
than assertable.

Validation here is deliberately strict. A scenario whose declared ground truth
does not match its injected faults would silently corrupt every benchmark
result computed from it, so such a scenario fails to load at all.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.app.domain.enums import ActionType, EvidenceSource, RiskCategory, RootCause

__all__ = [
    "AmbientProfile",
    "CargoSpec",
    "DataFault",
    "DataFaultType",
    "FaultType",
    "GroundTruth",
    "Scenario",
    "ScenarioPack",
    "load_pack",
    "load_scenario",
]


class FaultType(StrEnum):
    """Physical faults injected into the simulated world."""

    COMPRESSOR_DEGRADATION = "compressor_degradation"
    DOOR_LEFT_OPEN = "door_left_open"
    SENSOR_DRIFT = "sensor_drift"
    SENSOR_STUCK = "sensor_stuck"
    EXTREME_AMBIENT = "extreme_ambient"
    ROUTE_DELAY = "route_delay"
    REEFER_FUEL_EXHAUSTION = "reefer_fuel_exhaustion"
    GPS_DROPOUT = "gps_dropout"


class DataFaultType(StrEnum):
    """Faults in the data *about* the world rather than in the world itself.

    These are the faults that make cross-source reconciliation necessary. They
    are separated from physical faults because they do not change the cargo
    temperature; they change what the systems believe about it.
    """

    ERP_BOL_MISMATCH = "erp_bol_mismatch"
    #: The telemetry feed and a photographed control panel disagree about the
    #: same observation. Distinct from the physical `sensor_drift` fault: that
    #: one changes what the instrument reports, this one is a statement that
    #: two *sources* disagree, which is the thing a second modality can settle
    #: and the thing claim C6 is measured against.
    SENSOR_PANEL_DISAGREEMENT = "sensor_panel_disagreement"
    STALE_TELEMETRY = "stale_telemetry"
    DUPLICATE_EVENTS = "duplicate_events"
    OUT_OF_ORDER_EVENTS = "out_of_order_events"
    MISSING_MAINTENANCE_RECORD = "missing_maintenance_record"


class AmbientProfileType(StrEnum):
    CONSTANT = "constant"
    DIURNAL_MILD = "diurnal_mild"
    DIURNAL_HOT = "diurnal_hot"
    COLD = "cold"


#: Which physical fault produces which root cause. Used to verify that a
#: scenario's declared ground truth is consistent with what it actually injects.
FAULT_TO_ROOT_CAUSE: dict[FaultType, RootCause] = {
    FaultType.COMPRESSOR_DEGRADATION: RootCause.COMPRESSOR_DEGRADATION,
    FaultType.DOOR_LEFT_OPEN: RootCause.DOOR_LEFT_OPEN,
    FaultType.SENSOR_DRIFT: RootCause.SENSOR_MALFUNCTION,
    FaultType.SENSOR_STUCK: RootCause.SENSOR_MALFUNCTION,
    FaultType.EXTREME_AMBIENT: RootCause.ENVIRONMENTAL_HEAT,
    FaultType.ROUTE_DELAY: RootCause.ROUTE_DELAY,
    FaultType.REEFER_FUEL_EXHAUSTION: RootCause.REEFER_FUEL_EXHAUSTION,
    FaultType.GPS_DROPOUT: RootCause.DATA_INCONSISTENCY,
}

DATA_FAULT_TO_ROOT_CAUSE: dict[DataFaultType, RootCause] = {
    DataFaultType.ERP_BOL_MISMATCH: RootCause.INCORRECT_CARGO_CONFIGURATION,
    # Deliberately DATA_INCONSISTENCY rather than SENSOR_MALFUNCTION. Two
    # sources disagreeing says one of them is wrong, not which; concluding the
    # sensor is the broken one is the diagnosis, and a scenario that declared
    # it as ground truth would be handing the answer to the grader.
    DataFaultType.SENSOR_PANEL_DISAGREEMENT: RootCause.DATA_INCONSISTENCY,
    DataFaultType.STALE_TELEMETRY: RootCause.DATA_INCONSISTENCY,
    DataFaultType.DUPLICATE_EVENTS: RootCause.DATA_INCONSISTENCY,
    DataFaultType.OUT_OF_ORDER_EVENTS: RootCause.DATA_INCONSISTENCY,
    DataFaultType.MISSING_MAINTENANCE_RECORD: RootCause.DATA_INCONSISTENCY,
}


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CargoSpec(_Strict):
    """The cargo and its contractual temperature envelope."""

    cargo_class: str
    value_usd: float = Field(gt=0, description="Feeds the expected-value layer")
    permitted_min_c: float
    permitted_max_c: float

    @model_validator(mode="after")
    def _check_envelope(self) -> Self:
        if self.permitted_min_c >= self.permitted_max_c:
            raise ValueError(
                f"permitted_min_c ({self.permitted_min_c}) must be below "
                f"permitted_max_c ({self.permitted_max_c})"
            )
        return self


class AmbientProfile(_Strict):
    type: AmbientProfileType
    peak_c: float = Field(ge=-50, le=60)
    min_c: float = Field(default=10.0, ge=-50, le=60)

    @model_validator(mode="after")
    def _check_range(self) -> Self:
        if self.min_c > self.peak_c:
            raise ValueError(f"min_c ({self.min_c}) exceeds peak_c ({self.peak_c})")
        return self


class InjectedFault(_Strict):
    """A physical fault, with when it starts and how severe it becomes."""

    type: FaultType
    start_min: int = Field(ge=0)
    severity: float = Field(default=1.0, ge=0.0, le=1.0)
    ramp_min: int = Field(default=0, ge=0, description="Time to reach full severity")
    duration_min: int | None = Field(default=None, ge=1)
    # Fault-specific parameters, e.g. delay_min for ROUTE_DELAY.
    params: dict[str, float] = Field(default_factory=dict)


#: Which two sources each two-sided data fault puts in disagreement, and which
#: declared field carries each side's value.
#:
#: Held as a table rather than as branches in the grader, because the grader
#: has to build the two evidence records the reconciler will see, and a grader
#: that guessed which source said what would be measuring its own guess.
_CONFLICT_SIDES: dict[DataFaultType, tuple[tuple[str, str], tuple[str, str]]] = {
    DataFaultType.ERP_BOL_MISMATCH: (
        (EvidenceSource.SQL_LEGACY.value, "erp_value"),
        (EvidenceSource.DOCUMENT_EXTRACTION.value, "document_value"),
    ),
    DataFaultType.SENSOR_PANEL_DISAGREEMENT: (
        (EvidenceSource.TELEMETRY.value, "telemetry_value"),
        (EvidenceSource.VISUAL_INSPECTION.value, "panel_value"),
    ),
}


class DataFault(_Strict):
    """A disagreement between systems, injected deliberately."""

    type: DataFaultType
    field: str | None = None
    erp_value: float | str | None = None
    document_value: float | str | None = None
    #: The two sides of a sensor-vs-panel disagreement. Named for their sources
    #: rather than reusing `erp_value`/`document_value`: a field called
    #: `erp_value` holding what a photograph showed would be a lie in the one
    #: place a reader most needs to trust the name.
    telemetry_value: float | str | None = None
    panel_value: float | str | None = None
    params: dict[str, float] = Field(default_factory=dict)

    @property
    def sides(self) -> tuple[tuple[str, float | str], tuple[str, float | str]] | None:
        """The two disagreeing (source, value) pairs, or ``None`` if not two-sided.

        Not every data fault has two sides: a stale reading or a missing
        maintenance record is an absence, not a disagreement, and asking one of
        those for its sides gets ``None`` rather than an invented second value.
        """
        spec = _CONFLICT_SIDES.get(self.type)
        if spec is None:
            return None
        (left_source, left_field), (right_source, right_field) = spec
        left, right = getattr(self, left_field), getattr(self, right_field)
        if left is None or right is None:
            return None
        return (left_source, left), (right_source, right)

    @model_validator(mode="after")
    def _mismatch_needs_both_sides(self) -> Self:
        spec = _CONFLICT_SIDES.get(self.type)
        if spec is None:
            return self

        value_fields = [field_name for _, field_name in spec]
        missing = [name for name in ("field", *value_fields) if getattr(self, name) is None]
        if missing:
            raise ValueError(f"{self.type} requires {missing} so the conflict has two sides")

        left, right = (getattr(self, name) for name in value_fields)
        if left == right:
            raise ValueError(
                f"{self.type} declares identical values ({left!r}); that is not a disagreement"
            )
        return self

    @model_validator(mode="after")
    def _sides_belong_to_this_fault(self) -> Self:
        """A value declared on the wrong side is refused rather than ignored.

        `erp_value` on a sensor-vs-panel disagreement would be silently
        dropped, and the scenario would read as seeding a conflict it does not
        seed — which inflates C6's recall denominator with a case that was
        never actually built.
        """
        spec = _CONFLICT_SIDES.get(self.type)
        if spec is None:
            return self
        own = {field_name for _, field_name in spec}
        foreign = {
            name
            for names in _CONFLICT_SIDES.values()
            for _, name in names
            if name not in own and getattr(self, name) is not None
        }
        if foreign:
            raise ValueError(
                f"{self.type} carries {sorted(foreign)}, which belong to a different "
                "data fault; the value would be silently dropped"
            )
        return self


class GroundTruth(_Strict):
    """What actually happened. The basis for every diagnosis grader."""

    root_cause: RootCause
    contributing: list[RootCause] = Field(default_factory=list)
    breach_occurs: bool
    breach_at_min: int | None = Field(default=None, ge=0)
    correct_action: ActionType
    decisive_evidence: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_breach_consistency(self) -> Self:
        if self.breach_occurs and self.breach_at_min is None:
            raise ValueError("breach_occurs is true but breach_at_min is not set")
        if not self.breach_occurs and self.breach_at_min is not None:
            raise ValueError(f"breach_at_min={self.breach_at_min} set but breach_occurs is false")
        if self.root_cause in self.contributing:
            raise ValueError(f"{self.root_cause} is listed as both root cause and contributing")
        return self


class ExpectedBehaviour(_Strict):
    """What AxonBench expects the system to do. Benchmark expectations only."""

    risk_category: RiskCategory
    required_tools: list[str] = Field(default_factory=list)
    optional_tools: list[str] = Field(default_factory=list)
    forbidden_tools: list[str] = Field(default_factory=list)
    expected_evidence: list[str] = Field(default_factory=list)
    expected_documents: list[str] = Field(default_factory=list)
    approval_required: bool
    allowed_actions: list[ActionType] = Field(default_factory=list)
    forbidden_actions: list[ActionType] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_tool_and_action_sets_are_disjoint(self) -> Self:
        overlap = set(self.required_tools) & set(self.forbidden_tools)
        if overlap:
            raise ValueError(f"tools both required and forbidden: {sorted(overlap)}")
        action_overlap = set(self.allowed_actions) & set(self.forbidden_actions)
        if action_overlap:
            raise ValueError(f"actions both allowed and forbidden: {sorted(action_overlap)}")
        return self


class Scenario(_Strict):
    """A complete, reproducible world definition."""

    scenario_id: str = Field(pattern=r"^[a-z0-9_]+$")
    pack_version: str
    description: str
    seed: int = Field(ge=0)
    duration_minutes: int = Field(gt=0, le=2880)
    vehicle_id: str
    shipment_id: str
    cargo: CargoSpec
    ambient: AmbientProfile
    injected_faults: list[InjectedFault] = Field(default_factory=list)
    data_faults: list[DataFault] = Field(default_factory=list)
    ground_truth: GroundTruth
    expected: ExpectedBehaviour

    @model_validator(mode="after")
    def _faults_start_within_the_run(self) -> Self:
        for fault in self.injected_faults:
            if fault.start_min >= self.duration_minutes:
                raise ValueError(
                    f"{self.scenario_id}: fault {fault.type} starts at "
                    f"{fault.start_min}min, at or beyond the "
                    f"{self.duration_minutes}min run"
                )
        return self

    @model_validator(mode="after")
    def _breach_occurs_within_the_run(self) -> Self:
        gt = self.ground_truth
        if gt.breach_at_min is not None and gt.breach_at_min > self.duration_minutes:
            raise ValueError(
                f"{self.scenario_id}: breach at {gt.breach_at_min}min is beyond "
                f"the {self.duration_minutes}min run, so it is not observable"
            )
        return self

    @model_validator(mode="after")
    def _ground_truth_matches_injected_faults(self) -> Self:
        """The check that stops a mislabelled scenario corrupting the benchmark.

        Every declared cause must be traceable to something actually injected,
        and a scenario with no faults must declare no fault.
        """
        available = {FAULT_TO_ROOT_CAUSE[f.type] for f in self.injected_faults}
        available |= {DATA_FAULT_TO_ROOT_CAUSE[d.type] for d in self.data_faults}

        gt = self.ground_truth

        if not self.injected_faults and not self.data_faults:
            if gt.root_cause is not RootCause.NO_FAULT:
                raise ValueError(
                    f"{self.scenario_id}: declares root cause {gt.root_cause} but injects no faults"
                )
            if gt.breach_occurs:
                raise ValueError(f"{self.scenario_id}: declares a breach with no faults injected")
            return self

        if gt.root_cause is RootCause.NO_FAULT:
            raise ValueError(f"{self.scenario_id}: injects faults but declares NO_FAULT")

        if gt.root_cause not in available:
            raise ValueError(
                f"{self.scenario_id}: root cause {gt.root_cause} is not produced "
                f"by any injected fault. Available: {sorted(available)}"
            )

        unexplained = set(gt.contributing) - available
        if unexplained:
            raise ValueError(
                f"{self.scenario_id}: contributing causes {sorted(unexplained)} "
                f"are not produced by any injected fault. Available: {sorted(available)}"
            )
        return self


class ScenarioPack(_Strict):
    """A versioned collection of scenarios.

    The version is semver and is recorded on every benchmark run. A scenario's
    meaning must never change within a major version, or historical results
    stop being comparable.
    """

    pack_version: str
    description: str
    scenarios: dict[str, Scenario]

    @model_validator(mode="after")
    def _versions_agree(self) -> Self:
        mismatched = [
            sid for sid, s in self.scenarios.items() if s.pack_version != self.pack_version
        ]
        if mismatched:
            raise ValueError(
                f"scenarios declare a different pack_version than the pack "
                f"({self.pack_version}): {sorted(mismatched)}"
            )
        return self

    @property
    def breaching_scenarios(self) -> list[Scenario]:
        """Scenarios where a breach occurs - the population for lead time."""
        return [s for s in self.scenarios.values() if s.ground_truth.breach_occurs]


def load_scenario(path: Path) -> Scenario:
    return Scenario.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def load_pack(pack_dir: Path) -> ScenarioPack:
    """Load and validate every scenario in a pack directory.

    ``pack.yaml`` holds pack metadata; every other ``.yaml`` file is a scenario.
    """
    meta_path = pack_dir / "pack.yaml"
    if not meta_path.is_file():
        raise FileNotFoundError(f"Scenario pack metadata not found at {meta_path}")
    meta = yaml.safe_load(meta_path.read_text(encoding="utf-8"))

    scenarios: dict[str, Scenario] = {}
    for path in sorted(pack_dir.glob("*.yaml")):
        if path.name == "pack.yaml":
            continue
        scenario = load_scenario(path)
        if scenario.scenario_id in scenarios:
            raise ValueError(f"duplicate scenario_id {scenario.scenario_id}")
        if scenario.scenario_id != path.stem:
            raise ValueError(
                f"{path.name}: scenario_id {scenario.scenario_id!r} does not "
                "match the filename; they must agree so a scenario is findable"
            )
        scenarios[scenario.scenario_id] = scenario

    if not scenarios:
        raise ValueError(f"scenario pack at {pack_dir} contains no scenarios")

    return ScenarioPack.model_validate({**meta, "scenarios": scenarios})
