"""Row-cap behaviour of the query tool, using a fake asyncpg connection.

The old version wrapped agent SQL in "SELECT * FROM (...) LIMIT n", so a
trailing "--" commented the LIMIT out. These tests pin the cursor-based cap.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from pg_mcp import server  # noqa: E402


class _Txn:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Cursor:
    def __init__(self, n_rows):
        self._rows = ({"id": i, "title": f"book {i}"} for i in range(n_rows))

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._rows)
        except StopIteration:
            raise StopAsyncIteration from None


class _Conn:
    def __init__(self, n_rows):
        self.n_rows = n_rows
        self.cursor_sql = None

    def transaction(self, readonly=False):
        assert readonly is True
        return _Txn()

    async def execute(self, sql):
        return None

    def cursor(self, sql, *args, prefetch=None):
        self.cursor_sql = sql
        return _Cursor(self.n_rows)


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


def _run(sql, n_rows, monkeypatch):
    conn = _Conn(n_rows)

    async def fake_get_pool():
        return _Pool(conn)

    monkeypatch.setattr(server, "get_pool", fake_get_pool)
    return asyncio.run(server.query(sql)), conn


def test_trailing_comment_cannot_lift_the_cap(monkeypatch):
    out, conn = _run("SELECT * FROM books --", server.ROW_LIMIT * 10, monkeypatch)
    # drop the untrusted-data fence lines and the column header, keep the rows
    body = [ln for ln in out.split("\n") if ln and not ln.startswith(("(", "<<<"))]
    data_lines = body[1:]
    assert len(data_lines) == server.ROW_LIMIT
    assert f"truncated at {server.ROW_LIMIT} row limit" in out


def test_agent_sql_is_not_wrapped(monkeypatch):
    _, conn = _run("SELECT * FROM books --", 3, monkeypatch)
    assert conn.cursor_sql == "SELECT * FROM books --"


def test_exactly_the_limit_is_not_reported_as_truncated(monkeypatch):
    out, _ = _run("SELECT * FROM books", server.ROW_LIMIT, monkeypatch)
    assert "truncated" not in out
    assert f"({server.ROW_LIMIT} rows)" in out


def test_no_rows(monkeypatch):
    out, _ = _run("SELECT * FROM books WHERE false", 0, monkeypatch)
    assert out == "Query returned no rows."


def test_blocked_keyword_never_reaches_the_database(monkeypatch):
    async def boom():
        raise AssertionError("database should not be touched")

    monkeypatch.setattr(server, "get_pool", boom)
    out = asyncio.run(server.query("DELETE FROM books"))
    assert out.startswith("Error:")
