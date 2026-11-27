from __future__ import annotations

import asyncio
import atexit
import logging
import os
import re
import time
from typing import NoReturn

import asyncpg
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from pg_mcp.audit import AuditLog, build_record
from pg_mcp.output import truncate_utf8, untrusted_block
from pg_mcp.sql_guard import check_sql

log = logging.getLogger("pg_mcp")

mcp = FastMCP(
    "pg-mcp-server",
    instructions="Read-only PostgreSQL tools for AI agents",
)



def _env(name: str, libpq_name: str, default: str) -> str:
    """PG_* wins, then the standard libpq variable (PGHOST, ...), then the default."""
    return os.getenv(name) or os.getenv(libpq_name) or default


SSL_MODES = ("disable", "allow", "prefer", "require", "verify-ca", "verify-full")


def _pg_config() -> dict:
    config: dict = {
        "host": _env("PG_HOST", "PGHOST", "localhost"),
        "port": int(_env("PG_PORT", "PGPORT", "5432")),
        "database": _env("PG_DATABASE", "PGDATABASE", "bookstore"),
        "user": _env("PG_USER", "PGUSER", "mcp_readonly"),
        "password": _env("PG_PASSWORD", "PGPASSWORD", "mcp_readonly"),
    }
    sslmode = _env("PG_SSLMODE", "PGSSLMODE", "")
    if sslmode:
        if sslmode not in SSL_MODES:
            raise ValueError(f"PG_SSLMODE must be one of {', '.join(SSL_MODES)}")
        # asyncpg reads PGSSLROOTCERT for verify-ca / verify-full
        config["ssl"] = sslmode
    return config


PG_CONFIG = _pg_config()

ROW_LIMIT = min(int(os.getenv("QUERY_ROW_LIMIT", "100")), 500)
STATEMENT_TIMEOUT_MS = int(os.getenv("STATEMENT_TIMEOUT_MS", "5000"))
# Byte cap on every tool response, including the untrusted-data fence
MAX_BYTES = max(int(os.getenv("QUERY_MAX_BYTES", "200000")), 1024)

AUDIT = AuditLog()
AUDIT_FAILED = "Error: the audit record could not be written, so the result is withheld."

BLOCKED_KEYWORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE|COPY|EXECUTE)\b",
    re.IGNORECASE,
)

_pool: asyncpg.Pool | None = None


class QueryError(ToolError):
    """A database error, with the server message fenced as untrusted data."""

    def __init__(self, message: str, sqlstate: str | None):
        super().__init__(message)
        self.sqlstate = sqlstate


def _caller(ctx: Context | None) -> str:
    """Token ID in HTTP mode (set by the auth middleware), "stdio" otherwise."""
    if ctx is None:
        return "stdio"
    try:
        request = ctx.request_context.request
    except (ValueError, LookupError, AttributeError):
        return "stdio"
    if request is None:
        return "stdio"
    state = request.scope.get("state") or {}
    return str(state.get("pg_mcp_token_id") or "http:unknown")


class _Call:
    """One tool call: shapes the response and writes its audit record."""

    def __init__(self, tool: str, sql: str | None, param_count: int, ctx: Context | None):
        self.tool = tool
        self.sql = sql
        self.param_count = param_count
        self.ctx = ctx
        self.started = time.perf_counter()

    def _audit(self, outcome: str, text: str, rows: int = 0, sqlstate: str | None = None) -> bool:
        record = build_record(
            tool=self.tool,
            sql=self.sql,
            param_count=self.param_count,
            rows=rows,
            output_bytes=len(text.encode("utf-8")),
            duration_ms=(time.perf_counter() - self.started) * 1000,
            outcome=outcome,
            caller=_caller(self.ctx),
            sqlstate=sqlstate,
        )
        try:
            AUDIT.write(record)
        except OSError as exc:
            log.error("audit write failed: %s", exc.__class__.__name__)
            return False
        return True

    def refuse(self, text: str) -> str:
        return text if self._audit("refused", text) else AUDIT_FAILED

    def ok(self, body: str, rows: int, from_db: bool = True) -> str:
        text = untrusted_block(body, MAX_BYTES) if from_db else truncate_utf8(body, MAX_BYTES)
        return text if self._audit("ok", text, rows) else AUDIT_FAILED

    def fail(self, exc: Exception) -> NoReturn:
        if isinstance(exc, asyncpg.PostgresError):
            sqlstate = getattr(exc, "sqlstate", None)
            # Postgres messages can quote row values, so they are fenced too
            text = f"Error: database error {sqlstate}\n" + untrusted_block(str(exc), MAX_BYTES)
            self._audit("error", text, sqlstate=sqlstate)
            raise QueryError(text, sqlstate) from None
        self._audit("error", f"Error: {exc.__class__.__name__}")
        raise exc


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(**PG_CONFIG, min_size=1, max_size=5)
    return _pool


