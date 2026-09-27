"""What a model call costs, in dollars.

Kept in one table so that "what did this incident cost?" is a query rather
than a spreadsheet, and so the spend ceiling and the stored ``ModelInvocation``
cannot disagree about the arithmetic.

**Prices are per million tokens and are a snapshot.** They are not read from
the API - there is no endpoint for them - so they are configuration that can
go stale. That is stated rather than hidden: ``PRICES_AS_OF`` is part of every
cost estimate's provenance, and a model with no entry raises rather than being
costed at zero. A silent zero would make an uncosted model look free, which is
precisely the model somebody would then run a benchmark loop against.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "CACHE_READ_MULTIPLIER",
    "CACHE_WRITE_MULTIPLIER",
    "PRICES_AS_OF",
    "ModelPrice",
    "UnknownModelError",
    "estimate_cost_usd",
    "price_for",
]

#: When the table below was last checked against published pricing.
PRICES_AS_OF = "2026-09-26"

#: Fallback ratios, used only when a model does not state its own cached rate.
#:
#: **These were a single global ratio and that was wrong.** The docstring here
#: claimed they "apply uniformly across the models", which held while the table
#: contained three Anthropic models and broke the moment OpenAI's joined:
#: gpt-5 does cache reads at 0.1x its input rate, but the gpt-4.1 family is at
#: 0.25x and the gpt-4o family at 0.5x. A flat 0.1 **under-charged cached reads
#: by two and a half to five times** - and the spend ceiling is computed from
#: this, so it under-estimated in exactly the direction that lets a budget be
#: overrun. Per-model `cached_input_per_mtok` is now preferred and these are the
#: fallback for models that do not publish one.
CACHE_READ_MULTIPLIER = 0.1
CACHE_WRITE_MULTIPLIER = 1.25


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """Dollars per million tokens."""

    input_per_mtok: float
    output_per_mtok: float
    #: The published rate for a cached input token, when the vendor states one.
    #: `None` falls back to `input_per_mtok * CACHE_READ_MULTIPLIER`, which is
    #: correct for Anthropic and for OpenAI's gpt-5 family and wrong by up to
    #: five times for its others - so an entry that can state this should.
    cached_input_per_mtok: float | None = None

    @property
    def cache_read_per_mtok(self) -> float:
        """What a cached input token actually costs."""
        if self.cached_input_per_mtok is not None:
            return self.cached_input_per_mtok
        return self.input_per_mtok * CACHE_READ_MULTIPLIER


#: Only the models this system is configured to use. Deliberately not the full
#: catalogue: an entry here is a statement that the model has been considered
#: for this workload, and a table of everything invites picking from it.
PRICES: dict[str, ModelPrice] = {
    # Anthropic.
    "claude-opus-5": ModelPrice(input_per_mtok=5.00, output_per_mtok=25.00),
    "claude-sonnet-5": ModelPrice(input_per_mtok=2.00, output_per_mtok=10.00),
    "claude-haiku-4-5": ModelPrice(input_per_mtok=1.00, output_per_mtok=5.00),
    # OpenAI. Checked against developers.openai.com/api/docs/pricing on the
    # date in `PRICES_AS_OF`, Standard tier. Each states its own cached rate
    # because the ratio is **not** uniform across these families: 0.1x for
    # gpt-5, 0.25x for gpt-4.1, 0.5x for gpt-4o.
    "gpt-5": ModelPrice(1.25, 10.00, cached_input_per_mtok=0.125),
    "gpt-5-mini": ModelPrice(0.25, 2.00, cached_input_per_mtok=0.025),
    "gpt-4.1": ModelPrice(2.00, 8.00, cached_input_per_mtok=0.50),
    "gpt-4.1-mini": ModelPrice(0.40, 1.60, cached_input_per_mtok=0.10),
    "gpt-4o": ModelPrice(2.50, 10.00, cached_input_per_mtok=1.25),
    "gpt-4o-mini": ModelPrice(0.15, 0.60, cached_input_per_mtok=0.075),
    # Groq, open-weight. **Not yet checked against Groq's pricing page** (unlike
    # the OpenAI rows): recorded from memory and to be verified before any C12
    # dollar figure is quoted. Actual spend on the free tier is $0; these price
    # the tokens at what the same usage would cost on the paid tier, which is
    # what a cost-per-incident claim should mean.
    "openai/gpt-oss-120b": ModelPrice(0.15, 0.75, cached_input_per_mtok=0.075),
    "openai/gpt-oss-20b": ModelPrice(0.075, 0.30, cached_input_per_mtok=0.0375),
}


class UnknownModelError(KeyError):
    """A model with no price entry.

    Fatal rather than defaulted. Costing an unpriced model at zero would make
    it look free to the spend ceiling, and the first thing anybody does with a
    model that appears free is run a benchmark loop against it.
    """

    def __init__(self, model: str) -> None:
        known = ", ".join(sorted(PRICES))
        super().__init__(
            f"no price for {model!r}; add it to backend/app/llm/pricing.py. Priced models: {known}"
        )


def price_for(model: str) -> ModelPrice:
    try:
        return PRICES[model]
    except KeyError:
        raise UnknownModelError(model) from None


def estimate_cost_usd(
    *,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    """Dollars for one call.

    ``input_tokens`` is the *uncached* count, as the API reports it: cached
    reads and cache writes are billed separately and are passed separately.
    Adding them into ``input_tokens`` would overcharge a cached call by about
    ten times, which would make caching look like it had made things worse.
    """
    price = price_for(model)
    per_token_in = price.input_per_mtok / 1_000_000
    per_token_out = price.output_per_mtok / 1_000_000
    # The model's own cached rate, not a global ratio. See CACHE_READ_MULTIPLIER
    # for why that distinction is worth up to five times the cached-read bill.
    per_token_cached = price.cache_read_per_mtok / 1_000_000
    return (
        input_tokens * per_token_in
        + cache_read_tokens * per_token_cached
        + cache_write_tokens * per_token_in * CACHE_WRITE_MULTIPLIER
        + output_tokens * per_token_out
    )
