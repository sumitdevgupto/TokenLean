#!/usr/bin/env bash
# =============================================================================
# showcase-traffic.sh — generate all-on "showcase" traffic for dashboard capture
# =============================================================================
# Unlike the ablation harness (which deliberately runs mostly-disabled configs
# and cold caches, producing noisy dashboards), this sends realistic traffic
# with EVERY optimisation ON and warms the cache, so the Grafana Live Calls +
# aggregate dashboards show strong, honest numbers worth screenshotting.
#
# It:
#   1. Runs the post-deploy health check as a preflight gate (abort if unhealthy)
#   2. Resolves the tenant proxy key from .env
#   3. Sends N distinct prompts, each REPEATED (first = miss/real call, repeats =
#      cache hits) so the Cache Hit Rate tile lights up cheaply
#   4. Verifies data actually landed: Postgres usage_events, Prometheus counters,
#      and Langfuse traces (so you know the pitch-comparison dashboard will work)
#   5. Prints dashboard URLs + the capture time-window
#
# Usage:
#   ./scripts/local/showcase-traffic.sh [--tenant nova-med] [--distinct 6]
#                                       [--repeats 5] [--model gpt-4o-mini]
#                                       [--clean-showcase] [--skip-check]
#
#   --clean-showcase  Wipe ALL dashboard data sources first (app Postgres
#                     usage_events/cache_l2, the SEPARATE Langfuse database's
#                     traces/observations/scores, Redis, and the Prometheus
#                     TSDB) so every dashboard — including pitch-comparison,
#                     which reads Langfuse — shows ONLY this run's fresh data.
#
# Cost note: only the FIRST send of each distinct prompt hits the LLM; repeats
# are served from cache. Default 6×5 = 30 requests but only ~6 paid calls.
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
cd "${REPO_ROOT}"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()    { echo -e "${BLUE}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# Wipe EVERY dashboard data source for a fresh slate. Yesterday's manual cleanup
# missed Langfuse (its own `langfuse` database, separate from the app `token_opt`
# DB) — the pitch-comparison dashboard reads Langfuse `traces`, so old trace
# history kept showing. This clears all four sources the dashboards read from.
_clean_showcase_data() {
  warn "clean-showcase: wiping ALL dashboard data sources for a fresh slate..."

  # 1. App Postgres (per-call / aggregate dashboards)
  if docker exec token-opt-postgres psql -U token_opt -d token_opt \
       -c "TRUNCATE TABLE usage_events, cache_l2 RESTART IDENTITY;" >/dev/null 2>&1; then
    echo "  ✓ token_opt.usage_events + cache_l2 truncated"
  else
    warn "  could not truncate app tables (is token-opt-postgres up?)"
  fi

  # 2. Langfuse Postgres — SEPARATE 'langfuse' DB (pitch-comparison dashboard).
  #    CASCADE clears observations/scores that reference a trace.
  for t in traces observations scores; do
    if docker exec token-opt-postgres psql -U token_opt -d langfuse \
         -c "TRUNCATE TABLE ${t} RESTART IDENTITY CASCADE;" >/dev/null 2>&1; then
      echo "  ✓ langfuse.${t} truncated"
    else
      echo "  · langfuse.${t} skipped (table absent)"
    fi
  done

  # 3. Redis (L1 cache + counters — clean Cache Hit Rate)
  if docker exec token-opt-redis redis-cli FLUSHALL >/dev/null 2>&1; then
    echo "  ✓ redis flushed"
  else
    warn "  could not flush redis"
  fi

  # 4. Prometheus (Live Calls dashboard). Tiles read cumulative proxy counters, so
  #    a true zero needs BOTH: reset the proxy's in-process counters (recreate the
  #    container) AND wipe the TSDB history.
  # 4a. Recreate the proxy so its counters restart at 0. Include the commercial
  #     overlay when present so it does NOT revert to the core main:app command.
  local base="${REPO_ROOT}/docker-compose.yml" overlay="${REPO_ROOT}/docker-compose.commercial.yml"
  local files=(-f "$base")
  if [[ -f "$overlay" ]] && docker ps --format '{{.Names}}' | grep -q '^token-opt-portal$'; then
    files+=(-f "$overlay")
  fi
  if docker compose version >/dev/null 2>&1; then
    if COMPOSE_PROFILES="${COMPOSE_PROFILES:-portal}" docker compose "${files[@]}" up -d --force-recreate proxy >/dev/null 2>&1; then
      echo "  ✓ proxy recreated (in-process counters reset to 0)"
    else
      warn "  could not recreate proxy — counters may retain old totals"
    fi
  fi
  # 4b. Wipe the Prometheus TSDB volume.
  local prom_vol
  prom_vol=$(docker inspect token-opt-prometheus \
    --format '{{range .Mounts}}{{if eq .Destination "/prometheus"}}{{.Name}}{{end}}{{end}}' 2>/dev/null || echo "")
  if [[ -n "$prom_vol" ]]; then
    docker stop token-opt-prometheus >/dev/null 2>&1 || true
    docker run --rm -v "${prom_vol}":/data alpine sh -c 'rm -rf /data/* /data/.??* 2>/dev/null || true' >/dev/null 2>&1 || true
    docker start token-opt-prometheus >/dev/null 2>&1 || true
    echo "  ✓ prometheus TSDB wiped"
  else
    warn "  prometheus volume not found — skipped TSDB wipe (set dashboard time-range to a post-clean window instead)"
  fi
  # 4c. Wait for the proxy to be healthy again before returning (traffic follows).
  local waited=0
  while [[ $waited -lt 60 ]]; do
    [[ "$(curl -s http://localhost:4000/health 2>/dev/null)" == *'"status":"ok"'* ]] && break
    sleep 3; waited=$((waited + 3))
  done

  success "clean-showcase: all data sources wiped — dashboards will show only this run."
  echo ""
}

TENANT="nova-med"
DISTINCT=6
REPEATS=5
MODEL="gpt-4o-mini"
RUN_CHECK=true
CLEAN=false
PROXY_URL="http://localhost:4000"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --tenant)         TENANT="$2"; shift 2 ;;
    --distinct)       DISTINCT="$2"; shift 2 ;;
    --repeats)        REPEATS="$2"; shift 2 ;;
    --model)          MODEL="$2"; shift 2 ;;
    --clean-showcase) CLEAN=true; shift ;;
    --skip-check)     RUN_CHECK=false; shift ;;
    --help)           sed -n '/^# Usage:/,/^# ===/p' "$0" | head -24; exit 0 ;;
    *) error "Unknown option: $1" ;;
  esac
