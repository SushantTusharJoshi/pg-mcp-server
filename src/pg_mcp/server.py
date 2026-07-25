from __future__ import annotations

import atexit
import asyncio
import os
import re

import asyncpg
from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    "pg-mcp-server",
    instructions="Read-only PostgreSQL tools for AI agents",
)

PG_CONFIG = {
    "host": os.getenv("PG_HOST", "localhost"),
    "port": int(os.getenv("PG_PORT", "5432")),
    "database": os.getenv("PG_DATABASE", "bookstore"),
    "user": os.getenv("PG_USER", "mcp_readonly"),
    "password": os.getenv("PG_PASSWORD", "mcp_readonly"),
}

ROW_LIMIT = min(int(os.getenv("QUERY_ROW_LIMIT", "100")), 500)
STATEMENT_TIMEOUT_MS = int(os.getenv("STATEMENT_TIMEOUT_MS", "5000"))

BLOCKED_KEYWORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE|COPY|EXECUTE)\b",
    re.IGNORECASE,
)

_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(**PG_CONFIG, min_size=1, max_size=5)
    return _pool


@mcp.tool()
async def inspect_schema(table_name: str | None = None) -> str:
    """Inspect the database schema.

    Without arguments, lists all tables and their row counts.
    With a table_name, returns columns, types, nullability, defaults,
    and constraints for that table.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        if table_name is None:
            rows = await conn.fetch(
                """
                SELECT t.table_name,
                       (xpath('/row/count/text()',
                         query_to_xml(format('SELECT count(*) FROM %I', t.table_name), false, true, ''))
                       )[1]::text::bigint AS row_count
                FROM information_schema.tables t
                WHERE t.table_schema = 'public'
                  AND t.table_type = 'BASE TABLE'
                ORDER BY t.table_name
                """
            )
            lines = ["Tables in database:\n"]
            for r in rows:
                lines.append(f"  {r['table_name']} ({r['row_count']} rows)")
            return "\n".join(lines)

        cols = await conn.fetch(
            """
            SELECT column_name, data_type, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = $1
            ORDER BY ordinal_position
            """,
            table_name,
        )
        if not cols:
            return f"Table '{table_name}' not found."

        constraints = await conn.fetch(
            """
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
            """,
            table_name,
        )

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

        return "\n".join(lines)


@mcp.tool()
async def query(sql: str, params: list[str | int | float | bool | None] | None = None) -> str:
    """Execute a read-only SQL query against the database.

    Safety: the query runs inside a READ ONLY transaction with a
    statement timeout. DDL and DML keywords are rejected. Results
    are capped at the configured row limit.

    Args:
        sql: A SELECT query. Must not contain INSERT, UPDATE, DELETE,
             DROP, ALTER, CREATE, TRUNCATE, GRANT, REVOKE, COPY, or EXECUTE.
             Use $1, $2, ... for parameter placeholders.
        params: Optional list of parameter values matching the $N placeholders.
    """
    if BLOCKED_KEYWORDS.search(sql):
        return "Error: only SELECT queries are allowed. DDL/DML statements are blocked."

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction(readonly=True):
            await conn.execute(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}")

            limited_sql = f"SELECT * FROM ({sql}) AS _q LIMIT {ROW_LIMIT}"
            rows = await conn.fetch(limited_sql, *(params or []))

            if not rows:
                return "Query returned no rows."

            columns = list(rows[0].keys())
            lines = ["\t".join(columns)]
            for row in rows:
                lines.append("\t".join(str(v) for v in row.values()))

            footer = f"\n({len(rows)} row{'s' if len(rows) != 1 else ''})"
            if len(rows) == ROW_LIMIT:
                footer += f" [truncated at {ROW_LIMIT} row limit]"
            lines.append(footer)
            return "\n".join(lines)


@mcp.tool()
async def search_books(
    search_term: str,
    min_rating: float | None = None,
    in_stock: bool | None = None,
    limit: int = 10,
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
    pattern = f"%{search_term}%"

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

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)

    if not rows:
        return f"No books found matching '{search_term}'."

    lines = []
    for r in rows:
        stock = "In stock" if r["in_stock"] else "Out of stock"
        lines.append(
            f"- {r['title']} by {r['author']} ({r['published_year']})\n"
            f"  Genre: {r['genre']} | {r['pages']}pp | ${r['price']} | "
            f"Rating: {r['rating']}/5 | {stock}\n"
            f"  ISBN: {r['isbn']}"
        )

    return f"Found {len(rows)} result{'s' if len(rows) != 1 else ''}:\n\n" + "\n\n".join(lines)


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
