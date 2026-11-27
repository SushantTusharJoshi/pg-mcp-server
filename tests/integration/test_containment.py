"""PG-F5: every corpus case is refused or contained against a real Postgres.

Three views of the same corpus:
1. through the query tool (all layers): refused before the database;
2. straight to the database as mcp_readonly inside a READ ONLY transaction
   (Python layers bypassed): the database rejects it;
3. queries the parser accepts on purpose (pg_sleep, LIMIT tricks): the
   statement timeout and the row cap contain them.
Database state is compared before and after every case.
"""

from __future__ import annotations

import asyncio
import time

import asyncpg
import pytest

from pg_mcp import server
from tests.integration.conftest import _readonly_conn, run
from tests.sql_corpus import DB_ALLOWS_CONTAINED_ELSEWHERE, DB_BLOCKS, LIMIT_BYPASS, REFUSED

pytestmark = pytest.mark.integration


def _data_lines(out: str) -> list[str]:
    body = out.split("\n")[1:]
    return [ln for ln in body if ln and not ln.startswith("(")]


@pytest.mark.parametrize("name", sorted(REFUSED))
def test_tool_refuses_before_the_database(name, db_state, monkeypatch):
    async def no_db():
        raise AssertionError(f"{name} reached the database")

    before = db_state()
    monkeypatch.setattr(server, "get_pool", no_db)
    out = asyncio.run(server.query(REFUSED[name]))
    assert out.startswith("Error:"), out
    assert db_state() == before


def test_corpus_lists_are_consistent():
    assert set(DB_BLOCKS) <= set(REFUSED)
    assert set(DB_ALLOWS_CONTAINED_ELSEWHERE) <= set(REFUSED)
    assert not set(DB_BLOCKS) & set(DB_ALLOWS_CONTAINED_ELSEWHERE)


async def _run_readonly(sql: str) -> None:
    conn = await _readonly_conn()
    try:
        async with conn.transaction(readonly=True):
            async for _ in conn.cursor(sql):
                pass
    finally:
        await conn.close()


@pytest.mark.parametrize("name", DB_BLOCKS)
def test_database_alone_rejects(name, db_state):
    before = db_state()
    with pytest.raises(asyncpg.PostgresError):
        asyncio.run(_run_readonly(REFUSED[name]))
    assert db_state() == before


def test_pool_reset_clears_session_state(db_state):
    """set_config(..., false) and session advisory locks would outlive the call
    on a pooled connection; asyncpg resets the connection on release."""

    async def go():
        pool = await asyncpg.create_pool(**server.PG_CONFIG, min_size=1, max_size=1)
        try:
            async with pool.acquire() as conn:
                await conn.fetchval("SELECT set_config('work_mem', '2GB', false)")
                await conn.fetchval("SELECT pg_advisory_lock(4242)")
                held = await conn.fetchval("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'")
            async with pool.acquire() as conn:
                work_mem = await conn.fetchval("SHOW work_mem")
                after = await conn.fetchval("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'")
            return held, work_mem, after
        finally:
            await pool.close()

    held, work_mem, after = asyncio.run(go())
    assert held == 1
    assert work_mem != "2GB"
    assert after == 0


def test_pg_sleep_is_cancelled_by_statement_timeout(monkeypatch, db_state):
    monkeypatch.setattr(server, "STATEMENT_TIMEOUT_MS", 500)
    started = time.monotonic()
    with pytest.raises(asyncpg.QueryCanceledError):
        run(server.query, "SELECT pg_sleep(30)")
    assert time.monotonic() - started < 10


@pytest.mark.parametrize("name", sorted(LIMIT_BYPASS))
def test_limit_bypass_attempts_stay_capped(name):
    out = run(server.query, LIMIT_BYPASS[name])
    assert len(_data_lines(out)) == server.ROW_LIMIT
    assert f"truncated at {server.ROW_LIMIT} row limit" in out


def test_plain_select_still_works():
    out = run(server.query, "SELECT title FROM books WHERE rating >= $1 ORDER BY title", [4.5])
    assert "Kindred" in out
    assert "One Hundred Years of Solitude" in out
