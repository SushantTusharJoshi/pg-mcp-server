"""A fake asyncpg pool that serves canned rows for every tool."""

from __future__ import annotations


class Txn:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class Cursor:
    def __init__(self, rows):
        self._rows = iter(rows)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._rows)
        except StopIteration:
            raise StopAsyncIteration from None


class Conn:
    def __init__(self, rows=None, fetch_rows=None, error=None):
        self.rows = rows or []
        self.fetch_rows = fetch_rows or {}
        self.error = error
        self.calls: list[tuple[str, tuple]] = []

    def transaction(self, readonly=False):
        assert readonly is True
        return Txn()

    async def execute(self, sql):
        return None

    def cursor(self, sql, *args, prefetch=None):
        self.calls.append((sql, args))
        if self.error:
            raise self.error
        return Cursor(self.rows)

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        if self.error:
            raise self.error
        for key, rows in self.fetch_rows.items():
            if key in sql:
                return rows
        return []


class Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


class Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return Acquire(self.conn)


def install(monkeypatch, server, conn):
    async def fake_get_pool():
        return Pool(conn)

    monkeypatch.setattr(server, "get_pool", fake_get_pool)
    return conn


BOOK = {
    "title": "Kindred",
    "author": "Octavia Butler",
    "genre": "Science Fiction",
    "published_year": 1979,
    "pages": 264,
    "price": "14.99",
    "rating": "4.6",
    "in_stock": True,
    "isbn": "978-0807083697",
}
