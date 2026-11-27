# Audit log

Every tool call (`inspect_schema`, `query`, `search_books`) appends one JSON object on one line to the audit file. Refused and failed calls are recorded too.

- Path: `PG_MCP_AUDIT_LOG`, default `pg-mcp-audit.jsonl` in the server's working directory.
- The file is created with mode `0600` and only ever appended to.
- If a record cannot be written, the tool returns `Error: the audit record could not be written, so the result is withheld.` instead of the result. A call that already failed still raises its error.

Code: [`src/pg_mcp/audit.py`](../src/pg_mcp/audit.py) (record and writer), `redact_sql` in [`src/pg_mcp/sql_guard.py`](../src/pg_mcp/sql_guard.py). Tests: [`tests/test_audit.py`](../tests/test_audit.py).

## Fields

| Field | Type | Meaning |
|---|---|---|
| `ts` | string | UTC time the record was written, ISO 8601 with milliseconds |
| `tool` | string | `inspect_schema`, `query` or `search_books` |
| `caller` | string | Token ID in HTTP mode, `stdio` otherwise |
| `outcome` | string | `ok`, `refused` (blocked before the database) or `error` (raised) |
| `sql` | string or null | The SQL with every literal replaced by `?` and comments removed; null when it could not be parsed |
| `sql_sha256` | string or null | sha256 of the original SQL text, hex |
| `sql_unparsed` | bool | true when sqlglot could not parse the SQL, so only the hash is stored |
| `param_count` | int | How many bound parameters were sent (never their values) |
| `rows` | int | Rows returned to the agent |
| `output_bytes` | int | UTF-8 size of the text returned to the agent |
| `duration_ms` | number | Wall time of the call inside the server |
| `sqlstate` | string | Only on database errors: the Postgres SQLSTATE code, e.g. `42501` |

For `query` the SQL is the agent's. For `inspect_schema` and `search_books` it is the SQL the server wrote; the table name, search term and filters are bound parameters and are not stored.

## Example

```json
{"ts":"2026-11-27T15:04:12.381+00:00","tool":"query","caller":"stdio","outcome":"ok","sql":"SELECT title FROM books WHERE isbn = ? AND title ILIKE $1","sql_sha256":"5b0c...e1","sql_unparsed":false,"param_count":1,"rows":1,"output_bytes":243,"duration_ms":3.71}
{"ts":"2026-11-27T15:04:20.006+00:00","tool":"query","caller":"analyst-laptop","outcome":"refused","sql":"SELECT ?; SELECT ?","sql_sha256":"9f2a...7c","sql_unparsed":false,"param_count":0,"rows":0,"output_bytes":64,"duration_ms":1.02}
{"ts":"2026-11-27T15:04:31.554+00:00","tool":"query","caller":"stdio","outcome":"error","sql":"SELECT * FROM staff_salaries","sql_sha256":"c41d...0b","sql_unparsed":false,"param_count":0,"rows":0,"output_bytes":201,"duration_ms":2.40,"sqlstate":"42501"}
```

## Redaction

Literal values are what carry data in agent SQL (`WHERE email = 'someone@example.com'`), so they are removed before anything is written:

1. sqlglot parses the SQL (postgres dialect) and every literal node is replaced by `?`: strings, numbers, booleans, dollar-quoted strings, `E''`, `U&''`, `N''`, bit and hex strings, and whole `INTERVAL '...'` values. `$1`-style placeholders are kept.
2. The SQL is rendered without comments, so text hidden in `/* ... */` or `-- ...` is dropped.
3. The rendered SQL is tokenized again. If any literal token is still present, nothing is stored except the hash (`sql: null`, `sql_unparsed: true`).

Never written: parameter values, tool arguments, result rows, Postgres error messages (they can quote row values), and bearer tokens.

## Limits

- `sql_sha256` is a plain hash. Someone holding the log can confirm a guess of the exact SQL text, including its literals, by hashing the guess. Treat the file as sensitive and keep the `0600` mode.
- Identifiers are kept (table and column names, aliases). An alias written as `SELECT 1 AS "secret"` is stored.
- The file is not rotated or signed; ship it to your log system if you need retention or tamper evidence.
