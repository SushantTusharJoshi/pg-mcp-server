"""PG-F7 and PG-N10: the optional HTTP transport and its static bearer tokens."""

import json
import logging
import secrets

import pytest
from starlette.testclient import TestClient

from pg_mcp import auth, http_app, server
from tests import fakes

FULL = "full-" + secrets.token_urlsafe(24)
SCHEMA_ONLY = "schema-" + secrets.token_urlsafe(24)
TOKENS = json.dumps(
    {
        "analyst": {"sha256": auth.hash_token(FULL), "tools": ["query", "inspect_schema", "search_books"]},
        "catalog-bot": {"sha256": auth.hash_token(SCHEMA_ONLY), "tools": ["inspect_schema"]},
    }
)
HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def _call(tool, arguments, msg_id=1):
    return {"jsonrpc": "2.0", "id": msg_id, "method": "tools/call", "params": {"name": tool, "arguments": arguments}}


@pytest.fixture
def client(monkeypatch):
    fakes.install(monkeypatch, server, fakes.Conn(
        rows=[{"title": "Kindred"}],
        fetch_rows={"information_schema.tables": [{"table_name": "books", "row_count": 20}]},
    ))
    app = http_app.build_app(server.mcp, TOKENS)
    with TestClient(app, base_url="http://127.0.0.1:8000") as c:
        yield c


def _post(client, body, token=None, raw=None):
    headers = dict(HEADERS)
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    content = raw if raw is not None else json.dumps(body)
    return client.post("/mcp", content=content, headers=headers)


def test_missing_token_is_401(client):
    r = _post(client, _call("query", {"sql": "SELECT 1"}))
    assert r.status_code == 401
    assert r.headers["www-authenticate"].startswith("Bearer")


@pytest.mark.parametrize("value", ["Bearer wrong", "Basic " + FULL, FULL, "Bearer ", "bearer " + FULL[:-1]])
def test_wrong_token_is_401(client, value):
    headers = dict(HEADERS, Authorization=value)
    r = client.post("/mcp", content=json.dumps(_call("query", {"sql": "SELECT 1"})), headers=headers)
    assert r.status_code == 401


def test_get_without_token_is_401(client):
    assert client.get("/mcp", headers=HEADERS).status_code == 401


def test_allowed_tool_runs_and_audit_names_the_token(client, _audit_to_tmp):
    r = _post(client, _call("query", {"sql": "SELECT title FROM books WHERE isbn = 'x-123'"}), token=FULL)
    assert r.status_code == 200
    result = r.json()["result"]
    assert result["isError"] is False
    assert "Kindred" in result["content"][0]["text"]
    rec = json.loads(_audit_to_tmp.read_text().splitlines()[-1])
    assert rec["caller"] == "analyst"
    assert rec["tool"] == "query"


def test_scheme_is_case_insensitive(client):
    headers = dict(HEADERS, Authorization=f"bearer {FULL}")
    r = client.post("/mcp", content=json.dumps(_call("inspect_schema", {})), headers=headers)
    assert r.status_code == 200


def test_disallowed_tool_is_403(client, _audit_to_tmp):
    r = _post(client, _call("query", {"sql": "SELECT 1"}), token=SCHEMA_ONLY)
    assert r.status_code == 403
    assert not _audit_to_tmp.exists() or "query" not in _audit_to_tmp.read_text()


def test_restricted_token_can_call_its_tool(client):
    r = _post(client, _call("inspect_schema", {}), token=SCHEMA_ONLY)
    assert r.status_code == 200
    assert "books" in r.json()["result"]["content"][0]["text"]


def test_batch_with_a_disallowed_tool_is_403(client):
    batch = [_call("inspect_schema", {}, 1), _call("query", {"sql": "SELECT 1"}, 2)]
    assert _post(client, batch, token=SCHEMA_ONLY).status_code == 403


def test_duplicate_keys_are_400(client):
    """A parser that keeps the first 'name' and one that keeps the last could
    disagree about which tool is called; refuse the ambiguity."""
    raw = (
        '{"jsonrpc":"2.0","id":1,"method":"tools/call",'
        '"params":{"name":"inspect_schema","name":"query","arguments":{"sql":"SELECT 1"}}}'
    )
    assert _post(client, None, token=SCHEMA_ONLY, raw=raw).status_code == 400


