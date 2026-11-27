"""Integration fixtures: a real Postgres seeded from seed/init.sql.

These tests run only when PG_MCP_INTEGRATION=1. The server connects with the
usual PG_* settings (as mcp_readonly); setup and state checks use
PG_ADMIN_DSN, a superuser connection to the same bookstore database.
"""

from __future__ import annotations

import asyncio
import os

import asyncpg
import pytest

from pg_mcp import server


def pytest_collection_modifyitems(config, items):
    if os.getenv("PG_MCP_INTEGRATION") == "1":
        return
    skip = pytest.mark.skip(reason="set PG_MCP_INTEGRATION=1 and point PG_* at a seeded database")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


ADMIN_DSN = os.getenv("PG_ADMIN_DSN", "postgresql://postgres:postgres@localhost:5432/bookstore")


def run(coro_fn, *args, **kwargs):
    """Run one tool call on a fresh event loop with a fresh pool."""

    async def _go():
        server._pool = None
        try:
            return await coro_fn(*args, **kwargs)
        finally:
            await server.close_pool()

    return asyncio.run(_go())


async def _admin(sql: str, *args):
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        return await conn.fetch(sql, *args)
    finally:
        await conn.close()


def admin(sql: str, *args):
    return asyncio.run(_admin(sql, *args))


async def _readonly_conn() -> asyncpg.Connection:
    return await asyncpg.connect(**server.PG_CONFIG)


STATE_SQL = """
SELECT (SELECT md5(string_agg(b::text, ',' ORDER BY id)) FROM books b) AS books,
       (SELECT md5(string_agg(a::text, ',' ORDER BY id)) FROM authors a) AS authors,
       (SELECT md5(string_agg(g::text, ',' ORDER BY id)) FROM genres g) AS genres,
       (SELECT last_value FROM books_id_seq) AS books_seq,
       (SELECT count(*) FROM pg_largeobject_metadata) AS large_objects,
       (SELECT count(*) FROM pg_class WHERE relname = 'stolen') AS stolen_tables,
       (SELECT count(*) FROM pg_locks WHERE locktype = 'advisory') AS advisory_locks
"""


@pytest.fixture
def db_state():
    """Snapshot of everything a contained query must leave unchanged."""
    return lambda: dict(admin(STATE_SQL)[0])
