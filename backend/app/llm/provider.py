"""What the rest of the system may ask a language model, and what it gets back.

One narrow protocol, deliberately. Everything above this line - the workflow,
the hypothesis scorer, the narrator - talks to ``LLMProvider`` and never to
``anthropic``, so the model can be replaced by a cassette, a stub or a
different vendor without any of them noticing.

**The response carries no confidence and authors no observation.** That is
invariants I1 and I2 at the boundary where they are easiest to lose: a model
that could return a score would have that score persisted by somebody, and a
model that could return an observation would become a source. ``LLMResponse``
carries text, an optional parsed structure, and usage. Nothing else.

**Usage is not optional.** Every response must say what it cost, because the
spend ceiling and ``ModelInvocation`` both depend on it, and a provider that
could return "no usage available" would make the budget unenforceable on
exactly the path that forgot to report.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from backend.app.llm.pricing import estimate_cost_usd

__all__ = [
    "LLMProvider",
    "LLMRequest",
    "LLMResponse",
    "TokenUsage",
]


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """What one call consumed.

    ``cache_read_tokens`` is here rather than derived because it is the number
    that tells you whether prompt caching is actually working. Zero across
    repeated incidents means a silent invalidator has crept into the prefix -
    a timestamp, a UUID, an unsorted dict - and that is where the cost budget
    leaks. It is invisible unless something asserts on it, so it is stored on
    every invocation and a test asserts it is non-zero on a repeated prefix.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def cost_usd(self, model: str) -> float:
        return estimate_cost_usd(
            model=model,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cache_read_tokens=self.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens,
        )

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )


@dataclass(frozen=True, slots=True)
class LLMRequest:
    """One call, in a form that can be hashed, recorded and replayed.

    Every field that changes the model's answer is here, and nothing that does
    not. That is what makes the cassette key meaningful: two requests with the
    same key must be the same question, or a replay would answer a question
    nobody asked.
    """

    model: str
    #: The stable prefix. Put everything that does not change per incident
    #: here, because caching is a prefix match and the first differing byte
    #: invalidates the rest.
    system: str
    messages: tuple[dict[str, Any], ...]
    max_tokens: int = 4096
    #: JSON Schema the response must satisfy, when structured output is wanted.
    output_schema: dict[str, Any] | None = None
    #: `low` through `max`. Controls thinking depth and token spend.
    effort: str = "high"
    #: Which node asked. Recorded on the invocation so cost is attributable to
    #: a step rather than only to an incident.
    node: str = "unknown"
    prompt_version: str = "v1"
    #: Cache the stable prefix. On by default: the system prompt and tool
    #: definitions are identical across incidents, and not caching them is
    #: paying full price for the same tokens on every call.
    cache_prefix: bool = True

    def __post_init__(self) -> None:
        if not self.messages:
            raise ValueError("an LLM request must carry at least one message")
        if self.max_tokens < 1:
            raise ValueError(f"max_tokens={self.max_tokens} must be positive")


@dataclass(frozen=True, slots=True)
class LLMResponse:
    """What came back. No confidence, no observation, no authority."""

    text: str
    usage: TokenUsage
    model: str
    stop_reason: str | None = None
    #: The structured payload, when the request asked for one. Validated
    #: against the requested schema by the provider, so a caller never has to
    #: decide whether a missing key means "absent" or "malformed".
    parsed: dict[str, Any] | None = None
    #: True when this came from a cassette rather than the API. Carried so a
    #: benchmark cannot silently report replayed numbers as live ones.
    replayed: bool = False
    latency_ms: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    def cost_usd(self) -> float:
        return self.usage.cost_usd(self.model)


class LLMProvider(Protocol):
    """The only thing the rest of the system knows about language models."""

    @property
    def mode(self) -> str: ...

    async def complete(self, request: LLMRequest) -> LLMResponse: ...
