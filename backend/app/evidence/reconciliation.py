"""Deterministic reconciliation across evidence sources.

This is the module that detects Axon's most expensive undiagnosed problem: the
ERP and the signed Bill of Lading disagree about a cargo's permitted envelope,
and nothing in their current process notices.

Detection is a value comparison against declared tolerances, not a judgement
handed to a language model. That choice is what makes conflict-detection
precision and recall measurable at all (claim C6), and it is roughly 150 lines
of pure, fully testable code.

Two observations conflict when **all four** hold:

1. same `observation_type`
2. same `entity_ref`
3. overlapping validity windows
4. values differ beyond the type's declared tolerance

`supersedes` is deliberately distinct from `contradicts`. A newer reading from
the *same* source replaces an older one; that is not a disagreement between
sources, and surfacing it as a conflict would train dispatchers to ignore the
real ones.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from backend.app.domain.enums import EvidenceRelation, EvidenceSource
from backend.app.domain.evidence import Evidence, EvidenceLink
from backend.app.domain.taxonomy import Taxonomy, load_taxonomy

__all__ = [
    "Conflict",
    "ReconciliationResult",
    "reconcile",
]


@dataclass(frozen=True, slots=True)
class Conflict:
    """A disagreement between sources about the same observation."""

    observation_type: str
    entity_ref: str
    safety_critical: bool

    #: The evidence in disagreement, ordered by descending confidence.
    evidence: tuple[Evidence, ...]

    #: Which source wins, when the taxonomy declares an authority order.
    #: `None` means the system does not silently pick a side.
    authoritative_source: EvidenceSource | None
    authoritative_value: object | None

    @property
    def values(self) -> tuple[object, ...]:
        return tuple(e.value for e in self.evidence)

    @property
    def sources(self) -> tuple[EvidenceSource, ...]:
        return tuple(e.source for e in self.evidence)

    def describe(self) -> str:
        parts = [f"{e.source}={e.value}" for e in self.evidence]
        summary = f"{self.observation_type} on {self.entity_ref}: " + " vs ".join(parts)
        if self.authoritative_source is not None:
            summary += f" (using {self.authoritative_source}={self.authoritative_value})"
        return summary


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    """What reconciliation found across a set of evidence."""

    links: tuple[EvidenceLink, ...] = ()
    conflicts: tuple[Conflict, ...] = ()

    #: Evidence superseded by a newer reading from the same source. These stay
    #: in the record but are excluded from the agent's context bundle.
    superseded_ids: frozenset[str] = field(default_factory=frozenset)

    @property
    def has_safety_critical_conflict(self) -> bool:
        """Whether an unresolved conflict blocks automatic incident closure."""
        return any(c.safety_critical for c in self.conflicts)

    def conflicts_for(self, observation_type: str) -> tuple[Conflict, ...]:
        return tuple(c for c in self.conflicts if c.observation_type == observation_type)


def _group_key(evidence: Evidence) -> tuple[str, str]:
    return (evidence.observation_type, str(evidence.entity_ref))


def _find_superseded(group: list[Evidence]) -> set[str]:
    """Identify readings replaced by a newer one from the same source.

    Grouping by source matters: telemetry replacing its own earlier reading is
    normal operation. Telemetry disagreeing with a photograph is a conflict.
    """
    superseded: set[str] = set()
    by_source: dict[EvidenceSource, list[Evidence]] = defaultdict(list)
    for item in group:
        by_source[item.source].append(item)

    for items in by_source.values():
        if len(items) < 2:
            continue
        ordered = sorted(items, key=lambda e: e.observed_at)
        # Everything but the newest from this source is superseded.
        superseded.update(str(e.id) for e in ordered[:-1])

    return superseded


def reconcile(
    evidence: list[Evidence],
    *,
    taxonomy: Taxonomy | None = None,
) -> ReconciliationResult:
    """Detect conflicts, corroboration and supersession across evidence.

    Only `active` evidence participates. Retracted or already-superseded
    records are excluded rather than silently resurfacing as conflicts.
    """
    tax = taxonomy or load_taxonomy()

    active = [e for e in evidence if e.is_active]
    if len(active) < 2:
        return ReconciliationResult()

    groups: dict[tuple[str, str], list[Evidence]] = defaultdict(list)
    for item in active:
        groups[_group_key(item)].append(item)

    links: list[EvidenceLink] = []
    conflicts: list[Conflict] = []
    all_superseded: set[str] = set()

    for (observation_type, entity_ref), group in sorted(groups.items()):
        if len(group) < 2:
            continue

        spec = tax.spec(observation_type)

        superseded = _find_superseded(group)
        all_superseded.update(superseded)

        for older_id in superseded:
            older = next(e for e in group if str(e.id) == older_id)
            newer = max(
                (e for e in group if e.source == older.source),
                key=lambda e: e.observed_at,
            )
            links.append(
                EvidenceLink(
                    from_evidence_id=newer.id,
                    to_evidence_id=older.id,
                    relation=EvidenceRelation.SUPERSEDES,
                    rule_id="same_source_newer_reading",
                    detail=(
                        f"{newer.source} reading at {newer.observed_at.isoformat()} "
                        f"replaces {older.observed_at.isoformat()}"
                    ),
                )
            )

        # Only current readings can disagree with each other.
        current = [e for e in group if str(e.id) not in superseded]
        if len(current) < 2:
            continue

        conflicting: list[Evidence] = []

        for index, left in enumerate(current):
            for right in current[index + 1 :]:
                if not left.overlaps(right):
                    # An hour apart is a trend, not a disagreement.
                    continue

                if tax.conflicts(observation_type, left.value, right.value):
                    links.append(
                        EvidenceLink(
                            from_evidence_id=left.id,
                            to_evidence_id=right.id,
                            relation=EvidenceRelation.CONTRADICTS,
                            rule_id="value_beyond_tolerance",
                            detail=(
                                f"{left.source}={left.value} vs "
                                f"{right.source}={right.value} "
                                f"(tolerance {spec.conflict_tolerance})"
                            ),
                        )
                    )
                    for item in (left, right):
                        if item not in conflicting:
                            conflicting.append(item)
                else:
                    # Independent sources agreeing is itself informative: it
                    # raises confidence without needing a larger model.
                    if left.source != right.source:
                        links.append(
                            EvidenceLink(
                                from_evidence_id=left.id,
                                to_evidence_id=right.id,
                                relation=EvidenceRelation.CORROBORATES,
                                rule_id="independent_sources_agree",
                                detail=(
                                    f"{left.source} and {right.source} agree "
                                    f"within tolerance {spec.conflict_tolerance}"
                                ),
                            )
                        )

        if conflicting:
            ordered = tuple(sorted(conflicting, key=lambda e: e.confidence, reverse=True))
            winner = tax.authoritative_source(observation_type, [e.source for e in ordered])
            authoritative_value = None
            if winner is not None:
                authoritative_value = next(e.value for e in ordered if e.source == winner)

            conflicts.append(
                Conflict(
                    observation_type=observation_type,
                    entity_ref=entity_ref,
                    safety_critical=spec.safety_critical,
                    evidence=ordered,
                    authoritative_source=winner,
                    authoritative_value=authoritative_value,
                )
            )

    return ReconciliationResult(
        links=tuple(links),
        conflicts=tuple(conflicts),
        superseded_ids=frozenset(all_superseded),
    )


def resolve_value(
    evidence: list[Evidence],
    observation_type: str,
    *,
    taxonomy: Taxonomy | None = None,
) -> Evidence | None:
    """Pick the observation that should drive a decision.

    Authority order first, because a signed document outranks an ERP record
    regardless of how confident the ERP is. Confidence breaks ties within a
    source tier, and a more recent reading breaks ties within that.

    Returns `None` when there is nothing to choose from. Note that this decides
    which value is *used*; any conflict remains visible either way.
    """
    tax = taxonomy or load_taxonomy()
    candidates = [e for e in evidence if e.is_active and e.observation_type == observation_type]
    if not candidates:
        return None

    winner = tax.authoritative_source(observation_type, [e.source for e in candidates])
    if winner is not None:
        candidates = [e for e in candidates if e.source == winner] or candidates

    return max(candidates, key=lambda e: (e.confidence, e.observed_at))
