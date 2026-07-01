#!/bin/bash
PROJECT=$(gcloud config get-value project 2>/dev/null)
REGION="asia-south1"
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'

echo "GCP Status Check for project: $PROJECT"
echo "========================================"

# Cloud SQL
SQL_STATE=$(gcloud sql instances describe token-opt-pg --format="value(state)" 2>/dev/null || echo "NOT_FOUND")
if [[ "$SQL_STATE" == "RUNNABLE" ]]; then
  echo -e "${RED}❌ Cloud SQL: RUNNING (costing money)${NC}"
elif [[ "$SQL_STATE" == "STOPPED" ]]; then
  echo -e "${YELLOW}⚠️  Cloud SQL: STOPPED ($2/month storage)${NC}"
else
  echo -e "${GREEN}✅ Cloud SQL: NOT FOUND (₹0)${NC}"
fi

# Redis
REDIS_STATE=$(gcloud redis instances describe token-opt-redis --region=$REGION --format="value(state)" 2>/dev/null || echo "NOT_FOUND")
if [[ "$REDIS_STATE" == "READY" ]]; then
  echo -e "${RED}❌ Redis: RUNNING (costing money)${NC}"
else
  echo -e "${GREEN}✅ Redis: NOT FOUND (₹0)${NC}"
fi

# Cloud Run (counts active services in target region)
RUNNING_SERVICES=$(gcloud run services list --region=$REGION --format="value(SERVICE)" 2>/dev/null | wc -l)
if [[ $RUNNING_SERVICES -gt 0 ]]; then
  echo -e "${YELLOW}ℹ️  Cloud Run: $RUNNING_SERVICES services deployed in ${REGION} (scale to zero = ₹0 when idle)${NC}"
  gcloud run services list --region=$REGION --format="table(SERVICE, URL)"
else
  echo -e "${GREEN}✅ Cloud Run: No services found${NC}"
fi

echo ""
echo "To stop everything: ./scripts/gcp/stop-gcp.sh"