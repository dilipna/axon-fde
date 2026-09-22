"""The two quality claims that scenario pack v1.1.0 unblocked.

C1 (lead time) and C6 (cross-source contradiction detection) are graded here,
both by running the shipped code over the shipped scenario pack. Neither
consults a model and neither opens a connection: the simulator runs in
process, the detectors are pure, and `reconcile` is pure, which is what lets
the AxonBench CI job measure them with no database and no API key.

**Lead time is never emitted alone.** `claims.md` states that presenting C1
without its false-alarm rate is a misuse of the claim, because a detector that
alerts constantly has unbounded lead time and no value. The false-alarm rate
therefore rides in `companions`, computed at the same operating threshold,
over the pack's twenty control scenarios -- a population that exists precisely
so the rate is a measurement rather than an artifact of a dataset with nothing
to falsely alarm on.

**Both detectors' false-alarm rates are reported.** Quoting the predictive
arm's rate beside a baseline whose rate is silently assumed to be zero would
be the same misuse one level down. The threshold alarm false-alarms too, on
every scenario where the instrument reports a breach that never happened.

**A second lead-time figure, and why it is not optional.** The predictive
detector fires during the start-of-run settling transient on scenarios that
begin in hot ambient: the load starts at setpoint, a proportional controller
needs steady-state error to produce output, so the cargo genuinely climbs for
half an hour before levelling off, and a linear extrapolation cannot tell that
curve from a slow excursion. On those scenarios the alert precedes the causal
fault, so the "lead time" is not skill. Both medians are reported and the
subset is named, because a single headline number here would be quietly wrong
in a way nobody downstream could detect.
"""

from __future__ import annotations

import statistics
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from backend.app.domain.enums import EvidenceSource, Modality
from backend.app.domain.envelope import TemperatureEnvelope
from backend.app.domain.evidence import EntityRef, Evidence, ObservationValue, Provenance
from backend.app.domain.taxonomy import Taxonomy, load_taxonomy
from backend.app.evidence.reconciliation import reconcile
from backend.app.evidence.telemetry import TELEMETRY_COLUMNS, TelemetryReading
from backend.app.incidents.detection import BaselineDetector, Detector, PredictiveDetector
from backend.app.incidents.sweep import first_detection
from backend.app.risk.service import SlopeRiskService
from benchmarks.axonbench.claims import ClaimStatus
from benchmarks.axonbench.graders.base import GraderResult, Measurement, judge
from simulator.incidentforge.generator import TelemetryEvent, run_scenario
from simulator.incidentforge.scenarios import Scenario, ScenarioPack, load_pack

__all__ = ["DEFAULT_PACK_DIR", "ConflictGrader", "LeadTimeGrader"]

DEFAULT_PACK_DIR = Path(__file__).resolve().parents[3] / "data" / "scenarios" / "pack_v1"


def _to_reading(event: TelemetryEvent) -> TelemetryReading:
    """A simulated event as the telemetry adapter's reading.

    The adapter's own path is Parquet on disk, and going through it here would
    make the grader depend on `poe forge run-all` having been run -- which the
    AxonBench CI job deliberately does not do. The column vocabulary is what is
    stable, so the conversion uses `TELEMETRY_COLUMNS` rather than a second
    hand-written list that could drift from it.
    """
    return TelemetryReading(
        vehicle_id=event.vehicle_id,
        shipment_id=event.shipment_id,
        timestamp=event.timestamp,
        sequence=event.sequence,
        measurements={
            column: getattr(event, "fault_codes" if column == "fault_codes" else column)
            for column in TELEMETRY_COLUMNS
        },
    )


def _envelope_for(scenario: Scenario) -> TemperatureEnvelope:
    """The envelope the detectors judge against.

    The cargo block holds the *authoritative* limits -- what the signed Bill of
    Lading says -- which is what `resolve_envelope` returns once reconciliation
    has applied the taxonomy's authority order. Using the ERP's number instead
    would measure a system nobody ships: on the flagship it is 10 C, and the
    baseline detector never fires at all.
    """
    return TemperatureEnvelope(
        minimum_c=scenario.cargo.permitted_min_c,
        maximum_c=scenario.cargo.permitted_max_c,
    )


