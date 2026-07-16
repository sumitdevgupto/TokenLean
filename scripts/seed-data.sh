#!/usr/bin/env bash
# =============================================================================
# seed-data.sh — Seed Qdrant with a per-tenant RAG collection (GCP and Local)
# =============================================================================
# Usage:
#   ./scripts/seed-data.sh [--tenant ID] [--qdrant-url URL] [--gcp-project ID]
#
#   --tenant       → Tenant ID; seeds collection rag_<tenant> (default: "default"
#                    → rag_docs). Matches tenancy/context.py + the ingest pipeline.
#   --qdrant-url   → Use explicit Qdrant URL
#   --gcp-project  → Discover Cloud Run Qdrant URL from GCP
#   (none)         → Default to http://localhost:6333 (local Docker)
#
# The collection uses NAMED dense+sparse vectors and stamps tenant_id into every
# payload, so seeded data is byte-compatible with what the G03 doc pipeline upserts
# (the old pitch_docs / unnamed-384-dim scheme did NOT match retrieval and is gone).
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
QDRANT_URL="${QDRANT_URL:-}"
PROJECT_ID=""
TENANT="${SEED_TENANT:-default}"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()    { echo -e "${BLUE}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# ─── Argument parsing ─────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --tenant)      TENANT="$2"; shift 2 ;;
    --qdrant-url)  QDRANT_URL="$2"; shift 2 ;;
    --gcp-project) PROJECT_ID="$2"; shift 2 ;;
    --help)
      sed -n '/^# Usage:/,/^# ===/p' "$0" | head -20
      exit 0 ;;
    *) error "Unknown option: $1" ;;
  esac
done

# Collection follows tenancy/context.py: default → rag_docs, else rag_<tenant>.
if [[ "$TENANT" == "default" || -z "$TENANT" ]]; then
  COLLECTION="rag_docs"
else
  COLLECTION="rag_${TENANT}"
fi

# ─── Auto-detect Qdrant URL ───────────────────────────────────────────────────
if [[ -z "$QDRANT_URL" && -n "$PROJECT_ID" ]]; then
  info "Discovering Qdrant URL from GCP..."
  QDRANT_URL=$(gcloud run services describe token-opt-qdrant \
    --region=asia-south1 --project="$PROJECT_ID" \
    --format="value(status.url)" 2>/dev/null || echo "")
  if [[ -z "$QDRANT_URL" ]]; then
    error "Could not discover Qdrant URL in GCP project: ${PROJECT_ID}"
  fi
  success "Discovered Qdrant URL: ${QDRANT_URL}"
elif [[ -z "$QDRANT_URL" ]]; then
  QDRANT_URL="http://localhost:6333"
  info "Using default local Qdrant URL: ${QDRANT_URL}"
fi

# ─── Check if collection exists ───────────────────────────────────────────────
info "Checking Qdrant collection: ${COLLECTION}..."

# App-layer auth: the GCP-managed Qdrant enforces an api-key (qdrant-api-key secret,
# exported as QDRANT_API_KEY by the calling deploy script). Empty locally → no header.
CURL_AUTH=()
[[ -n "${QDRANT_API_KEY:-}" ]] && CURL_AUTH=(-H "api-key: ${QDRANT_API_KEY}")

HTTP_STATUS=$(curl -s -o /dev/null -w "%{http_code}" "${CURL_AUTH[@]}" \
  "${QDRANT_URL}/collections/${COLLECTION}" 2>/dev/null || echo "000")

if [[ "$HTTP_STATUS" == "200" ]]; then
  POINTS_COUNT=$(curl -s "${CURL_AUTH[@]}" "${QDRANT_URL}/collections/${COLLECTION}" 2>/dev/null | \
    python3 -c "import sys, json; d=json.load(sys.stdin); print(d.get('result', {}).get('points_count', 0))" 2>/dev/null || echo "0")
  if [[ "$POINTS_COUNT" -gt 0 ]]; then
    success "Collection ${COLLECTION} already has ${POINTS_COUNT} documents — skipping seed"
    exit 0
  fi
  info "Collection exists but is empty — seeding..."
else
  info "Collection not found — creating and seeding..."
fi

# ─── Seed data (named dense+sparse, tenant_id-stamped) ────────────────────────
# Delegates to the canonical per-tenant seeder so the collection schema (named
# dense/sparse vectors) and payload (tenant_id) match exactly what the G03 doc
# pipeline upserts — the old inline 384-dim unnamed-vector seed did NOT.
info "Seeding tenant '${TENANT}' → collection ${COLLECTION}..."

SEED_SCRIPT="${REPO_ROOT}/pitch-test-plan/src/seed_qdrant_tenants.py"
if [[ ! -f "$SEED_SCRIPT" ]]; then
  error "Canonical seeder not found: ${SEED_SCRIPT}"
fi

QDRANT_URL="${QDRANT_URL}" python3 "$SEED_SCRIPT" \
  --tenants "${TENANT}" \
  --qdrant-url "${QDRANT_URL}" \
  || error "Seeding failed — ensure qdrant-client, sentence-transformers, fastembed are installed"

# ─── Verify ───────────────────────────────────────────────────────────────────
POINTS_COUNT=$(curl -s "${CURL_AUTH[@]}" "${QDRANT_URL}/collections/${COLLECTION}" 2>/dev/null | \
  python3 -c "import sys, json; d=json.load(sys.stdin); print(d.get('result', {}).get('points_count', 0))" 2>/dev/null || echo "0")

if [[ "$POINTS_COUNT" -gt 0 ]]; then
  success "Seeding complete: ${POINTS_COUNT} documents in ${COLLECTION}"
else
  warn "Seeding may have failed — collection appears empty"
fi
