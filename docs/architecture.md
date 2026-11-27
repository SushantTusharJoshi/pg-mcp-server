# Architecture

How a tool call travels through the code on `main`, and what CI runs. Every box names the file that does the work.

```mermaid
flowchart TB
  subgraph runtime["Runtime: one tool call"]
    direction TB
    agent["MCP client / AI agent"]
    stdio["stdio transport (default)<br/>server.py main()"]
    http["HTTP transport, PG_MCP_TRANSPORT=http<br/>http_app.py build_app(), 127.0.0.1:8000/mcp"]
    auth["Bearer token check<br/>auth.py BearerAuthMiddleware<br/>sha256 + constant-time compare<br/>401 no token, 403 tool not allowed"]
    tools["FastMCP tools<br/>server.py"]

    agent --> stdio --> tools
    agent --> http --> auth --> tools

    tools --> q["query(sql, params)"]
    tools --> schema["inspect_schema()<br/>fixed catalog SQL, table name bound as $1"]
    tools --> search["search_books(), only when PG_MCP_BOOKSTORE_TOOLS is on<br/>server.py build_mcp()<br/>fixed SQL with placeholders,<br/>LIKE wildcards escaped, limit clamped"]

    q --> kw{"Keyword blocklist<br/>server.py BLOCKED_KEYWORDS"}
    kw -- "match" --> refuse["Refusal text<br/>server.py _Call.refuse"]
    kw -- "no match" --> parse{"sqlglot parse<br/>sql_guard.py check_sql<br/>one SELECT, no data-modifying CTE,<br/>no SELECT INTO, no FOR UPDATE,<br/>no side-effect functions"}
    parse -- "refused" --> refuse
    parse -- "one plain read" --> txn["BEGIN READ ONLY + SET LOCAL statement_timeout<br/>server.py query()"]
    txn --> cursor["Cursor read, stops at the row cap<br/>QUERY_ROW_LIMIT, max 500<br/>and the byte cap QUERY_MAX_BYTES"]

    pool["asyncpg pool<br/>server.py get_pool()"]
    cursor --> pool
    schema --> pool
    search --> pool

    pg[("PostgreSQL 16, role mcp_readonly<br/>seed/init.sql: GRANT SELECT on authors, genres, books<br/>(or any database and read-only role set by PG_*)<br/>REVOKE TEMPORARY, REVOKE EXECUTE on<br/>pg_terminate_backend, pg_cancel_backend, pg_notify, ...")]
    pool --> pg

    pg --> fence["Untrusted-data fence + byte cap<br/>output.py untrusted_block()<br/>random block id per response"]
    pg -- "Postgres error" --> fence
    fence --> okcall["server.py _Call.ok / _Call.fail"]

    refuse --> audit
    okcall --> audit["Audit record<br/>audit.py build_record()<br/>sql_guard.py redact_sql(): literals become ?,<br/>sha256 of the original SQL, no values, no tokens"]
    audit --> jsonl[("PG_MCP_AUDIT_LOG<br/>append-only JSONL, mode 0600<br/>audit.py AuditLog.write()")]
    audit -- "write failed" --> withheld["Result withheld<br/>server.py AUDIT_FAILED"]
    jsonl --> reply["Tool result to the agent"]
  end

  subgraph ci["CI: .github/workflows/ci.yml, every push to main and pull request"]
    direction TB
    lint["lint: ruff check src/ tests/"]
    unit["test: pytest -m 'not integration'<br/>tests/test_sql_guard.py, test_server.py, test_output.py,<br/>test_audit.py, test_http_auth.py, test_query_cap.py, test_config.py,<br/>test_generic_mode.py"]
    svc[("postgres:16 service container")]
    seed["psql -f seed/init.sql"]
    integ["integration: pytest tests/integration<br/>test_containment.py runs the corpus in tests/sql_corpus.py<br/>test_privileges.py checks the GRANTs and REVOKEs"]
    clinicseed["psql -f seed/sample_clinic.sql<br/>into a second database, clinic"]
    generic["generic mode: PG_MCP_INTEGRATION=clinic, PG_MCP_BOOKSTORE_TOOLS=0<br/>test_generic_clinic.py: same layers on another schema,<br/>real MCP stdio client"]
    svc --> seed --> integ
    svc --> clinicseed --> generic
  end
```

Notes, each checked against the code:

- Only `query` runs inside `BEGIN READ ONLY` with a statement timeout. `inspect_schema` and `search_books` run SQL the server wrote, with values bound as parameters, as the same `mcp_readonly` role ([server.py](../src/pg_mcp/server.py)).
- `build_mcp` registers `inspect_schema` and `query` always and `search_books` only when `PG_MCP_BOOKSTORE_TOOLS` is on (the default). With it off, the server is generic: the same two tools and the same layers against whatever database and role `PG_*` names. CI proves this on [seed/sample_clinic.sql](../seed/sample_clinic.sql) ([test_generic_clinic.py](../tests/integration/test_generic_clinic.py)).
- Refusals, successes and errors all go through `_Call`, so every call writes an audit record before anything is returned ([server.py](../src/pg_mcp/server.py), [audit.py](../src/pg_mcp/audit.py)). Database error messages are fenced like rows, because Postgres can quote row values in them.
- In HTTP mode the middleware also returns `413` for a body over 1 MB and `400` for a body that is not JSON or has duplicate keys, and passes the token ID on as the audit `caller` ([auth.py](../src/pg_mcp/auth.py)). In stdio mode the caller is `stdio`.
- Locally, [docker-compose.yml](../docker-compose.yml) runs `postgres:16-alpine` on `127.0.0.1` and loads the same `seed/init.sql`.
