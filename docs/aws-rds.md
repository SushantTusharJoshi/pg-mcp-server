# Optional: running against Amazon RDS for PostgreSQL

> Status: unverified. Nothing in this repository has been run against Amazon RDS. The steps below follow the standard Postgres and RDS behaviour the server relies on, and should be treated as a plan until someone runs them and records the result here.

The server only needs a Postgres endpoint, a role, and TLS settings. It contains no AWS code and creates no AWS resources.

## Connection settings

The server reads `PG_HOST`, `PG_PORT`, `PG_DATABASE`, `PG_USER`, `PG_PASSWORD`, and falls back to the standard libpq names (`PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`, `PGPASSWORD`) when the `PG_` ones are unset.

TLS is set with `PG_SSLMODE` (or `PGSSLMODE`), which is passed to asyncpg as its `ssl` mode: `disable`, `allow`, `prefer`, `require`, `verify-ca` or `verify-full`. For RDS use `verify-full` and point `PGSSLROOTCERT` at the RDS certificate bundle for your region, downloaded from the AWS documentation.

```bash
export PGHOST=mydb.xxxxxxxx.us-east-1.rds.amazonaws.com
export PGPORT=5432
export PGDATABASE=bookstore
export PGUSER=mcp_readonly
export PGPASSWORD=...            # from your secret store, not a file in the repo
export PG_SSLMODE=verify-full
export PGSSLROOTCERT=/path/to/global-bundle.pem
python -m pg_mcp.server
```

## Database setup

RDS gives you a master user, not a superuser, so check each statement in `seed/init.sql` before running it:

- `CREATE ROLE`, `GRANT SELECT ON TABLE ...`, `REVOKE TEMPORARY ON DATABASE ...` should work as the master user.
- `REVOKE EXECUTE ON FUNCTION pg_catalog.pg_terminate_backend ... FROM PUBLIC` and the other `pg_catalog` revokes may be refused, because the master user does not own those functions. If they are refused, the integration tests in `tests/integration/test_privileges.py` will show which rights `mcp_readonly` still has; record the result here.
- `rds_superuser` membership must not be granted to `mcp_readonly`. `test_no_inherited_roles` checks that the role has no memberships at all.

## Network

- Keep the instance private (no public accessibility) and reach it from inside the VPC.
- Allow the server's security group to reach port 5432, nothing wider.
- If you run the HTTP mode, keep it on `127.0.0.1` or behind a TLS-terminating proxy inside the VPC.

## To verify

1. Run `seed/init.sql` as the master user and note any statement that fails.
2. Run the integration suite with `PG_MCP_INTEGRATION=1`, `PG_*` pointing at RDS, and `PG_ADMIN_DSN` as the master user.
3. Confirm `PG_SSLMODE=verify-full` connects and that a wrong `PGSSLROOTCERT` fails.
4. Update the status line at the top of this file with the date and results.
