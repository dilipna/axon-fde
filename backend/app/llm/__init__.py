"""The language model boundary.

Everything above this package talks to `LLMProvider` and never to `anthropic`,
which is what lets the model be replaced by a cassette without any caller
noticing - and what keeps invariant I1 enforceable: nothing here can author an
observation, because `LLMResponse` has nowhere to put one.
"""

from backend.app.llm.anthropic_provider import AnthropicProvider, MissingAPIKeyError
from backend.app.llm.cassettes import CassetteLibrary, CassetteMissError, cassette_key
from backend.app.llm.pricing import UnknownModelError, estimate_cost_usd
from backend.app.llm.provider import LLMProvider, LLMRequest, LLMResponse, TokenUsage
from backend.app.llm.spend import SpendLedger, SpendLimitExceededError

__all__ = [
    "AnthropicProvider",
    "CassetteLibrary",
    "CassetteMissError",
    "LLMProvider",
    "LLMRequest",
    "LLMResponse",
    "MissingAPIKeyError",
    "SpendLedger",
    "SpendLimitExceededError",
    "TokenUsage",
    "UnknownModelError",
    "cassette_key",
    "estimate_cost_usd",
]
