"""C13: every row of the failure matrix, run against the shipped workflow.

The rows and their expected outcomes are pre-registered in
`docs/evaluation/failure_matrix.md`. Before the `NO_LLM` rung existed these
tests found 5 of 11 rows wrong: an LLM outage of any kind crashed the
workflow, an unusable answer ran silently as `FULL`, and missing facility data
was read as "no capacity" and produced a recommendation 7.6x worse in
expected value.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.app.agents.degradation import MUST_NOT_DEGRADE
from backend.app.llm.provider import LLMRequest, ProviderConfigurationError
from benchmarks.axonbench.claims import CLAIMS
from benchmarks.axonbench.failure_injection import (
    ROWS,
    ScriptedProvider,
    Variant,
    _execute,
    run_row,
)
from benchmarks.axonbench.graders.degradation import DegradationGrader

pytestmark = pytest.mark.agent

MATRIX = Path(__file__).resolve().parents[2] / "docs" / "evaluation" / "failure_matrix.md"


@pytest.mark.parametrize("row", ROWS, ids=[row.row_id for row in ROWS])
async def test_each_failure_matrix_row_degrades_as_registered(row) -> None:  # type: ignore[no-untyped-def]
    result = await run_row(row)
    assert result.correct, "\n".join(result.detail)
    assert not result.fabricated, "\n".join(result.detail)


def test_the_suite_runs_exactly_the_rows_the_matrix_registers() -> None:
    """Three places state the row count; they must not drift apart.

    The matrix document is the authority - it was committed before the suite -
    so a row added to the suite without the document, or to the document
    without the suite, fails here rather than quietly changing what C13 means.
    """
    registered = re.findall(r"^\| \*\*(F\d+)\*\* \|", MATRIX.read_text(encoding="utf-8"), re.M)
    assert registered == [row.row_id for row in ROWS]
    assert CLAIMS["C13"].required_cases == len(ROWS)


async def test_a_missing_api_key_is_raised_not_degraded() -> None:
    """A misconfigured deployment must fail loudly.

    Degrading here would run without the model for ever and look healthy -
    the failure mode the `MUST_NOT_DEGRADE` list exists to prevent.
    """

    class NoKeyError(ProviderConfigurationError):
        pass

    def fail(_request: LLMRequest) -> BaseException:
        return NoKeyError("AXON_LLM_MODE=live needs a key")

    run = await _execute(Variant("no key", lambda: ScriptedProvider(fail={"propose_links": fail})))
    assert isinstance(run.raised, NoKeyError)
    assert ProviderConfigurationError in MUST_NOT_DEGRADE


async def test_the_templated_narrative_passes_the_same_grounding_check() -> None:
    """Not exempted: it went through `check_grounding` and was found grounded."""
    run = await _execute(
        Variant("down", lambda: ScriptedProvider(fail={"propose_links": lambda _r: TimeoutError()}))
    )
    assert run.state is not None
    assert run.state["narrative_source"] == "template"
    assert run.state["grounding"] is not None and run.state["grounding"].grounded
    assert run.state["notes"]["rule_ranking"], "NO_LLM must still rank causes, from the rules"
    # Only one model call was attempted: a down model is not asked again.
    assert run.provider.calls == ["propose_links"]


class TestTheGraderCanFail:
    """A grader that passes everything is worse than none."""

    @staticmethod
    def _row(row_id: str, *, correct: bool = True, fabricated: bool = False) -> SimpleNamespace:
        return SimpleNamespace(row_id=row_id, correct=correct, fabricated=fabricated, detail=())

    def test_a_fabricating_row_fails_the_gate(self) -> None:
        rows = [self._row(f"F{i}") for i in range(1, 11)] + [self._row("F11", fabricated=True)]
        result = DegradationGrader(rows).grade()
        assert result.measurement.value > 0
        assert not result.passed_gate

    def test_a_wrongly_degraded_row_lowers_the_companion_not_the_gate(self) -> None:
        rows = [self._row(f"F{i}") for i in range(1, 11)] + [self._row("F11", correct=False)]
        result = DegradationGrader(rows).grade()
        assert result.passed_gate
        assert result.measurement.companions["correct_degradation_rate"] == pytest.approx(10 / 11)
        assert result.measurement.companions["F11_correct"] == 0.0

    def test_too_few_rows_is_insufficient_data(self) -> None:
        result = DegradationGrader([self._row("F1")]).grade()
        assert result.status.value == "INSUFFICIENT_DATA"
