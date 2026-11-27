# Security

pg-mcp-server gives an AI agent read access to a PostgreSQL database. The agent writes its own SQL, so the design assumes that SQL can be hostile and puts the boundary in Postgres itself. The Python checks refuse early and explain why; they are not what keeps the data safe.

## Threat model

| Who | What they can do | What we protect |
|---|---|---|
| The agent, possibly steered by a prompt injection | Send any SQL text and parameters to `query`, any arguments to the other tools | Data integrity, tables outside the allowlist, other database sessions, server files, availability |
| Whoever can write rows the agent reads | Put instruction-like text in a book title or other column | The agent's next decisions |
| A network client (HTTP mode only) | Send requests to the HTTP endpoint | Tool access without a valid token, tools outside a token's list |
| Someone who reads the audit file or logs | Read what was recorded | Parameter values, literals in SQL, row data, tokens |

Out of scope: a compromised host or Postgres superuser, and the operator's own choice of what to put in the granted tables.

## Layers, what each stops, and its test

| # | Layer | Stops | Test |
|---|---|---|---|
| 1 | Keyword blocklist (regex) | Obvious DDL/DML and `COPY`, before a connection is used | [`tests/test_server.py`](tests/test_server.py), [`test_query_cap.py::test_blocked_keyword_never_reaches_the_database`](tests/test_query_cap.py) |
| 2 | sqlglot parse ([`sql_guard.py`](src/pg_mcp/sql_guard.py)) | More than one statement; anything but SELECT / WITH ... SELECT; data-modifying CTEs; `SELECT INTO`; `FOR UPDATE`; side-effect, session and dynamic-SQL functions (`pg_notify`, `set_config`, `nextval`, `lo_*`, `dblink*`, advisory locks, `query_to_xml`); SQL it cannot parse | [`tests/test_sql_guard.py`](tests/test_sql_guard.py), [`integration/test_containment.py::test_tool_refuses_before_the_database`](tests/integration/test_containment.py) |
| 3 | `BEGIN READ ONLY` transaction | Every write, even one hidden from layers 1 and 2 (for example inside `query_to_xml('DEL' \|\| 'ETE ...')`) | [`test_containment.py::test_database_alone_rejects`](tests/integration/test_containment.py) |
| 4 | Role `mcp_readonly` ([`seed/init.sql`](seed/init.sql)) | Reading any table but `authors`, `genres`, `books`; sequences; `CREATE` and `TEMP`; `pg_terminate_backend`, `pg_cancel_backend`, `pg_notify`, `pg_logical_emit_message`; inherited rights (no role memberships) | [`tests/integration/test_privileges.py`](tests/integration/test_privileges.py) |
| 5 | `SET LOCAL statement_timeout` | Long-running queries such as `pg_sleep(30)` | [`test_containment.py::test_pg_sleep_is_cancelled_by_statement_timeout`](tests/integration/test_containment.py) |
| 6 | Row cap (cursor) and byte cap (`QUERY_MAX_BYTES`) | Huge results, including `LIMIT ALL`, trailing `--`, `UNION ALL`, recursive CTEs and wide rows | [`test_containment.py::test_limit_bypass_attempts_stay_capped`](tests/integration/test_containment.py), [`test_wide_rows_hit_the_byte_cap`](tests/integration/test_containment.py), [`tests/test_output.py`](tests/test_output.py) |
| 7 | asyncpg pool reset on release | Session settings and advisory locks outliving a call on a pooled connection | [`test_containment.py::test_pool_reset_clears_session_state`](tests/integration/test_containment.py) |
| 8 | Untrusted-data fence ([`output.py`](src/pg_mcp/output.py)) | Database text, including error messages, being mistaken for server or user text; a row faking the end of the block (random id per response) | [`tests/test_output.py`](tests/test_output.py) |
| 9 | Audit log ([`audit.py`](src/pg_mcp/audit.py), [format](docs/audit-log.md)) | Calls without a record (result withheld if the write fails); values reaching the log | [`tests/test_audit.py`](tests/test_audit.py) |
| 10 | HTTP bearer tokens ([`auth.py`](src/pg_mcp/auth.py)) | Requests without a valid token (401), tool calls outside a token's list (403), ambiguous bodies (400), token leaks via logs or timing | [`tests/test_http_auth.py`](tests/test_http_auth.py) |
| 11 | Network defaults | Docker Postgres and the HTTP server are bound to `127.0.0.1`; FastMCP's Host-header check stays on | [`test_http_auth.py::test_default_bind_is_loopback`](tests/test_http_auth.py) |

