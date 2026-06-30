#!/usr/bin/env bash
# =============================================================================
# check-local-and-gcp-status.sh — Check status of both GCP and local deployments
# =============================================================================
# Usage:
#   ./scripts/check-local-and-gcp-status.sh [--project ID] [--region REGION]
#
# Shows:
#   - GCP resources (Cloud SQL, Redis, Cloud Run)
#   - Local Docker containers
#   - Cost implications
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PROJECT_ID=""
REGION="asia-south1"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()    { echo -e "${BLUE}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project) PROJECT_ID="$2"; shift 2 ;;
    --region)  REGION="$2"; shift 2 ;;
    --help)
      sed -n '/^# Usage:/,/^# ===/p' "$0" | head -20
      exit 0 ;;
    *) error "Unknown option: $1" ;;
  esac
done

if [[ -z "$PROJECT_ID" ]]; then
  PROJECT_ID=$(gcloud config get-value project 2>/dev/null || echo "")
fi

echo ""
echo -e "${BLUE}╔════════════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║  TOKEN OPTIMISATION FRAMEWORK — STATUS CHECK                 ║${NC}"
echo -e "${BLUE}╚════════════════════════════════════════════════════════════════╝${NC}"
echo ""

# ─── GCP Status ───────────────────────────────────────────────────────────────
echo -e "${BLUE}─── GCP Status ─────────────────────────────────────────────────${NC}"

if [[ -n "$PROJECT_ID" ]]; then
  echo "Project: ${PROJECT_ID}"
  
  # Cloud SQL
  SQL_STATE=$(gcloud sql instances describe token-opt-pg --format="value(state)" 2>/dev/null || echo "NOT_FOUND")
  if [[ "$SQL_STATE" == "RUNNABLE" ]]; then
    echo -e "  ${RED}❌ Cloud SQL: RUNNING (costing money)${NC}"
  elif [[ "$SQL_STATE" == "STOPPED" ]]; then
    echo -e "  ${YELLOW}⚠️  Cloud SQL: STOPPED (~$2/month storage)${NC}"
  else
    echo -e "  ${GREEN}✅ Cloud SQL: NOT FOUND (₹0)${NC}"
  fi

  # Redis
  REDIS_STATE=$(gcloud redis instances describe token-opt-redis --region="$REGION" --format="value(state)" 2>/dev/null || echo "NOT_FOUND")
  if [[ "$REDIS_STATE" == "READY" ]]; then
    echo -e "  ${RED}❌ Redis: RUNNING (costing money)${NC}"
  else
    echo -e "  ${GREEN}✅ Redis: NOT FOUND (₹0)${NC}"
  fi

  # Cloud Run
  RUNNING_SERVICES=$(gcloud run services list --region="$REGION" --format="value(SERVICE)" 2>/dev/null | wc -l | tr -d ' ' || echo "0")
  if [[ $RUNNING_SERVICES -gt 0 ]]; then
    echo -e "  ${YELLOW}ℹ️  Cloud Run: $RUNNING_SERVICES services (scale-to-zero = ₹0 when idle)${NC}"
  else
    echo -e "  ${GREEN}✅ Cloud Run: No services${NC}"
  fi
else
  echo "  No GCP project configured"
fi

# ─── Local Docker Status ──────────────────────────────────────────────────────
echo ""
echo -e "${BLUE}─── Local Docker Status ────────────────────────────────────────${NC}"

if docker info > /dev/null 2>&1; then
  RUNNING_CONTAINERS=$(docker ps --filter "name=token-opt" --format "{{.Names}}" 2>/dev/null | wc -l | tr -d ' ' || echo "0")
  if [[ "$RUNNING_CONTAINERS" -gt 0 ]]; then
    echo -e "  ${GREEN}✅ Containers running: $RUNNING_CONTAINERS${NC}"
    docker ps --filter "name=token-opt" --format "  • {{.Names}} ({{.Status}})"
    echo ""
    echo -e "  ${GREEN}Proxy:     http://localhost:4000${NC}"
    echo -e "  ${GREEN}LLMLingua: http://localhost:8080${NC}"
    echo -e "  ${GREEN}Qdrant:    http://localhost:6333${NC}"
  else
    echo -e "  ${YELLOW}⚠️  No local containers running${NC}"
  fi
else
  echo -e "  ${YELLOW}⚠️  Docker not running${NC}"
fi

# ─── Cost Summary ─────────────────────────────────────────────────────────────
echo ""
echo -e "${BLUE}─── Cost Summary ───────────────────────────────────────────────${NC}"
echo "  GCP: Depends on running resources above"
echo "  Local: ₹0 (your laptop electricity)"
