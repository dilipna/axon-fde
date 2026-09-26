"""The OpenAI implementation, and where it genuinely differs from Anthropic's.

Same three modes as `AnthropicProvider`, same `LLMProvider` protocol, same
cassette library and spend ledger. The protocol was written in B8 so a vendor
could be swapped without anything above this line noticing, and this module is
the test of that claim: nothing outside `backend/app/llm/` changed to add it.

This is the only module permitted to import ``openai``, and an import-linter
contract enforces it.

**Three differences that are real, and are not smoothed over.**

1. **There is no cache-write, and no cache breakpoint to place.** Anthropic
   caching is explicit: you mark a prefix and pay a premium to write it.
   OpenAI's is automatic for prompts over ~1024 tokens, with nothing to mark
   and no write charge. So `LLMRequest.cache_prefix` has **no effect** here and
   `cache_write_tokens` is always 0. The flag is not rejected, because a
   request built for either vendor must stay portable, but it does nothing and
   saying so here is better than a reader assuming the prefix was marked.

   What survives is the number that matters: `cached_tokens` is still reported
   and still recorded, so "is caching working?" is still answerable, which is
   the whole reason B8 stored it.

2. **`effort` only exists on reasoning models.** Sending
   `reasoning_effort` to a non-reasoning model is an API error rather than an
   ignored field, so it is sent only for models declared as reasoning models in
   `REASONING_MODELS`. A model missing from that set silently loses its effort
   setting, which is why the set is explicit rather than a name pattern.

3. **Token counts are named differently and mean slightly different things.**
   OpenAI's `prompt_tokens` is the *total* input including cached tokens, while
   Anthropic's `input_tokens` excludes them. Adding OpenAI's figure straight
   into `TokenUsage.input_tokens` would bill every cached token twice - once at
   full rate and once at the cached rate - making a well-cached call look more
   expensive than an uncached one. The subtraction below is the fix, and it is
   the kind of thing that is invisible until a cost number is questioned.
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
    from openai import AsyncOpenAI

__all__ = ["DEFAULT_CASSETTE_DIR", "REASONING_MODELS", "MissingOpenAIKeyError", "OpenAIProvider"]

#: Committed to the repository, same as the Anthropic provider's. Cassettes are
#: reviewable artefacts and the two vendors share the directory: a cassette key
#: includes the model, so they cannot collide.
DEFAULT_CASSETTE_DIR = Path(__file__).resolve().parents[3] / "data" / "cassettes"

#: Models that accept `reasoning_effort`. Listed explicitly rather than matched
#: on a name prefix: OpenAI's naming has not been stable enough for a pattern,
#: and the failure mode of guessing wrong is a 400 on a paid call rather than a
#: degraded answer.
REASONING_MODELS: frozenset[str] = frozenset(
    {
        "o3",
        "o3-mini",
        "o4-mini",
        "gpt-5",
        "gpt-5-mini",
    }
)

#: OpenAI calls it `max_completion_tokens` on newer models and `max_tokens` on
#: older ones, and sending the wrong one is a 400. Newer wins by default
#: because that is what the configured models are.
_MAX_TOKENS_FIELD = "max_completion_tokens"


class MissingOpenAIKeyError(RuntimeError):
    """A mode that needs the API was selected without a key."""

    def __init__(self, mode: LLMMode) -> None:
        super().__init__(
            f"AXON_LLM_MODE={mode.value} needs OPENAI_API_KEY, which is not set. "
            "Leave the mode at `cassette` to run offline."
        )


class OpenAIProvider:
    """Talks to ChatGPT, or to a cassette, depending on the mode."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        cassette_dir: Path | None = None,
        ledger: SpendLedger | None = None,
        client: AsyncOpenAI | None = None,
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
            MissingOpenAIKeyError: A live mode without credentials.
        """
        mode = self._settings.axon_llm_mode
        if mode is LLMMode.CASSETTE:
            # No ledger check: a replay costs nothing, and charging the budget
            # for it would make a large offline suite refuse to run.
            return self._cassettes.load(request)

        # Checked before the client is built, so a refusal cannot race a
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

        # The system prompt goes first as an ordinary message. There is no
        # cache breakpoint to place: OpenAI caches long prefixes automatically,
        # so `request.cache_prefix` is deliberately not consulted here. Keeping
        # the system content first is still what makes that automatic caching
        # effective, because it is a prefix match either way.
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": request.system},
            *[dict(message) for message in request.messages],
        ]

        kwargs: dict[str, Any] = {
            "model": request.model,
            "messages": messages,
            _MAX_TOKENS_FIELD: request.max_tokens,
        }
        if request.model in REASONING_MODELS:
            kwargs["reasoning_effort"] = request.effort
        if request.output_schema is not None:
            # `strict` is what makes this a constraint rather than a request.
            # Without it the model may return prose, and the failure surfaces
            # as a parse error at the point of use instead of a refusal here.
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "axon_structured_output",
                    "schema": request.output_schema,
                    "strict": True,
                },
            }

        completion = await client.chat.completions.create(**kwargs)
        latency_ms = int((time.perf_counter() - started) * 1000)

        choice = completion.choices[0]
        text = choice.message.content or ""

        usage = _usage_from(completion.usage)
        return LLMResponse(
            text=text,
            usage=usage,
            model=completion.model,
            stop_reason=choice.finish_reason,
            parsed=_parsed_from(text, request),
            replayed=False,
            latency_ms=latency_ms,
        )

    def _build_client(self) -> AsyncOpenAI:
        if self._settings.openai_api_key is None:
            raise MissingOpenAIKeyError(self._settings.axon_llm_mode)
        # Imported here rather than at module scope so the offline path never
        # pays for the import, and a missing optional dependency cannot break a
        # cassette-only run.
        from openai import AsyncOpenAI

        self._client = AsyncOpenAI(api_key=self._settings.openai_api_key.get_secret_value())
        return self._client


def _usage_from(raw: Any) -> TokenUsage:
    """OpenAI usage, converted without double-billing the cached tokens.

    `prompt_tokens` is the total input *including* anything served from cache,
    where `TokenUsage.input_tokens` means the uncached remainder - that is what
    `estimate_cost_usd` charges at full rate. Passing the total through would
    bill cached tokens twice and make a well-cached call read as more expensive
    than an uncached one, which is the opposite of the thing caching is
    measured for.
    """
    if raw is None:
        # Not defaulted to zero silently. A response with no usage would make
        # the spend ceiling unenforceable on exactly the path that forgot to
        # report, which `provider.py` calls out as not optional.
        raise ValueError(
            "the OpenAI response carried no usage block, so this call cannot be "
            "costed and the spend ceiling cannot be enforced against it"
        )

    prompt_tokens = int(getattr(raw, "prompt_tokens", 0) or 0)
    details = getattr(raw, "prompt_tokens_details", None)
    cached = int(getattr(details, "cached_tokens", 0) or 0) if details is not None else 0

    return TokenUsage(
        input_tokens=max(0, prompt_tokens - cached),
        output_tokens=int(getattr(raw, "completion_tokens", 0) or 0),
        cache_read_tokens=cached,
        # OpenAI does not charge for cache writes and does not report them.
        # Zero is the true value here, not a missing one.
        cache_write_tokens=0,
    )


def _parsed_from(text: str, request: LLMRequest) -> dict[str, Any] | None:
    """The structured payload, when one was asked for.

    OpenAI returns structured output as JSON in the message content rather than
    as a separate parsed field, so it is decoded here. A request that asked for
    no schema gets `None` rather than a speculative parse of prose that happens
    to start with a brace.
    """
    if request.output_schema is None:
        return None

    import json

    try:
        decoded = json.loads(text)
    except json.JSONDecodeError as exc:
        # Raised rather than returned as None. `strict` schema output means a
        # non-JSON body is a broken contract, and quietly handing back None
        # would push the failure to whichever caller dereferenced it first.
        raise ValueError(
            f"structured output was requested but the response was not JSON: {exc}"
        ) from exc

    if not isinstance(decoded, dict):
        raise ValueError(
            f"structured output must decode to an object, got {type(decoded).__name__}"
        )
    return decoded