def test_tools_list_needs_a_token_but_no_tool_right(client):
    r = _post(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, token=SCHEMA_ONLY)
    assert r.status_code == 200
    names = {t["name"] for t in r.json()["result"]["tools"]}
    assert names == {"inspect_schema", "query", "search_books"}


def test_tokens_never_logged(client, caplog, _audit_to_tmp):
    caplog.set_level(logging.DEBUG)
    _post(client, _call("query", {"sql": "SELECT 1"}), token=FULL)
    _post(client, _call("query", {"sql": "SELECT 1"}), token=SCHEMA_ONLY)
    _post(client, _call("query", {"sql": "SELECT 1"}), token="Bearer-guess-" + FULL[::-1])
    for token in (FULL, SCHEMA_ONLY, FULL[::-1]):
        assert token not in caplog.text
        assert token not in _audit_to_tmp.read_text()


def test_error_responses_do_not_echo_the_token(client):
    for token in (FULL[::-1], SCHEMA_ONLY):
        r = _post(client, _call("query", {"sql": "SELECT 1"}), token=token)
        assert token not in r.text


def test_default_bind_is_loopback(monkeypatch):
    monkeypatch.delenv("PG_MCP_HTTP_HOST", raising=False)
    monkeypatch.delenv("PG_MCP_HTTP_PORT", raising=False)
    assert http_app.http_settings() == ("127.0.0.1", 8000)


def test_run_http_passes_the_host_to_uvicorn(monkeypatch):
    import uvicorn

    seen = {}
    monkeypatch.delenv("PG_MCP_HTTP_HOST", raising=False)
    monkeypatch.setenv("PG_MCP_TOKENS", TOKENS)
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: seen.update(kw))
    http_app.run_http(server.mcp)
    assert seen["host"] == "127.0.0.1"


def test_stdio_is_the_default_transport(monkeypatch):
    seen = []
    monkeypatch.delenv("PG_MCP_TRANSPORT", raising=False)
    monkeypatch.setattr(server.mcp, "run", lambda transport: seen.append(transport))
    server.main()
    assert seen == ["stdio"]


def test_unknown_transport_exits(monkeypatch):
    monkeypatch.setenv("PG_MCP_TRANSPORT", "sse")
    with pytest.raises(SystemExit):
        server.main()


class TestTokenConfig:
    KNOWN = {"query", "inspect_schema", "search_books"}

    def test_http_mode_needs_tokens(self):
        with pytest.raises(ValueError, match="required"):
            http_app.build_app(server.mcp, "")

    def test_plaintext_token_is_rejected(self):
        with pytest.raises(ValueError, match="hash"):
            auth.load_tokens(json.dumps({"a": {"sha256": "my-secret-token", "tools": ["query"]}}), self.KNOWN)

    def test_unknown_tool_is_rejected(self):
        bad = json.dumps({"a": {"sha256": "0" * 64, "tools": ["drop_everything"]}})
        with pytest.raises(ValueError, match="unknown tools"):
            auth.load_tokens(bad, self.KNOWN)

    def test_empty_tool_list_is_rejected(self):
        with pytest.raises(ValueError):
            auth.load_tokens(json.dumps({"a": {"sha256": "0" * 64, "tools": []}}), self.KNOWN)

    def test_only_hashes_are_held_in_memory(self):
        entries = auth.load_tokens(TOKENS, self.KNOWN)
        held = repr(entries)
        assert FULL not in held and SCHEMA_ONLY not in held
        assert all(len(e.digest) == 32 for e in entries)


def test_compare_is_constant_time_and_checks_every_entry(monkeypatch):
    entries = auth.load_tokens(TOKENS, {"query", "inspect_schema", "search_books"})
    calls = []
    real = auth.hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(auth.hmac, "compare_digest", spy)
    assert auth.verify_token(FULL, entries).token_id == "analyst"  # first entry matches
    assert len(calls) == len(entries)  # no early exit after the match
    calls.clear()
    assert auth.verify_token("nope", entries) is None
    assert len(calls) == len(entries)
