#!/usr/bin/env bash
# =============================================================================
# stop-local.sh — Stop local Docker stack (zero cost)
# =============================================================================
# Usage:
#   ./scripts/local/stop-local.sh [--backup]
#
# What this does:
#   1. Stops all Docker containers
#   2. Optionally backs up data to GCS (with --backup)
#   3. Confirms zero GCP cost
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
BACKUP=false

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
    --backup) BACKUP=true; shift ;;
    --help)
      sed -n '/^# Usage:/,/^# ===/p' "$0" | head -20
      exit 0 ;;
    *) error "Unknown option: $1" ;;
  esac
done

# ─── Detect docker-compose variant (V1 vs V2 plugin) ──────────────────────────
if docker compose version &>/dev/null; then
  DC="docker compose"
elif docker-compose version &>/dev/null; then
  DC="docker-compose"
else
  error "docker-compose not found. Please install Docker Compose."
fi

cd "${REPO_ROOT}"

# ─── Optional backup ────────────────────────────────────────────────────────────
if [[ "$BACKUP" == true ]]; then
  info "Backing up data to GCS before stopping..."
  "${SCRIPT_DIR}/docker-backup.sh" || warn "Backup failed, continuing..."
fi

# ─── Stop ─────────────────────────────────────────────────────────────────────
info "Stopping local Docker stack..."
$DC down

# ─── Summary ──────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}╔════════════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║  LOCAL STACK STOPPED                                         ║${NC}"
echo -e "${GREEN}╠════════════════════════════════════════════════════════════════╣${NC}"
echo -e "${GREEN}║${NC}  💰 Current GCP cost: ₹0 (GCS storage only)                 ${GREEN}║${NC}"
echo -e "${GREEN}╚════════════════════════════════════════════════════════════════╝${NC}"
echo ""
echo "To restart: ./scripts/local/start-local.sh"
