#!/usr/bin/env bash
#
# clear-cache.sh — delete ONLY the keys the benchmark inserted, nothing else.
#
# The benchmark runs under a dedicated tenant (default "bench", via the X-Tenant-ID
# header in run_benchmark.py), so every key it creates is namespaced under
# "t:<tenant>:" (cache layers tok_opt:l1/l2/step, rate-limit counters, etc.).
# Deleting that prefix removes exactly the benchmark's data and leaves every other
# tenant's data and all global state untouched.
#
# Usage: clear-cache.sh [tenant]   (default tenant: bench)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
cd "$REPO"

TENANT="${1:-bench}"

# Run KEYS+DEL inside the redis container (one exec). The benchmark keyspace is
# tiny so KEYS is fine. Tenant is passed as $1 to the inner sh (no interpolation).
docker compose exec -T redis sh -c '
prefix="t:$1:"
keys=$(redis-cli KEYS "${prefix}*")
n=$(printf "%s" "$keys" | grep -c .)
if [ "$n" -gt 0 ]; then printf "%s\n" "$keys" | xargs redis-cli del >/dev/null; fi
echo "[clear-cache] deleted $n key(s) under ${prefix}* (benchmark tenant only)"
' sh "$TENANT"

# The L2 semantic cache lives in Postgres (cache_l2), not Redis — purge this
# tenant's rows too, or a prior run's stored answers replay on the next run.
if docker compose exec -T postgres psql -U token_opt -d token_opt -c \
     "DELETE FROM cache_l2 WHERE tenant_id = '${TENANT}';" >/dev/null 2>&1; then
  echo "[clear-cache] cleared Postgres L2 rows for tenant '${TENANT}'"
else
  echo "[clear-cache] Postgres L2 clear skipped (table absent or psql unavailable)"
fi
