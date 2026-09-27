"""The language model boundary.

Everything above this package talks to `LLMProvider` and never to a vendor SDK,
which is what lets the model be replaced by a cassette - or by a different
vendor - without any caller noticing, and what keeps invariant I1 enforceable:
nothing here can author an observation, because `LLMResponse` has nowhere to
put one.

Two implementations exist. `build_provider()` returns whichever
`AXON_LLM_VENDOR` selects; call that rather than naming a provider class, or
the next vendor change becomes an edit at every call site.
"""

# Required, not stylistic: `build_provider`'s return and parameter
# annotations name types imported only under TYPE_CHECKING, and without
# postponed evaluation they are resolved at def time and raise NameError.
# mypy passes either way - it never executes the module - so the only
# thing that catches this is importing the package.
from __future__ import annotations

from typing import TYPE_CHECKING

from backend.app.llm.anthropic_provider import AnthropicProvider, MissingAPIKeyError
from backend.app.llm.cassettes import CassetteLibrary, CassetteMissError, cassette_key
from backend.app.llm.openai_provider import MissingOpenAIKeyError, OpenAIProvider
from backend.app.llm.pricing import UnknownModelError, estimate_cost_usd
from backend.app.llm.provider import LLMProvider, LLMRequest, LLMResponse, TokenUsage
from backend.app.llm.spend import SpendLedger, SpendLimitExceededError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from backend.app.config import Settings

GROQ_BASE_URL = "https://api.groq.com/openai/v1"

__all__ = [
    "AnthropicProvider",
    "CassetteLibrary",
    "CassetteMissError",
    "LLMProvider",
    "LLMRequest",
    "LLMResponse",
    "MissingAPIKeyError",
    "MissingOpenAIKeyError",
    "OpenAIProvider",
    "SpendLedger",
    "SpendLimitExceededError",
    "TokenUsage",
    "UnknownModelError",
    "build_provider",
    "cassette_key",
    "estimate_cost_usd",
]


def build_provider(settings: Settings | None = None) -> LLMProvider:
    """The provider the configuration selects.

    One place where the vendor switch happens, so no caller has to know there
    are two implementations. Added when OpenAI joined Anthropic: before that
    every call site named `AnthropicProvider` directly, and switching vendors
    would have meant editing each one - which is how a codebase ends up half
    migrated with nothing reporting it.
    """
    from backend.app.config import LLMVendor, get_settings

    resolved = settings or get_settings()
    if resolved.axon_llm_vendor is LLMVendor.OPENAI:
        return OpenAIProvider(settings=resolved)
    if resolved.axon_llm_vendor is LLMVendor.GROQ:
        # Groq's free tier answers a burst with 429 and a retry-after, so the
        # client is told to wait and retry rather than fail the whole run.
        return OpenAIProvider(settings=resolved, base_url=GROQ_BASE_URL, max_retries=20)
    return AnthropicProvider(settings=resolved)
