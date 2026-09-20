"""The Anthropic implementation, and the mode switch around it.

Three modes, and the default is the one that cannot spend money:

- ``CASSETTE`` replays recordings and raises on a miss. No network, no key,
  no cost. This is what the test suite and the demo run under.
- ``RECORD`` calls the API and writes what comes back. Deliberate and
  chargeable.
- ``LIVE`` calls the API and records nothing.

``CASSETTE`` is the default in ``Settings`` rather than ``LIVE`` so that an
accidental run - a stray test, a misconfigured CI job, a script somebody
copied - fails with a missing cassette rather than with a bill.

**Prompt caching is on by default and its effectiveness is measured.** The
system prompt is the stable prefix and carries the cache breakpoint; the
per-incident messages come after it. ``cache_read_tokens`` is recorded on
every invocation precisely so that a silent invalidator creeping into the
prefix shows up as a number going to zero rather than as a larger bill three
weeks later.

This is the only module in the application permitted to import ``anthropic``,
and an import-linter contract enforces that. Everything else talks to
``LLMProvider``.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from backend.app.config import LLMMode, Settings, get_settings
from backend.app.llm.cassettes import CassetteLibrary
from backend.app.llm.provider import LLMRequest, LLMResponse, TokenUsage
from backend.app.llm.spend import SpendLedger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from anthropic import AsyncAnthropic

__all__ = ["DEFAULT_CASSETTE_DIR", "AnthropicProvider", "MissingAPIKeyError"]

#: Committed to the repository. Cassettes are reviewable artefacts: a pull
#: request that changes what the model was asked should show that in the diff.
DEFAULT_CASSETTE_DIR = Path(__file__).resolve().parents[3] / "data" / "cassettes"


class MissingAPIKeyError(RuntimeError):
    """A mode that needs the API was selected without a key."""

    def __init__(self, mode: LLMMode) -> None:
        super().__init__(
            f"AXON_LLM_MODE={mode.value} needs ANTHROPIC_API_KEY, which is not set. "
            "Leave the mode at `cassette` to run offline."
        )


class AnthropicProvider:
    """Talks to Claude, or to a cassette, depending on the mode."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        cassette_dir: Path | None = None,
        ledger: SpendLedger | None = None,
        client: AsyncAnthropic | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._cassettes = CassetteLibrary(cassette_dir or DEFAULT_CASSETTE_DIR)
        self._ledger = ledger or SpendLedger(self._settings.axon_daily_spend_limit_usd)
        self._client = client

    @property
    def mode(self) -> str:
        return self._settings.axon_llm_mode.value

    @property
    def ledger(self) -> SpendLedger:
        return self._ledger

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Answer the request from a cassette or from the API.

        Raises:
            CassetteMissError: Cassette mode, and nothing was recorded.
            SpendLimitExceededError: The call would breach the daily ceiling.
            MissingAPIKeyError: A live mode without credentials.
        """
        mode = self._settings.axon_llm_mode
        if mode is LLMMode.CASSETTE:
            # No ledger check: a replay costs nothing, and charging the budget
            # for it would make a large offline suite refuse to run.
            return self._cassettes.load(request)

        # Checked before the client is even built, so a refusal cannot race a
        # request that is already in flight.
        self._ledger.check(model=request.model, max_tokens=request.max_tokens)

        response = await self._call(request)
        self._ledger.record(response.cost_usd())

        if mode is LLMMode.RECORD:
            self._cassettes.save(request, response)
        return response

    async def _call(self, request: LLMRequest) -> LLMResponse:
        client = self._client or self._build_client()
        started = time.perf_counter()

        # The system prompt carries the cache breakpoint because it is the
        # stable prefix - identical across incidents - and caching is a prefix
        # match, so the breakpoint has to sit at the end of what does not
        # change. Putting it after the messages would cache nothing reusable.
        system: list[dict[str, Any]] = [{"type": "text", "text": request.system}]
        if request.cache_prefix:
            system[0]["cache_control"] = {"type": "ephemeral"}

        kwargs: dict[str, Any] = {
            "model": request.model,
            "max_tokens": request.max_tokens,
            "system": system,
            "messages": list(request.messages),
            "output_config": {"effort": request.effort},
        }
        if request.output_schema is not None:
            # Structured output is a constraint on the response, not a tool.
            # Asking for JSON in the prompt and parsing it is the alternative,
            # and it fails open: a model that returns prose produces a parse
            # error at the point of use rather than a refusal at the boundary.
            kwargs["output_config"]["format"] = {
                "type": "json_schema",
                "schema": request.output_schema,
            }

        message = await client.messages.create(**kwargs)
        latency_ms = int((time.perf_counter() - started) * 1000)

        text = "".join(
            block.text for block in message.content if getattr(block, "type", None) == "text"
        )
        usage = TokenUsage(
            input_tokens=message.usage.input_tokens,
            output_tokens=message.usage.output_tokens,
            cache_read_tokens=getattr(message.usage, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(message.usage, "cache_creation_input_tokens", 0) or 0,
        )
        return LLMResponse(
            text=text,
            usage=usage,
            model=message.model,
            stop_reason=message.stop_reason,
            parsed=getattr(message, "parsed_output", None),
            replayed=False,
            latency_ms=latency_ms,
        )

    def _build_client(self) -> AsyncAnthropic:
        if self._settings.anthropic_api_key is None:
            raise MissingAPIKeyError(self._settings.axon_llm_mode)
        # Imported here rather than at module scope so that the offline path
        # never pays for the import, and so a missing optional dependency
        # cannot break a cassette-only run.
        from anthropic import AsyncAnthropic

        self._client = AsyncAnthropic(api_key=self._settings.anthropic_api_key.get_secret_value())
        return self._client
