#!/usr/bin/env bash
# =============================================================================
# deploy-local.sh — Deploy TokenLean — Token Optimisation Framework locally via Docker
# =============================================================================
# Usage:
#   ./scripts/local/deploy-local.sh [--seed] [--backup-to-gcs] [--with-grafana] [--recreate] [--tenants TENANT_LIST] [--no-check]
#
# Options:
#   --seed              Seed Qdrant with pitch_docs collection
#   --backup-to-gcs     Backup Redis to GCS
#   --with-grafana      Start observability stack (Prometheus + Grafana)
#   --recreate          Force rebuild --no-cache (use after code changes)
#   --tenants LIST      Comma-separated tenant IDs for multi-tenant seeding (e.g., NOVA-STG-01,SHOP-STG-01)
#   --no-check          Skip the post-deployment health check at the end
#
# What this does:
#   1. Checks Docker is running
#   2. Builds all images locally (no Cloud Build, zero GCP cost)
#   3. Starts infrastructure + application containers
#   4. Waits for health checks
#   5. Seeds Qdrant with pitch_docs (if --seed)
#   6. Optionally backs up to GCS
#
# All G1-G18 optimisations are available via Docker networking.
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
SEED=false
BACKUP_TO_GCS=false
WITH_GRAFANA=false
RECREATE=false
TENANTS=""
RUN_CHECK=true

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()    { echo -e "${BLUE}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# ─── Argument parsing ─────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --seed)        SEED=true; shift ;;
    --backup-to-gcs) BACKUP_TO_GCS=true; shift ;;
    --with-grafana) WITH_GRAFANA=true; shift ;;
    --recreate)    RECREATE=true; shift ;;
    --tenants)     TENANTS="$2"; shift 2 ;;
    --no-check)    RUN_CHECK=false; shift ;;
    --help)
      sed -n '/^# Usage:/,/^# ===/p' "$0" | head -20
      exit 0 ;;
    *) error "Unknown option: $1" ;;
  esac
done

# ─── Load .env file if it exists ──────────────────────────────────────────────
ENV_FILE="${REPO_ROOT}/.env"
if [[ -f "$ENV_FILE" ]]; then
  info "Loading environment variables from ${ENV_FILE}..."
  set -a
  source "$ENV_FILE"
  set +a
  success "Loaded env file"
fi

# ─── Validate required env vars ───────────────────────────────────────────────
if [[ -z "${OPENAI_API_KEY:-}" ]]; then
  warn "OPENAI_API_KEY is not set — the routellm sidecar (required by the proxy for G6 routing) needs it for embedding-based routing. Set it in ${ENV_FILE}."
fi

# ─── Check Docker ─────────────────────────────────────────────────────────────
info "Checking Docker..."
if ! docker info > /dev/null 2>&1; then
  error "Docker is not running. Please start Docker Desktop."
fi
if docker compose version &>/dev/null; then
  DC="docker compose"
elif docker-compose version &>/dev/null; then
  DC="docker-compose"
else
  error "docker-compose not found. Please install Docker Compose."
fi
success "Docker is running"

# ─── Check docker-compose.yml exists ──────────────────────────────────────────
if [[ ! -f "${REPO_ROOT}/docker-compose.yml" ]]; then
  error "docker-compose.yml not found at ${REPO_ROOT}/docker-compose.yml"
fi

# ─── Build and start ──────────────────────────────────────────────────────────
cd "${REPO_ROOT}"

# Set compose profiles via env var (more compatible than --profile flag)
if [[ "$WITH_GRAFANA" == true ]]; then
  export COMPOSE_PROFILES="observability"
  info "Observability profile enabled (Prometheus + Grafana)"
fi

info "Building all images locally (zero GCP cost)..."
if [[ "$RECREATE" == true ]]; then
  info "Forcing clean rebuild (--no-cache)..."
  $DC build --no-cache --parallel
else
  $DC build --parallel
fi

# ─── Generate local proxy API key (first run only) ──────────────────────────
LOCAL_KEYS_FILE="${REPO_ROOT}/config/local-keys.json"
LOCAL_PROXY_KEY=""
if [[ -f "$LOCAL_KEYS_FILE" ]]; then
  info "Local proxy API key already exists at ${LOCAL_KEYS_FILE} — skipping generation"
else
  info "Generating local proxy API key..."
  LOCAL_PROXY_KEY="tok-$(openssl rand -hex 24)"
  LOCAL_KEY_HASH=$(echo -n "$LOCAL_PROXY_KEY" | sha256sum | awk '{print $1}')
  printf '{"%s": "admin"}\n' "$LOCAL_KEY_HASH" > "$LOCAL_KEYS_FILE"
  success "Generated local proxy API key"
fi

info "Starting services..."
$DC up -d

# ─── Wait for health checks ─────────────────────────────────────────────────
info "Waiting for services to be healthy..."