The corpus behind layers 2 to 7 is [`tests/sql_corpus.py`](tests/sql_corpus.py). CI runs it against a `postgres:16` service seeded from `seed/init.sql`, and the job fails if any case gets through. Cases the database alone would run are listed in `DB_ALLOWS_CONTAINED_ELSEWHERE` with the layer that contains them.

### HTTP mode is a simplification

The MCP specification describes OAuth 2.1-based authorization for HTTP transports (an authorization server, protected resource metadata, audience-bound short-lived tokens). HTTP mode here uses static bearer tokens instead: the operator generates a long random token per client and configures only its sha256 and allowed tools in `PG_MCP_TOKENS`. That fits one operator and a few known clients. It is not a substitute for the OAuth flow when clients are third parties.

## Audit findings from Nov 4, 2026

An internal source review of commit `02651e0` found:

| Finding | Status |
|---|---|
| The row cap wrapped agent SQL as `SELECT * FROM (...) LIMIT n`, so a trailing `--` commented the LIMIT out | Fixed in `7349a42`: rows are read from a cursor and reading stops at the cap. Tests: `tests/test_query_cap.py`, `test_limit_bypass_attempts_stay_capped` |
| `mcp_readonly` could call `pg_terminate_backend` and `pg_cancel_backend` (end or cancel other sessions of the same role) and `pg_notify` (send messages) from inside a read-only transaction | Fixed in `7349a42` by `REVOKE EXECUTE ... FROM PUBLIC`. Tests: `test_privileges.py::test_has_no_execute_on_revoked_functions`, `test_calling_revoked_functions_fails` |
| `docker-compose.yml` published Postgres on all interfaces with the superuser password `postgres` | Fixed earlier in `397b401`: bound to `127.0.0.1`, `POSTGRES_PASSWORD` required |

Found while building the corpus for this change and fixed in the same series: `pg_logical_emit_message` was executable by PUBLIC and writes to the WAL from a read-only transaction (now revoked); `GRANT ... ON ALL TABLES` plus default privileges made every future table readable (now three explicit grants).

## Residual risks

- Reads inside the allowlist are unrestricted. The agent can read every row and column of `authors`, `genres` and `books`. Grant columns or use views if that is too much.
- The fence is a label. It tells a model which text is data; it cannot force the model to ignore instructions in that text.
- Memory: the byte cap trims output, but one very large value (`SELECT repeat('x', 500000000)`) is still built by Postgres and received by the server before trimming. The statement timeout bounds time, not memory.
- Load: a query can use a lot of database CPU for up to `STATEMENT_TIMEOUT_MS`, across up to 5 pooled connections. There is no rate limit.
- The parser's function denylist cannot be complete. Layers 3 and 4 are the boundary; the denylist only improves refusals.
- sqlglot may refuse valid Postgres it cannot parse (fails closed), and accepting SQL is not a proof that it is safe.
- The audit record is written after the query runs. When the write fails, the result is withheld, but the query has already executed.
- `sql_sha256` lets someone holding the audit file confirm a guessed SQL string, literals included. Keep the file private (it is created `0600`). The file is not rotated or signed.
- HTTP tokens do not expire, changing them needs a restart, `tools/list` is not filtered per token, and the server does not terminate TLS. Put a TLS proxy in front for anything beyond loopback.
- In stdio mode the only identity check is the local operating-system user.
- Unqualified table names resolve through `search_path`. `mcp_readonly` has no `CREATE` anywhere (tested), so it cannot plant objects, but another role that can create objects in a schema earlier in the path could.
- `seed/init.sql` runs only on a fresh volume. After upgrading, run `docker compose down -v` once.
- The seed password for `mcp_readonly` is a demo value. Change it outside local use.
- Amazon RDS is not tested. On RDS the master user may not be allowed to revoke rights on `pg_catalog` functions; see [docs/aws-rds.md](docs/aws-rds.md).
- Follow-up: migrate to the mcp 2.x SDK; this code pins `mcp<2`.

## Reporting a vulnerability

Please do not open a public issue with details. Use GitHub's private vulnerability reporting for this repository (Security tab, "Report a vulnerability"). If that button is not shown, open an issue titled "security contact" with no details, and the maintainer will reply with a private channel.
