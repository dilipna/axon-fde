"""Does the narrative say only what the evidence supports?

A language model writing an incident summary will, given the chance, cite an
observation that does not exist and quote a number nobody measured. Both are
fluent, plausible and wrong, and neither is visible to a reader who does not
already know the answer - which is every reader who needs the summary.

**The check is deterministic and it is not a model.** A grounding check that
asked an LLM whether a citation was real could be talked out of its answer by
the same text it was checking, and would fail in a correlated way with the
thing it was checking. So this module does set membership and numeric
comparison, and nothing else.

Two failures are detected, and they are different:

- **A fabricated citation** - the narrative cites an evidence id that is not
  in the bundle. This is disqualifying: the recommendation claims a basis it
  does not have.
- **An unsupported number** - a figure appears in the prose that matches no
  evidence value, no risk number and no cost. Usually an invented reading,
  occasionally arithmetic the model did in its head.

Numbers are matched against an explicit set of permitted values rather than
against "anything in the evidence", because a narrative legitimately quotes
things that are not observations: a probability, an expected value, a facility
count. The caller supplies them, which keeps this module from having to know
what a recommendation is made of.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from backend.app.domain.evidence import Evidence

__all__ = [
    "NUMERIC_TOLERANCE",
    "GroundingFailure",
    "GroundingReport",
    "check_grounding",
]

#: How close a quoted number must be to a permitted one to count as the same
#: number. Absolute, not relative: the values here are temperatures in degrees
#: and probabilities in [0, 1], and a relative tolerance would be far too
#: loose on a probability and far too tight on a dollar figure.
#:
#: 0.05 accepts a narrative rounding 7.94 C to 7.9 and rejects one inventing
#: 8.4 where the reading said 7.9.
NUMERIC_TOLERANCE = 0.05

#: Bare single-digit integers are treated as counts and not checked.
#:
#: Prose is full of them - "3 readings", "the last 2 hours", "one of 4
#: facilities" - and demanding that each match an observation buries a real
#: fabrication under false positives, which is how a check stops being read.
#:
#: **Written as a digit test rather than a magnitude threshold, deliberately.**
#: The first draft ignored everything below 10, which silently exempted every
#: temperature in the system: the flagship envelope is 8.0 C and its readings
#: sit between 5 and 10, so the check would have passed an invented
#: temperature - the single most consequential number in a cold-chain
#: narrative - while diligently checking the dollar figures. A figure written
#: with a decimal point is a measurement and is always checked.
_COUNT_LIKE = re.compile(r"^\d$")

#: Matches integers and decimals, with optional thousands separators, a
#: leading sign, and a trailing percent sign.
_NUMBER = re.compile(r"[-+]?\d{1,3}(?:,\d{3})+(?:\.\d+)?|[-+]?\d+(?:\.\d+)?%?")


class GroundingFailure(StrEnum):
    """What was wrong, in a form that can be counted."""

    FABRICATED_CITATION = "fabricated_citation"
    UNSUPPORTED_NUMBER = "unsupported_number"
    NO_CITATIONS = "no_citations"


@dataclass(frozen=True, slots=True)
class GroundingReport:
    """Whether the narrative may be shown, and every reason it may not."""

    failures: tuple[GroundingFailure, ...] = ()
    fabricated_ids: tuple[str, ...] = ()
    unsupported_numbers: tuple[float, ...] = ()
    checked_numbers: int = 0
    checked_citations: int = 0

    @property
    def grounded(self) -> bool:
        return not self.failures

    def describe(self) -> str:
        if self.grounded:
            return (
                f"grounded: {self.checked_citations} citations and "
                f"{self.checked_numbers} figures all supported"
            )
        parts: list[str] = []
        if self.fabricated_ids:
            parts.append(f"citations not in the bundle: {', '.join(self.fabricated_ids)}")
        if self.unsupported_numbers:
            parts.append(
                "figures matching no evidence: "
                + ", ".join(f"{value:g}" for value in self.unsupported_numbers)
            )
        if GroundingFailure.NO_CITATIONS in self.failures:
            parts.append("the narrative cites nothing at all")
        return "; ".join(parts)

    def as_payload(self) -> dict[str, object]:
        """Stored on the recommendation as ``grounding_check``."""
        return {
            "grounded": self.grounded,
            "failures": [failure.value for failure in self.failures],
            "fabricated_ids": list(self.fabricated_ids),
            "unsupported_numbers": list(self.unsupported_numbers),
            "checked_citations": self.checked_citations,
            "checked_numbers": self.checked_numbers,
        }


@dataclass(frozen=True, slots=True)
class PermittedValues:
    """Numbers a narrative is allowed to quote besides observation values.

    Supplied by the caller rather than inferred, because a narrative quotes
    things that are not observations - a probability, an expected value, a
    detour time - and a checker that guessed at which would reject correct
    prose or accept invented figures depending on the guess.
    """

    values: tuple[float, ...] = field(default_factory=tuple)

    @classmethod
    def of(cls, *values: float | int | None) -> PermittedValues:
        return cls(tuple(float(value) for value in values if value is not None))


def check_grounding(
    narrative: str,
    *,
    cited_evidence_ids: Sequence[str],
    evidence: Sequence[Evidence],
    permitted: PermittedValues | None = None,
    require_citations: bool = True,
) -> GroundingReport:
    """Check a narrative against the bundle it claims to rest on.

    ``require_citations`` exists for the one legitimate case of a narrative
    with none - a scripted message that makes no factual claim. It defaults to
    demanding them, because the common case is a recommendation, and an
    uncited recommendation is an opinion.
    """
    by_id = {str(item.id) for item in evidence}
    fabricated = tuple(
        evidence_id for evidence_id in cited_evidence_ids if evidence_id not in by_id
    )

    permitted_values = list(permitted.values if permitted else ())
    permitted_values.extend(_numeric_values(evidence))

    quoted = _numbers_in(narrative)
    unsupported = tuple(
        readings[0]
        for readings in quoted
        if not any(_matches_any(reading, permitted_values) for reading in readings)
    )

    failures: list[GroundingFailure] = []
    if fabricated:
        failures.append(GroundingFailure.FABRICATED_CITATION)
    if unsupported:
        failures.append(GroundingFailure.UNSUPPORTED_NUMBER)
    if require_citations and not cited_evidence_ids:
        failures.append(GroundingFailure.NO_CITATIONS)

    return GroundingReport(
        failures=tuple(failures),
        fabricated_ids=fabricated,
        unsupported_numbers=unsupported,
        checked_numbers=len(quoted),
        checked_citations=len(cited_evidence_ids),
    )


def _numeric_values(evidence: Sequence[Evidence]) -> list[float]:
    """Every number an observation actually reports.

    Booleans are excluded explicitly: in Python ``True`` is ``1``, and letting
    a boolean observation license the figure "1" in prose would be a hole with
    no upside.
    """
    values: list[float] = []
    for item in evidence:
        if isinstance(item.value, bool):
            continue
        if isinstance(item.value, int | float):
            values.append(float(item.value))
        # Set-valued observations hold strings by construction - fault codes,
        # not readings - so there is nothing numeric to extract from them.
        # mypy proves the branch that tried was unreachable.
    return values


def _numbers_in(text: str) -> tuple[tuple[float, ...], ...]:
    """Pull the figures out of prose, each as its acceptable readings.

    One entry per figure written, not per reading. A percentage has two
    acceptable readings - "78%" legitimately refers to a stored 0.78 and to a
    written 78 - and the figure is supported if *either* matches. Returning
    them as separate figures, which the first draft did, meant a correct "78%"
    was reported as one supported number and one fabricated one.
    """
    found: list[tuple[float, ...]] = []
    for raw in _NUMBER.findall(text):
        cleaned = raw.replace(",", "")
        if cleaned.endswith("%"):
            magnitude = float(cleaned[:-1])
            found.append((magnitude, magnitude / 100.0))
        elif _COUNT_LIKE.match(cleaned):
            continue
        else:
            found.append((float(cleaned),))
    return tuple(found)


def _matches_any(value: float, permitted: Iterable[float]) -> bool:
    return any(abs(value - candidate) <= NUMERIC_TOLERANCE for candidate in permitted)
