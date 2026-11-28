"""inspect_schema(table): constraint lines built from the pg_catalog rows."""

import asyncio

from pg_mcp import server
from tests import fakes

COLUMNS = [
    {"column_name": "id", "data_type": "integer", "is_nullable": "NO", "column_default": None},
    {"column_name": "author_id", "data_type": "integer", "is_nullable": "NO", "column_default": None},
    {"column_name": "rating", "data_type": "numeric", "is_nullable": "YES", "column_default": None},
]


def _row(name, ctype, column, foreign_table=None, foreign_column=None, check_clause=None):
    return {
        "constraint_name": name,
        "constraint_type": ctype,
        "column_name": column,
        "foreign_table": foreign_table,
        "foreign_column": foreign_column,
        "check_clause": check_clause,
    }


CONSTRAINTS = [
    _row("books_rating_check", "CHECK", "rating", check_clause="CHECK (rating >= 0::numeric)"),
    _row("books_author_id_fkey", "FOREIGN KEY", "author_id", "authors", "id"),
    _row("books_pkey", "PRIMARY KEY", "id"),
    _row("books_isbn_key", "UNIQUE", "isbn"),
]


def _describe(monkeypatch, constraints):
    conn = fakes.install(
        monkeypatch,
        server,
        fakes.Conn(
            fetch_rows={
                "information_schema.columns": COLUMNS,
                "pg_catalog.pg_constraint": constraints,
            }
        ),
    )
    return asyncio.run(server.inspect_schema("books")), conn


def test_constraint_lines_keep_the_documented_format(monkeypatch):
    out, _ = _describe(monkeypatch, CONSTRAINTS)
    assert "\nConstraints:" in out
    assert "  author_id: FOREIGN KEY -> authors(id) (books_author_id_fkey)" in out
    assert "  id: PRIMARY KEY (books_pkey)" in out
    assert "  isbn: UNIQUE (books_isbn_key)" in out
    assert "  rating: CHECK (rating >= 0::numeric) (books_rating_check)" in out


def test_no_constraint_rows_means_no_constraints_section(monkeypatch):
    out, _ = _describe(monkeypatch, [])
    assert "Columns:" in out and "Constraints:" not in out


def test_table_name_is_bound_and_the_query_reads_pg_catalog(monkeypatch):
    _, conn = _describe(monkeypatch, CONSTRAINTS)
    sql, args = conn.calls[-1]
    assert sql is server.CONSTRAINTS_SQL and args == ("books",)
    assert "information_schema" not in sql
    assert "has_table_privilege(c.oid, 'SELECT')" in sql
