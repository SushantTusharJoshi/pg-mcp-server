"""PG-F6: generic mode on a second schema (seed/sample_clinic.sql).

Runs with PG_MCP_INTEGRATION=clinic, PG_* pointing at the clinic database as
clinic_readonly, PG_MCP_BOOKSTORE_TOOLS=0, and PG_ADMIN_DSN as a superuser on
the same database. Nothing here mentions books: the server is configured by
environment only, and every security layer is the same code path as the
bookstore suite.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time

import asyncpg
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from pg_mcp import server
from tests.integration.conftest import _readonly_conn, admin, run
from tests.sql_corpus import LIMIT_BYPASS

pytestmark = [pytest.mark.integration, pytest.mark.clinic]

ROLE = "clinic_readonly"
GRANTED = ["appointments", "clinicians", "patients"]
REVOKED = [
    "pg_catalog.pg_terminate_backend(integer, bigint)",
    "pg_catalog.pg_cancel_backend(integer)",
    "pg_catalog.pg_notify(text, text)",
]

CLINIC_STATE_SQL = """
SELECT (SELECT md5(string_agg(a::text, ',' ORDER BY id)) FROM appointments a) AS appointments,
       (SELECT md5(string_agg(p::text, ',' ORDER BY id)) FROM patients p) AS patients,
       (SELECT md5(string_agg(c::text, ',' ORDER BY id)) FROM clinicians c) AS clinicians,
       (SELECT last_value FROM appointments_id_seq) AS appointments_seq
