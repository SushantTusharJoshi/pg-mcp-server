"""Parse agent SQL with sqlglot and refuse anything that is not one plain read.

This is one layer among several. The database (read-only transaction, a role
with SELECT on three tables, revoked functions, statement timeout) is the
boundary; this layer refuses early, before a connection is used, and gives the
agent a clear reason.
"""

from __future__ import annotations

import logging

import sqlglot
from sqlglot import exp
from sqlglot.dialects.postgres import Postgres
from sqlglot.errors import ParseError, TokenError
from sqlglot.tokens import TokenType

DIALECT = "postgres"

# sqlglot logs parser notes (for example "falling back to Command") that add noise, not signal
logging.getLogger("sqlglot").setLevel(logging.ERROR)

# Node types that write, change the schema, or are statements sqlglot could not
# model (it falls back to exp.Command for those, so refusing Command fails closed).
_FORBIDDEN_NODES: tuple[type[exp.Expression], ...] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.TruncateTable,
    exp.Copy,
    exp.Set,
    exp.Command,
    exp.Into,  # SELECT ... INTO new_table creates a table
    exp.Lock,  # SELECT ... FOR UPDATE / FOR SHARE takes row locks
)

# Functions with side effects, session state, dynamic SQL or file access.
# The database also stops most of these (read-only transaction, REVOKE,
# superuser-only defaults); refusing them here keeps the reason readable.
_FORBIDDEN_FUNCTIONS = frozenset(
    {
        "pg_terminate_backend",
        "pg_cancel_backend",
        "pg_notify",
        "set_config",
        "nextval",
        "setval",
        "loread",
        "lowrite",
        "pg_read_file",
        "pg_read_binary_file",
        "pg_ls_dir",
        "pg_stat_file",
        "pg_reload_conf",
        "pg_rotate_logfile",
        "pg_logical_emit_message",
        "txid_current",
        "pg_current_xact_id",
    }
)
_FORBIDDEN_FUNCTION_PREFIXES = (
    "lo_",  # large objects: lo_create, lo_import, lo_export, lo_unlink, ...
    "dblink",  # opens connections to other databases
    "pg_advisory",  # session advisory locks outlive the transaction
    "pg_try_advisory",
    "query_to_xml",  # runs a SQL string the parser cannot see
    "cursor_to_xml",
)


def _function_name(node: exp.Func) -> str:
    if isinstance(node, exp.Anonymous):
        return str(node.name).lower()
    return node.sql_name().lower()


def _is_read(root: exp.Expression) -> bool:
    while isinstance(root, (exp.Subquery, exp.Paren)):
        root = root.this
    return isinstance(root, (exp.Select, exp.SetOperation))


def check_sql(sql: str) -> str | None:
    """Return None when the SQL is one plain read, else a short refusal reason."""
    try:
        statements = [s for s in sqlglot.parse(sql, read=DIALECT) if s is not None]
    except (ParseError, TokenError):
        return "the SQL could not be parsed as PostgreSQL"

    if len(statements) != 1:
        return f"expected exactly one statement, found {len(statements)}"

    root = statements[0]
    if not _is_read(root):
        kind = "this statement type" if isinstance(root, exp.Command) else root.key.upper()
        return f"only SELECT or WITH ... SELECT is allowed, not {kind}"

    for node in root.walk():
        if isinstance(node, _FORBIDDEN_NODES):
            return f"{node.key.upper()} is not allowed inside a read"
        if isinstance(node, exp.Func):
            name = _function_name(node)
            if name in _FORBIDDEN_FUNCTIONS or name.startswith(_FORBIDDEN_FUNCTION_PREFIXES):
                return f"function {name}() is not allowed"

    return None


# -- redaction for the audit log ---------------------------------------------

_LITERAL_NODES: tuple[type[exp.Expression], ...] = (
    exp.Literal,
    exp.Boolean,
    exp.RawString,  # $$dollar quoted$$
    exp.ByteString,  # E'escaped'
    exp.UnicodeString,
    exp.National,
    exp.BitString,
    exp.HexString,
)

_LITERAL_TOKENS = frozenset(
    {
        TokenType.STRING,
        TokenType.NUMBER,
        TokenType.HEREDOC_STRING,
        TokenType.BIT_STRING,
        TokenType.HEX_STRING,
        TokenType.BYTE_STRING,
        TokenType.NATIONAL_STRING,
        TokenType.RAW_STRING,
        TokenType.UNICODE_STRING,
    }
)

REDACTED = "?"


def _blank(node: exp.Expression) -> exp.Expression:
    if isinstance(node, exp.Interval):
        return exp.var(f"INTERVAL {REDACTED}")  # INTERVAL '1 day' carries its value in the unit too
    # $1 is a Parameter holding the literal 1; keep placeholders as they are
    if isinstance(node, _LITERAL_NODES) and not isinstance(node.parent, exp.Parameter):
        return exp.var(REDACTED)
    return node


def _has_literal_tokens(sql: str) -> bool:
    tokens = Postgres().tokenize(sql)
    for prev, tok in zip([None, *tokens], tokens, strict=False):
        if tok.token_type not in _LITERAL_TOKENS:
            continue
        if tok.token_type == TokenType.NUMBER and prev is not None and prev.token_type == TokenType.PARAMETER:
            continue  # the 1 in $1
        return True
    return False


def redact_sql(sql: str) -> str | None:
    """Return the SQL with every literal replaced by ? and comments dropped,
    or None when it cannot be parsed (the caller then stores only a hash)."""
    try:
        statements = [s for s in sqlglot.parse(sql, read=DIALECT) if s is not None]
        out = "; ".join(s.transform(_blank).sql(dialect=DIALECT, comments=False) for s in statements)
        # Belt and braces: if any literal token survived the rewrite, store nothing.
        if _has_literal_tokens(out):
            return None
    except (ParseError, TokenError):
        return None
    return out
