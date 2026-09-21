"""Who is allowed to decide how confident the system is.

The division of labour here is the whole design, and it is invariant I2 made
operational:

- **Rules propose priors.** A deterministic function looks at the observable
  signals and says how plausible each root cause is *before* anything is
  linked to it.
- **The model proposes links.** It reads the evidence and says which
  observations bear on which hypothesis, and why. ``ProposedLink`` has no
  confidence field, so there is nowhere for it to put a number.
- **A deterministic scorer owns every confidence.** It combines the rule prior
  with the evidence's *own* confidences - which came from
  ``Taxonomy.confidence()``, not from anything a model said.

**The model can never raise a hypothesis above its rule prior.** That is the
property that makes this safe rather than merely tidy, and it is the one a
test pins. A model that linked forty high-confidence readings to the wrong
cause would still be capped by what the rules think plausible; the best it can
do is fail to raise a hypothesis that deserved raising, which is visible in
the output rather than silently persuasive.

This is also what stops prompt injection from mattering here. An instruction
smuggled into a document can at most cause a *link* to be proposed. It cannot
set a score, because the type has no field for one and the scorer never reads
model output.

Pure: no I/O, no clock, no model.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from backend.app.domain.enums import ConfidenceBasis, RootCause
from backend.app.domain.evidence import Evidence

__all__ = [
    "MAX_SUPPORTING_EVIDENCE",
    "PRIOR_OBSERVATION_TYPES",
    "ProposedLink",
    "ScoredHypothesis",
    "rule_priors",
    "score_hypotheses",
]

#: Every observation type the prior rules read.
#:
#: Declared in one place and checked at every lookup, so a rule cannot quietly
#: read a type that does not exist. It could: the first draft read
#: `setpoint_temp_c`, `door_open_state` and `reefer_fault_codes`, none of
#: which are declared. Nothing raised - the lookups simply missed, every prior
#: stayed at its floor, and the whole linking step would have been capped at
#: 0.02 for ever. A test asserts this set is a subset of the taxonomy, so the
#: two cannot drift apart.
PRIOR_OBSERVATION_TYPES = frozenset(
    {
        "cargo_temp_c",
        "ambient_temp_c",
        "permitted_temp_max_c",
        "compressor_rpm",
        "reefer_status",
        "fault_code",
        "door_state",
        "maintenance_warning",
    }
)

#: How many pieces of evidence can contribute to one hypothesis.
#:
#: Beyond a handful, extra corroboration from the same source tells you almost
#: nothing new - a thousand readings from one thermometer are one thermometer -
#: and without a cap the support term would climb with the *length* of a
#: replay rather than with the strength of the case. Four is the point past
#: which the increments here fall below a hundredth.
MAX_SUPPORTING_EVIDENCE = 4

#: The floor a rule prior can take. Never zero: a hypothesis with a zero prior
#: can never be raised by any amount of evidence, which would make the rules a
#: veto over the evidence rather than a starting point.
_MIN_PRIOR = 0.02

#: The ceiling any hypothesis can reach. Nothing in this system is certain,
#: and a 1.0 would propagate into the expected-value arithmetic as a claim
#: that the outcome is known.
_MAX_CONFIDENCE = 0.95


@dataclass(frozen=True, slots=True)
class ProposedLink:
    """What the model is permitted to say.

    Note what is absent: any number. The model proposes that some evidence
    bears on some root cause and explains why; how much that is worth is not
    its decision. Adding a ``confidence`` field here would be the single
    change that breaks I2, which is why the omission is stated rather than
    left to be noticed.
    """

    root_cause: RootCause
    #: Evidence ids, as strings. Checked against the bundle by the grounding
    #: check before they reach the scorer - an id that does not exist is a
    #: fabrication, not a weak link.
    evidence_ids: tuple[str, ...]
    rationale: str = ""

    def __post_init__(self) -> None:
        if not self.evidence_ids:
            raise ValueError(
                f"a link to {self.root_cause.value} cites no evidence; an "
                "unsupported hypothesis is a guess, and the scorer has nothing "
                "to weigh"
            )


@dataclass(frozen=True, slots=True)
class ScoredHypothesis:
    """A root cause, its confidence, and every input that produced it."""

    root_cause: RootCause
    confidence: float
    #: What the rules thought before anything was linked. Stored so the
    #: model's contribution is visible as a difference rather than assumed.
    prior: float
    #: The evidence that actually counted, after capping and deduplication.
    supporting_evidence_ids: tuple[str, ...] = ()
    basis: ConfidenceBasis = ConfidenceBasis.DERIVED
    rationale: str = ""

    @property
    def uplift_over_prior(self) -> float:
        """How far the links moved the number. Never positive - see the module docstring."""
        return self.confidence - self.prior

    def explain(self) -> dict[str, object]:
        return {
            "root_cause": self.root_cause.value,
            "confidence": round(self.confidence, 4),
            "prior": round(self.prior, 4),
            "supporting_evidence": list(self.supporting_evidence_ids),
            "basis": self.basis.value,
        }


def rule_priors(evidence: Sequence[Evidence]) -> dict[RootCause, float]:
    """How plausible each root cause is, from the signals alone.

    Deliberately crude. This is a prior, not a diagnosis: its job is to bound
    what the linking step can claim, and a prior that was already confident
    would leave nothing for the evidence to do. Every rule reads a single
    declared observation type, so a missing signal lowers a prior rather than
    silently defaulting it (I6).

    **Every name below is a declared taxonomy type, and a test asserts that.**
    The first draft read `setpoint_temp_c`, `door_open_state` and
    `reefer_fault_codes` - none of which exist. Nothing raised: the lookups
    simply missed, every prior stayed at the floor for ever, and the linking
    step would have been permanently capped at 0.02 on every hypothesis. A
    rule that reads a type the system cannot produce is not a weak rule, it is
    an absent one, and it fails silently in the direction of looking cautious.

    There is no setpoint in this vocabulary. Headroom is measured against
    ``permitted_temp_max_c``, the contractual ceiling, which is the number the
    rest of the system judges against.
    """
    latest: dict[str, Evidence] = {}
    for item in evidence:
        current = latest.get(item.observation_type)
        if current is None or item.observed_at > current.observed_at:
            latest[item.observation_type] = item

    def _declared(observation_type: str) -> Evidence | None:
        """Look up a signal, refusing any name not in the declared set.

        The guard is what makes the first draft's bug impossible rather than
        merely fixed: a typo or an invented type raises here, in every test,
        instead of missing silently and pinning every prior to its floor.
        """
        if observation_type not in PRIOR_OBSERVATION_TYPES:
            raise KeyError(
                f"{observation_type!r} is not in PRIOR_OBSERVATION_TYPES; a rule "
                "reading an undeclared type never fires and never says so"
            )
        return latest.get(observation_type)

    def numeric(observation_type: str) -> float | None:
        item = _declared(observation_type)
        if item is None or isinstance(item.value, bool):
            return None
        return float(item.value) if isinstance(item.value, int | float) else None

    def categorical(observation_type: str) -> str | None:
        item = _declared(observation_type)
        return item.value if item is not None and isinstance(item.value, str) else None

    def members(observation_type: str) -> tuple[str, ...]:
        item = _declared(observation_type)
        if item is None or not isinstance(item.value, list):
            return ()
        return tuple(str(entry) for entry in item.value)

    priors = dict.fromkeys(RootCause, _MIN_PRIOR)
    faults = members("fault_code")
    cargo = numeric("cargo_temp_c")
    ambient = numeric("ambient_temp_c")
    ceiling = numeric("permitted_temp_max_c")
    status = categorical("reefer_status")
    door = categorical("door_state")
    rpm = numeric("compressor_rpm")

    # Headroom against the contractual ceiling. `None` when either number is
    # missing, which leaves every temperature-driven prior at the floor rather
    # than assuming a ceiling nobody stated.
    headroom = None if cargo is None or ceiling is None else ceiling - cargo

    # A unit that is running and still losing headroom is the signature of
    # lost capacity. Fault codes raise it; their absence does not rule it out,
    # because the flagship compressor degrades silently for over an hour.
    if headroom is not None and headroom <= 1.0:
        priors[RootCause.COMPRESSOR_DEGRADATION] = 0.80 if faults else 0.55
    if faults:
        priors[RootCause.COMPRESSOR_DEGRADATION] = max(
            priors[RootCause.COMPRESSOR_DEGRADATION], 0.70
        )
    if status == "fault":
        priors[RootCause.COMPRESSOR_DEGRADATION] = max(
            priors[RootCause.COMPRESSOR_DEGRADATION], 0.75
        )

    # A reported breach with no fault codes and a unit that is running
    # normally is more likely a lying instrument than a failing one. This is
    # the sensor-drift scenario, and this prior is what stops the linking step
    # building a confident compressor story on a thermometer's word.
    if (
        headroom is not None
        and headroom <= 1.0
        and not faults
        and status in {"running", "cycling"}
        and rpm is not None
        and rpm > 0
    ):
        priors[RootCause.SENSOR_MALFUNCTION] = 0.45

    # `ajar` counts. A door that is not closed is a door letting heat in, and
    # treating only `open` as a fault would miss the commonest version.
    if door in {"open", "ajar"}:
        priors[RootCause.DOOR_LEFT_OPEN] = 0.75

    if ambient is not None and ambient > 30.0:
        priors[RootCause.ENVIRONMENTAL_HEAT] = 0.40

    if members("maintenance_warning"):
        priors[RootCause.COMPRESSOR_DEGRADATION] = max(
            priors[RootCause.COMPRESSOR_DEGRADATION], 0.45
        )

    # Nothing wrong is always on the table, and it is high when there is
    # comfortable headroom. A system whose null hypothesis is pinned at the
    # floor will always find a cause.
    if headroom is not None and headroom >= 2.0 and not faults and status != "fault":
        priors[RootCause.NO_FAULT] = 0.70

    return priors


def score_hypotheses(
    links: Sequence[ProposedLink],
    *,
    evidence: Sequence[Evidence],
    priors: dict[RootCause, float] | None = None,
) -> tuple[ScoredHypothesis, ...]:
    """Turn proposed links into scored hypotheses. The only scoring path.

    Confidence is ``prior x support``, where support is built from the
    *evidence's own* confidences and is capped at 1.0. So the product can
    never exceed the prior: the model redistributes belief among hypotheses
    and may lower one, but cannot raise any above what the rules allowed.

    Links citing evidence that is not in the bundle are dropped, not
    guessed at. The grounding check should have caught that first; this is the
    second layer, and it is here because a scorer that silently accepted an
    unknown id would make the first layer optional.
    """
    computed_priors = priors if priors is not None else rule_priors(evidence)
    by_id = {str(item.id): item for item in evidence}

    scored: list[ScoredHypothesis] = []
    for link in links:
        prior = max(_MIN_PRIOR, min(1.0, computed_priors.get(link.root_cause, _MIN_PRIOR)))

        # Deduplicated, then capped. Both matter: a model repeating the same
        # id four times would otherwise manufacture corroboration out of one
        # observation.
        seen: list[str] = []
        for evidence_id in link.evidence_ids:
            if evidence_id in by_id and evidence_id not in seen:
                seen.append(evidence_id)
        usable = seen[:MAX_SUPPORTING_EVIDENCE]

        scored.append(
            ScoredHypothesis(
                root_cause=link.root_cause,
                confidence=min(_MAX_CONFIDENCE, prior * _support(usable, by_id)),
                prior=prior,
                supporting_evidence_ids=tuple(usable),
                rationale=link.rationale,
            )
        )

    # Highest confidence first, then by name so the order is reproducible.
    scored.sort(key=lambda hypothesis: (-hypothesis.confidence, hypothesis.root_cause.value))
    return tuple(scored)


def _support(evidence_ids: Sequence[str], by_id: dict[str, Evidence]) -> float:
    """How much the cited evidence backs a hypothesis, in [0, 1].

    Built from each observation's own confidence, with diminishing returns:
    the first piece carries most of the weight and the fourth adds little.
    That shape is deliberate. A linear sum would let quantity substitute for
    quality, and the cheapest thing for a model to produce is quantity.

    Returns 0.0 for no usable evidence, which collapses the hypothesis rather
    than leaving it at its prior - an unsupported hypothesis is not as good as
    an unexamined one.
    """
    if not evidence_ids:
        return 0.0
    remaining = 1.0
    support = 0.0
    for position, evidence_id in enumerate(evidence_ids):
        weight = 0.5**position
        support += by_id[evidence_id].confidence * weight * remaining
        remaining = max(0.0, 1.0 - support)
    return min(1.0, support)


@dataclass(frozen=True, slots=True)
class ScoringReport:
    """The hypotheses and what was thrown away reaching them."""

    hypotheses: tuple[ScoredHypothesis, ...] = ()
    dropped_evidence_ids: tuple[str, ...] = field(default_factory=tuple)
