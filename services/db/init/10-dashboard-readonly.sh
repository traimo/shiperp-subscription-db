#!/bin/bash
# Read-only login for the shiperp-dashboard container: SELECT on every table, now and later (the app's
# import drops and recreates the tables, so default privileges are what keeps the grant alive).
set -euo pipefail

if [ -z "${DASHBOARD_DB_PASSWORD:-}" ]; then
  echo "DASHBOARD_DB_PASSWORD not set - skipping the dashboard_ro role" >&2
  exit 0
fi

psql -v ON_ERROR_STOP=1 -v pw="$DASHBOARD_DB_PASSWORD" \
     --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
CREATE ROLE dashboard_ro LOGIN PASSWORD :'pw';
GRANT CONNECT ON DATABASE :"DBNAME" TO dashboard_ro;
GRANT USAGE ON SCHEMA public TO dashboard_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO dashboard_ro;
ALTER DEFAULT PRIVILEGES FOR ROLE CURRENT_USER IN SCHEMA public GRANT SELECT ON TABLES TO dashboard_ro;
SQL
