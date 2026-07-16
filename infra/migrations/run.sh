#!/bin/sh
# =============================================================================
# run.sh — in-container migration runner for the Cloud Run migration Job.
# =============================================================================
# Runs INSIDE the VPC (Cloud Run Job with --network=default --vpc-egress), so it
# reaches the private-IP Cloud SQL instance the off-VPC deploy host cannot.
#
# Connection: the Cloud SQL Auth Proxy socket mounted at /cloudsql/<conn> by the
# job's --set-cloudsql-instances flag. psql connects over that Unix socket, which
# works identically for public- and private-IP instances (the connector routes).
#
# Env (set by scripts/gcp/run-migrations-job.sh via --set-secrets/--set-env-vars):
#   PGPASSWORD          DB password (from Secret Manager secret token-opt-db-password)
#   DB_CONNECTION_NAME  Cloud SQL connection name  <project>:<region>:token-opt-pg
#   RUN_PGVECTOR        "true" to also apply pgvector.sql (enable_qdrant=false)
#
# Order mirrors the Terraform depends_on graph:
#   billing → tenant_configs, audit_events → rls   (pgvector independent, optional)
# Fails LOUDLY (set -e + ON_ERROR_STOP=1): any migration error aborts the job.
set -eu

SOCK_DIR="/cloudsql/${DB_CONNECTION_NAME}"
PGHOST="$SOCK_DIR"
PGUSER="token_opt_app"
PGDATABASE="token_opt"
export PGHOST PGUSER PGDATABASE

# Wait for the Cloud SQL Auth Proxy socket to appear (the connector creates it
# asynchronously at container start; max ~30s).
i=0
while [ ! -S "${SOCK_DIR}/.s.PGSQL.5432" ]; do
  i=$((i + 1))
  if [ "$i" -gt 60 ]; then
    echo "ERROR: Cloud SQL socket ${SOCK_DIR}/.s.PGSQL.5432 never appeared" >&2
    ls -la "$SOCK_DIR" 2>/dev/null || true
    exit 1
  fi
  sleep 0.5
done

run_sql() {
  file="$1"
  echo ">>> Applying ${file}"
  psql -v ON_ERROR_STOP=1 -f "/migrations/${file}"
  echo "<<< OK ${file}"
}

# Dependency order: billing first, then tenant_configs + audit_events, then rls last.
run_sql billing.sql
run_sql tenant_configs.sql
run_sql audit_events.sql
run_sql rls_policies.sql

if [ "${RUN_PGVECTOR:-false}" = "true" ]; then
  run_sql pgvector.sql
else
  echo "--- pgvector.sql skipped (RUN_PGVECTOR != true; Qdrant backend)"
fi

echo "ALL MIGRATIONS APPLIED SUCCESSFULLY"