TABLES_SQL = """
    SELECT t.table_name,
           (xpath('/row/count/text()',
             query_to_xml(format('SELECT count(*) FROM %I', t.table_name), false, true, ''))
           )[1]::text::bigint AS row_count
    FROM information_schema.tables t
    WHERE t.table_schema = 'public'
      AND t.table_type = 'BASE TABLE'
    ORDER BY t.table_name
"""

COLUMNS_SQL = """
    SELECT column_name, data_type, is_nullable, column_default
    FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = $1
    ORDER BY ordinal_position
"""

CONSTRAINTS_SQL = """
    SELECT tc.constraint_name, tc.constraint_type,
           kcu.column_name,
           ccu.table_name AS foreign_table,
           ccu.column_name AS foreign_column
    FROM information_schema.table_constraints tc
    JOIN information_schema.key_column_usage kcu
      ON tc.constraint_name = kcu.constraint_name
    LEFT JOIN information_schema.constraint_column_usage ccu
      ON tc.constraint_name = ccu.constraint_name
      AND tc.constraint_type = 'FOREIGN KEY'
    WHERE tc.table_schema = 'public' AND tc.table_name = $1
    ORDER BY tc.constraint_type, kcu.column_name
"""


@mcp.tool()
async def inspect_schema(table_name: str | None = None, ctx: Context | None = None) -> str:
    """Inspect the database schema.

    Without arguments, lists all tables and their row counts.
    With a table_name, returns columns, types, nullability, defaults,
    and constraints for that table.
    """
    call = _Call("inspect_schema", TABLES_SQL if table_name is None else COLUMNS_SQL, int(table_name is not None), ctx)
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            if table_name is None:
                rows = await conn.fetch(TABLES_SQL)
                lines = ["Tables in database:\n"]
                for r in rows:
                    lines.append(f"  {r['table_name']} ({r['row_count']} rows)")
                return call.ok("\n".join(lines), rows=len(rows))

            cols = await conn.fetch(COLUMNS_SQL, table_name)
            if not cols:
                return call.ok(f"Table '{table_name}' not found.", rows=0, from_db=False)
            constraints = await conn.fetch(CONSTRAINTS_SQL, table_name)
    except Exception as exc:
        call.fail(exc)

    lines = [f"Table: {table_name}\n", "Columns:"]
    for c in cols:
        nullable = "NULL" if c["is_nullable"] == "YES" else "NOT NULL"
        default = f" DEFAULT {c['column_default']}" if c["column_default"] else ""
        lines.append(f"  {c['column_name']}: {c['data_type']} {nullable}{default}")

    if constraints:
        lines.append("\nConstraints:")
        for c in constraints:
            detail = c["constraint_type"]
            if c["foreign_table"]:
                detail += f" -> {c['foreign_table']}({c['foreign_column']})"
            lines.append(f"  {c['column_name']}: {detail} ({c['constraint_name']})")

    return call.ok("\n".join(lines), rows=len(cols))


