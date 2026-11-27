"""PG-N1 and PG-N9: what the mcp_readonly role can and cannot do."""

from __future__ import annotations

import asyncio

import asyncpg
import pytest

from pg_mcp import server
from tests.integration.conftest import _readonly_conn, admin, run

pytestmark = pytest.mark.integration

ROLE = "mcp_readonly"
REVOKED = [
    "pg_catalog.pg_terminate_backend(integer, bigint)",
    "pg_catalog.pg_cancel_backend(integer)",
    "pg_catalog.pg_notify(text, text)",
]


async def _ro(sql: str, *args):
    conn = await _readonly_conn()
    try:
        return await conn.fetch(sql, *args)
    finally:
        await conn.close()


def ro(sql: str, *args):
    return asyncio.run(_ro(sql, *args))


def test_server_connects_as_mcp_readonly():
    assert ro("SELECT current_user")[0][0] == ROLE


@pytest.mark.parametrize("fn", REVOKED)
def test_has_no_execute_on_revoked_functions(fn):
    assert ro("SELECT has_function_privilege($1, $2, 'EXECUTE')", ROLE, fn)[0][0] is False


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT pg_terminate_backend(pg_backend_pid())",
        "SELECT pg_cancel_backend(pg_backend_pid())",
        "SELECT pg_notify('chan', 'msg')",
    ],
)
def test_calling_revoked_functions_fails(sql):
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        ro(sql)


def test_wal_message_function_revoked():
    rows = ro(
        "SELECT p.oid::regprocedure::text AS fn, has_function_privilege($1, p.oid, 'EXECUTE') AS ok "
        "FROM pg_proc p WHERE p.proname = 'pg_logical_emit_message'",
        ROLE,
    )
    assert rows, "pg_logical_emit_message not found"
    assert all(r["ok"] is False for r in rows), rows


def test_role_attributes_are_minimal():
    r = admin(
        "SELECT rolsuper, rolcreaterole, rolcreatedb, rolreplication, rolbypassrls, rolinherit "
        "FROM pg_roles WHERE rolname = $1",
        ROLE,
    )[0]
    assert not any([r["rolsuper"], r["rolcreaterole"], r["rolcreatedb"], r["rolreplication"], r["rolbypassrls"]])


def test_no_inherited_roles():
    """Membership in e.g. pg_read_all_data or pg_signal_backend would add rights
    that the grants in seed/init.sql do not show."""
    rows = admin(
        """
        WITH RECURSIVE m(roleid) AS (
            SELECT am.roleid FROM pg_auth_members am JOIN pg_roles r ON r.oid = am.member
            WHERE r.rolname = $1
            UNION
            SELECT am.roleid FROM pg_auth_members am JOIN m ON am.member = m.roleid
        )
        SELECT pg_roles.rolname FROM m JOIN pg_roles ON pg_roles.oid = m.roleid
        """,
        ROLE,
    )
    assert [r["rolname"] for r in rows] == []


@pytest.mark.parametrize("table", ["books", "authors", "genres"])
def test_table_privileges_are_select_only(table):
    for priv in ["SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"]:
        has = ro("SELECT has_table_privilege($1, $2, $3)", ROLE, table, priv)[0][0]
        assert has is (priv == "SELECT"), (table, priv)


def test_no_create_or_temp():
    assert ro("SELECT has_schema_privilege($1, 'public', 'CREATE')", ROLE)[0][0] is False
    assert ro("SELECT has_database_privilege($1, current_database(), 'CREATE')", ROLE)[0][0] is False
    assert ro("SELECT has_database_privilege($1, current_database(), 'TEMP')", ROLE)[0][0] is False


def test_no_sequence_privileges():
    assert ro("SELECT has_sequence_privilege($1, 'books_id_seq', 'USAGE')", ROLE)[0][0] is False


def test_insert_fails_even_outside_a_read_only_transaction():
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        ro("INSERT INTO genres (name) VALUES ('should not exist')")
    assert admin("SELECT count(*) FROM genres WHERE name = 'should not exist'")[0][0] == 0


@pytest.fixture
def ungranted_table():
    admin("DROP TABLE IF EXISTS staff_salaries")
    admin("CREATE TABLE staff_salaries (name text, salary int)")
    admin("INSERT INTO staff_salaries VALUES ('someone', 100000)")
    yield "staff_salaries"
    admin("DROP TABLE IF EXISTS staff_salaries")


def test_new_table_is_not_readable(ungranted_table):
    """PG-N9: the allowlist lives in the database, so a table created after the
    seed (by its owner, with default privileges) is not readable."""
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        ro(f"SELECT * FROM {ungranted_table}")
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        run(server.query, f"SELECT * FROM {ungranted_table}")
    listing = run(server.inspect_schema)
    assert ungranted_table not in listing
    assert "books" in listing


def test_readable_tables_are_exactly_the_allowlist():
    rows = ro(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'v', 'm', 'S', 'p', 'f') "
        "AND has_table_privilege($1, c.oid, 'SELECT') ORDER BY 1",
        ROLE,
    )
    assert [r["relname"] for r in rows] == ["authors", "books", "genres"]


def test_dblink_cannot_write_if_installed():
    """dblink opens a second session outside the read-only transaction, so only
    role privileges stop it. The parser refuses dblink; this checks the role."""
    if not admin("SELECT 1 FROM pg_extension WHERE extname = 'dblink'"):
        pytest.skip("dblink extension not installed")
    cfg = server.PG_CONFIG
    conninfo = f"host={cfg['host']} port={cfg['port']} dbname={cfg['database']} user={cfg['user']} password={cfg['password']}"
    with pytest.raises(asyncpg.PostgresError):
        ro("SELECT dblink_exec($1, 'INSERT INTO genres (name) VALUES (''via dblink'')')", conninfo)
    assert admin("SELECT count(*) FROM genres WHERE name = 'via dblink'")[0][0] == 0
