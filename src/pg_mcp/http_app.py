"""Optional streamable HTTP transport (PG_MCP_TRANSPORT=http)."""

from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from pg_mcp.auth import BearerAuthMiddleware, load_tokens

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000


def http_settings() -> tuple[str, int]:
    return os.getenv("PG_MCP_HTTP_HOST", DEFAULT_HOST), int(os.getenv("PG_MCP_HTTP_PORT", str(DEFAULT_PORT)))


def build_app(mcp: FastMCP, tokens_json: str | None = None):
    """The FastMCP streamable HTTP app behind the bearer-token middleware."""
    raw = tokens_json if tokens_json is not None else os.getenv("PG_MCP_TOKENS")
    known = {t.name for t in mcp._tool_manager.list_tools()}
    entries = load_tokens(raw, known)

    # Each POST stands alone: no server-side session to bind to a token.
    mcp.settings.stateless_http = True
    mcp.settings.json_response = True
    allowed_hosts = [h.strip() for h in os.getenv("PG_MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
    if allowed_hosts:
        # Host-header check against DNS rebinding, for binds other than loopback
        mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=allowed_hosts,
            allowed_origins=[f"https://{h}" for h in allowed_hosts] + [f"http://{h}" for h in allowed_hosts],
        )
    mcp._session_manager = None  # a session manager runs once; build a fresh one per app
    return BearerAuthMiddleware(mcp.streamable_http_app(), entries)


def run_http(mcp: FastMCP) -> None:
    import uvicorn

    host, port = http_settings()
    app = build_app(mcp)
    # uvicorn's access log records method, path and status only, never headers
    uvicorn.run(app, host=host, port=port, log_level=os.getenv("PG_MCP_LOG_LEVEL", "info"))
