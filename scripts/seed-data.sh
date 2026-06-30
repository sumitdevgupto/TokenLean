#!/usr/bin/env bash
# =============================================================================
# seed-data.sh — Seed Qdrant with pitch_docs (works for GCP and Local)
# =============================================================================
# Usage:
#   ./scripts/seed-data.sh [--qdrant-url URL] [--gcp-project ID]
#
# Auto-detects environment:
#   --qdrant-url   → Use explicit Qdrant URL
#   --gcp-project  → Discover Cloud Run Qdrant URL from GCP
#   (none)         → Default to http://localhost:6333 (local Docker)
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
QDRANT_URL="${QDRANT_URL:-}"
PROJECT_ID=""
COLLECTION="pitch_docs"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()    { echo -e "${BLUE}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# ─── Argument parsing ─────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --qdrant-url)  QDRANT_URL="$2"; shift 2 ;;
    --gcp-project) PROJECT_ID="$2"; shift 2 ;;
    --help)
      sed -n '/^# Usage:/,/^# ===/p' "$0" | head -20
      exit 0 ;;
    *) error "Unknown option: $1" ;;
  esac
done

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

HTTP_STATUS=$(curl -s -o /dev/null -w "%{http_code}" \
  "${QDRANT_URL}/collections/${COLLECTION}" 2>/dev/null || echo "000")

if [[ "$HTTP_STATUS" == "200" ]]; then
  POINTS_COUNT=$(curl -s "${QDRANT_URL}/collections/${COLLECTION}" 2>/dev/null | \
    python3 -c "import sys, json; d=json.load(sys.stdin); print(d.get('result', {}).get('points_count', 0))" 2>/dev/null || echo "0")
  if [[ "$POINTS_COUNT" -gt 0 ]]; then
    success "Collection ${COLLECTION} already has ${POINTS_COUNT} documents — skipping seed"
    exit 0
  fi
  info "Collection exists but is empty — seeding..."
else
  info "Collection not found — creating and seeding..."
fi

# ─── Create collection ────────────────────────────────────────────────────────
info "Creating collection ${COLLECTION}..."
curl -s -X PUT "${QDRANT_URL}/collections/${COLLECTION}" \
  -H "Content-Type: application/json" \
  -d '{
    "vectors": {
      "size": 384,
      "distance": "Cosine"
    }
  }' > /dev/null || error "Failed to create collection"
success "Collection created"

# ─── Seed data ────────────────────────────────────────────────────────────────
info "Seeding documents..."

# Check if seed script exists
SEED_SCRIPT="${REPO_ROOT}/pitch-test-plan/src/seed_direct.py"
SEED_OK=false
if [[ -f "$SEED_SCRIPT" ]]; then
  python3 "$SEED_SCRIPT" \
    --qdrant-url "${QDRANT_URL}" \
    --collection "${COLLECTION}" \
    && SEED_OK=true \
    || warn "Python seed script failed, falling back to inline seed"
else
  warn "seed_direct.py not found, using inline seed data"
fi

# Inline fallback: generate 384-dim vectors matching collection config
if [[ "$SEED_OK" != true ]]; then
  python3 -c "
import json, random, urllib.request
random.seed(42)
points = [
    {'id': i+1, 'vector': [random.gauss(0,0.1) for _ in range(384)],
     'payload': {'text': f'Sample document {i+1}', 'source': 'seed-fallback'}}
    for i in range(2)
]
data = json.dumps({'points': points}).encode()
req = urllib.request.Request('${QDRANT_URL}/collections/${COLLECTION}/points',
    data=data, headers={'Content-Type': 'application/json'}, method='PUT')
urllib.request.urlopen(req, timeout=10)
print('Inline fallback seed complete')
" 2>/dev/null || warn "Inline fallback seed failed"
fi

# ─── Verify ───────────────────────────────────────────────────────────────────
POINTS_COUNT=$(curl -s "${QDRANT_URL}/collections/${COLLECTION}" 2>/dev/null | \
  python3 -c "import sys, json; d=json.load(sys.stdin); print(d.get('result', {}).get('points_count', 0))" 2>/dev/null || echo "0")

if [[ "$POINTS_COUNT" -gt 0 ]]; then
  success "Seeding complete: ${POINTS_COUNT} documents in ${COLLECTION}"
else
  warn "Seeding may have failed — collection appears empty"
fi