def _fault_onset(scenario: Scenario) -> int | None:
    """The earliest minute at which anything was injected, or ``None``."""
    if not scenario.injected_faults:
        return None
    return min(fault.start_min for fault in scenario.injected_faults)


@dataclass(frozen=True, slots=True)
class _ScenarioOutcome:
    """What both detectors did on one scenario."""

    scenario_id: str
    breach_occurs: bool
    breach_at_min: int | None
    fault_onset_min: int | None
    baseline_min: int | None
    axon_min: int | None

    @property
    def lead_time(self) -> int | None:
        """Minutes of warning, or ``None`` when one of the two never fired.

        A scenario where only one arm fired has no lead time to report. It is
        counted and named rather than dropped, because dropping the ones where
        the threshold alarm stayed silent -- the stuck-sensor scenarios, where
        the predictive arm wins most decisively -- would bias the median
        downward, and dropping the ones the predictive arm missed would bias it
        up. Both exclusions are reported as companions.
        """
        if self.baseline_min is None or self.axon_min is None:
            return None
        return self.baseline_min - self.axon_min

    @property
    def alerted_before_the_fault_started(self) -> bool:
        return (
            self.axon_min is not None
            and self.fault_onset_min is not None
            and self.axon_min < self.fault_onset_min
        )


def _run_pack(
    pack: ScenarioPack,
    *,
    baseline_factory: Callable[[], Detector],
    axon_factory: Callable[[], Detector],
    taxonomy: Taxonomy,
) -> list[_ScenarioOutcome]:
    """Both detectors over every scenario, on identical input."""
    outcomes: list[_ScenarioOutcome] = []
    for scenario_id in sorted(pack.scenarios):
        scenario = pack.scenarios[scenario_id]
        result = run_scenario(scenario)
        readings = [_to_reading(event) for event in result.events]
        envelope = _envelope_for(scenario)

        baseline = first_detection(
            readings, detector=baseline_factory(), envelope=envelope, taxonomy=taxonomy
        )
        axon = first_detection(
            readings, detector=axon_factory(), envelope=envelope, taxonomy=taxonomy
        )
        outcomes.append(
            _ScenarioOutcome(
                scenario_id=scenario_id,
                breach_occurs=scenario.ground_truth.breach_occurs,
                breach_at_min=scenario.ground_truth.breach_at_min,
                fault_onset_min=_fault_onset(scenario),
                baseline_min=None if baseline is None else baseline[1],
                axon_min=None if axon is None else axon[1],
            )
        )
    return outcomes