done

PY=python3; command -v python3 &>/dev/null || PY=python

echo ""
echo -e "${BLUE}╔════════════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║  SHOWCASE TRAFFIC (all-on) — for dashboard capture             ║${NC}"
echo -e "${BLUE}╚════════════════════════════════════════════════════════════════╝${NC}"
echo ""

# ─── 1. Preflight health gate ─────────────────────────────────────────────────
if [[ "$RUN_CHECK" == true ]]; then
  info "Preflight: running post-deployment health check..."
  if ! bash "${SCRIPT_DIR}/post-deploy-check-local.sh" >/dev/null 2>&1; then
    bash "${SCRIPT_DIR}/post-deploy-check-local.sh" || true
    error "Stack is not healthy — fix the failures above before generating showcase traffic (or pass --skip-check to override)."
  fi
  success "Stack healthy — proceeding."
else
  warn "Skipping preflight health check (--skip-check)."
fi

# ─── 1b. Config awareness (dashboards only reflect ENABLED groups) ────────────
# The Grafana tiles can only show what the pipeline actually did. If a high-visibility
# optimisation is disabled in the live config, warn — e.g. G05 off ⇒ Cache Hit Rate
# tile stays flat no matter how many repeats we send.
if [[ -f "${REPO_ROOT}/config/config.yaml" ]]; then
  "$PY" - "${REPO_ROOT}/config/config.yaml" <<'PY' || true
import sys, yaml
groups = (yaml.safe_load(open(sys.argv[1])) or {}).get("groups", {}) or {}
def on(prefix):
    for k, v in groups.items():
        if k.lower().replace("_", "").startswith(prefix):
            return bool(isinstance(v, dict) and v.get("enabled"))
    return None
SHOWCASE = {"g5": "G05 cache (Cache Hit Rate tile)", "g1": "G01 compression",
            "g19": "G19 headroom pruning", "g21": "G21 cache alignment"}
off = [label for pre, label in SHOWCASE.items() if on(pre) is False]
if off:
    print("\033[1;33m[WARN]\033[0m  Disabled optimisations (won't appear in dashboards): " + "; ".join(off))
    if any("G05" in x for x in off):
        print("\033[1;33m[WARN]\033[0m  → Cache Hit Rate will stay ~0. Enable groups.G5_cache.enabled: true in config.yaml")
        print("         (wait ~60s for hot-reload) if you want the cache story in the screenshots.")
else:
    print("\033[0;32m[OK]\033[0m    All key showcase optimisations (G05/G01/G19/G21) are enabled.")
PY
  echo ""
fi

