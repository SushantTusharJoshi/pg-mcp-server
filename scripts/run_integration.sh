#!/usr/bin/env bash
# Seed a fresh Postgres and run both integration suites.
#
# One source of truth for the integration steps: the `integration` job in
# ci.yml and the nightly matrix in security-drift.yml both call this script.
#
# Expects the job env from those workflows (PG_MCP_INTEGRATION=1, PG_HOST,
# PG_PORT, PG_DATABASE=bookstore, PG_USER=mcp_readonly, PG_PASSWORD,
# PG_ADMIN_DSN) and a superuser `postgres` with password PG_ADMIN_PASSWORD
# (default `postgres`, the throwaway CI container).
set -euo pipefail

cd "$(dirname "$0")/.."

host="${PG_HOST:-localhost}"
port="${PG_PORT:-5432}"
admin_password="${PG_ADMIN_PASSWORD:-postgres}"

admin_psql() {
  PGPASSWORD="$admin_password" psql -h "$host" -p "$port" -U postgres -v ON_ERROR_STOP=1 "$@"
}

echo "::group::Load seed/init.sql"
admin_psql -d bookstore -f seed/init.sql
echo "::endgroup::"

echo "::group::Load seed/sample_clinic.sql into a second database"
admin_psql -d bookstore -c "CREATE DATABASE clinic"
admin_psql -d clinic -f seed/sample_clinic.sql
echo "::endgroup::"

echo "::group::Bookstore suite (SQL corpus, privileges)"
python -m pytest tests/integration -v -m integration
echo "::endgroup::"

echo "::group::Generic mode on the clinic schema (PG-F6)"
PG_MCP_INTEGRATION=clinic \
PG_MCP_BOOKSTORE_TOOLS=0 \
PG_DATABASE=clinic \
PG_USER=clinic_readonly \
PG_PASSWORD=clinic_readonly \
PG_ADMIN_DSN="postgresql://postgres:${admin_password}@${host}:${port}/clinic" \
  python -m pytest tests/integration -v -m integration
echo "::endgroup::"
