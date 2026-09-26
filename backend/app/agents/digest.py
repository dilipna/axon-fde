"""What the model is shown: a compact, citable digest of the evidence.

A six-hour run is thousands of observations and a model cannot read them, so
it is shown a digest: the latest reading of each type, the trend over the last
half hour, and any faults seen recently. Every line carries a **handle**
(``E07``) that names one specific observation, and the model cites handles.

**Handles, not evidence ids, and that is load-bearing.** An evidence id is a
random UUID, minted fresh on every run. Put in a prompt it would change the
cassette key on every replay and turn a recording into a permanent miss. A
handle is assigned by a fixed ordering over the bundle, so the same incident
produces byte-identical prompt text every time - which is what makes a
cassette replayable and what makes a diff of two prompts mean something.

The digest is **a pure function of the evidence**. No clock (it reads the
newest observation's own timestamp), no I/O, no model. It also authors no
observation: every figure in it is a value that is in the bundle, or a
least-squares slope over values that are, and the handle table maps each back
to the evidence it came from. The one thing the model can do with a handle is
cite it, and a handle that is not in the table is a fabrication that the
caller can detect by set membership.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta

from backend.app.domain.evidence import Evidence

__all__ = [
    "FAULT_WINDOW_MINUTES",
    "MIN_TREND_READINGS",
    "TREND_WINDOW_MINUTES",
    "EvidenceDigest",
    "build_digest",
]

#: The window a trend is fitted over. Matches the risk service's own window
#: (30 minutes) so the trend the model reads is the trend the detector fired
#: on, rather than a second, differently-defined one.
TREND_WINDOW_MINUTES = 30

#: How far back a fault code still counts as "seen recently".
FAULT_WINDOW_MINUTES = 60

#: A trend needs this many readings inside its window. Below it the line is
#: omitted rather than reported: a slope over three points is a number that
#: looks like a trend and is not one.
MIN_TREND_READINGS = 5


@dataclass(frozen=True, slots=True)
class EvidenceDigest:
    """The prompt text, and the table that turns a citation back into evidence."""

    text: str
    #: handle -> evidence id (as a string). The model's citations are resolved
    #: through this and nowhere else.
    handles: dict[str, str]


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _slope_per_minute(points: Sequence[tuple[float, float]]) -> float:
    """Least-squares slope of (minute, value) pairs.

    A fit and not the difference between endpoints, for the same reason
    `risk/features.py` fits: the endpoints of a noisy series are two samples of
    noise, and the model would be told a trend the detector did not see.
    """
    n = len(points)
    mean_x = sum(x for x, _ in points) / n
    mean_y = sum(y for _, y in points) / n
    den = sum((x - mean_x) ** 2 for x, _ in points)
    if den == 0:
        return 0.0
    return sum((x - mean_x) * (y - mean_y) for x, y in points) / den


def _show(value: object) -> str:
    if _is_number(value):
        return f"{float(value):.2f}"  # type: ignore[arg-type]
    if isinstance(value, list):
        return "[" + ", ".join(str(item) for item in value) + "]"
    return str(value)


def build_digest(evidence: Sequence[Evidence], *, conflicts: Sequence[str] = ()) -> EvidenceDigest:
    """Summarise a bundle into citable text.

    Deterministic: the same evidence, in any order, gives the same text.
    """
    handles: dict[str, str] = {}

    def handle_for(item: Evidence) -> str:
        name = f"E{len(handles) + 1:02d}"
        handles[name] = str(item.id)
        return name

    if not evidence:
        return EvidenceDigest(text="No observations are available.", handles=handles)

    by_type: dict[str, list[Evidence]] = {}
    for item in evidence:
        by_type.setdefault(item.observation_type, []).append(item)
    # Ties on timestamp broken by the value's text so the order never depends
    # on the order the bundle arrived in.
    for items in by_type.values():
        items.sort(key=lambda e: (e.observed_at, e.source.value, _show(e.value)))

    newest = max(item.observed_at for item in evidence)
    oldest = min(item.observed_at for item in evidence)
    span_min = round((newest - oldest).total_seconds() / 60)

    lines = [
        f"Evidence for one vehicle, covering {span_min} minutes up to the newest observation.",
        "Cite observations by their handle, e.g. E07. Only these handles exist.",
        "",
        "Latest observation of each type, one line per source that reported it:",
    ]
    # One line per (type, source), not per type. Two sources reporting the same
    # type is exactly a cross-source disagreement, and showing only whichever
    # sorted last would present the ERP's 10 C as *the* ceiling and hide the
    # Bill of Lading's 8 C from the model entirely.
    for observation_type in sorted(by_type):
        per_source: dict[str, Evidence] = {}
        for item in by_type[observation_type]:  # ascending, so the newest wins
            per_source[item.source.value] = item
        for source in sorted(per_source):
            latest = per_source[source]
            age = round((newest - latest.observed_at).total_seconds() / 60)
            lines.append(
                f"[{handle_for(latest)}] {observation_type} = {_show(latest.value)}"
                f" (source {source}, {age} min before the newest)"
            )

    trend_lines: list[str] = []
    for observation_type in sorted(by_type):
        window_start = newest - timedelta(minutes=TREND_WINDOW_MINUTES)
        window = [
            item
            for item in by_type[observation_type]
            if item.observed_at >= window_start and _is_number(item.value)
        ]
        if len(window) < MIN_TREND_READINGS:
            continue
        points = [
            ((item.observed_at - window[0].observed_at).total_seconds() / 60, float(item.value))  # type: ignore[arg-type]
            for item in window
        ]
        slope = _slope_per_minute(points)
        trend_lines.append(
            f"{observation_type}: {_show(window[0].value)} [{handle_for(window[0])}] -> "
            f"{_show(window[-1].value)} over {round(points[-1][0])} min, "
            f"slope {slope:+.3f} per minute"
        )
    lines.append("")
    lines.append(f"Trends over the last {TREND_WINDOW_MINUTES} minutes:")
    lines.extend(trend_lines or ["(too few readings in the window to fit any)"])

    fault_lines: list[str] = []
    fault_start = newest - timedelta(minutes=FAULT_WINDOW_MINUTES)
    seen: dict[str, Evidence] = {}
    for item in by_type.get("fault_code", []):
        if item.observed_at < fault_start or not isinstance(item.value, list):
            continue
        for code in item.value:
            seen[str(code)] = item  # ascending order, so the latest wins
    for code in sorted(seen):
        fault_lines.append(f"fault code {code} [{handle_for(seen[code])}]")
    lines.append("")
    lines.append(f"Fault codes reported in the last {FAULT_WINDOW_MINUTES} minutes:")
    lines.extend(fault_lines or ["(none - the unit reported no active fault)"])

    lines.append("")
    lines.append("Cross-source conflicts found by deterministic reconciliation:")
    lines.extend(sorted(conflicts) or ["(none)"])

    return EvidenceDigest(text="\n".join(lines), handles=handles)