# ─── 2. Resolve tenant proxy key ──────────────────────────────────────────────
[[ -f "${REPO_ROOT}/.env" ]] && { set -a; source "${REPO_ROOT}/.env"; set +a; }
VARNAME="ROI_PROXY_API_KEY_$(echo "$TENANT" | tr '[:lower:]-' '[:upper:]_')"
KEY="${!VARNAME:-}"
[[ -n "$KEY" ]] || error "No proxy key for tenant '$TENANT' (expected \$${VARNAME} in .env). Run scripts/commercial/setup-tenants.sh first."
success "Resolved key for tenant '$TENANT' (${KEY:0:10}…)"

# ─── 2b. Optional full wipe for a fresh dashboard slate ───────────────────────
if [[ "$CLEAN" == true ]]; then
  _clean_showcase_data
fi

# ─── 3. Baselines (so we can prove data landed) ───────────────────────────────
pg_count() { docker exec token-opt-postgres psql -U token_opt -d "$1" -tAc "$2" 2>/dev/null | tr -d '[:space:]'; }
prom_val() { curl -s "http://localhost:9090/api/v1/query?query=$1" 2>/dev/null | "$PY" -c "import sys,json;r=json.load(sys.stdin)['data']['result'];print(r[0]['value'][1] if r else 0)" 2>/dev/null || echo 0; }

UE_BEFORE=$(pg_count token_opt "select count(*) from usage_events;"); UE_BEFORE=${UE_BEFORE:-0}
LF_BEFORE=$(pg_count langfuse "select count(*) from traces;"); LF_BEFORE=${LF_BEFORE:-0}
REQS_BEFORE=$(prom_val "sum(token_opt_requests_total)"); REQS_BEFORE=${REQS_BEFORE%.*}; REQS_BEFORE=${REQS_BEFORE:-0}
CACHE_BEFORE=$(prom_val "sum(token_opt_cache_hits_total)"); CACHE_BEFORE=${CACHE_BEFORE%.*}; CACHE_BEFORE=${CACHE_BEFORE:-0}
info "Baseline — usage_events: ${UE_BEFORE}, langfuse traces: ${LF_BEFORE}, prom requests: ${REQS_BEFORE}, cache hits: ${CACHE_BEFORE}"
echo ""

# ─── 4. Send showcase traffic ─────────────────────────────────────────────────
info "Sending ${DISTINCT} distinct prompts × ${REPEATS} repeats to ${PROXY_URL} (model=${MODEL}, tenant=${TENANT})..."
KEY="$KEY" TENANT="$TENANT" MODEL="$MODEL" PROXY_URL="$PROXY_URL" DISTINCT="$DISTINCT" REPEATS="$REPEATS" "$PY" - <<'PYEOF'
import os, json, time, urllib.request, urllib.error

KEY = os.environ["KEY"]; TENANT = os.environ["TENANT"]; MODEL = os.environ["MODEL"]
URL = os.environ["PROXY_URL"].rstrip("/") + "/v1/chat/completions"
DISTINCT = int(os.environ["DISTINCT"]); REPEATS = int(os.environ["REPEATS"])

# A mix of sizes/shapes: short FAQ, medium support, and a long context turn so
# compression (G01) + structured pruning (G19) have something to bite on.
LONG_CTX = (
    "You are reviewing a production incident postmortem. Context: at 02:14 UTC the "
    "checkout service p99 latency rose from 120ms to 4.8s. The on-call found the "
    "connection pool saturated (max 20) against the primary Postgres, which was "
    "running a long ANALYZE triggered by the nightly stats job overlapping a schema "
    "migration that added an index on orders(created_at). Retries amplified load; "
    "the circuit breaker did not trip because the health endpoint hit a cached read "
    "replica that stayed green. Mitigation was to raise the pool to 60, pause the "
    "migration, and shed traffic via the CDN. "
) * 3
PROMPTS = [
    "What is your return policy for opened items?",
    "How do I reset my account password if I lost access to my email?",
    "Summarize the difference between horizontal and vertical pod autoscaling.",
    "A customer was double-charged for one order. What are the exact refund steps?",
    "Explain what a database connection pool is in two sentences.",
    LONG_CTX + "\n\nQuestion: give a 3-bullet root-cause summary and one prevention item.",
    "Draft a one-line friendly apology for a delayed shipment.",
    "What does HTTP 429 mean and how should a client handle it?",
][:DISTINCT]

