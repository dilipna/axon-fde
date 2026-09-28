"""The two workflow nodes that call a language model.

Everything else in the graph is a pure function of the state. These two are the
only places a model is consulted, and each is followed by a deterministic node
that can reject what it produced: `propose_links` by `score_hypotheses`, and
`narrate` by `check_grounding`.

**What comes back is filtered here, not trusted.** A cited handle that is not
in the digest is dropped and counted; a link left with no valid citation is
dropped, because `ProposedLink` refuses an empty citation list and an
unsupported hypothesis is a guess. Nothing the model returns is written into
the state until it has been through the digest's handle table, which is what
keeps a fabricated id from ever reaching the scorer.

**Every call leaves an invocation record in `notes["invocations"]`.** Tokens,
cost, latency and whether it was replayed. It is the source of C12's numbers,
and it exists on every call rather than only when somebody remembered, because
cost that is only reported by the code path that thought of reporting it is
cost that quietly goes unmeasured.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from backend.app.agents.degradation import MUST_NOT_DEGRADE, ModelUnavailableError
from backend.app.agents.digest import EvidenceDigest, build_digest
from backend.app.agents.prompts import (
    LINKS_SCHEMA,
    LINKS_SYSTEM,
    NARRATE_SCHEMA,
    NARRATE_SYSTEM,
    PROMPT_VERSION,
)
from backend.app.agents.scoring import ProposedLink
from backend.app.agents.state import IncidentState
from backend.app.domain.enums import RootCause
from backend.app.llm.provider import LLMProvider, LLMRequest, LLMResponse

__all__ = ["ModelNodes", "make_model_nodes"]

#: Generous ceilings. A reasoning model spends part of its completion budget on
#: thinking before it writes the answer, so a tight ceiling truncates the
#: answer rather than the thinking. The spend guard prices the worst case from
#: these, so they are also what bounds a runaway call.
LINKS_MAX_TOKENS = 6000
NARRATE_MAX_TOKENS = 4000


def _invocation(node: str, response: LLMResponse) -> dict[str, Any]:
    return {
        "node": node,
        "model": response.model,
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
        "cache_read_tokens": response.usage.cache_read_tokens,
        "cost_usd": response.cost_usd(),
        "latency_ms": response.latency_ms,
        "replayed": response.replayed,
        "stop_reason": response.stop_reason,
    }


def _with_invocation(state: IncidentState, record: dict[str, Any], **extra: Any) -> dict[str, Any]:
    notes = dict(state.get("notes", {}))
    notes["invocations"] = [*notes.get("invocations", []), record]
    notes.update(extra)
    return notes


class ModelNodes:
    """Builds the two model-facing node functions around one provider."""

    def __init__(
        self,
        provider: LLMProvider,
        *,
        links_model: str,
        narrate_model: str,
        effort: str = "low",
    ) -> None:
        self._provider = provider
        self._links_model = links_model
        self._narrate_model = narrate_model
        self._effort = effort

    async def _complete(self, request: LLMRequest) -> LLMResponse:
        """Call the provider, turning an outage into something the graph degrades on.

        Only the provider call is wrapped. A bug in this module's own parsing
        is still a bug and still raises; widening the net to it would turn a
        defect into a quiet `NO_LLM` run.
        """
        try:
            return await self._provider.complete(request)
        except MUST_NOT_DEGRADE:
            raise
        except Exception as exc:
            raise ModelUnavailableError(request.node, f"{type(exc).__name__}: {exc}") from exc

    @staticmethod
    def _digest(state: IncidentState) -> EvidenceDigest:
        return build_digest(state.get("evidence", []), conflicts=state.get("conflicts", []))

    async def propose_links(self, state: IncidentState) -> dict[str, Any]:
        digest = self._digest(state)
        response = await self._complete(
            LLMRequest(
                model=self._links_model,
                system=LINKS_SYSTEM,
                messages=({"role": "user", "content": digest.text},),
                max_tokens=LINKS_MAX_TOKENS,
                output_schema=LINKS_SCHEMA,
                effort=self._effort,
                node="propose_links",
                prompt_version=PROMPT_VERSION,
            )
        )

        if response.parsed is None:
            # No structure at all is a failure to answer, not an answer of "no
            # links". Treating it as the latter ran the incident on nothing and
            # reported FULL (failure matrix F4).
            raise ModelUnavailableError("propose_links", "the answer had no usable structure")
        links: list[ProposedLink] = []
        dropped_handles: list[str] = []
        raw_links = response.parsed.get("links", [])
        for raw in raw_links:
            valid = [h for h in raw.get("evidence_handles", []) if h in digest.handles]
            dropped_handles.extend(
                h for h in raw.get("evidence_handles", []) if h not in digest.handles
            )
            if not valid:
                continue
            try:
                cause = RootCause(raw["root_cause"])
            except ValueError:
                # Strict schema output should make this unreachable; if it is
                # reached the link is refused, not coerced to the nearest cause.
                continue
            links.append(
                ProposedLink(
                    root_cause=cause,
                    evidence_ids=tuple(digest.handles[h] for h in dict.fromkeys(valid)),
                    rationale=str(raw.get("rationale", "")),
                )
            )

        return {
            "proposed_links": links,
            "notes": _with_invocation(
                state,
                _invocation("propose_links", response),
                links_returned=len(raw_links),
                links_kept=len(links),
                handles_dropped=dropped_handles,
            ),
            "_tokens": response.usage.total_tokens,
        }

    async def narrate(self, state: IncidentState) -> dict[str, Any]:
        digest = self._digest(state)
        response = await self._complete(
            LLMRequest(
                model=self._narrate_model,
                system=NARRATE_SYSTEM,
                messages=({"role": "user", "content": _narrate_message(state, digest)},),
                max_tokens=NARRATE_MAX_TOKENS,
                output_schema=NARRATE_SCHEMA,
                effort=self._effort,
                node="narrate",
                prompt_version=PROMPT_VERSION,
            )
        )
        parsed = response.parsed or {}
        if not str(parsed.get("narrative", "")).strip():
            raise ModelUnavailableError("narrate", "the answer contained no narrative")
        cited = [str(h) for h in parsed.get("cited_handles", [])]
        # An unknown handle is not dropped: it is passed through as an id that
        # is not in the bundle, so the grounding check sees the fabrication and
        # rejects the narrative. Silently filtering it here would hide from the
        # check the very thing it exists to catch.
        cited_ids = [digest.handles.get(h, f"unknown-handle:{h}") for h in cited]
        return {
            "narrative": str(parsed.get("narrative", "")),
            "cited_evidence_ids": cited_ids,
            "notes": _with_invocation(state, _invocation("narrate", response)),
            "_tokens": response.usage.total_tokens,
        }


def _narrate_message(state: IncidentState, digest: EvidenceDigest) -> str:
    """The digest plus the computed figures the narrative may quote."""
    risk = state.get("risk")
    decision = state.get("decision")
    lines = [digest.text, "", "Computed figures (you may quote these):"]
    if risk is not None:
        lines.append(f"- probability the cargo breaches its envelope: {risk.probability:.2f}")
    lines.append(f"- cargo value: {state.get('cargo_value_usd', 0.0):.0f} USD")

    hypotheses = state.get("hypotheses", [])
    lines.append("")
    lines.append("Leading hypotheses, most likely first (scores withheld):")
    lines.extend(f"- {h.root_cause.value}" for h in hypotheses[:3])
    if not hypotheses:
        lines.append("- (none survived scoring)")

    lines.append("")
    lines.append("Interventions ranked by expected value, best first:")
    feasible = decision.feasible_options[:3] if decision else ()
    for rank, option in enumerate(feasible, start=1):
        lines.append(
            f"{rank}. {option.action.value}: direct cost {option.direct_cost_usd:.0f} USD, "
            f"delay {option.delay_minutes:.0f} min, "
            f"expected value {option.expected_value_usd:.0f} USD"
        )
    if not feasible:
        lines.append("(nothing feasible)")
    return "\n".join(lines)


def make_model_nodes(
    provider: LLMProvider,
    *,
    links_model: str,
    narrate_model: str,
    effort: str = "low",
) -> tuple[
    Callable[[IncidentState], Awaitable[dict[str, Any]]],
    Callable[[IncidentState], Awaitable[dict[str, Any]]],
]:
    """The `(propose_links, narrate)` pair `IncidentWorkflow` takes."""
    nodes = ModelNodes(
        provider, links_model=links_model, narrate_model=narrate_model, effort=effort
    )
    return nodes.propose_links, nodes.narrate
