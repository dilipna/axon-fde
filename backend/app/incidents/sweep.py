"""Walking a detector over a stream of readings, one minute at a time.

This is the loop that turns a recording into a detection, and it exists as its
own module for one reason: **two callers need it and they must not drift.**
`ScenarioReplay` runs it against a database, persisting as it goes; AxonBench's
lead-time grader runs it in memory over 60 scenarios with no database at all.
If each kept its own copy, the benchmark would eventually be measuring a
slightly different detector from the one that ships, and the difference would
be invisible — which is precisely how the B3 prototype came to evaluate only
from a full window onward and miss that the real implementation fires at
minute 29.

**Pure by construction.** Nothing here opens a connection, reads a clock or
writes a row. That is what lets the grader import it: the contract forbidding
graders from touching a database would refuse `incidents.replay`, which drags
in pyodbc through the ERP adapter.

**The window size is asked for, not assumed.** A predictive detector needs its
fit window *plus* its debounce lookback. Handing it only the fit window makes
the "did the threshold hold three readings ago?" check find too little data,
answer no every time, and never fire at all — a total failure that looks like
a detector with nothing to say.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime

from backend.app.domain.envelope import TemperatureEnvelope
from backend.app.domain.evidence import Evidence
from backend.app.domain.taxonomy import Taxonomy, load_taxonomy
from backend.app.evidence.telemetry import TelemetryReading, reading_to_evidence
from backend.app.incidents.detection import Detection, Detector
from backend.app.risk.features import DEFAULT_WINDOW_MINUTES

__all__ = ["SweepStep", "first_detection", "history_minutes", "sweep"]


def history_minutes(detector: Detector) -> int:
    """How much history this detector must be handed on every evaluation.

    Detectors that need more than the default fit window say so through
    ``required_history_minutes``. The baseline threshold alarm does not
    declare one because it reads only the latest reading — it is handed the
    same window anyway, so that no lead-time difference can be attributed to
    one arm having been fed better data than the other.
    """
    return int(getattr(detector, "required_history_minutes", DEFAULT_WINDOW_MINUTES))


@dataclass(frozen=True, slots=True)
class SweepStep:
    """One minute of the sweep: the reading, what it produced, what was found."""

    reading: TelemetryReading
    #: The evidence this reading produced. Handed back rather than kept,
    #: because the caller that persists needs the exact records the detector
    #: saw, not a re-derivation of them.
    evidence: list[Evidence]
    detection: Detection | None

    @property
    def minute(self) -> int:
        return self.reading.sequence


def sweep(
    readings: Sequence[TelemetryReading],
    *,
    detector: Detector,
    envelope: TemperatureEnvelope,
    taxonomy: Taxonomy | None = None,
    scenario_run_id: str | None = None,
) -> Iterator[SweepStep]:
    """Yield one step per reading, with whatever the detector found.

    The sweep does not stop at the first detection. Stopping is the caller's
    decision: a replay opens an incident and breaks, while a grader that
    wanted to count every alarm would keep going. Deciding here would impose
    one caller's policy on the other.
    """
    tax = taxonomy or load_taxonomy()
    window: deque[list[Evidence]] = deque(maxlen=history_minutes(detector) + 1)

    for reading in readings:
        produced = reading_to_evidence(
            reading,
            taxonomy=tax,
            scenario_run_id=scenario_run_id,
        )
        window.append(produced)
        visible = [item for step in window for item in step]
        detection = detector.evaluate(visible, envelope=envelope, now=reading.timestamp)
        yield SweepStep(reading=reading, evidence=produced, detection=detection)


def first_detection(
    readings: Sequence[TelemetryReading],
    *,
    detector: Detector,
    envelope: TemperatureEnvelope,
    taxonomy: Taxonomy | None = None,
) -> tuple[Detection, int] | None:
    """The first detection and the minute it fired, or ``None`` if never.

    The minute is counted from the first reading rather than taken from the
    wall clock, so a lead time measured between two detectors is a property of
    the recording and not of when the benchmark happened to run.
    """
    if not readings:
        return None
    started_at: datetime = readings[0].timestamp

    for step in sweep(readings, detector=detector, envelope=envelope, taxonomy=taxonomy):
        if step.detection is not None:
            minute = round((step.detection.detected_at - started_at).total_seconds() / 60)
            return step.detection, minute
    return None
