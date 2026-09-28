"""The `NO_LLM` rung of the degradation ladder: what the workflow does without a model.

`docs/architecture/overview.md` §7 has promised this since the architecture was
written - "rule hypotheses, rule actions, templated narrative" - and until
C13's failure-injection suite ran, none of it existed: a provider timeout
propagated straight out of the graph and the run lost everything it had
established. See `docs/evaluation/failure_matrix.md`, rows F1-F4.

**Why degrading is safe here, and where it is not.** The model in this system
proposes links and writes prose; it never owns a number (I2) and never chooses
the action (the decision engine does). So losing it costs explanation quality
and nothing that the recommendation rests on - C5 measured exactly that, with
the action identical on all 44 investigated incidents. What must *not*
degrade is a configuration or integrity failure: a missing key, an unknown
model, a cassette miss. Those are raised, because a system that quietly ran
without its model would look healthy, and a benchmark that quietly replaced
its LLM arm with rules would report the wrong arm under the right name.

**The template is checked like anything else.** Its narrative cites real
evidence and states only figures the run computed, and it passes through the
same `check_grounding` node a model's narrative does. It is not exempted
because it was written by code: an exemption is the kind of special case that
outlives the reason it was granted.
"""

from __future__ import annotations

from backend.app.agents.scoring import rule_priors
from backend.app.agents.state import IncidentState
from backend.app.llm.cassettes import CassetteMissError
from backend.app.llm.pricing import UnknownModelError
from backend.app.llm.provider import ProviderConfigurationError

__all__ = [
    "MUST_NOT_DEGRADE",
    "NO_LLM",
    "ModelUnavailableError",
    "rule_ranking",
    "templated_narrative",
]

NO_LLM = "NO_LLM"

#: Failures that are raised rather than degraded. Each one means the system
#: is misconfigured or its measurement harness is broken - not that a
#: dependency is down - and degrading would hide it.
MUST_NOT_DEGRADE: tuple[type[BaseException], ...] = (
    CassetteMissError,
    ProviderConfigurationError,
    UnknownModelError,
)


class ModelUnavailableError(RuntimeError):
    """A model node could not get a usable answer. The workflow degrades on it.

    Raised by the model nodes for an outage, a rate limit, a spend ceiling, or
    an answer with no usable structure - and never for the failures in
    `MUST_NOT_DEGRADE`.
    """

    def __init__(self, node: str, why: str) -> None:
        super().__init__(f"{node}: {why}")
        self.node = node
        self.why = why


def rule_ranking(state: IncidentState) -> list[dict[str, object]]:
    """The rules' own cause ranking, as the `NO_LLM` hypotheses.

    Ranked by prior, ties broken by name so the order is stable - the same
    ordering the benchmark's rules-only arm uses. These are priors, not
    confidences: nothing was linked to them, so no support was computed, and
    they are stored as what they are rather than dressed as scored hypotheses.
    """
    priors = rule_priors(state.get("evidence", []))
    ordered = sorted(priors.items(), key=lambda kv: (-kv[1], kv[0].value))
    return [{"root_cause": cause.value, "prior": round(prior, 4)} for cause, prior in ordered]


def templated_narrative(state: IncidentState) -> tuple[str, list[str]]:
    """A fixed-template narrative over computed figures, and what it cites.

    Every figure is either the value of an evidence item it cites (the latest
    reading, the contractual ceiling) or one the run computed (the breach
    probability, the recommended option's cost and delay), so the grounding
    check can verify each one. The degradation reason is deliberately *not*
    quoted: it can carry numbers - a status code, a dollar ceiling - that no
    evidence supports.
    """
    evidence = state.get("evidence", [])
    envelope = state.get("envelope")
    risk = state.get("risk")
    decision = state.get("decision")
    best = decision.recommended if decision else None

    cited: list[str] = []
    parts: list[str] = []

    readings = [item for item in evidence if item.observation_type == "cargo_temp_c"]
    latest = max(readings, key=lambda item: item.observed_at) if readings else None
    if latest is not None and envelope is not None:
        ceiling_id = envelope.source_evidence_ids[1]
        cited.extend([str(latest.id), ceiling_id])
        parts.append(
            f"The latest cargo reading is {float(latest.value):.1f} C against a permitted "  # type: ignore[arg-type]
            f"ceiling of {envelope.maximum_c:.1f} C."
        )
    if risk is not None:
        horizon = "within the hour" if risk.horizon_minutes == 60 else "within the forecast horizon"
        parts.append(
            f"The estimated probability of breaching it {horizon} is {risk.probability:.2f}."
        )

    ranking = rule_ranking(state)
    if ranking:
        cause = str(ranking[0]["root_cause"]).replace("_", " ")
        parts.append(f"The leading rule-derived cause is {cause}.")
    if best is not None:
        parts.append(
            f"Recommended action: {best.action.value.replace('_', ' ')}, at a direct cost of "
            f"{best.direct_cost_usd:.2f} USD and {best.delay_minutes:.0f} minutes of delay."
        )
    parts.append(
        "This summary was produced from a fixed template because the language model "
        "was unavailable."
    )
    return " ".join(parts), cited
