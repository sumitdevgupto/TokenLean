#!/usr/bin/env bash
# =============================================================================
# post-deploy-check-local.sh — Post-deployment health check for the local stack
# =============================================================================
# Local counterpart of scripts/gcp/post-deploy-check.sh. Backend- and
# overlay-aware so it does NOT false-fail on a healthy stack:
#   • G07 vector backend is auto-detected from config/config.yaml — the Qdrant
#     tenant-collection check is skipped under the pgvector backend (where no
#     Qdrant collections exist by design).
#   • Container checks use `docker ps` by name, so they work from any CWD and
#     regardless of which compose files/overlays are in play.
#   • The commercial overlay (portal) is auto-detected and validated when present.
#
# Usage:
#   ./scripts/local/post-deploy-check-local.sh [--vector qdrant|pgvector]
#
# What this validates:
#   1. Required containers are running (proxy, redis, postgres, tika; qdrant when
#      the backend is qdrant; portal when the commercial overlay is up)
#   2. Proxy responds to /health
#   3. Prometheus metrics endpoint is accessible
#   4. Qdrant has tenant collections (rag_<tenant>, hyphens preserved) — qdrant backend only
#   5. Grafana accessibility (optional)
#   6. Commercial portal reachable (only when the portal container is up)
#
# Exit codes:
#   0 = All validations passed
#   1 = One or more validations failed
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
cd "${REPO_ROOT}"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()    { echo -e "${BLUE}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[PASS]${NC}   $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
fail()    { echo -e "${RED}[FAIL]${NC}   $*"; }

ERRORS=0
VECTOR_OVERRIDE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --vector) VECTOR_OVERRIDE="$2"; shift 2 ;;
    --help)   sed -n '/^# Usage:/,/^# ===/p' "$0" | head -20; exit 0 ;;
    *) shift ;;
  esac
done

echo ""
echo -e "${BLUE}╔════════════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║  POST-DEPLOYMENT VALIDATION (local)                            ║${NC}"
echo -e "${BLUE}╚════════════════════════════════════════════════════════════════╝${NC}"
echo ""

# ─── Detect G07 vector backend + commercial overlay ───────────────────────────
detect_backend() {
  [[ -n "$VECTOR_OVERRIDE" ]] && { echo "$VECTOR_OVERRIDE"; return; }
  if command -v python3 &>/dev/null && [[ -f "${REPO_ROOT}/config/config.yaml" ]]; then
    python3 - "${REPO_ROOT}/config/config.yaml" <<'PY' 2>/dev/null || echo "qdrant"
import sys, yaml
try:
    cfg = yaml.safe_load(open(sys.argv[1])) or {}
    groups = cfg.get("groups", {}) or {}
    g = groups.get("G7_retrieval") or groups.get("g7_retrieval") or {}
    print("pgvector" if g.get("use_pgvector_fallback") else "qdrant")
except Exception:
    print("qdrant")
PY
  else
    echo "qdrant"
  fi
}

BACKEND="$(detect_backend)"
COMMERCIAL=false
if docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^token-opt-portal$'; then
  COMMERCIAL=true
fi
info "G07 vector backend: ${BACKEND}   commercial overlay: ${COMMERCIAL}"
echo ""

# container_up <short-name> → 0 if token-opt-<name> is Up
container_up() {
  docker ps --format '{{.Names}} {{.Status}}' 2>/dev/null \
    | grep -E "^token-opt-$1 " | grep -qiE "Up|healthy"
}

# ─── Test 1: Container Health ─────────────────────────────────────────────────
info "Test 1: Checking container health..."

# name:required — qdrant required only under the qdrant backend; portal only when commercial
CHECKS=("proxy:true" "redis:true" "postgres:true" "tika:true")
[[ "$BACKEND" == "qdrant" ]] && CHECKS+=("qdrant:true") || CHECKS+=("qdrant:false")
[[ "$COMMERCIAL" == true ]]  && CHECKS+=("portal:true")

for entry in "${CHECKS[@]}"; do
  name="${entry%%:*}"; required="${entry##*:}"
  if container_up "$name"; then
    success "Container 'token-opt-${name}' is running"
  elif [[ "$required" == true ]]; then
    fail "Container 'token-opt-${name}' is not running"
    ERRORS=$((ERRORS + 1))
  else
    warn "Container 'token-opt-${name}' not running (optional for backend=${BACKEND})"
  fi
done
echo ""

