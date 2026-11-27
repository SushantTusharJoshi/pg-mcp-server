"""PG-F6: generic mode. search_books is registered only when PG_MCP_BOOKSTORE_TOOLS is on."""

import asyncio
import json
import os
import subprocess
import sys

import pytest
from starlette.testclient import TestClient

from pg_mcp import auth, http_app, server
from tests import fakes

GENERIC = {"inspect_schema", "query"}
SRC = os.path.join(os.path.dirname(__file__), "..", "src")
ALL = GENERIC | {"search_books"}


def _names(mcp) -> set[str]:
    return {t.name for t in mcp._tool_manager.list_tools()}


# -- the flag -----------------------------------------------------------------------


@pytest.mark.parametrize("value", [None, ""])
def test_flag_defaults_on(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("PG_MCP_BOOKSTORE_TOOLS", raising=False)
    else:
        monkeypatch.setenv("PG_MCP_BOOKSTORE_TOOLS", value)
    assert server._flag("PG_MCP_BOOKSTORE_TOOLS", True) is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "OFF", " False "])
def test_flag_off_values(monkeypatch, value):
    monkeypatch.setenv("PG_MCP_BOOKSTORE_TOOLS", value)
    assert server._flag("PG_MCP_BOOKSTORE_TOOLS", True) is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "ON"])
def test_flag_on_values(monkeypatch, value):
    monkeypatch.setenv("PG_MCP_BOOKSTORE_TOOLS", value)
    assert server._flag("PG_MCP_BOOKSTORE_TOOLS", False) is True


@pytest.mark.parametrize("value", ["2", "disabled", "bookstore"])
def test_flag_rejects_unknown_values(monkeypatch, value):
    monkeypatch.setenv("PG_MCP_BOOKSTORE_TOOLS", value)
    with pytest.raises(ValueError, match="PG_MCP_BOOKSTORE_TOOLS"):
        server._flag("PG_MCP_BOOKSTORE_TOOLS", True)


# -- registration -----------------------------------------------------------------


def test_generic_server_has_only_the_generic_tools():
    assert _names(server.build_mcp(bookstore_tools=False)) == GENERIC


def test_bookstore_server_adds_search_books():
    assert _names(server.build_mcp(bookstore_tools=True)) == ALL


def test_default_module_server_keeps_all_three_tools():
    # CI runs the unit suite without PG_MCP_BOOKSTORE_TOOLS, so this is the default
    if os.getenv("PG_MCP_BOOKSTORE_TOOLS"):
        pytest.skip("PG_MCP_BOOKSTORE_TOOLS is set in this environment")
    assert server.BOOKSTORE_TOOLS is True
    assert _names(server.mcp) == ALL


def _tools_in_fresh_process(flag: str | None) -> set[str]:
    env = {k: v for k, v in os.environ.items() if k != "PG_MCP_BOOKSTORE_TOOLS"}
    env["PYTHONPATH"] = SRC
    if flag is not None:
        env["PG_MCP_BOOKSTORE_TOOLS"] = flag
    code = "import json; from pg_mcp import server; print(json.dumps(sorted(t.name for t in server.mcp._tool_manager.list_tools())))"
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True)
    return set(json.loads(out.stdout))


def test_env_flag_off_at_import_drops_search_books():
    assert _tools_in_fresh_process("0") == GENERIC


def test_env_flag_unset_at_import_keeps_search_books():
    assert _tools_in_fresh_process(None) == ALL


def test_bad_flag_value_stops_startup():
    env = dict(os.environ, PG_MCP_BOOKSTORE_TOOLS="maybe", PYTHONPATH=SRC)
    out = subprocess.run([sys.executable, "-c", "import pg_mcp.server"], env=env, capture_output=True, text=True)
    assert out.returncode != 0
    assert "PG_MCP_BOOKSTORE_TOOLS must be one of" in out.stderr


def test_tool_schemas_are_unchanged_by_the_flag():
    """The generic tools are the same objects either way: same description and input schema."""
    generic = {t.name: t for t in server.build_mcp(False)._tool_manager.list_tools()}
    full = {t.name: t for t in server.build_mcp(True)._tool_manager.list_tools()}
    for name in GENERIC:
        assert generic[name].description == full[name].description
        assert generic[name].parameters == full[name].parameters
        assert generic[name].fn is full[name].fn


# -- HTTP mode ----------------------------------------------------------------------


def test_http_token_naming_an_unregistered_tool_fails_at_startup():
    tokens = json.dumps({"a": {"sha256": "0" * 64, "tools": ["query", "search_books"]}})
    with pytest.raises(ValueError, match="unknown tools"):
        http_app.build_app(server.build_mcp(False), tokens)


def test_http_tools_list_in_generic_mode(monkeypatch):
    token = "generic-test-token-value"
    tokens = json.dumps({"analyst": {"sha256": auth.hash_token(token), "tools": ["query", "inspect_schema"]}})
    fakes.install(monkeypatch, server, fakes.Conn())
    app = http_app.build_app(server.build_mcp(False), tokens)
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    with TestClient(app, base_url="http://127.0.0.1:8000") as c:
        r = c.post("/mcp", content=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}), headers=headers)
        assert r.status_code == 200
        assert {t["name"] for t in r.json()["result"]["tools"]} == GENERIC
        call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "search_books", "arguments": {"search_term": "x"}}}
        assert c.post("/mcp", content=json.dumps(call), headers=headers).status_code == 403


# -- the generic tools on a non-bookstore schema (fake pool) -------------------------


def test_generic_tools_on_a_clinic_shaped_schema(monkeypatch, _audit_to_tmp):
    fakes.install(monkeypatch, server, fakes.Conn(
        rows=[{"specialty": "Cardiology", "visits": 2}],
        fetch_rows={"information_schema.tables": [{"table_name": "appointments", "row_count": 10}]},
    ))
    listing = asyncio.run(server.inspect_schema())
    assert "appointments (10 rows)" in listing and "<<< UNTRUSTED DATABASE DATA" in listing
    out = asyncio.run(server.query("SELECT specialty, count(*) AS visits FROM clinicians GROUP BY 1"))
    assert "Cardiology\t2" in out
    refused = asyncio.run(server.query("DELETE FROM appointments"))
    assert refused.startswith("Error:")
    outcomes = [json.loads(x)["outcome"] for x in _audit_to_tmp.read_text().splitlines()]
    assert outcomes == ["ok", "ok", "refused"]
