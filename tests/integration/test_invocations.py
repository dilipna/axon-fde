"""Model invocations, stored so cost is a query rather than a spreadsheet.

The assertion that earns its place here is the one about
``cache_read_tokens``. Prompt caching failing is silent: every call succeeds,
every answer is right, and the only symptom is a bill that arrives three weeks
later. The number is stored on every row precisely so that a silent
invalidator in the prefix shows up as a column going to zero.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.db.app.repositories import IncidentRepository
from backend.app.db.app.repositories.invocation import InvocationRepository
from backend.app.domain.enums import DetectedBy, IncidentSeverity
from backend.app.llm.provider import LLMRequest, LLMResponse, TokenUsage

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 20, 14, 0, tzinfo=UTC)


def request(**overrides: object) -> LLMRequest:
    base: dict[str, object] = {
        "model": "claude-opus-5",
        "system": "A stable prefix, identical across incidents.",
        "messages": ({"role": "user", "content": "Narrate this incident."},),
        "node": "narrate",
        "prompt_version": "v1",
    }
    base.update(overrides)
    return LLMRequest(**base)  # type: ignore[arg-type]


def response(**overrides: object) -> LLMResponse:
    base: dict[str, object] = {
        "text": "The compressor is degrading.",
        "usage": TokenUsage(input_tokens=400, output_tokens=200, cache_read_tokens=3_000),
        "model": "claude-opus-5",
        "latency_ms": 1200,
    }
    base.update(overrides)
    return LLMResponse(**base)  # type: ignore[arg-type]


@pytest.fixture
def incident_maker(app_session: AsyncSession):
    async def make():
        return await IncidentRepository(app_session).create(
            correlation_key="vehicle:AX-042:thermal_excursion",
            incident_type="thermal_excursion",
            severity=IncidentSeverity.SEV2,
            entity_kind="vehicle",
            entity_id="AX-042",
            detected_by=DetectedBy.AXON,
            detected_at=NOW - timedelta(minutes=30),
        )

    return make


async def test_an_invocation_records_what_it_cost(
    app_session: AsyncSession, incident_maker
) -> None:
    incident = await incident_maker()
    repo = InvocationRepository(app_session)
    stored = await repo.record(request(), response(), incident_id=incident.id)

    assert stored.node == "narrate"
    assert stored.prompt_version == "v1"
    assert stored.cost_estimate_usd == pytest.approx(response().cost_usd())
    assert stored.cost_estimate_usd > 0


async def test_cache_reads_are_stored_and_are_non_zero_on_a_repeated_prefix(
    app_session: AsyncSession, incident_maker
) -> None:
    """The number that says whether prompt caching is working.

    Zero across repeated incidents means a silent invalidator has crept into
    the prefix - a timestamp, a UUID, an unsorted dict - and that is where the
    cost budget actually leaks. It is invisible unless something asserts on
    it, so this is the assertion.
    """
    incident = await incident_maker()
    repo = InvocationRepository(app_session)

    # First call writes the cache; the second and third read it.
    await repo.record(
        request(),
        response(usage=TokenUsage(input_tokens=400, output_tokens=200, cache_write_tokens=3_000)),
        incident_id=incident.id,
    )
    for _ in range(2):
        await repo.record(request(), response(), incident_id=incident.id)

    rows = await repo.for_incident(incident.id)
    assert len(rows) == 3
    assert rows[0].cache_read_tokens == 0, "the first call has nothing to read"
    assert all(row.cache_read_tokens > 0 for row in rows[1:]), (
        "repeated identical prefixes must hit the cache; zero here means a "
        "silent invalidator in the prompt prefix"
    )


async def test_cost_per_incident_is_a_query(app_session: AsyncSession, incident_maker) -> None:
    """Asked of the database by people who are not running the application."""
    incident = await incident_maker()
    repo = InvocationRepository(app_session)
    for _ in range(3):
        await repo.record(request(), response(), incident_id=incident.id)

    assert await repo.cost_for_incident(incident.id) == pytest.approx(3 * response().cost_usd())


async def test_an_incident_with_no_model_calls_costs_nothing(
    app_session: AsyncSession, incident_maker
) -> None:
    """Zero, not None.

    The rules-only arm makes no model calls at all, and its cost has to be
    comparable with the LLM arm's rather than being a missing value that a
    report has to special-case.
    """
    incident = await incident_maker()
    assert await InvocationRepository(app_session).cost_for_incident(incident.id) == 0.0


async def test_the_model_that_answered_is_recorded_not_the_one_requested(
    app_session: AsyncSession, incident_maker
) -> None:
    """A server-side fallback can answer on a different model.

    Costing the call at the requested model's price would understate it, and
    the provenance would name a model that never saw the prompt.
    """
    incident = await incident_maker()
    stored = await InvocationRepository(app_session).record(
        request(model="claude-opus-5"),
        response(model="claude-sonnet-5"),
        incident_id=incident.id,
    )
    assert stored.model_id == "claude-sonnet-5"
    assert stored.cost_estimate_usd == pytest.approx(response(model="claude-sonnet-5").cost_usd())
