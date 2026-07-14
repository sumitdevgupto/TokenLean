#!/bin/bash
# check-gcp-status.sh — read-only cost/status snapshot (what's still running + billing).
# Resolves project/region the SAME way as the deploy + lifecycle scripts (flag >
# .env.gcp GCP_PROJECT_ID/GCP_REGION > gcloud config / asia-south1) so it inspects the
# SAME project/region the stack was deployed into — else it would default to asia-south1
# and falsely report everything NOT FOUND for a deploy in another region.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
PROJECT=""
REGION=""
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'

# Load .env.gcp (fallback .env) for GCP_PROJECT_ID / GCP_REGION
ENV_FILE="${REPO_ROOT}/.env.gcp"
[[ -f "$ENV_FILE" ]] || ENV_FILE="${REPO_ROOT}/.env"
[[ -f "$ENV_FILE" ]] && { set -a; source "$ENV_FILE"; set +a; }

# Args: --project / --region override everything
while [[ $# -gt 0 ]]; do
  case "$1" in
    --project) PROJECT="$2"; shift 2 ;;
    --region)  REGION="$2";  shift 2 ;;
    *) shift ;;
  esac
done

# Resolve: flag > .env.gcp > gcloud config / default
[[ -z "$PROJECT" ]] && PROJECT="${GCP_PROJECT_ID:-}"
[[ -z "$PROJECT" ]] && PROJECT=$(gcloud config get-value project 2>/dev/null)
[[ -z "$REGION" ]]  && REGION="${GCP_REGION:-asia-south1}"

echo "GCP Status Check for project: $PROJECT  (region: $REGION)"
echo "========================================"

# Cloud SQL
SQL_STATE=$(gcloud sql instances describe token-opt-pg --project="$PROJECT" --format="value(state)" 2>/dev/null || echo "NOT_FOUND")
if [[ "$SQL_STATE" == "RUNNABLE" ]]; then
  echo -e "${RED}❌ Cloud SQL: RUNNING (costing money)${NC}"
elif [[ "$SQL_STATE" == "STOPPED" ]]; then
  echo -e "${YELLOW}⚠️  Cloud SQL: STOPPED ($2/month storage)${NC}"
else
  echo -e "${GREEN}✅ Cloud SQL: NOT FOUND (₹0)${NC}"
fi

# Redis — check BOTH backends (docker GCE VM is the commercial DEFAULT; Memorystore is opt-in)
REDIS_STATE=$(gcloud redis instances describe token-opt-redis --region="$REGION" --project="$PROJECT" --format="value(state)" 2>/dev/null || echo "NOT_FOUND")
if [[ "$REDIS_STATE" == "READY" ]]; then
  echo -e "${RED}❌ Redis (Memorystore): RUNNING (costing money)${NC}"
else
  echo -e "${GREEN}✅ Redis (Memorystore): NOT FOUND (₹0)${NC}"
fi

REDIS_VM_STATE=$(gcloud compute instances describe token-opt-redis-vm --zone="${REGION}-a" --project="$PROJECT" --format="value(status)" 2>/dev/null || echo "NOT_FOUND")
if [[ "$REDIS_VM_STATE" == "RUNNING" ]]; then
  echo -e "${RED}❌ Redis (docker VM): RUNNING (costing money)${NC}"
elif [[ "$REDIS_VM_STATE" == "TERMINATED" ]]; then
  echo -e "${YELLOW}⚠️  Redis (docker VM): STOPPED (boot disk only, ~pennies/month)${NC}"
else
  echo -e "${GREEN}✅ Redis (docker VM): NOT FOUND (₹0)${NC}"
fi

# Cloud Run (counts active services in target region)
RUNNING_SERVICES=$(gcloud run services list --region="$REGION" --project="$PROJECT" --format="value(SERVICE)" 2>/dev/null | wc -l)
if [[ $RUNNING_SERVICES -gt 0 ]]; then
  echo -e "${YELLOW}ℹ️  Cloud Run: $RUNNING_SERVICES services deployed in ${REGION} (scale to zero = ₹0 when idle)${NC}"
  gcloud run services list --region="$REGION" --project="$PROJECT" --format="table(SERVICE, URL)"
else
  echo -e "${GREEN}✅ Cloud Run: No services found${NC}"
fi

echo ""
echo "To stop everything: ./scripts/gcp/stop-gcp.sh"