"""


def _state():
    return dict(admin(CLINIC_STATE_SQL)[0])


async def _ro(sql: str, *args):
    conn = await _readonly_conn()
    try:
        return await conn.fetch(sql, *args)
    finally:
        await conn.close()


def ro(sql: str, *args):
    return asyncio.run(_ro(sql, *args))


async def _ro_readonly_txn(sql: str) -> None:
    conn = await _readonly_conn()
    try:
        async with conn.transaction(readonly=True):
            await conn.execute(sql)
    finally:
        await conn.close()


def _data_lines(out: str) -> list[str]:
    body = [ln for ln in out.split("\n") if ln and not ln.startswith(("(", "<<<"))]
    return body[1:]


# -- configured by environment only ----------------------------------------------


def test_env_points_the_server_at_the_clinic():
    assert server.PG_CONFIG["database"] == "clinic"
    assert server.PG_CONFIG["user"] == ROLE
    assert tuple(ro("SELECT current_user, current_database()")[0]) == (ROLE, "clinic")


def test_bookstore_tool_is_not_registered():
    assert server.BOOKSTORE_TOOLS is False, "run this suite with PG_MCP_BOOKSTORE_TOOLS=0"
    names = {t.name for t in server.mcp._tool_manager.list_tools()}
    assert names == {"inspect_schema", "query"}


def test_quick_start_over_stdio(tmp_path):
    """The README quick start: python -m pg_mcp.server with env only, driven by a real MCP client."""
    env = dict(os.environ, PG_MCP_AUDIT_LOG=str(tmp_path / "audit.jsonl"))
    params = StdioServerParameters(command=sys.executable, args=["-m", "pg_mcp.server"], env=env)

    async def go():
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            tools = {t.name for t in (await session.list_tools()).tools}
            listing = await session.call_tool("inspect_schema", {})
            rows = await session.call_tool("query", {"sql": "SELECT count(*) AS n FROM patients WHERE city = $1",
                                                      "params": ["Springfield"]})
            refused = await session.call_tool("query", {"sql": "DELETE FROM patients"})
            return tools, listing.content[0].text, rows.content[0].text, refused.content[0].text

    tools, listing, rows, refused = asyncio.run(go())
    assert tools == {"inspect_schema", "query"}
    assert "patients (6 rows)" in listing
    assert "n\n2\n" in rows
    assert refused.startswith("Error:")
    outcomes = [json.loads(x)["outcome"] for x in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert outcomes == ["ok", "ok", "refused"]


# -- the generic tools work on a schema they have never seen ---------------------


def test_inspect_schema_lists_exactly_the_granted_tables():
    out = run(server.inspect_schema)
    assert "<<< UNTRUSTED DATABASE DATA" in out
    assert "appointments (10 rows)" in out
    assert "clinicians (4 rows)" in out
    assert "patients (6 rows)" in out
    assert "billing_notes" not in out
    assert "books" not in out


def test_inspect_schema_describes_a_table():
    out = run(server.inspect_schema, "appointments")
    assert "<<< UNTRUSTED DATABASE DATA" in out
    assert "patient_id: integer NOT NULL" in out
    assert "scheduled_at: timestamp with time zone NOT NULL" in out
    assert "status: text NOT NULL DEFAULT 'scheduled'::text" in out


def test_ungranted_table_looks_missing():
    assert "not found" in run(server.inspect_schema, "billing_notes")


def test_query_joins_with_bound_params():
    out = run(
        server.query,
        "SELECT c.specialty, count(*) AS visits FROM appointments a "
        "JOIN clinicians c ON c.id = a.clinician_id WHERE a.status = $1 "
        "GROUP BY c.specialty ORDER BY visits DESC, c.specialty",
        ["completed"],
    )
    assert "<<< UNTRUSTED DATABASE DATA" in out
    assert [ln.split("\t") for ln in _data_lines(out)] == [
        ["Cardiology", "2"],
        ["Family Medicine", "2"],
        ["Pediatrics", "2"],
    ]
    assert "(3 rows)" in out


def test_query_on_ungranted_table_is_denied_by_the_role():
    with pytest.raises(server.QueryError) as info:
        run(server.query, "SELECT note FROM billing_notes")
    assert info.value.sqlstate == "42501"


# -- the same security layers ------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM appointments",
        "UPDATE patients SET city = 'x'",
        "SELECT 1; DROP TABLE clinicians",
        "WITH x AS (DELETE FROM appointments RETURNING *) SELECT * FROM x",
        "SELECT * INTO stolen FROM patients",
        "SELECT * FROM patients FOR UPDATE",
        "SELECT query_to_xml('select 1', true, true, '')",
    ],
)
def test_tool_refuses_writes_before_the_database(sql, monkeypatch):
    async def no_db():
        raise AssertionError("reached the database")

    before = _state()
    monkeypatch.setattr(server, "get_pool", no_db)
    assert asyncio.run(server.query(sql)).startswith("Error:")
    assert _state() == before


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM appointments",
        "UPDATE patients SET city = 'x'",
        "INSERT INTO clinicians (name, specialty) VALUES ('x', 'y')",
        "CREATE TABLE stolen (id int)",
    ],
)
def test_read_only_transaction_alone_rejects_writes(sql):
    before = _state()
    with pytest.raises(asyncpg.PostgresError):
        asyncio.run(_ro_readonly_txn(sql))
    assert _state() == before


def test_insert_fails_even_outside_a_read_only_transaction():
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        ro("INSERT INTO clinicians (name, specialty) VALUES ('should not exist', 'x')")
    assert admin("SELECT count(*) FROM clinicians WHERE name = 'should not exist'")[0][0] == 0


def test_readable_tables_are_exactly_the_allowlist():
    rows = ro(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'v', 'm', 'S', 'p', 'f') "
        "AND has_table_privilege($1, c.oid, 'SELECT') ORDER BY 1",
        ROLE,
    )
    assert [r["relname"] for r in rows] == GRANTED


@pytest.mark.parametrize("table", GRANTED)
def test_table_privileges_are_select_only(table):
    for priv in ["SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"]:
        has = ro("SELECT has_table_privilege($1, $2, $3)", ROLE, table, priv)[0][0]
        assert has is (priv == "SELECT"), (table, priv)


def test_no_create_temp_or_sequences():
    assert ro("SELECT has_schema_privilege($1, 'public', 'CREATE')", ROLE)[0][0] is False
    assert ro("SELECT has_database_privilege($1, current_database(), 'CREATE')", ROLE)[0][0] is False
    assert ro("SELECT has_database_privilege($1, current_database(), 'TEMP')", ROLE)[0][0] is False
    assert ro("SELECT has_sequence_privilege($1, 'appointments_id_seq', 'USAGE')", ROLE)[0][0] is False


@pytest.mark.parametrize("fn", REVOKED)
def test_side_channel_functions_are_revoked_in_this_database(fn):
    assert ro("SELECT has_function_privilege($1, $2, 'EXECUTE')", ROLE, fn)[0][0] is False


def test_wal_message_function_revoked():
    rows = ro(
        "SELECT has_function_privilege($1, p.oid, 'EXECUTE') AS ok "
        "FROM pg_proc p WHERE p.proname = 'pg_logical_emit_message'",
        ROLE,
    )
    assert rows and all(r["ok"] is False for r in rows)


def test_role_attributes_are_minimal():
    r = admin(
        "SELECT rolsuper, rolcreaterole, rolcreatedb, rolreplication, rolbypassrls "
        "FROM pg_roles WHERE rolname = $1",
        ROLE,
    )[0]
    assert not any(r.values())


def test_pg_sleep_is_cancelled_by_statement_timeout(monkeypatch):
    monkeypatch.setattr(server, "STATEMENT_TIMEOUT_MS", 500)
    started = time.monotonic()
    with pytest.raises(server.QueryError) as info:
        run(server.query, "SELECT pg_sleep(30)")
    assert info.value.sqlstate == "57014"
    assert time.monotonic() - started < 10


@pytest.mark.parametrize("name", sorted(LIMIT_BYPASS))
def test_row_cap_holds(name):
    out = run(server.query, LIMIT_BYPASS[name])
    assert len(_data_lines(out)) == server.ROW_LIMIT
    assert f"truncated at {server.ROW_LIMIT} row limit" in out


def test_byte_cap_holds():
    out = run(server.query, "SELECT repeat('x', 5000) AS a FROM generate_series(1, 100)")
    assert len(out.encode("utf-8")) <= server.MAX_BYTES
    assert f"truncated at {server.MAX_BYTES} byte limit" in out


def test_audit_record_has_no_values(_audit_to_tmp):
    run(server.query, "SELECT reason FROM appointments WHERE status = 'cancelled' AND patient_id = $1", [3])
    (rec,) = [json.loads(x) for x in _audit_to_tmp.read_text().splitlines()]
    assert rec["tool"] == "query" and rec["outcome"] == "ok" and rec["rows"] == 1
    assert "cancelled" not in _audit_to_tmp.read_text()
    assert "Annual check-up" not in _audit_to_tmp.read_text()
