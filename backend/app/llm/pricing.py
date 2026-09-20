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
PRICES_AS_OF = "2026-06-24"

#: Cached input is billed at a fraction of the normal input rate, and writing
#: to the cache costs more than not caching. Both are ratios rather than
#: absolute prices because they apply uniformly across the models here.
CACHE_READ_MULTIPLIER = 0.1
CACHE_WRITE_MULTIPLIER = 1.25


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """Dollars per million tokens."""

    input_per_mtok: float
    output_per_mtok: float


#: Only the models this system is configured to use. Deliberately not the full
#: catalogue: an entry here is a statement that the model has been considered
#: for this workload, and a table of everything invites picking from it.
PRICES: dict[str, ModelPrice] = {
    "claude-opus-5": ModelPrice(input_per_mtok=5.00, output_per_mtok=25.00),
    "claude-sonnet-5": ModelPrice(input_per_mtok=2.00, output_per_mtok=10.00),
    "claude-haiku-4-5": ModelPrice(input_per_mtok=1.00, output_per_mtok=5.00),
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
    return (
        input_tokens * per_token_in
        + cache_read_tokens * per_token_in * CACHE_READ_MULTIPLIER
        + cache_write_tokens * per_token_in * CACHE_WRITE_MULTIPLIER
        + output_tokens * per_token_out
    )