MAX_WAIT=120
ELAPSED=0

while [[ $ELAPSED -lt $MAX_WAIT ]]; do
  ALL_HEALTHY=true

  for service in redis postgres qdrant tika proxy; do
    if ! $DC ps "$service" | grep -q "healthy"; then
      ALL_HEALTHY=false
      break
    fi
  done

  if [[ "$ALL_HEALTHY" == true ]]; then
    success "All core services are healthy"
    break
  fi

  sleep 5
  ELAPSED=$((ELAPSED + 5))
  echo -n "."
done

if [[ "$ALL_HEALTHY" != true ]]; then
  warn "Some services may not be fully healthy yet. Check with: $DC ps"
fi

# ─── Apply Row-Level Security policies (I2 defense-in-depth) ──────────────────
# Best-effort: the migration's IF EXISTS guards skip tables not yet created.
# Re-run deploy (or apply infra/migrations/rls_policies.sql) after first traffic
# if a table was created later.
RLS_SQL="${REPO_ROOT}/infra/migrations/rls_policies.sql"
if [[ -f "$RLS_SQL" ]]; then
  info "Applying RLS policies (I2)..."
  if docker exec -i token-opt-postgres psql -U token_opt -d token_opt < "$RLS_SQL" >/dev/null 2>&1; then
    success "RLS policies applied"
  else
    warn "RLS apply skipped/failed (tables may not exist yet) — re-run after first traffic"
  fi
fi

# ─── Seed Qdrant ──────────────────────────────────────────────────────────────
if [[ "$SEED" == true ]]; then
  info "Seeding Qdrant with pitch_docs..."
  "${SCRIPT_DIR}/../seed-data.sh" --qdrant-url http://localhost:6333 || warn "Seeding failed"
fi

# ─── Seed Tenant Collections ─────────────────────────────────────────────────
if [[ -n "$TENANTS" ]]; then
  info "Seeding Qdrant with tenant collections for: ${TENANTS}"
  if command -v python3 &> /dev/null; then
    python3 "${REPO_ROOT}/pitch-test-plan/src/seed_qdrant_tenants.py" --tenants "${TENANTS}" --recreate || warn "Tenant seeding failed — ensure Python dependencies installed (fastembed, qdrant-client, sentence-transformers)"
  else
    warn "python3 not found — skipping tenant seeding"
  fi
fi

# ─── Backup to GCS (optional) ─────────────────────────────────────────────────
if [[ "$BACKUP_TO_GCS" == true ]]; then
  info "Backing up to GCS..."
  "${SCRIPT_DIR}/docker-backup.sh" || warn "Backup failed"
fi

# ─── Post-deployment health check ─────────────────────────────────────────────
# Non-fatal by design: the deploy has completed, so we surface any problems
# loudly but don't exit non-zero (callers/orchestrators still get a 0 deploy).
# Skip with --no-check.
if [[ "$RUN_CHECK" == true ]]; then
  echo ""
  info "Running post-deployment health check..."
  if "${SCRIPT_DIR}/post-deploy-check-local.sh"; then
    success "Post-deployment health check passed"
  else
    warn "Post-deployment health check reported issues (see above) — deploy finished, but verify before sending traffic"
  fi
fi

# ─── Summary ──────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}╔════════════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║  LOCAL DEPLOYMENT COMPLETE                                     ║${NC}"
echo -e "${GREEN}╠════════════════════════════════════════════════════════════════╣${NC}"
echo -e "${GREEN}║${NC}  Proxy:     http://localhost:4000                             ${GREEN}║${NC}"
echo -e "${GREEN}║${NC}  LLMLingua: http://localhost:8080                             ${GREEN}║${NC}"
echo -e "${GREEN}║${NC}  Qdrant:    http://localhost:6333                             ${GREEN}║${NC}"
if [[ "$WITH_GRAFANA" == true ]]; then
  echo -e "${GREEN}║${NC}  Grafana:   http://localhost:3000                          ${GREEN}║${NC}"
fi
echo -e "${GREEN}║${NC}                                                               ${GREEN}║${NC}"
if [[ -n "$TENANTS" ]]; then
  echo -e "${GREEN}║${NC}  Tenants:   ${TENANTS}                                          ${GREEN}║${NC}"
fi
echo -e "${GREEN}║${NC}  💰 GCP Cost: ₹0                                              ${GREEN}║${NC}"
echo -e "${GREEN}╚════════════════════════════════════════════════════════════════╝${NC}"
echo ""
if [[ -n "$LOCAL_PROXY_KEY" ]]; then
  echo "Proxy API key (local dev, save this — shown only once):"
  echo "  ${LOCAL_PROXY_KEY}"
  echo ""
fi
echo "Commands:"
echo "  $DC ps                     # Check status"
echo "  $DC logs -f proxy            # View proxy logs"
echo "  ./scripts/local/stop-local.sh      # Stop (zero cost)"
