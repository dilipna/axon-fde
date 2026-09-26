"""The OpenAI provider, and the three places its API differs from Anthropic's.

The `LLMProvider` protocol was written in B8 with the claim that a vendor could
be swapped without anything above it noticing. This suite is the test of that
claim, so most of it is about the *seams* rather than about happy-path calls:
the token accounting, the schema contract, and the fields that silently do
nothing on this vendor.

Nothing here touches the network. The client is injected.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from backend.app.config import Environment, LLMMode, LLMVendor, Settings
from backend.app.llm import build_provider
from backend.app.llm.openai_provider import (
    REASONING_MODELS,
    MissingOpenAIKeyError,
    OpenAIProvider,
)
from backend.app.llm.provider import LLMRequest

pytestmark = pytest.mark.unit


def settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "_env_file": None,
        "axon_env": Environment.LOCAL,
        "axon_llm_vendor": LLMVendor.OPENAI,
        "axon_llm_mode": LLMMode.LIVE,
        "openai_api_key": "sk-test-not-a-real-key",
    }
    return Settings(**{**base, **overrides})


def request(**overrides: Any) -> LLMRequest:
    base: dict[str, Any] = {
        "model": "gpt-4o-mini",
        "system": "You link evidence. You never author an observation.",
        "messages": ({"role": "user", "content": "Why is this trailer warming?"},),
        "max_tokens": 256,
    }
    return LLMRequest(**{**base, **overrides})


# ---------------------------------------------------------------------------
# A fake client, recording exactly what the provider sent
# ---------------------------------------------------------------------------


@dataclass
class _Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    prompt_tokens_details: Any = None


@dataclass
class _Details:
    cached_tokens: int = 0


class _FakeCompletions:
    def __init__(self, payload: Any) -> None:
        self._payload = payload
        self.received: dict[str, Any] = {}

    async def create(self, **kwargs: Any) -> Any:
        self.received = kwargs
        return self._payload


class _FakeClient:
    def __init__(self, payload: Any) -> None:
        self.chat = type("_Chat", (), {"completions": _FakeCompletions(payload)})()

    @property
    def sent(self) -> dict[str, Any]:
        return self.chat.completions.received


def _completion(
    *,
    text: str = "The compressor is losing capacity.",
    prompt_tokens: int = 1000,
    completion_tokens: int = 50,
    cached_tokens: int = 0,
    model: str = "gpt-4o-mini",
    finish_reason: str = "stop",
    usage: Any | _Usage | None = ...,  # type: ignore[assignment]
) -> Any:
    message = type("_Msg", (), {"content": text})()
    choice = type("_Choice", (), {"message": message, "finish_reason": finish_reason})()
    resolved = (
        _Usage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            prompt_tokens_details=_Details(cached_tokens=cached_tokens),
        )
        if usage is ...
        else usage
    )
    return type("_Completion", (), {"choices": [choice], "model": model, "usage": resolved})()


# ---------------------------------------------------------------------------
# Token accounting - the bug that would make caching look expensive
# ---------------------------------------------------------------------------


class TestTokenAccounting:
    async def test_cached_tokens_are_not_billed_twice(self):
        """OpenAI's `prompt_tokens` includes cached tokens; ours must not.

        `TokenUsage.input_tokens` is the *uncached* remainder, because
        `estimate_cost_usd` charges it at full rate and charges
        `cache_read_tokens` at a tenth. Passing OpenAI's total straight through
        would bill every cached token at both rates, so a well-cached call would
        cost *more* than an uncached one - inverting the thing caching is
        measured for, and doing it quietly.
        """
        client = _FakeClient(_completion(prompt_tokens=1000, cached_tokens=800))
        provider = OpenAIProvider(settings=settings(), client=client)

        response = await provider.complete(request())

        assert response.usage.input_tokens == 200
        assert response.usage.cache_read_tokens == 800
        # The total is still the total: nothing was lost in the split.
        assert response.usage.input_tokens + response.usage.cache_read_tokens == 1000

    async def test_a_cached_call_costs_less_than_an_uncached_one(self):
        """The property the split above exists to produce, asserted directly."""
        uncached = _FakeClient(_completion(prompt_tokens=1000, cached_tokens=0))
        cached = _FakeClient(_completion(prompt_tokens=1000, cached_tokens=900))

        a = await OpenAIProvider(settings=settings(), client=uncached).complete(request())
        b = await OpenAIProvider(settings=settings(), client=cached).complete(request())

        assert b.cost_usd() < a.cost_usd()

    async def test_cache_writes_are_zero_rather_than_guessed(self):
        """OpenAI has no cache-write charge and reports none. Zero is true here."""
        client = _FakeClient(_completion(prompt_tokens=500, cached_tokens=100))
        response = await OpenAIProvider(settings=settings(), client=client).complete(request())

        assert response.usage.cache_write_tokens == 0

    async def test_a_response_with_no_usage_is_refused_not_costed_at_zero(self):
        """An uncosted call would make the spend ceiling unenforceable.

        `provider.py` states that usage is not optional. A missing usage block
        defaulted to zero would pass the ledger check every time, on precisely
        the code path that failed to report.
        """
        client = _FakeClient(_completion(usage=None))
        provider = OpenAIProvider(settings=settings(), client=client)

        with pytest.raises(ValueError, match="no usage"):
            await provider.complete(request())


# ---------------------------------------------------------------------------
# What the provider actually sends
# ---------------------------------------------------------------------------


class TestTheRequest:
    async def test_the_system_prompt_leads_the_messages(self):
        """Caching here is an automatic prefix match, so order is the whole lever."""
        client = _FakeClient(_completion())
        await OpenAIProvider(settings=settings(), client=client).complete(request())

        messages = client.sent["messages"]
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"

    async def test_effort_is_sent_only_to_models_that_accept_it(self):
        """`reasoning_effort` on a non-reasoning model is a 400, not an ignored field.

        The failure would land on a paid call in record mode, which is the worst
        place to discover it.
        """
        plain = _FakeClient(_completion())
        await OpenAIProvider(settings=settings(), client=plain).complete(
            request(model="gpt-4o-mini", effort="high")
        )
        assert "reasoning_effort" not in plain.sent

        reasoning_model = next(iter(sorted(REASONING_MODELS)))
        smart = _FakeClient(_completion(model=reasoning_model))
        await OpenAIProvider(settings=settings(), client=smart).complete(
            request(model=reasoning_model, effort="low")
        )
        assert smart.sent["reasoning_effort"] == "low"

    async def test_a_schema_is_sent_as_a_strict_constraint(self):
        """Without `strict` the model may answer in prose and the schema is a hint.

        A hint fails open: the error surfaces as a KeyError wherever the payload
        is first dereferenced, rather than as a refusal at this boundary.
        """
        client = _FakeClient(_completion(text='{"cause": "compressor_degradation"}'))
        schema = {
            "type": "object",
            "properties": {"cause": {"type": "string"}},
            "required": ["cause"],
            "additionalProperties": False,
        }
        response = await OpenAIProvider(settings=settings(), client=client).complete(
            request(output_schema=schema)
        )

        sent = client.sent["response_format"]
        assert sent["type"] == "json_schema"
        assert sent["json_schema"]["strict"] is True
        assert sent["json_schema"]["schema"] == schema
        assert response.parsed == {"cause": "compressor_degradation"}

    async def test_prose_where_a_schema_was_promised_is_an_error(self):
        client = _FakeClient(_completion(text="The compressor is failing."))
        provider = OpenAIProvider(settings=settings(), client=client)

        with pytest.raises(ValueError, match="not JSON"):
            await provider.complete(request(output_schema={"type": "object"}))

    async def test_no_schema_means_no_speculative_parse(self):
        """Text that happens to look like JSON is still text."""
        client = _FakeClient(_completion(text='{"looks": "structured"}'))
        response = await OpenAIProvider(settings=settings(), client=client).complete(request())

        assert response.parsed is None


# ---------------------------------------------------------------------------
# The vendor switch, and the guarantees it must not weaken
# ---------------------------------------------------------------------------


class TestTheVendorSwitch:
    def test_the_factory_returns_the_configured_vendor(self):
        assert isinstance(build_provider(settings()), OpenAIProvider)

        from backend.app.llm.anthropic_provider import AnthropicProvider

        anthropic = build_provider(
            settings(axon_llm_vendor=LLMVendor.ANTHROPIC, anthropic_api_key="sk-ant-test")
        )
        assert isinstance(anthropic, AnthropicProvider)

    async def test_cassette_mode_needs_no_key_and_spends_nothing(self, tmp_path):
        """The default mode must stay free and offline on this vendor too.

        A vendor port that quietly made the offline path require credentials
        would break every test, the demo and CI at once - and the reason would
        look like a missing cassette.
        """
        from backend.app.llm.cassettes import CassetteMissError

        provider = OpenAIProvider(
            settings=settings(axon_llm_mode=LLMMode.CASSETTE, openai_api_key=None),
            cassette_dir=tmp_path,
        )

        with pytest.raises(CassetteMissError):
            await provider.complete(request())

    async def test_a_live_mode_without_a_key_is_refused_before_any_call(self):
        provider = OpenAIProvider(settings=settings(openai_api_key=None))

        with pytest.raises(MissingOpenAIKeyError, match="OPENAI_API_KEY"):
            await provider.complete(request())

    async def test_the_spend_ceiling_is_checked_before_the_request(self):
        """A refusal must not race a call that is already in flight."""
        client = _FakeClient(_completion())
        provider = OpenAIProvider(
            settings=settings(axon_daily_spend_limit_usd=0.0000001), client=client
        )

        from backend.app.llm.spend import SpendLimitExceededError

        with pytest.raises(SpendLimitExceededError):
            await provider.complete(request())
        assert client.sent == {}, "the API was called despite the refusal"

    async def test_a_live_response_is_not_marked_replayed(self):
        """A benchmark must never report replayed numbers as live ones."""
        client = _FakeClient(_completion())
        response = await OpenAIProvider(settings=settings(), client=client).complete(request())

        assert response.replayed is False
