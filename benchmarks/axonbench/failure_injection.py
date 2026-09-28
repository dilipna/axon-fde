"""C13: the failure-injection suite, one run per row of the failure matrix.

The rows, and what each is expected to do, are pre-registered in
`docs/evaluation/failure_matrix.md`, which was committed before this file
existed. Change a row there first, with a date and a reason; this module only
executes the matrix.

**What runs is the shipped code.** The real `IncidentWorkflow`, the real graph
and the real model nodes (`make_model_nodes`), with one substitution: the
`LLMProvider` is a scripted stand-in that answers like a healthy model, or
raises exactly where a row says the dependency fails. Failures are injected at
the provider boundary - the same place an outage, a rate limit or a spend
ceiling would surface - so the suite exercises whatever the model nodes and
the graph actually do with them, rather than a double of it.

Nothing here opens a connection or calls a model. It is deterministic and
needs neither Docker nor a key, which is what lets it run in CI.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from backend.app.agents.budget import Budget, BudgetState
from backend.app.agents.graph import ESCALATE, IncidentWorkflow, build_graph, initial_state
from backend.app.agents.nodes.model_nodes import make_model_nodes
from backend.app.agents.state import IncidentState
from backend.app.decision.catalogue import FacilityOption
from backend.app.domain.enums import EvidenceSource, Modality
from backend.app.domain.evidence import EntityRef, Evidence, Provenance
from backend.app.llm.cassettes import CassetteMissError
from backend.app.llm.provider import LLMRequest, LLMResponse, TokenUsage
from backend.app.llm.spend import SpendLimitExceededError

__all__ = ["ROWS", "RowResult", "run_all", "run_row"]

START = datetime(2026, 9, 20, 14, 0, tzinfo=UTC)
NOW = START + timedelta(minutes=41)

#: A priced model id, so the model nodes' cost accounting runs as it does in
#: production. Nothing is called; the price only feeds the invocation record.
MODEL = "openai/gpt-oss-120b"

GARY = FacilityOption(
    facility_id="CS-11",
    name="Gary Cold Storage",
    capabilities=frozenset({"cold_storage", "pharma_certified", "trailer_swap"}),
    slots_available=3,
    detour_minutes=45.0,
)


# -- the incident every row starts from ---------------------------------------


def _observation(
    observation_type: str,
    value: float,
    *,
    at: datetime,
    source: EvidenceSource = EvidenceSource.TELEMETRY,
    modality: Modality = Modality.TIMESERIES,
) -> Evidence:
    return Evidence.create(
        entity_ref=EntityRef(kind="vehicle", id="AX-042"),
        source=source,
        modality=modality,
        observation_type=observation_type,
        value=value,
        observed_at=at,
        ingested_at=at + timedelta(seconds=5),
        provenance=Provenance(
            producer="failure_injection", producer_version="1.0.0", note="C13 fixture"
        ),
    )


def _envelope(source: EvidenceSource, maximum: float) -> list[Evidence]:
    modality = (
        Modality.TEXT if source is EvidenceSource.DOCUMENT_EXTRACTION else Modality.STRUCTURED
    )
    return [
        _observation("permitted_temp_min_c", 2.0, at=START, source=source, modality=modality),
        _observation("permitted_temp_max_c", maximum, at=START, source=source, modality=modality),
    ]


def _readings(minutes: range) -> list[Evidence]:
    """A degrading trailer: a steady climb toward an 8.0 C ceiling."""
    return [
        _observation("cargo_temp_c", 5.5 + 0.06 * minute, at=START + timedelta(minutes=minute))
        for minute in minutes
    ]


def healthy_bundle() -> list[Evidence]:
    """The flagship's shape: the ERP says 10 C, the signed document says 8 C.

    Both envelopes are present, so a healthy run has a real conflict to
    reconcile and the document's ceiling to judge against.
    """
    return [
        *_envelope(EvidenceSource.SQL_LEGACY, 10.0),
        *_envelope(EvidenceSource.DOCUMENT_EXTRACTION, 8.0),
        *_readings(range(41)),
    ]


# -- a provider that answers like a model, or fails where it is told to -------

_HANDLE = re.compile(r"\bE\d{2}\b")


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://llm.invalid/v1/chat/completions")
    return httpx.HTTPStatusError(
        f"HTTP {status} from the provider",
        request=request,
        response=httpx.Response(status, request=request),
    )


@dataclass
class ScriptedProvider:
    """Answers the two model nodes, or raises where a row injects a failure.

    ``fail`` maps a node name to a factory for the exception that node's call
    raises. A factory, because some failures - a cassette miss - are built
    from the request that missed.
    ``unusable`` names nodes whose answer comes back with no usable structure.
    ``fabricate`` makes the narrative cite a handle that does not exist.
    """

    fail: dict[str, Callable[[LLMRequest], BaseException]] = field(default_factory=dict)
    unusable: frozenset[str] = frozenset()
    fabricate: bool = False
    calls: list[str] = field(default_factory=list)

    @property
    def mode(self) -> str:
        return "scripted"

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request.node)
        failure = self.fail.get(request.node)
        if failure is not None:
            raise failure(request)

        usage = TokenUsage(input_tokens=1200, output_tokens=300)
        if request.node in self.unusable:
            return LLMResponse(text="", usage=usage, model=request.model, parsed=None)

        content = str(request.messages[0]["content"])
        handles = _HANDLE.findall(content)
        if request.node == "propose_links":
            parsed: dict[str, Any] = {
                "links": [
                    {
                        "root_cause": "compressor_degradation",
                        "evidence_handles": handles[-3:],
                        "rationale": "The cargo temperature climbs steadily.",
                    }
                ]
            }
        else:
            cited = ["E99"] if self.fabricate else handles[-1:]
            parsed = {
                "narrative": "The cargo temperature is climbing toward its permitted ceiling.",
                "cited_handles": cited,
            }
        return LLMResponse(text="", usage=usage, model=request.model, parsed=parsed)


# -- the rows -----------------------------------------------------------------


@dataclass(frozen=True)
class Variant:
    """One injected run. Most rows have one; F1 has one per error kind."""

    label: str
    provider: Callable[[], ScriptedProvider]
    evidence: Callable[[], list[Evidence]] = healthy_bundle
    facilities: tuple[FacilityOption, ...] = (GARY,)
    budget: Budget | None = None


@dataclass(frozen=True)
class Row:
    row_id: str
    title: str
    #: One of ``no_llm``, ``escalate``, ``raise``, ``full`` - the matrix's
    #: expected outcome, in the vocabulary `_judge` checks.
    expected: str
    variants: tuple[Variant, ...]
    #: Words the stated reason must contain, so an escalation for the wrong
    #: cause does not count as the right degradation.
    reason_mentions: tuple[str, ...] = ()
    #: The exception type a ``raise`` row must end in.
    raises: type[BaseException] | None = None


def _always(error: BaseException) -> Callable[[LLMRequest], BaseException]:
    return lambda _request: error


def _provider(**kwargs: Any) -> Callable[[], ScriptedProvider]:
    return lambda: ScriptedProvider(**kwargs)


_OUTAGES: tuple[tuple[str, BaseException], ...] = (
    ("connection refused", ConnectionError("connection refused")),
    ("timeout", TimeoutError("the provider did not answer in time")),
    ("HTTP 503", _http_error(503)),
    ("HTTP 429", _http_error(429)),
)

ROWS: tuple[Row, ...] = (
    Row(
        "F1",
        "LLM unreachable during propose_links",
        "no_llm",
        tuple(
            Variant(label, _provider(fail={"propose_links": _always(error)}))
            for label, error in _OUTAGES
        ),
    ),
    Row(
        "F2",
        "LLM unreachable during narrate",
        "no_llm",
        (Variant("timeout", _provider(fail={"narrate": _always(TimeoutError("timed out"))})),),
    ),
    Row(
        "F3",
        "daily spend ceiling reached",
        "no_llm",
        (
            Variant(
                "ceiling",
                _provider(
                    fail={
                        "propose_links": lambda request: SpendLimitExceededError(
                            spent=4.99, projected=0.02, limit=5.0, model=request.model
                        )
                    }
                ),
            ),
        ),
    ),
    Row(
        "F4",
        "model returns unusable output",
        "no_llm",
        (
            Variant("no links structure", _provider(unusable=frozenset({"propose_links"}))),
            Variant("no narrative", _provider(unusable=frozenset({"narrate"}))),
        ),
    ),
    Row(
        "F5",
        "model fabricates a citation",
        "escalate",
        (Variant("unknown handle", _provider(fabricate=True)),),
        reason_mentions=("not grounded",),
    ),
    Row(
        "F6",
        "cassette miss in replay mode",
        "raise",
        (
            Variant(
                "miss",
                _provider(
                    fail={
                        "propose_links": lambda request: CassetteMissError(
                            "0" * 16, request, Path("data/cassettes/missing.json")
                        )
                    }
                ),
            ),
        ),
        raises=CassetteMissError,
    ),
    Row(
        "F7",
        "no temperature envelope in the evidence",
        "escalate",
        (Variant("no envelope", _provider(), evidence=lambda: _readings(range(41))),),
        reason_mentions=("envelope",),
    ),
    Row(
        "F8",
        "sensor dropout",
        "escalate",
        (
            Variant(
                "three readings",
                _provider(),
                evidence=lambda: [
                    *_envelope(EvidenceSource.DOCUMENT_EXTRACTION, 8.0),
                    *_readings(range(38, 41)),
                ],
            ),
        ),
        reason_mentions=("could not be fitted",),
    ),
    Row(
        "F9",
        "per-incident budget exhausted",
        "escalate",
        (Variant("steps", _provider(), budget=Budget(max_steps=5)),),
        reason_mentions=("budget", "steps"),
    ),
    Row(
        "F10",
        "legacy ERP unreachable",
        "full",
        (
            Variant(
                "document only",
                _provider(),
                evidence=lambda: [
                    *_envelope(EvidenceSource.DOCUMENT_EXTRACTION, 8.0),
                    *_readings(range(41)),
                ],
            ),
        ),
    ),
    Row(
        "F11",
        "no facility data",
        "escalate",
        (
            Variant("no facilities", _provider(), facilities=()),
            # A total ERP outage: no ERP evidence *and* no facility list, which
            # is also read from the ERP. Amended into the matrix 2026-09-28.
            Variant(
                "ERP down entirely",
                _provider(),
                evidence=lambda: [
                    *_envelope(EvidenceSource.DOCUMENT_EXTRACTION, 8.0),
                    *_readings(range(41)),
                ],
                facilities=(),
            ),
        ),
        reason_mentions=("facilit",),
    ),
)


# -- running and judging ------------------------------------------------------


@dataclass(frozen=True)
class RowResult:
    row_id: str
    title: str
    expected: str
    correct: bool
    fabricated: bool
    #: One line per variant: what happened, and why it was judged as it was.
    detail: tuple[str, ...]


@dataclass
class _Run:
    state: IncidentState | None
    raised: BaseException | None
    provider: ScriptedProvider
    supplied_ids: frozenset[str]


async def _execute(variant: Variant) -> _Run:
    provider = variant.provider()
    propose, narrate = make_model_nodes(provider, links_model=MODEL, narrate_model=MODEL)
    flow = IncidentWorkflow(
        propose_links=propose,
        narrate=narrate,
        facilities=list(variant.facilities),
        now=lambda: NOW,
    )
    evidence = variant.evidence()
    state = initial_state(
        evidence=evidence,
        cargo_value_usd=184_000.0,
        budget=BudgetState(budget=variant.budget or Budget(), started_at=NOW),
        shipment_id="SH-2041",
        vehicle_id="AX-042",
    )
    supplied = frozenset(str(item.id) for item in evidence)
    try:
        final: IncidentState = await build_graph(flow).ainvoke(state)
    except Exception as exc:
        return _Run(state=None, raised=exc, provider=provider, supplied_ids=supplied)
    return _Run(state=final, raised=None, provider=provider, supplied_ids=supplied)


def _fabrications(run: _Run) -> list[str]:
    """Everything in the final state that nobody observed or computed."""
    state = run.state
    if state is None:
        return []
    found: list[str] = []
    final_ids = {str(item.id) for item in state.get("evidence", [])}
    if not final_ids <= run.supplied_ids:
        found.append(f"{len(final_ids - run.supplied_ids)} observations appeared from nowhere")
    visited = [entry["node"] for entry in state.get("trace", [])]
    if "request_approval" in visited:
        report = state.get("grounding")
        if report is None or not report.grounded:
            found.append("an ungrounded narrative reached the approval request")
        if state.get("degraded_mode") == "NO_LLM" and state.get("narrative_source") != "template":
            found.append("a model-authored narrative was presented while the model was down")
    if state.get("envelope") is None and state.get("risk") is not None:
        found.append("a risk probability with no envelope to judge it against")
    return found


def _judge(row: Row, run: _Run, healthy_action: str | None) -> tuple[bool, str]:
    if row.expected == "raise":
        ok = row.raises is not None and isinstance(run.raised, row.raises)
        return ok, f"raised {type(run.raised).__name__}" if run.raised else "did not raise"

    if run.raised is not None:
        return False, f"crashed: {type(run.raised).__name__}: {run.raised}"
    state = run.state
    assert state is not None
    visited = [entry["node"] for entry in state.get("trace", [])]
    reason = state.get("escalation_reason", "")
    mode = state.get("degraded_mode", "FULL")
    decision = state.get("decision")
    top = decision.recommended if decision else None
    action = top.action.value if top else None

    if row.expected == "escalate":
        ok = (
            visited[-1] == ESCALATE
            and "request_approval" not in visited
            and all(word in reason for word in row.reason_mentions)
        )
        return (
            ok,
            f"escalated: {reason!r}" if reason else f"did not escalate; ended at {visited[-1]}",
        )

    reached = "request_approval" in visited and visited[-1] != ESCALATE
    if row.expected == "no_llm":
        ok = reached and mode == "NO_LLM" and action == healthy_action
        return ok, (
            f"mode={mode}, reached approval={reached}, action={action} "
            f"(healthy: {healthy_action}), reason={reason!r}"
        )

    # full
    envelope = state.get("envelope")
    erp_present = any(
        item.source is EvidenceSource.SQL_LEGACY for item in state.get("evidence", [])
    )
    ok = reached and mode == "FULL" and envelope is not None and not erp_present
    return ok, f"mode={mode}, reached approval={reached}, envelope={envelope is not None}"


async def _healthy_action() -> str | None:
    run = await _execute(Variant("healthy", _provider()))
    if run.state is None:
        raise RuntimeError(f"the healthy baseline run itself failed: {run.raised!r}")
    decision = run.state.get("decision")
    top = decision.recommended if decision else None
    return top.action.value if top else None


async def run_row(row: Row, *, healthy_action: str | None = None) -> RowResult:
    baseline = healthy_action if healthy_action is not None else await _healthy_action()
    correct = True
    fabricated = False
    lines: list[str] = []
    for variant in row.variants:
        run = await _execute(variant)
        ok, why = _judge(row, run, baseline)
        found = _fabrications(run)
        correct = correct and ok
        fabricated = fabricated or bool(found)
        verdict = "ok" if ok else "WRONG"
        lines.append(
            f"[{verdict}] {variant.label}: {why}"
            + (f"; FABRICATED: {', '.join(found)}" if found else "")
        )
    return RowResult(row.row_id, row.title, row.expected, correct, fabricated, tuple(lines))


async def _run_all_async() -> list[RowResult]:
    baseline = await _healthy_action()
    return [await run_row(row, healthy_action=baseline) for row in ROWS]


def run_all() -> list[RowResult]:
    """Every row of the matrix, in order. Synchronous for the runner."""
    return asyncio.run(_run_all_async())


if __name__ == "__main__":
    for result in run_all():
        status = "PASS" if result.correct and not result.fabricated else "FAIL"
        print(f"{status} {result.row_id} {result.title} (expected {result.expected})")
        for line in result.detail:
            print(f"      {line}")