# -- query -------------------------------------------------------------------
# What it does: runs one agent-written SELECT and returns at most ROW_LIMIT rows.
# Catch it:     "How do you let an agent query a database without letting it write?"
# Card:         docs/cards/query.md
@mcp.tool()
async def query(
    sql: str,
    params: list[str | int | float | bool | None] | None = None,
    ctx: Context | None = None,
) -> str:
    """Execute a read-only SQL query against the database.

    Safety: the query must parse as exactly one SELECT (or WITH ... SELECT)
    and runs inside a READ ONLY transaction with a statement timeout.
    DDL and DML keywords are rejected. Results are capped by rows and bytes,
    and are returned inside an untrusted-data block.

    Args:
        sql: A SELECT query. Must not contain INSERT, UPDATE, DELETE,
             DROP, ALTER, CREATE, TRUNCATE, GRANT, REVOKE, COPY, or EXECUTE.
             Use $1, $2, ... for parameter placeholders.
        params: Optional list of parameter values matching the $N placeholders.
    """
    call = _Call("query", sql, len(params or []), ctx)

    # DECISION: a write keyword anywhere -> refuse before touching the database
    if BLOCKED_KEYWORDS.search(sql):
        return call.refuse("Error: only SELECT queries are allowed. DDL/DML statements are blocked.")

    # DECISION: the parser must see exactly one SELECT with no writes inside
    reason = check_sql(sql)
    if reason is not None:
        return call.refuse(f"Error: query refused: {reason}.")

    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            # STEP: read-only transaction + timeout, so Postgres itself rejects writes and long runs
            async with conn.transaction(readonly=True):
                await conn.execute(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}")

                # LOOP: read from a cursor and stop at the row cap or the byte cap; the agent's
                # SQL is never wrapped in a string, so a trailing comment cannot remove the limit
                columns: list[str] = []
                lines: list[str] = []
                size = 0
                truncated = ""
                async for row in conn.cursor(sql, *(params or []), prefetch=ROW_LIMIT + 1):
                    if len(lines) == ROW_LIMIT:
                        truncated = f"{ROW_LIMIT} row limit"
                        break
                    if not columns:
                        columns = list(row.keys())
                    line = "\t".join(str(v) for v in row.values())
                    size += len(line.encode("utf-8")) + 1
                    if size > MAX_BYTES and lines:
                        truncated = f"{MAX_BYTES} byte limit"
                        break
                    lines.append(line)
    except Exception as exc:
        call.fail(exc)

    # DECISION: nothing came back
    if not lines:
        return call.ok("Query returned no rows.", rows=0, from_db=False)

    # RETURN: tab-separated table + row-count footer, fenced as untrusted data
    return call.ok(_format_rows(columns, lines, truncated), rows=len(lines))


def _format_rows(columns: list[str], lines: list[str], truncated: str) -> str:
    """Render rows as a tab-separated table with a row-count footer."""
    footer = f"\n({len(lines)} row{'s' if len(lines) != 1 else ''})"
    if truncated:
        footer += f" [truncated at {truncated}]"
    return "\n".join(["\t".join(columns), *lines, footer])


@mcp.tool()
async def search_books(
    search_term: str,
    min_rating: float | None = None,
    in_stock: bool | None = None,
    limit: int = 10,
    ctx: Context | None = None,
) -> str:
    """Search for books by title, author name, or genre.

    Uses case-insensitive pattern matching across title, author name,
    and genre name. Results can be filtered by minimum rating and
    stock availability.

    Args:
        search_term: Text to search for in book title, author name, or genre.
        min_rating: Optional minimum rating filter (0.0 to 5.0).
        in_stock: Optional filter for stock availability.
        limit: Maximum number of results (default 10, max 50).
    """
    limit = min(max(1, limit), 50)
    escaped = search_term.replace("%", "\\%").replace("_", "\\_")
    pattern = f"%{escaped}%"

    conditions = [
        "(b.title ILIKE $1 OR a.name ILIKE $1 OR g.name ILIKE $1)"
    ]
    params: list[str | float | bool] = [pattern]
    idx = 2

    if min_rating is not None:
        conditions.append(f"b.rating >= ${idx}")
        params.append(min_rating)
        idx += 1

    if in_stock is not None:
        conditions.append(f"b.in_stock = ${idx}")
        params.append(in_stock)
        idx += 1

    where = " AND ".join(conditions)

    sql = f"""
        SELECT b.title, a.name AS author, g.name AS genre,
               b.published_year, b.pages, b.price,
               b.rating, b.in_stock, b.isbn
        FROM books b
        JOIN authors a ON a.id = b.author_id
        JOIN genres g ON g.id = b.genre_id
        WHERE {where}
        ORDER BY b.rating DESC
        LIMIT ${idx}
    """
    params.append(limit)

    call = _Call("search_books", sql, len(params), ctx)
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)
    except Exception as exc:
        call.fail(exc)

    if not rows:
        return call.ok(f"No books found matching '{search_term}'.", rows=0, from_db=False)

    lines = []
    for r in rows:
        stock = "In stock" if r["in_stock"] else "Out of stock"
        lines.append(
            f"- {r['title']} by {r['author']} ({r['published_year']})\n"
            f"  Genre: {r['genre']} | {r['pages']}pp | ${r['price']} | "
            f"Rating: {r['rating']}/5 | {stock}\n"
            f"  ISBN: {r['isbn']}"
        )

    body = f"Found {len(rows)} result{'s' if len(rows) != 1 else ''}:\n\n" + "\n\n".join(lines)
    return call.ok(body, rows=len(rows))


async def close_pool():
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def _shutdown_pool():
    if _pool is not None:
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(close_pool())
            else:
                loop.run_until_complete(close_pool())
        except RuntimeError:
            pass


atexit.register(_shutdown_pool)


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