def _rate(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


@dataclass(frozen=True, slots=True)
class LeadTimeGrader:
    """C1: the predictive arm raises an incident before the threshold alarm.

    Both arms are swept over the identical reading stream by
    `incidents.sweep`, which is the same loop `ScenarioReplay` runs against a
    database. That shared loop is deliberate: a grader with its own copy would
    eventually measure a detector slightly different from the one that ships.
    """

    claim_id: str = "C1"
    pack_dir: Path = DEFAULT_PACK_DIR
    #: Injectable for one reason: a grader that cannot be made to fail has not
    #: been shown to work. Handing this a detector that fires on the first
    #: reading is how the false-alarm rate is proven to be able to move.
    axon_factory: Callable[[], Detector] = lambda: PredictiveDetector(SlopeRiskService())
    baseline_factory: Callable[[], Detector] = BaselineDetector

    def measure(self) -> Measurement:
        taxonomy = load_taxonomy()
        pack = load_pack(self.pack_dir)
        outcomes = _run_pack(
            pack,
            baseline_factory=self.baseline_factory,
            axon_factory=self.axon_factory,
            taxonomy=taxonomy,
        )

        breaches = [o for o in outcomes if o.breach_occurs]
        controls = [o for o in outcomes if not o.breach_occurs]

        leads = [o.lead_time for o in breaches if o.lead_time is not None]
        # The subset whose warning began after the thing it is warning about.
        honest = [
            o.lead_time
            for o in breaches
            if o.lead_time is not None and not o.alerted_before_the_fault_started
        ]

        median = statistics.median(leads) if leads else 0.0
        quartiles = statistics.quantiles(leads, n=4) if len(leads) >= 2 else None
        iqr = (quartiles[2] - quartiles[0]) if quartiles else 0.0

        axon_false_alarms = sum(1 for o in controls if o.axon_min is not None)
        baseline_false_alarms = sum(1 for o in controls if o.baseline_min is not None)

        return Measurement(
            value=float(median),
            cases=len(breaches),
            companions={
                "median_lead_time_min": float(median),
                "lead_time_iqr_min": float(iqr),
                # The number C1 may never be quoted without.
                "false_alarm_rate": _rate(axon_false_alarms, len(controls)),
                "baseline_false_alarm_rate": _rate(baseline_false_alarms, len(controls)),
                "axon_detection_rate": _rate(
                    sum(1 for o in breaches if o.axon_min is not None), len(breaches)
                ),
                "baseline_detection_rate": _rate(
                    sum(1 for o in breaches if o.baseline_min is not None), len(breaches)
                ),
                "median_lead_time_after_fault_onset_min": float(
                    statistics.median(honest) if honest else 0.0
                ),
                "alerts_preceding_fault_onset_rate": _rate(
                    sum(1 for o in breaches if o.alerted_before_the_fault_started), len(breaches)
                ),
                "lead_time_sample_size": float(len(leads)),
            },
            detail={
                "pack_version": pack.pack_version,
                "breach_scenarios": len(breaches),
                "control_scenarios": len(controls),
                # The operating point. Lead time and false-alarm rate are only
                # comparable between runs that share it, and a stored number
                # without it cannot be checked against anything.
                "operating_point": {
                    "axon_detector": self.axon_factory().name,
                    "baseline_detector": self.baseline_factory().name,
                },
                "breaches_without_threshold_alarm": sorted(
                    o.scenario_id for o in breaches if o.baseline_min is None
                ),
                "breaches_missed_by_axon": sorted(
                    o.scenario_id for o in breaches if o.axon_min is None
                ),
                "controls_with_a_false_alarm": sorted(
                    o.scenario_id for o in controls if o.axon_min is not None
                ),
                "per_scenario": [
                    {
                        "scenario_id": o.scenario_id,
                        "breach_at_min": o.breach_at_min,
                        "fault_onset_min": o.fault_onset_min,
                        "baseline_min": o.baseline_min,
                        "axon_min": o.axon_min,
                        "lead_time_min": o.lead_time,
                    }
                    for o in outcomes
                ],
            },
        )

    def grade(self) -> GraderResult:
        measurement = self.measure()
        if not measurement.companions.get("lead_time_sample_size"):
            # Not judged by case count: the dataset requirement can be met
            # while no scenario yields a lead time at all, and a median over an
            # empty sample would be reported as 0 minutes -- a number that
            # reads as "no advantage" rather than as "not measured".
            return GraderResult(
                claim_id=self.claim_id,
                measurement=measurement,
                status=ClaimStatus.INSUFFICIENT_DATA,
                reason=(
                    "no true-breach scenario had both detectors fire, so there is no "
                    "lead time to take a median of"
                ),
            )
        return judge(self.claim_id, measurement)


# ---------------------------------------------------------------------------
# C6
# ---------------------------------------------------------------------------

_BENCH_ENTITY = EntityRef(kind="shipment", id="SH-BENCH")

#: Conflict-capable channels used to build the negative set: an observation
#: type and two sources the taxonomy says can both report it.
#:
#: The pairs are the ones the pack's positives use, plus the weather channel,
#: so the negatives probe the same machinery the positives do.
_NEGATIVE_CHANNELS: tuple[tuple[str, EvidenceSource, EvidenceSource], ...] = (
    ("cargo_temp_c", EvidenceSource.TELEMETRY, EvidenceSource.VISUAL_INSPECTION),
    ("permitted_temp_max_c", EvidenceSource.SQL_LEGACY, EvidenceSource.DOCUMENT_EXTRACTION),
    ("reefer_fuel_pct", EvidenceSource.TELEMETRY, EvidenceSource.VISUAL_INSPECTION),
    ("compressor_rpm", EvidenceSource.TELEMETRY, EvidenceSource.VISUAL_INSPECTION),
    ("ambient_temp_c", EvidenceSource.TELEMETRY, EvidenceSource.WEATHER_API),
)

#: How close a negative sits to its type's tolerance. 0.8 rather than, say,
#: 0.2 because a negative built far inside tolerance tests nothing: any
#: implementation gets it right, and precision measured over easy negatives is
#: a number that cannot go down.
_NEGATIVE_MARGIN = 0.8

_BASE_VALUE: dict[str, float] = {
    "cargo_temp_c": 5.0,
    "permitted_temp_max_c": 8.0,
    "reefer_fuel_pct": 60.0,
    "compressor_rpm": 1800.0,
    "ambient_temp_c": 24.0,
}


def _observed_at() -> datetime:
    """A fixed instant, so confidence does not decay between runs.

    `Evidence.create` derives confidence partly from age. Using the wall clock
    would make the same scenario produce different confidences on every run and
    the stored numbers would not be reproducible from their provenance.
    """
    return datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def _evidence(
    observation_type: str,
    source: EvidenceSource,
    value: ObservationValue,
    *,
    taxonomy: Taxonomy,
) -> Evidence:
    moment = _observed_at()
    return Evidence.create(
        entity_ref=_BENCH_ENTITY,
        source=source,
        modality=Modality.STRUCTURED,
        observation_type=observation_type,
        value=value,
        observed_at=moment,
        ingested_at=moment,
        provenance=Provenance(producer="axonbench", producer_version="1.0.0"),
        taxonomy=taxonomy,
    )


@dataclass(frozen=True, slots=True)
class _Case:
    """One conflict-detection case and what should have happened."""

    label: str
    observation_type: str
    should_conflict: bool
    evidence: tuple[Evidence, Evidence]


@dataclass(frozen=True, slots=True)
class ConflictGrader:
    """C6: the system detects when enterprise sources disagree.

    **Positives** are the pack's declared `data_faults` that name two sides:
    ERP-vs-Bill-of-Lading mismatches and telemetry-vs-panel disagreements. Each
    is built into the two evidence records the reconciler would see and graded
    on whether a conflict is raised for that observation type.

    **Negatives are constructed, and how they are constructed decides whether
    precision means anything.** One per scenario, on a conflict-capable channel
    that scenario does not seed, with the two sources placed at 80% of the
    taxonomy's own `conflict_tolerance` for that type -- close enough to
    disagree, inside the band that says they do not. For a type with zero
    tolerance the hardest negative available is exact agreement, and that is
    what is used. Building negatives far inside tolerance would make precision
    a number that cannot go down.
    """

    claim_id: str = "C6"
    pack_dir: Path = DEFAULT_PACK_DIR
    #: Injectable so the grader can be shown to fail. The only way to feed a
    #: broken reconciler to a pure function is to hand it one.
    reconcile_fn: Callable[..., Any] = reconcile
    _taxonomy: Taxonomy | None = field(default=None)

    def _cases(self, pack: ScenarioPack, taxonomy: Taxonomy) -> list[_Case]:
        cases: list[_Case] = []

        for scenario_id in sorted(pack.scenarios):
            scenario = pack.scenarios[scenario_id]
            seeded: set[str] = set()

            for index, fault in enumerate(scenario.data_faults):
                sides = fault.sides
                if sides is None or fault.field is None:
                    continue
                seeded.add(fault.field)
                (left_source, left_value), (right_source, right_value) = sides
                cases.append(
                    _Case(
                        label=f"{scenario_id}#{index}:{fault.type.value}:{fault.field}",
                        observation_type=fault.field,
                        should_conflict=True,
                        evidence=(
                            _evidence(
                                fault.field,
                                EvidenceSource(left_source),
                                left_value,
                                taxonomy=taxonomy,
                            ),
                            _evidence(
                                fault.field,
                                EvidenceSource(right_source),
                                right_value,
                                taxonomy=taxonomy,
                            ),
                        ),
                    )
                )

            negative = self._negative_for(scenario_id, seeded, taxonomy)
            if negative is not None:
                cases.append(negative)

        return cases

    def _negative_for(self, scenario_id: str, seeded: set[str], taxonomy: Taxonomy) -> _Case | None:
        """A near-miss on a channel this scenario does not seed.

        The channel is picked by a CRC of the scenario id, **not** by
        `hash()`: Python randomises string hashing per process, so the first
        version of this line silently built a different negative set on every
        run and made the stored precision unreproducible from its own
        provenance -- the one property this whole harness exists to guarantee.
        """
        candidates = [channel for channel in _NEGATIVE_CHANNELS if channel[0] not in seeded]
        if not candidates:
            return None
        index = zlib.crc32(scenario_id.encode()) % len(candidates)
        observation_type, left, right = candidates[index]

        spec = taxonomy.spec(observation_type)
        tolerance = spec.conflict_tolerance
        offset = float(tolerance) * _NEGATIVE_MARGIN if isinstance(tolerance, int | float) else 0.0
        base = _BASE_VALUE[observation_type]

        return _Case(
            label=f"{scenario_id}#negative:{observation_type}:{offset:g}",
            observation_type=observation_type,
            should_conflict=False,
            evidence=(
                _evidence(observation_type, left, base, taxonomy=taxonomy),
                _evidence(observation_type, right, base + offset, taxonomy=taxonomy),
            ),
        )

    def measure(self) -> Measurement:
        taxonomy = self._taxonomy or load_taxonomy()
        pack = load_pack(self.pack_dir)
        cases = self._cases(pack, taxonomy)

        true_positive = false_negative = false_positive = true_negative = 0
        missed: list[str] = []
        spurious: list[str] = []

        for case in cases:
            result = self.reconcile_fn(list(case.evidence), taxonomy=taxonomy)
            raised = any(
                conflict.observation_type == case.observation_type for conflict in result.conflicts
            )
            if case.should_conflict and raised:
                true_positive += 1
            elif case.should_conflict:
                false_negative += 1
                missed.append(case.label)
            elif raised:
                false_positive += 1
                spurious.append(case.label)
            else:
                true_negative += 1

        positives = true_positive + false_negative
        recall = _rate(true_positive, positives)
        precision = _rate(true_positive, true_positive + false_positive)

        return Measurement(
            value=recall,
            # The claim's unit is seeded conflicts, so only the positives count
            # towards its dataset requirement. The negatives are what make
            # precision meaningful and are reported beside it.
            cases=positives,
            companions={
                "recall": recall,
                "precision": precision,
                "seeded_conflicts": float(positives),
                "negative_cases": float(false_positive + true_negative),
            },
            detail={
                "pack_version": pack.pack_version,
                "true_positives": true_positive,
                "false_negatives": false_negative,
                "false_positives": false_positive,
                "true_negatives": true_negative,
                "negative_margin_of_tolerance": _NEGATIVE_MARGIN,
                "missed_conflicts": sorted(missed),
                "spurious_conflicts": sorted(spurious),
                # Which channel each scenario's negative was built on. Recorded
                # because precision is only as meaningful as the negatives
                # behind it, and a reader who cannot see which near-misses were
                # offered cannot judge the 1.00. It is also the only part of
                # this result that reveals the channel rotation, which is what
                # makes the reproducibility of the negative set testable at
                # all - the counts alone are identical whether the rotation is
                # stable or reseeded on every run.
                "negative_channels": sorted(
                    case.label.split("#negative:")[0] + ":" + case.observation_type
                    for case in cases
                    if not case.should_conflict
                ),
            },
        )

    def grade(self) -> GraderResult:
        return judge(self.claim_id, self.measure())
