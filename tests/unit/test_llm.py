"""The model boundary: cassettes, the spend ceiling, and what a call costs.

The headline property here is that a cassette miss **fails loudly**. A suite
that re-records on a miss asserts whatever the model said this morning, keeps
passing, and shows nothing in the diff but cassette files - so the thing the
tests protected stops being protected without anybody deciding that.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from backend.app.config import LLMMode, Settings
from backend.app.llm.anthropic_provider import AnthropicProvider, MissingAPIKeyError
from backend.app.llm.cassettes import CassetteLibrary, CassetteMissError, cassette_key
from backend.app.llm.pricing import (
    CACHE_READ_MULTIPLIER,
    UnknownModelError,
    estimate_cost_usd,
)
from backend.app.llm.provider import LLMRequest, LLMResponse, TokenUsage
from backend.app.llm.spend import SpendLedger, SpendLimitExceededError


def request(**overrides: object) -> LLMRequest:
    base: dict[str, object] = {
        "model": "claude-opus-5",
        "system": "You link and interpret evidence. You never author an observation.",
        "messages": ({"role": "user", "content": "Summarise incident AX-042."},),
        "max_tokens": 1024,
        "node": "narrate",
        "prompt_version": "v1",
    }
    base.update(overrides)
    return LLMRequest(**base)  # type: ignore[arg-type]


def response(**overrides: object) -> LLMResponse:
    base: dict[str, object] = {
        "text": "The compressor is degrading.",
        "usage": TokenUsage(input_tokens=1200, output_tokens=180, cache_read_tokens=4000),
        "model": "claude-opus-5",
        "stop_reason": "end_turn",
        "latency_ms": 900,
    }
    base.update(overrides)
    return LLMResponse(**base)  # type: ignore[arg-type]


def settings(**overrides: object) -> Settings:
    base: dict[str, object] = {"_env_file": None, "axon_llm_mode": LLMMode.CASSETTE}
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Cassettes
# ---------------------------------------------------------------------------


class TestACassetteMissFailsLoudly:
    async def test_a_missing_cassette_raises_rather_than_calling_the_api(
        self, tmp_path: Path
    ) -> None:
        """The B8 acceptance criterion, and the reason cassettes are safe.

        Nothing is recorded, nothing is sent, and the error names the command
        that would re-record on purpose.
        """
        provider = AnthropicProvider(settings=settings(), cassette_dir=tmp_path)
        with pytest.raises(CassetteMissError) as raised:
            await provider.complete(request())

        assert "AXON_LLM_MODE=record" in str(raised.value)
        assert list(tmp_path.iterdir()) == [], "a miss must not write anything"

    async def test_an_edited_prompt_is_a_miss_rather_than_a_stale_replay(
        self, tmp_path: Path
    ) -> None:
        """Changing the prompt must not replay answers to the previous one.

        This is the failure that makes silent re-recording so damaging: the
        prompt changes, the cassette answers the *old* question, and the test
        reports that the edit had no effect.
        """
        library = CassetteLibrary(tmp_path)
        library.save(request(), response())
        provider = AnthropicProvider(settings=settings(), cassette_dir=tmp_path)

        # Same question, different system prompt.
        with pytest.raises(CassetteMissError):
            await provider.complete(request(system="A completely different instruction."))

    async def test_bumping_the_prompt_version_invalidates_its_cassettes(
        self, tmp_path: Path
    ) -> None:
        """A version bump is how a prompt edit is declared, so it must bite."""
        library = CassetteLibrary(tmp_path)
        library.save(request(prompt_version="v1"), response())
        provider = AnthropicProvider(settings=settings(), cassette_dir=tmp_path)

        with pytest.raises(CassetteMissError):
            await provider.complete(request(prompt_version="v2"))

    async def test_a_stored_cassette_replays_without_network(self, tmp_path: Path) -> None:
        library = CassetteLibrary(tmp_path)
        library.save(request(), response())
        provider = AnthropicProvider(settings=settings(), cassette_dir=tmp_path)

        replayed = await provider.complete(request())
        assert replayed.text == "The compressor is degrading."
        assert replayed.usage.cache_read_tokens == 4000
        assert replayed.replayed is True


class TestTheCassetteKey:
    def test_the_same_request_keys_the_same_way(self) -> None:
        assert cassette_key(request()) == cassette_key(request())

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("model", "claude-sonnet-5"),
            ("system", "different"),
            ("max_tokens", 2048),
            ("effort", "low"),
            ("prompt_version", "v9"),
            ("output_schema", {"type": "object"}),
        ],
    )
    def test_anything_that_changes_the_answer_changes_the_key(
        self, field: str, value: object
    ) -> None:
        assert cassette_key(request(**{field: value})) != cassette_key(request())

    def test_a_label_that_cannot_change_the_answer_does_not_churn_cassettes(self) -> None:
        """`node` is a label for attributing cost, not part of the question.

        Including it would mean moving a call between workflow nodes
        invalidated every recording, for no change in what was asked.
        """
        assert cassette_key(request(node="narrate")) == cassette_key(request(node="triage"))

    def test_a_billing_decision_does_not_change_the_key(self) -> None:
        """Whether the prefix is cached changes the price, not the answer."""
        assert cassette_key(request(cache_prefix=True)) == cassette_key(request(cache_prefix=False))


def test_a_recording_stores_the_prompt_beside_the_hash(tmp_path: Path) -> None:
    """A directory of opaque digests is unreviewable.

    The point of committing cassettes is that a human can read what the model
    was asked in a pull request, so the request is stored next to the answer.
    """
    path = CassetteLibrary(tmp_path).save(request(), response())
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["request"]["system"].startswith("You link and interpret")
    assert stored["response"]["usage"]["cache_read_tokens"] == 4000


def test_a_replayed_response_cannot_claim_to_be_live(tmp_path: Path) -> None:
    """Otherwise a benchmark could report replayed numbers as measured ones."""
    library = CassetteLibrary(tmp_path)
    library.save(request(), response(replayed=False))
    assert library.load(request()).replayed is True


# ---------------------------------------------------------------------------
# Spend
# ---------------------------------------------------------------------------


class TestTheSpendCeiling:
    def test_a_call_that_would_exceed_the_ceiling_is_refused_before_it_is_sent(
        self,
    ) -> None:
        """Refuses rather than warns, and refuses in advance.

        A warning after the fact is a description of the bill, not a control.
        """
        ledger = SpendLedger(limit_usd=1.00, today=date(2026, 9, 20))
        ledger.record(0.98, now=date(2026, 9, 20))
        with pytest.raises(SpendLimitExceededError, match="Nothing was sent"):
            ledger.check(model="claude-opus-5", max_tokens=4096, now=date(2026, 9, 20))

    def test_a_call_inside_the_ceiling_is_allowed(self) -> None:
        ledger = SpendLedger(limit_usd=5.00, today=date(2026, 9, 20))
        ledger.record(0.10, now=date(2026, 9, 20))
        ledger.check(model="claude-opus-5", max_tokens=4096, now=date(2026, 9, 20))

    def test_the_bound_is_the_worst_case_output_not_the_typical_one(self) -> None:
        """`max_tokens` is a hard ceiling, so the output cost is knowable exactly.

        128k of Opus output is $3.20. A ledger that projected a typical
        response instead would let a runaway generation through the one check
        that exists to stop it.
        """
        ledger = SpendLedger(limit_usd=3.00, today=date(2026, 9, 20))
        with pytest.raises(SpendLimitExceededError):
            ledger.check(model="claude-opus-5", max_tokens=128_000, now=date(2026, 9, 20))
        # The same empty ledger allows a small call.
        ledger.check(model="claude-opus-5", max_tokens=1024, now=date(2026, 9, 20))

    def test_a_runaway_loop_is_stopped_within_one_call_of_the_ceiling(self) -> None:
        """Each iteration's spend lands before the next is allowed to start.

        That is the property that matters: not that the ceiling is exact, but
        that it is not an unbounded overshoot.
        """
        ledger = SpendLedger(limit_usd=0.50, today=date(2026, 9, 20))
        calls = 0
        with pytest.raises(SpendLimitExceededError):
            for _ in range(10_000):
                ledger.check(model="claude-haiku-4-5", max_tokens=4096, now=date(2026, 9, 20))
                ledger.record(0.02, now=date(2026, 9, 20))
                calls += 1
        assert calls < 30, "the loop should be stopped promptly, not eventually"

    def test_the_ledger_resets_at_midnight(self) -> None:
        """A daily ceiling that never reset would be a lifetime ceiling.

        A long-running worker would otherwise hold yesterday's total for ever
        and refuse everything from its second day onward.
        """
        ledger = SpendLedger(limit_usd=1.00, today=date(2026, 9, 20))
        ledger.record(0.99, now=date(2026, 9, 20))
        ledger.check(model="claude-opus-5", max_tokens=1024, now=date(2026, 9, 21))
        assert ledger.spent_usd == 0.0

    async def test_replaying_a_cassette_does_not_consume_budget(self, tmp_path: Path) -> None:
        """An offline suite must not be refused for being large.

        A replay costs nothing, so charging the budget for it would make the
        ceiling a limit on how many tests may run.
        """
        library = CassetteLibrary(tmp_path)
        library.save(request(), response())
        ledger = SpendLedger(limit_usd=0.0, today=date(2026, 9, 20))
        provider = AnthropicProvider(settings=settings(), cassette_dir=tmp_path, ledger=ledger)
        await provider.complete(request())
        assert ledger.spent_usd == 0.0


# ---------------------------------------------------------------------------
# Cost
# ---------------------------------------------------------------------------


class TestCost:
    def test_cached_input_is_billed_at_a_fraction_of_normal_input(self) -> None:
        """Cache reads are a tenth of the input rate.

        Folding them into `input_tokens` would overcharge a well-cached call
        about tenfold, which would make caching look like it had made things
        worse - and somebody would then turn it off.
        """
        cached = estimate_cost_usd(
            model="claude-opus-5", input_tokens=0, output_tokens=0, cache_read_tokens=1_000_000
        )
        uncached = estimate_cost_usd(model="claude-opus-5", input_tokens=1_000_000, output_tokens=0)
        assert cached == pytest.approx(uncached * CACHE_READ_MULTIPLIER)

    def test_an_unpriced_model_raises_rather_than_costing_zero(self) -> None:
        """A model that appears free is the one a benchmark loop gets pointed at."""
        with pytest.raises(UnknownModelError, match="no price for"):
            estimate_cost_usd(model="claude-imaginary-9", input_tokens=10, output_tokens=10)

    def test_a_million_output_tokens_of_opus_costs_twenty_five_dollars(self) -> None:
        """One anchored number, so a units slip is visible.

        A factor of a thousand in either direction is otherwise invisible in a
        formula made entirely of divisions.
        """
        assert estimate_cost_usd(
            model="claude-opus-5", input_tokens=0, output_tokens=1_000_000
        ) == pytest.approx(25.00)


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------


def test_the_default_mode_cannot_spend_money() -> None:
    """Cassette, not live.

    An accidental run - a stray test, a misconfigured job, a copied script -
    should fail with a missing cassette rather than with a bill.
    """
    assert Settings(_env_file=None).axon_llm_mode is LLMMode.CASSETTE


async def test_a_live_mode_without_a_key_says_so_plainly(tmp_path: Path) -> None:
    provider = AnthropicProvider(
        settings=settings(axon_llm_mode=LLMMode.LIVE, anthropic_api_key=None),
        cassette_dir=tmp_path,
    )
    with pytest.raises(MissingAPIKeyError, match="ANTHROPIC_API_KEY"):
        await provider.complete(request())


def test_the_response_has_nowhere_to_put_an_observation() -> None:
    """Invariant I1, at the boundary where it is easiest to lose.

    A provider that could return a confidence would have that confidence
    persisted by somebody; one that could return an observation would become
    an `EvidenceSource`. Asserted on the type rather than on behaviour,
    because the protection is structural.
    """
    fields = set(LLMResponse.__dataclass_fields__)
    assert "confidence" not in fields
    assert "observation" not in fields
    assert "evidence" not in fields
