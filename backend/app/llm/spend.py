"""The daily spend ceiling.

A careless benchmark loop can burn a month's budget in an hour, and the
failure is silent while it happens: every call succeeds, the results look
fine, and the bill arrives later. So the ceiling **refuses rather than warns**,
and it refuses *before* the call rather than noticing afterwards.

**What the guard can and cannot know before a call.** The output cost is
bounded exactly, because ``max_tokens`` is a hard ceiling the model cannot
exceed. The input cost is not knowable locally without a token count, and
counting costs a round trip of its own. So the check is:

    already spent  +  worst-case output cost of this call  >  limit  ->  refuse

which is conservative on the half that can run away (a long generation) and
retrospective on the half that cannot (the prompt, whose size the caller
already chose). The consequence is stated rather than glossed: the ceiling can
be overshot by at most one call's input cost, never by an unbounded amount,
and never by a runaway loop - because each iteration's spend lands in the
ledger before the next one is allowed to start.

The ledger is per process and in memory. That is honest for Phase 1, where the
thing being guarded against is a loop inside one process; a fleet of workers
needs a shared counter, and that belongs with the deployment rather than here.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from backend.app.llm.pricing import price_for

__all__ = ["SpendLedger", "SpendLimitExceededError"]


class SpendLimitExceededError(RuntimeError):
    """The daily ceiling would be exceeded. Nothing was sent."""

    def __init__(self, *, spent: float, projected: float, limit: float, model: str) -> None:
        super().__init__(
            f"refusing a {model} call: ${spent:.4f} already spent today and this "
            f"call could add up to ${projected:.4f}, over the ${limit:.2f} daily "
            "ceiling (AXON_DAILY_SPEND_LIMIT_USD). Nothing was sent."
        )
        self.spent = spent
        self.projected = projected
        self.limit = limit


class SpendLedger:
    """Tracks what has been spent today and refuses calls that would exceed it."""

    def __init__(self, limit_usd: float, *, today: date | None = None) -> None:
        if limit_usd < 0:
            raise ValueError(f"limit_usd={limit_usd} cannot be negative")
        self._limit = limit_usd
        self._day = today or datetime.now(UTC).date()
        self._spent = 0.0
        self._calls = 0

    @property
    def spent_usd(self) -> float:
        return self._spent

    @property
    def calls(self) -> int:
        return self._calls

    @property
    def limit_usd(self) -> float:
        return self._limit

    def _roll_over(self, now: date) -> None:
        """Reset at midnight UTC.

        Checked on every access rather than scheduled, so a long-running
        process does not hold yesterday's total for ever - which would turn a
        daily ceiling into a ceiling for the lifetime of the process.
        """
        if now != self._day:
            self._day = now
            self._spent = 0.0
            self._calls = 0

    def check(self, *, model: str, max_tokens: int, now: date | None = None) -> None:
        """Refuse if this call could take the day over the ceiling.

        Raises:
            SpendLimitExceededError: Before anything is sent.
        """
        self._roll_over(now or datetime.now(UTC).date())
        price = price_for(model)
        worst_case_output = max_tokens * price.output_per_mtok / 1_000_000
        projected = self._spent + worst_case_output
        if projected > self._limit:
            raise SpendLimitExceededError(
                spent=self._spent, projected=projected, limit=self._limit, model=model
            )

    def record(self, cost_usd: float, *, now: date | None = None) -> float:
        """Add an actual cost to the day's total, returning the new total.

        Actual, not projected. The check uses a worst case so it can refuse in
        advance; the ledger holds what was really billed, so the next check is
        made against reality rather than against an accumulating pessimism that
        would refuse long before the real ceiling.
        """
        self._roll_over(now or datetime.now(UTC).date())
        if cost_usd < 0:
            raise ValueError(f"cost_usd={cost_usd} cannot be negative")
        self._spent += cost_usd
        self._calls += 1
        return self._spent
