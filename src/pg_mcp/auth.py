"""Static bearer tokens for the optional HTTP transport.

This is a deliberate simplification. The MCP specification describes
OAuth 2.1-based authorization for HTTP transports (authorization server,
protected resource metadata, audience-bound access tokens). Here each client
gets a long random token; the server stores only its sha256 and the tools it
may call. Tokens never appear in logs, the audit file, or error responses.

PG_MCP_TOKENS (JSON):
    {"<token_id>": {"sha256": "<64 hex chars>", "tools": ["query", "inspect_schema"]}}
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass

from starlette.datastructures import Headers
from starlette.responses import JSONResponse

STATE_KEY = "pg_mcp_token_id"
MAX_BODY_BYTES = 1_000_000


@dataclass(frozen=True)
class TokenEntry:
    token_id: str
    digest: bytes
    tools: frozenset[str]


def hash_token(token: str) -> str:
    """The value to put in PG_MCP_TOKENS for a given token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def load_tokens(raw: str | None, known_tools: set[str]) -> list[TokenEntry]:
    """Parse PG_MCP_TOKENS. Raises ValueError on anything malformed."""
    if not raw:
        raise ValueError("PG_MCP_TOKENS is required in HTTP mode")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise ValueError("PG_MCP_TOKENS is not valid JSON") from None
    if not isinstance(data, dict) or not data:
        raise ValueError("PG_MCP_TOKENS must be a non-empty JSON object")

    entries = []
    for token_id, spec in data.items():
        if not isinstance(spec, dict):
            raise ValueError(f"token {token_id!r}: expected an object")
        digest_hex = str(spec.get("sha256", "")).lower()
        if len(digest_hex) != 64 or any(c not in "0123456789abcdef" for c in digest_hex):
            raise ValueError(f"token {token_id!r}: sha256 must be 64 hex characters (store the hash, not the token)")
        tools = spec.get("tools")
        if not isinstance(tools, list) or not tools:
            raise ValueError(f"token {token_id!r}: tools must be a non-empty list")
        unknown = set(tools) - known_tools
        if unknown:
            raise ValueError(f"token {token_id!r}: unknown tools {sorted(unknown)}")
        entries.append(TokenEntry(str(token_id), bytes.fromhex(digest_hex), frozenset(tools)))
    return entries


# -- auth check --------------------------------------------------------------
# What it does: maps a presented bearer token to a token entry, or to nothing.
# Catch it:     "How do you check an API token without leaking it through timing or logs?"
# Card:         docs/cards/auth-check.md
def verify_token(presented: str, entries: list[TokenEntry]) -> TokenEntry | None:
    digest = hashlib.sha256(presented.encode("utf-8")).digest()
    match = None
    for entry in entries:
        # compare against every entry, constant time, no early exit
        if hmac.compare_digest(digest, entry.digest):
            match = entry
    return match


def _bearer(headers: Headers) -> str | None:
    value = headers.get("authorization", "")
    scheme, _, token = value.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def _no_duplicate_keys(pairs):
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate key")
    return dict(pairs)


def _called_tools(body: bytes) -> list[str] | None:
    """Tool names in tools/call messages; None if the body is not clean JSON."""
    try:
        message = json.loads(body, object_pairs_hook=_no_duplicate_keys)
    except (ValueError, UnicodeDecodeError):
        return None
    messages = message if isinstance(message, list) else [message]
    names = []
    for m in messages:
        if isinstance(m, dict) and m.get("method") == "tools/call":
            params = m.get("params")
            name = params.get("name") if isinstance(params, dict) else None
            names.append(name if isinstance(name, str) else "")
    return names


def _error(status: int, message: str) -> JSONResponse:
    headers = {"WWW-Authenticate": 'Bearer realm="pg-mcp-server"'} if status == 401 else None
    return JSONResponse({"error": message}, status_code=status, headers=headers)


class BearerAuthMiddleware:
    """401 without a valid token, 403 for a tool the token may not call."""

    def __init__(self, app, entries: list[TokenEntry]):
        self.app = app
        self.entries = entries

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        presented = _bearer(Headers(scope=scope))
        entry = verify_token(presented, self.entries) if presented else None
        if entry is None:
            await _error(401, "missing or invalid bearer token")(scope, receive, send)
            return

        body = b""
        if scope.get("method") == "POST":
            more = True
            while more:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                body += message.get("body", b"")
                more = message.get("more_body", False)
                if len(body) > MAX_BODY_BYTES:
                    await _error(413, "request body too large")(scope, receive, send)
                    return
            tools = _called_tools(body)
            if tools is None:
                await _error(400, "request body must be JSON without duplicate keys")(scope, receive, send)
                return
            denied = sorted({t for t in tools if t not in entry.tools})
            if denied:
                await _error(403, f"token {entry.token_id!r} may not call {', '.join(denied)}")(scope, receive, send)
                return

        scope.setdefault("state", {})[STATE_KEY] = entry.token_id
        replayed = False

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)