ok = miss = hit = err = 0
for i, prompt in enumerate(PROMPTS):
    for r in range(REPEATS):
        body = json.dumps({
            "model": MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "max_tokens": 160,
        }).encode()
        req = urllib.request.Request(URL, data=body, headers={
            "Authorization": f"Bearer {KEY}",
            "Content-Type": "application/json",
            "X-Tenant-ID": TENANT,
            "X-User-ID": "showcase",
        })
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode())
            ok += 1
            topt = data.get("_token_opt", {}) if isinstance(data, dict) else {}
            cached = bool(topt.get("cache_hit"))
            if r == 0 and not cached:
                miss += 1
            elif cached:
                hit += 1
            mark = "H" if cached else ("M" if r == 0 else ".")
        except urllib.error.HTTPError as e:
            err += 1; mark = f"E{e.code}"
        except Exception as e:
            err += 1; mark = "Ex"
        print(mark, end="", flush=True)
    print(f"  prompt {i+1}/{len(PROMPTS)}")
print(f"\nSent: ok={ok} miss(real-call)~{miss} cache-hit~{hit} errors={err}")
if err:
    raise SystemExit(1 if ok == 0 else 0)
PYEOF
TRAFFIC_RC=$?
echo ""
[[ $TRAFFIC_RC -eq 0 ]] || warn "Some requests errored (see marks above: E401=auth, Exxx=other)."

# ─── 5. Verify data landed ────────────────────────────────────────────────────
info "Waiting 10s for async Langfuse flush + metrics scrape..."
sleep 10

UE_AFTER=$(pg_count token_opt "select count(*) from usage_events;"); UE_AFTER=${UE_AFTER:-0}
LF_AFTER=$(pg_count langfuse "select count(*) from traces;"); LF_AFTER=${LF_AFTER:-0}
REQS_AFTER=$(prom_val "sum(token_opt_requests_total)"); REQS_AFTER=${REQS_AFTER%.*}; REQS_AFTER=${REQS_AFTER:-0}
CACHE_AFTER=$(prom_val "sum(token_opt_cache_hits_total)"); CACHE_AFTER=${CACHE_AFTER%.*}; CACHE_AFTER=${CACHE_AFTER:-0}
AGG=$(docker exec token-opt-postgres psql -U token_opt -d token_opt -tAc \
  "select round(100.0*sum(tokens_saved)::numeric/nullif(sum(baseline_tokens),0),1)||'% saved, cost_saved=$'||round(sum(cost_saved_usd),4) from usage_events;" 2>/dev/null | tr -d ' ')

echo ""
echo -e "${BLUE}── Verification (deltas are THIS run) ───────────────────────${NC}"
printf "  usage_events:    %s → %s  (+%s)\n" "$UE_BEFORE" "$UE_AFTER" "$((UE_AFTER - UE_BEFORE))"
printf "  langfuse traces: %s → %s  (+%s)\n" "$LF_BEFORE" "$LF_AFTER" "$((LF_AFTER - LF_BEFORE))"
printf "  prom requests:   %s → %s  (+%s)\n" "$REQS_BEFORE" "$REQS_AFTER" "$((REQS_AFTER - REQS_BEFORE))"
printf "  prom cache hits: %s → %s  (+%s this run)\n" "$CACHE_BEFORE" "$CACHE_AFTER" "$((CACHE_AFTER - CACHE_BEFORE))"
printf "  aggregate (all): %s\n" "${AGG:-n/a}"
echo ""
if [[ "$((CACHE_AFTER - CACHE_BEFORE))" -le 0 ]]; then
  warn "No cache hits registered this run — repeats didn't hit G05. Cache Hit Rate tile will stay low; check G05 enabled in config.yaml."
fi

if [[ "$((LF_AFTER - LF_BEFORE))" -gt 0 ]]; then
  success "Langfuse IS ingesting — the pitch-comparison dashboard will populate."
else
  warn "Langfuse traces did NOT increase — pitch-comparison dashboard won't update. Check: langfuse_enabled in config.yaml + proxy langfuse keys."
fi

# ─── 6. Capture guidance ──────────────────────────────────────────────────────
echo -e "${GREEN}╔════════════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║  SHOWCASE TRAFFIC COMPLETE — ready to capture                  ║${NC}"
echo -e "${GREEN}╚════════════════════════════════════════════════════════════════╝${NC}"
echo ""
echo "Grafana dashboards (set Tenant=${TENANT}, time range = Last 15 minutes):"
echo "  Live Calls:       http://localhost:3000/d/token-opt-live?var-Tenant=${TENANT}&from=now-15m&to=now"
echo "  Pitch Comparison: http://localhost:3000/d/token-opt-pitch?var-Tenant=${TENANT}"
echo "  Per-Call:         http://localhost:3000/d/token-opt-per-call"
echo ""
echo "Tip: re-run with more repeats to push the Cache Hit Rate higher:"
echo "  ./scripts/local/showcase-traffic.sh --distinct ${DISTINCT} --repeats 10"
