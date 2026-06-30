#!/usr/bin/env bash
# =============================================================================
# start-local.sh — Start local Docker stack (zero GCP cost)
# =============================================================================
# Usage:
#   ./scripts/local/start-local.sh [--seed]
#
# What this does:
#   1. Checks Docker is running
#   2. Starts all containers via docker-compose
#   3. Waits for health checks
#   4. Seeds Qdrant if empty (optional with --seed)
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
SEED=false

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()    { echo -e "${BLUE}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# ─── Load .env file if it exists ──────────────────────────────────────────────
ENV_FILE="${REPO_ROOT}/.env"
if [[ -f "$ENV_FILE" ]]; then
  info "Loading environment variables from ${ENV_FILE}..."
  set -a
  source "$ENV_FILE"
  set +a
  success "Loaded env file"
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --seed) SEED=true; shift ;;
    --help)
      sed -n '/^# Usage:/,/^# ===/p' "$0" | head -20
      exit 0 ;;
    *) error "Unknown option: $1" ;;
  esac
done

# ─── Check Docker ─────────────────────────────────────────────────────────────
info "Checking Docker..."
if ! docker info > /dev/null 2>&1; then
  error "Docker is not running. Please start Docker Desktop."
fi
success "Docker is running"

# ─── Detect docker-compose variant (V1 vs V2 plugin) ──────────────────────────
if docker compose version &>/dev/null; then
  DC="docker compose"
elif docker-compose version &>/dev/null; then
  DC="docker-compose"
else
  error "docker-compose not found. Please install Docker Compose."
fi

# ─── Start ──────────────────────────────────────────────────────────────────────
cd "${REPO_ROOT}"

info "Starting local Docker stack..."
$DC up -d

# ─── Wait for health ────────────────────────────────────────────────────────────
info "Waiting for services to be healthy..."
MAX_WAIT=120
ELAPSED=0

while [[ $ELAPSED -lt $MAX_WAIT ]]; do
  ALL_HEALTHY=true
  for service in redis postgres qdrant tika; do
    if ! $DC ps "$service" | grep -q "healthy" 2>/dev/null; then
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
  warn "Some services may not be fully healthy yet."
fi

# ─── Seed if requested ──────────────────────────────────────────────────────────
if [[ "$SEED" == true ]]; then
  "${SCRIPT_DIR}/../seed-data.sh" --qdrant-url http://localhost:6333
fi

# ─── Summary ──────────────────────────────────────────────────────────────────
echo ""
success "Local stack is running!"
echo "  Proxy:     http://localhost:4000"
echo "  LLMLingua: http://localhost:8080"
echo "  Qdrant:    http://localhost:6333"
echo ""
echo "Stop with: ./scripts/local/stop-local.sh"
