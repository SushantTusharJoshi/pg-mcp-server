"""Append one JSON line per tool call. Format: docs/audit-log.md.

What is stored: the SQL with every literal replaced by ?, a sha256 of the
original SQL, counts, timing, outcome and caller. What is never stored:
parameter values, tool arguments, result rows, error messages, tokens.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime, timezone

from pg_mcp.sql_guard import redact_sql

DEFAULT_PATH = "pg-mcp-audit.jsonl"


def audit_path() -> str:
    return os.getenv("PG_MCP_AUDIT_LOG") or os.path.join(os.getcwd(), DEFAULT_PATH)


# -- audit writer ------------------------------------------------------------
# What it does: turns one finished tool call into one JSON line with no values in it.
# Catch it:     "How do you audit what an agent ran without logging the data it sent?"
# Card:         docs/cards/audit-writer.md
def build_record(
    *,
    tool: str,
    sql: str | None,
    param_count: int,
    rows: int,
    output_bytes: int,
    duration_ms: float,
    outcome: str,
    caller: str,
    sqlstate: str | None = None,
) -> dict:
    # STEP: who, what and when
    record: dict = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "tool": tool,
        "caller": caller,
        "outcome": outcome,
    }
    # DECISION: no SQL for this call -> nothing to redact
    if sql is None:
        record.update(sql=None, sql_sha256=None, sql_unparsed=False)
    else:
        # STEP: sqlglot replaces every literal with ?, drops comments; None if it cannot parse
        normalized = redact_sql(sql)
        record.update(
            sql=normalized,
            sql_sha256=hashlib.sha256(sql.encode("utf-8")).hexdigest(),
            sql_unparsed=normalized is None,
        )
    record.update(
        param_count=param_count,
        rows=rows,
        output_bytes=output_bytes,
        duration_ms=round(duration_ms, 2),
    )
    if sqlstate:
        record["sqlstate"] = sqlstate
    # RETURN: counts and codes only; the server withholds the result if write() fails
    return record


class AuditLog:
    """Append-only JSONL file, created with mode 0600."""

    def __init__(self, path: str | None = None):
        self._path = path
        self._lock = threading.Lock()

    @property
    def path(self) -> str:
        return self._path or audit_path()

    def write(self, record: dict) -> None:
        # STEP: one line, appended, file created 0600
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._lock:
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as f:
                f.write(line)