# ─── Test 2: Proxy Health Endpoint ────────────────────────────────────────────
# Retried: right after a deploy the container is "Up" before uvicorn finishes booting
# (app import + bge-small embedding warm ≈ 8-9s), so a single no-retry curl false-fails
# on a healthy stack. Poll up to ~30s so warm-up never reports a spurious error.
info "Test 2: Checking proxy health endpoint..."
HEALTH_RESPONSE=""
for _ in $(seq 1 10); do
  HEALTH_RESPONSE=$(curl -s -m 5 http://localhost:4000/health 2>/dev/null || echo "")
  [[ "$HEALTH_RESPONSE" == *'"status":"ok"'* ]] && break
  sleep 3
done
if [[ "$HEALTH_RESPONSE" == *'"status":"ok"'* ]]; then
  success "Proxy health endpoint responding"
  echo "       Response: $HEALTH_RESPONSE"
else
  fail "Proxy health check failed"
  ERRORS=$((ERRORS + 1))
fi
echo ""

# ─── Test 3: Metrics Endpoint ─────────────────────────────────────────────────
info "Test 3: Checking Prometheus metrics endpoint..."
METRICS_RESPONSE=""
for _ in $(seq 1 10); do
  METRICS_RESPONSE=$(curl -s -m 5 http://localhost:4000/metrics 2>/dev/null | head -5 || echo "")
  [[ -n "$METRICS_RESPONSE" && "$METRICS_RESPONSE" == *"# HELP"* ]] && break
  sleep 3
done
if [[ -n "$METRICS_RESPONSE" && "$METRICS_RESPONSE" == *"# HELP"* ]]; then
  success "Metrics endpoint accessible"
  COUNT=$(curl -s -m 5 http://localhost:4000/metrics 2>/dev/null | grep -c "^# HELP" || echo "0")
  echo "       Found $COUNT metric definitions"
else
  fail "Metrics endpoint not responding correctly"
  ERRORS=$((ERRORS + 1))
fi
echo ""

# ─── Test 4: Qdrant Tenant Collections (qdrant backend only) ──────────────────
info "Test 4: Checking Qdrant tenant collections..."
if [[ "$BACKEND" != "qdrant" ]]; then
  warn "G07 backend is '${BACKEND}' — Qdrant tenant collections are not part of this deploy; skipping."
else
  TENANTS=("NOVA-STG-01" "SHOP-STG-01" "BUIL-STG-01")
  MISSING_COLLECTIONS=0
  for tenant in "${TENANTS[@]}"; do
    # Collection name = rag_<tenant_id> with hyphens preserved (see seed_qdrant_tenants.py).
    COLLECTION="rag_${tenant}"
    RESPONSE=$(curl -s "http://localhost:6333/collections/${COLLECTION}" 2>/dev/null || echo "")
    if [[ "$RESPONSE" == *'"status":"ok"'* ]]; then
      success "Collection '${COLLECTION}' exists"
    else
      fail "Collection '${COLLECTION}' not found"
      MISSING_COLLECTIONS=$((MISSING_COLLECTIONS + 1))
    fi
  done
  [[ $MISSING_COLLECTIONS -gt 0 ]] && ERRORS=$((ERRORS + 1))
fi
echo ""

# ─── Test 5: Grafana Accessibility (optional) ─────────────────────────────────
info "Test 5: Checking Grafana dashboard..."
GRAFANA_RESPONSE=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:3000/api/health 2>/dev/null || echo "000")
if [[ "$GRAFANA_RESPONSE" == "200" ]]; then
  success "Grafana accessible at http://localhost:3000"
else
  warn "Grafana health check returned HTTP $GRAFANA_RESPONSE (not started, or observability profile off)"
fi
echo ""

# ─── Test 6: Commercial Portal (only when overlay is up) ──────────────────────
if [[ "$COMMERCIAL" == true ]]; then
  info "Test 6: Checking commercial portal..."
  PORTAL_STATUS=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:8090/portal/ 2>/dev/null || echo "000")
  if [[ "$PORTAL_STATUS" =~ ^(200|301|302|307|308)$ ]]; then
    success "Portal reachable at http://localhost:8090/portal/ (HTTP $PORTAL_STATUS)"
  else
    warn "Portal returned HTTP $PORTAL_STATUS (may still be starting)"
  fi
  echo ""
fi

# ─── Summary ──────────────────────────────────────────────────────────────────
echo -e "${BLUE}╔════════════════════════════════════════════════════════════════╗${NC}"
if [[ $ERRORS -eq 0 ]]; then
  echo -e "${GREEN}║  ALL VALIDATIONS PASSED                                        ║${NC}"
  echo -e "${GREEN}╠════════════════════════════════════════════════════════════════╣${NC}"
  echo -e "${GREEN}║${NC}  ✅ Required containers healthy                                ${GREEN}║${NC}"
  echo -e "${GREEN}║${NC}  ✅ Proxy responding on :4000                                  ${GREEN}║${NC}"
  echo -e "${GREEN}║${NC}  ✅ Metrics endpoint accessible                                ${GREEN}║${NC}"
  echo -e "${GREEN}║${NC}  ✅ Vector backend (${BACKEND}) checks satisfied                    ${GREEN}║${NC}"
  echo -e "${GREEN}╚════════════════════════════════════════════════════════════════╝${NC}"
  echo ""
  echo "Next step:"
  echo "  python pitch-test-plan/src/run_roi_pitch.py --live --dataset DS1 --tenant-id NOVA-STG-01 --sample-size 3"
  exit 0
else
  echo -e "${RED}║  VALIDATION FAILED — $ERRORS error(s) found                      ║${NC}"
  echo -e "${RED}╠════════════════════════════════════════════════════════════════╣${NC}"
  echo -e "${RED}║${NC}  Troubleshooting:                                              ${RED}║${NC}"
  echo -e "${RED}║${NC}    docker ps                        # Check container status   ${RED}║${NC}"
  echo -e "${RED}║${NC}    docker logs -f token-opt-proxy   # View proxy logs          ${RED}║${NC}"
  echo -e "${RED}║${NC}    docker logs -f token-opt-qdrant  # View Qdrant logs         ${RED}║${NC}"
  echo -e "${RED}║${NC}                                                                ${RED}║${NC}"
  echo -e "${RED}╚════════════════════════════════════════════════════════════════╝${NC}"
  exit 1
fi
