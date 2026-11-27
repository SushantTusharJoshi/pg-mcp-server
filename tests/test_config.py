"""PG-F10: libpq-style env fallbacks and PG_SSLMODE."""

import pytest

from pg_mcp import server

KEYS = ["PG_HOST", "PGHOST", "PG_PORT", "PGPORT", "PG_DATABASE", "PGDATABASE", "PG_USER", "PGUSER",
        "PG_PASSWORD", "PGPASSWORD", "PG_SSLMODE", "PGSSLMODE"]


@pytest.fixture
def clean_env(monkeypatch):
    for k in KEYS:
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


def test_defaults(clean_env):
    cfg = server._pg_config()
    assert cfg == {"host": "localhost", "port": 5432, "database": "bookstore",
                   "user": "mcp_readonly", "password": "mcp_readonly"}


def test_libpq_fallbacks(clean_env):
    clean_env.setenv("PGHOST", "db.internal")
    clean_env.setenv("PGPORT", "6543")
    clean_env.setenv("PGUSER", "reader")
    cfg = server._pg_config()
    assert (cfg["host"], cfg["port"], cfg["user"]) == ("db.internal", 6543, "reader")


def test_pg_prefix_wins(clean_env):
    clean_env.setenv("PGHOST", "libpq-host")
    clean_env.setenv("PG_HOST", "pg-host")
    assert server._pg_config()["host"] == "pg-host"


def test_sslmode_passed_to_asyncpg(clean_env):
    clean_env.setenv("PG_SSLMODE", "verify-full")
    assert server._pg_config()["ssl"] == "verify-full"


def test_no_ssl_key_unless_set(clean_env):
    assert "ssl" not in server._pg_config()


def test_bad_sslmode_rejected(clean_env):
    clean_env.setenv("PG_SSLMODE", "yes-please")
    with pytest.raises(ValueError):
        server._pg_config()
