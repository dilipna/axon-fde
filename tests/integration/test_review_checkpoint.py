"""Persist review across disconnected Postgres savers in an owned temporary schema.

Run only against the disposable integration database. No existing application
schema is truncated by this test; the UUID schema is created and removed here.
"""

from typing import Any, cast
from uuid import uuid4

import pytest
import sqlalchemy as sa
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import Command
from psycopg.conninfo import make_conninfo

from backend.app.config import get_settings
from backend.app.db.app.session import build_engine
from tests.agent.test_durable_review import Harness

pytestmark = pytest.mark.integration


async def test_pending_review_survives_postgres_connection_and_graph_reconstruction(
    app_db_ready: None,
) -> None:
    schema = "review_test_" + uuid4().hex
    engine = build_engine(owner=True)
    created = False
    try:
        async with engine.begin() as connection:
            await connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
            created = True
        dsn = make_conninfo(
            get_settings().postgres_dsn.replace("postgresql+asyncpg://", "postgresql://", 1),
            options=f"-csearch_path={schema}",
        )
        h = Harness()
        async with AsyncPostgresSaver.from_conn_string(dsn) as saver:
            await saver.setup()
            h.saver = cast(Any, saver)
            paused = await h.graph().ainvoke(h.state, h.config)
            assert paused["__interrupt__"]
            tokens_before = paused["budget"].tokens
            assert h.effects == []
        # No connection, graph instance or saver survives this boundary.
        h.grant()
        async with AsyncPostgresSaver.from_conn_string(dsn) as saver:
            h.saver = cast(Any, saver)
            final = await h.graph().ainvoke(Command(resume=h.response()), h.config)
            assert not final.get("escalation_reason")
            assert final["budget"].tokens == tokens_before
            assert final["budget"].steps == 14
            assert len(h.effects) == 1
    finally:
        if created:
            # Identifier is a fixed prefix + generated UUID, never user input.
            assert schema.startswith("review_test_") and len(schema) == 44
            async with engine.begin() as connection:
                await connection.execute(sa.text(f'DROP SCHEMA "{schema}" CASCADE'))
        await engine.dispose()
