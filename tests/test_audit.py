"""PG-F4: one audit record per tool call, with no literal or parameter value in it."""

import asyncio
import json
import os

import asyncpg
import pytest

from pg_mcp import server
from pg_mcp.audit import build_record
from pg_mcp.sql_guard import redact_sql
from tests import fakes

FIELDS = {"ts", "tool", "caller", "outcome", "sql", "sql_sha256", "sql_unparsed",
          "param_count", "rows", "output_bytes", "duration_ms"}


def _records(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_query_record_has_no_literals_or_params(monkeypatch, _audit_to_tmp):
    fakes.install(monkeypatch, server, fakes.Conn(rows=[{"title": "Kindred"}]))
    sql = "SELECT title FROM books WHERE isbn = 'SECRET-ISBN-123' AND price > 42.5 AND title ILIKE $1 /* note: hunter2 */"
    out = asyncio.run(server.query(sql, ["%PARAM-VALUE-XYZ%"]))
    assert "Kindred" in out

    raw = _audit_to_tmp.read_text()
    for value in ["SECRET-ISBN-123", "42.5", "PARAM-VALUE-XYZ", "hunter2"]:
        assert value not in raw

    (rec,) = _records(_audit_to_tmp)
    assert FIELDS <= rec.keys()
    assert rec["tool"] == "query"
    assert rec["caller"] == "stdio"
    assert rec["outcome"] == "ok"
    assert rec["sql"] == "SELECT title FROM books WHERE isbn = ? AND price > ? AND title ILIKE $1"
    assert rec["sql_unparsed"] is False
    assert len(rec["sql_sha256"]) == 64
    assert rec["param_count"] == 1
    assert rec["rows"] == 1
    assert rec["output_bytes"] == len(out.encode("utf-8"))
    assert rec["duration_ms"] >= 0


def test_every_tool_writes_a_record(monkeypatch, _audit_to_tmp):
    conn = fakes.Conn(
        rows=[{"n": 1}],
        fetch_rows={
            "information_schema.tables": [{"table_name": "books", "row_count": 20}],
            "information_schema.columns": [
                {"column_name": "id", "data_type": "integer", "is_nullable": "NO", "column_default": None}
            ],
            "FROM books b": [fakes.BOOK],
        },
    )
    fakes.install(monkeypatch, server, conn)
    asyncio.run(server.inspect_schema())
    asyncio.run(server.inspect_schema("books"))
    asyncio.run(server.query("SELECT 1"))
    asyncio.run(server.search_books("SEARCH-TERM-SECRET", min_rating=4.25))

    recs = _records(_audit_to_tmp)
    assert [r["tool"] for r in recs] == ["inspect_schema", "inspect_schema", "query", "search_books"]
    assert all(r["outcome"] == "ok" for r in recs)
    raw = _audit_to_tmp.read_text()
    assert "SEARCH-TERM-SECRET" not in raw
    assert "4.25" not in raw
    assert recs[3]["param_count"] == 3


def test_refusal_is_recorded(monkeypatch, _audit_to_tmp):
    out = asyncio.run(server.query("SELECT 1; SELECT 'x-secret'"))
    assert out.startswith("Error:")
    (rec,) = _records(_audit_to_tmp)
    assert rec["outcome"] == "refused"
    assert rec["sql"] == "SELECT ?; SELECT ?"
    assert "x-secret" not in _audit_to_tmp.read_text()


def test_unparseable_sql_stores_only_the_hash(monkeypatch, _audit_to_tmp):
    asyncio.run(server.query("SELEC 'unparsed-secret' FROM books"))
    (rec,) = _records(_audit_to_tmp)
    assert rec["sql"] is None
    assert rec["sql_unparsed"] is True
    assert len(rec["sql_sha256"]) == 64
    assert "unparsed-secret" not in _audit_to_tmp.read_text()


def test_database_error_is_recorded_without_the_message(monkeypatch, _audit_to_tmp):
    err = asyncpg.InsufficientPrivilegeError("permission denied for table staff_salaries")
    fakes.install(monkeypatch, server, fakes.Conn(error=err))
    with pytest.raises(server.QueryError):
        asyncio.run(server.query("SELECT * FROM staff_salaries"))
    (rec,) = _records(_audit_to_tmp)
    assert rec["outcome"] == "error"
    assert rec["sqlstate"] == "42501"
    assert "permission denied" not in _audit_to_tmp.read_text()


def test_audit_failure_withholds_the_result(monkeypatch, tmp_path):
    monkeypatch.setenv("PG_MCP_AUDIT_LOG", str(tmp_path / "missing-dir" / "audit.jsonl"))
    fakes.install(monkeypatch, server, fakes.Conn(rows=[{"title": "Kindred"}]))
    out = asyncio.run(server.query("SELECT title FROM books"))
    assert out == server.AUDIT_FAILED


def test_audit_file_is_private(monkeypatch, _audit_to_tmp):
    asyncio.run(server.query("SELECT 1; SELECT 2"))
    assert os.stat(_audit_to_tmp).st_mode & 0o077 == 0


@pytest.mark.parametrize(
    "sql, secret",
    [
        ("SELECT $$dollar-secret$$", "dollar-secret"),
        ("SELECT $tag$tagged-secret$tag$", "tagged-secret"),
        ("SELECT E'escaped-secret'", "escaped-secret"),
        ("SELECT U&'unicode-secret'", "unicode-secret"),
        ("SELECT B'1011'", "1011"),
        ("SELECT X'beef'", "beef"),
        ("SELECT now() - INTERVAL '17 days'", "17"),
        ("SELECT '2031-05-06'::date", "2031"),
        ("SELECT * FROM t WHERE id IN (9001, 9002)", "9001"),
        ("SELECT * FROM t LIMIT 777", "777"),
    ],
)
def test_redaction_blanks_every_literal_kind(sql, secret):
    out = redact_sql(sql)
    assert out is not None
    assert secret not in out


def test_build_record_without_sql():
    rec = build_record(tool="x", sql=None, param_count=0, rows=0, output_bytes=0,
                       duration_ms=1.234, outcome="ok", caller="stdio")
    assert rec["sql"] is None and rec["sql_unparsed"] is False
