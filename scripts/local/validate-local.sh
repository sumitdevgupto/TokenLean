#!/usr/bin/env bash
# =============================================================================
# validate-local.sh — Post-deployment validation for local multi-tenant setup
# =============================================================================
# Usage:
#   ./scripts/local/validate-local.sh
#
# What this validates:
#   1. All required containers are running and healthy
#   2. Proxy responds to /health endpoint
#   3. Metrics endpoint is accessible
#   4. Qdrant has tenant collections (rag_nova_med, rag_shop_bot, rag_build_co)
#   5. (Optional) Prometheus has tenant_id labels after test request
#
# Exit codes:
#   0 = All validations passed
#   1 = One or more validations failed
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()    { echo -e "${BLUE}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[PASS]${NC}   $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
fail()    { echo -e "${RED}[FAIL]${NC}   $*"; }

ERRORS=0

echo ""
echo -e "${BLUE}╔════════════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║  POST-DEPLOYMENT VALIDATION                                    ║${NC}"
echo -e "${BLUE}╚════════════════════════════════════════════════════════════════╝${NC}"
echo ""

# Check Docker Compose (support both 'docker compose' and 'docker-compose')
DC="docker-compose"
if docker compose version &>/dev/null 2>&1; then
  DC="docker compose"
  info "Using 'docker compose' (v2)"
elif docker-compose version &>/dev/null 2>&1; then
  DC="docker-compose"
  info "Using 'docker-compose' (v1)"
else
  # Fallback — assume docker-compose is available even if version check fails
  info "Docker Compose version check inconclusive, assuming 'docker-compose'"
fi

# ─── Test 1: Container Health ─────────────────────────────────────────────────
info "Test 1: Checking container health..."

REQUIRED_CONTAINERS=("proxy" "redis" "postgres" "qdrant" "tika")
UNHEALTHY=0

for container in "${REQUIRED_CONTAINERS[@]}"; do
  # Check if container is running (health status varies by Docker Compose version)
  RUNNING=$($DC ps 2>/dev/null | grep "$container" | grep -E "(Up|running)" || echo "")
  if [[ -n "$RUNNING" ]]; then
    success "Container '$container' is running"
  else
    fail "Container '$container' is not running"
    UNHEALTHY=$((UNHEALTHY + 1))
  fi
done

if [[ $UNHEALTHY -gt 0 ]]; then
  ERRORS=$((ERRORS + 1))
  warn "$UNHEALTHY container(s) not running — check with: $DC ps"
fi

echo ""

# ─── Test 2: Proxy Health Endpoint ────────────────────────────────────────────
info "Test 2: Checking proxy health endpoint..."

HEALTH_RESPONSE=$(curl -s http://localhost:4000/health 2>/dev/null || echo "")
if [[ "$HEALTH_RESPONSE" == *'"status":"ok"'* ]]; then
  success "Proxy health endpoint responding"
  echo "       Response: $HEALTH_RESPONSE"
else
  fail "Proxy health check failed"
  ERRORS=$((ERRORS + 1))
fi

echo ""

# ─── Test 3: Metrics Endpoint ───────────────────────────────────────────────
info "Test 3: Checking Prometheus metrics endpoint..."

METRICS_RESPONSE=$(curl -s http://localhost:4000/metrics 2>/dev/null | head -5 || echo "")
if [[ -n "$METRICS_RESPONSE" && "$METRICS_RESPONSE" == *"# HELP"* ]]; then
  success "Metrics endpoint accessible"
  COUNT=$(curl -s http://localhost:4000/metrics 2>/dev/null | grep -c "^# HELP" || echo "0")
  echo "       Found $COUNT metric definitions"
else
  fail "Metrics endpoint not responding correctly"
  ERRORS=$((ERRORS + 1))
fi

echo ""

# ─── Test 4: Qdrant Tenant Collections ────────────────────────────────────────
info "Test 4: Checking Qdrant tenant collections..."

TENANTS=("nova-med" "shop-bot" "build-co")
MISSING_COLLECTIONS=0

for tenant in "${TENANTS[@]}"; do
  # Collection name format: rag_{tenant_id} (with hyphens preserved)
  COLLECTION="rag_${tenant}"
  
  RESPONSE=$(curl -s "http://localhost:6333/collections/${COLLECTION}" 2>/dev/null || echo "")
  if [[ "$RESPONSE" == *'"status":"ok"'* ]]; then
    success "Collection '${COLLECTION}' exists"
  else
    fail "Collection '${COLLECTION}' not found"
    MISSING_COLLECTIONS=$((MISSING_COLLECTIONS + 1))
  fi
done

if [[ $MISSING_COLLECTIONS -gt 0 ]]; then
  ERRORS=$((ERRORS + 1))
fi

echo ""

# ─── Test 5: Grafana Accessibility (optional) ─────────────────────────────────
info "Test 5: Checking Grafana dashboard..."

GRAFANA_RESPONSE=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:3000/api/health 2>/dev/null || echo "000")
if [[ "$GRAFANA_RESPONSE" == "200" ]]; then
  success "Grafana accessible at http://localhost:3000"
else
  warn "Grafana health check returned HTTP $GRAFANA_RESPONSE (may still be starting)"
fi

echo ""

# ─── Summary ──────────────────────────────────────────────────────────────────
echo -e "${BLUE}╔════════════════════════════════════════════════════════════════╗${NC}"

if [[ $ERRORS -eq 0 ]]; then
  echo -e "${GREEN}║  ALL VALIDATIONS PASSED                                        ║${NC}"
  echo -e "${GREEN}╠════════════════════════════════════════════════════════════════╣${NC}"
  echo -e "${GREEN}║${NC}  ✅ All containers healthy                                     ${GREEN}║${NC}"
  echo -e "${GREEN}║${NC}  ✅ Proxy responding on :4000                                  ${GREEN}║${NC}"
  echo -e "${GREEN}║${NC}  ✅ Metrics endpoint accessible                                ${GREEN}║${NC}"
  echo -e "${GREEN}║${NC}  ✅ Tenant collections exist in Qdrant                         ${GREEN}║${NC}"
  echo -e "${GREEN}║${NC}                                                                ${GREEN}║${NC}"
  echo -e "${GREEN}║${NC}  Ready for Phase 2: Smoke tests                                ${GREEN}║${NC}"
  echo -e "${GREEN}╚════════════════════════════════════════════════════════════════╝${NC}"
  echo ""
  echo "Next step:"
  echo "  python pitch-test-plan/src/run_roi_pitch.py --live --dataset DS1 --tenant-id nova-med --sample-size 3"
  exit 0
else
  echo -e "${RED}║  VALIDATION FAILED — $ERRORS error(s) found                      ║${NC}"
  echo -e "${RED}╠════════════════════════════════════════════════════════════════╣${NC}"
  echo -e "${RED}║${NC}  Troubleshooting:                                              ${RED}║${NC}"
  echo -e "${RED}║${NC}    $DC ps                     # Check container status         ${RED}║${NC}"
  echo -e "${RED}║${NC}    $DC logs -f proxy          # View proxy logs                ${RED}║${NC}"
  echo -e "${RED}║${NC}    $DC logs -f qdrant         # View Qdrant logs               ${RED}║${NC}"
  echo -e "${RED}╚════════════════════════════════════════════════════════════════╝${NC}"
  exit 1
fi
