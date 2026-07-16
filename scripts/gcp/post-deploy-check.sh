#!/usr/bin/env bash
# =============================================================================
# post-deploy-check.sh — Comprehensive health check for all deployed components (GCP)
# =============================================================================
# Usage:
#   ./scripts/gcp/post-deploy-check.sh [--project PROJECT_ID] [--region REGION]
#
# What this checks:
#   1. Cloud SQL instance status
#   2. Memorystore Redis status
#   3. Cloud Run services (all 7 services)
#   4. Service endpoints (HTTP connectivity)
#   5. Secret Manager access
#   6. GCS bucket access
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PROJECT_ID=""
REGION=""   # resolved below: --region flag > GCP_REGION (.env.gcp) > asia-south1

# Service names
SQL_INSTANCE="token-opt-pg"
REDIS_INSTANCE="token-opt-redis"

# Cloud Run services to check (base — always present). Toggle-gated services
# (Qdrant, self-hosted observability) are appended after .env is loaded, below.
SERVICES=(
  "token-proxy"
  "llmlingua-svc"
  "routellm-svc"
  "langfuse-svc"
  "tika-svc"
  "portal-svc"
)

# Colors
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()    { echo -e "${BLUE}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[FAIL]${NC}   $*"; }

# ─── Load .env file if exists ───────────────────────────────────────────────────
ENV_FILE="${REPO_ROOT}/.env"
if [[ -f "$ENV_FILE" ]]; then
  info "Loading environment variables from .env..."
  set -a
  source "$ENV_FILE"
  set +a
fi

# ─── Argument parsing ─────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --project) PROJECT_ID="$2"; shift 2 ;;
    --region)  REGION="$2"; shift 2 ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

# Resolve project
if [[ -z "$PROJECT_ID" ]]; then
  PROJECT_ID="${GCP_PROJECT_ID:-}"
fi
if [[ -z "$PROJECT_ID" ]]; then
  PROJECT_ID=$(gcloud config get-value project 2>/dev/null)
fi
if [[ -z "$PROJECT_ID" ]]; then
  echo "No GCP project set. Use --project or: gcloud config set project PROJECT_ID"
  exit 1
fi

# Resolve region
if [[ -z "$REGION" ]]; then
  REGION="${GCP_REGION:-asia-south1}"
fi

# Counters
HEALTHY=0
UNHEALTHY=0
WARNINGS=0

echo ""
echo -e "${BLUE}╔══════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║   TokenLean — Token Optimisation Framework           ║${NC}"
echo -e "${BLUE}║   Health Check                                       ║${NC}"
echo -e "${BLUE}╚══════════════════════════════════════════════════════╝${NC}"
echo ""
echo "Project: ${PROJECT_ID}"
echo "Region:  ${REGION}"
echo ""

# ─── Deployment toggles (match gcp-deploy.sh guards; default = enabled) ────────
# A cost-optimized deploy can disable Qdrant (G07 → pgvector fallback) and/or
# self-hosted observability (→ Cloud Monitoring). Those services then never
# exist, so checking for them must NOT count as UNHEALTHY.
ENABLE_QDRANT="${ENABLE_QDRANT:-true}"
ENABLE_SELF_HOSTED_OBS="${ENABLE_SELF_HOSTED_OBS:-true}"

if [[ "$ENABLE_SELF_HOSTED_OBS" == "true" ]]; then
  SERVICES+=("grafana-svc" "token-opt-prometheus" "token-opt-alertmanager")
else
  info "Self-hosted observability disabled (ENABLE_SELF_HOSTED_OBS=${ENABLE_SELF_HOSTED_OBS}) — skipping Prometheus/Grafana/Alertmanager checks (Cloud Monitoring in use)."
fi
if [[ "$ENABLE_QDRANT" == "true" ]]; then
  SERVICES+=("token-opt-qdrant")
else
  info "Qdrant disabled (ENABLE_QDRANT=${ENABLE_QDRANT}) — skipping Qdrant checks (G07 pgvector fallback in use)."
fi
echo ""

# ─── Check 1: Cloud SQL ───────────────────────────────────────────────────────
info "Checking Cloud SQL..."
SQL_STATE=$(gcloud sql instances describe "${SQL_INSTANCE}" \
  --project="$PROJECT_ID" \
  --format="value(state)" 2>/dev/null || echo "UNKNOWN")

if [[ "$SQL_STATE" == "RUNNABLE" ]]; then
  success "Cloud SQL: ${SQL_STATE}"
  HEALTHY=$((HEALTHY+1))
else
  error "Cloud SQL: ${SQL_STATE} (expected: RUNNABLE)"
  UNHEALTHY=$((UNHEALTHY+1))
fi

# ─── Check 2: Redis (Memorystore OR the docker-Redis GCE VM) ─────────────────
# The commercial deploy defaults to `--redis docker` (a GCE COS VM, token-opt-redis-vm);
# `--redis memorystore` provisions Memorystore (token-opt-redis) instead. Only ONE exists per
# deploy — accept either, and only FAIL when NEITHER is present.
info "Checking Redis (Memorystore or docker-Redis VM)..."
REDIS_EXISTS=$(gcloud redis instances list \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --filter="name:${REDIS_INSTANCE}" \
  --format="value(name)" 2>/dev/null || echo "")

if [[ -n "$REDIS_EXISTS" ]]; then
  REDIS_STATE=$(gcloud redis instances describe "${REDIS_INSTANCE}" \
    --project="$PROJECT_ID" --region="$REGION" --format="value(state)" 2>/dev/null || echo "UNKNOWN")
  REDIS_HOST=$(gcloud redis instances describe "${REDIS_INSTANCE}" \
    --project="$PROJECT_ID" --region="$REGION" --format="value(host)" 2>/dev/null || echo "")
  if [[ "$REDIS_STATE" == "READY" ]]; then
    success "Memorystore Redis: ${REDIS_STATE} (${REDIS_HOST})"; HEALTHY=$((HEALTHY+1))
  else
    warn "Memorystore Redis: ${REDIS_STATE} (not READY yet)"; WARNINGS=$((WARNINGS+1))
  fi
else
  # No Memorystore → check the docker-Redis GCE VM (the commercial default, redis_backend=docker).
  REDIS_VM="token-opt-redis-vm"; REDIS_VM_ZONE="${REGION}-a"
  VM_STATE=$(gcloud compute instances describe "${REDIS_VM}" \
    --project="$PROJECT_ID" --zone="${REDIS_VM_ZONE}" --format="value(status)" 2>/dev/null || echo "")
  if [[ -z "$VM_STATE" ]]; then
    error "Redis: NEITHER Memorystore (${REDIS_INSTANCE}) NOR the docker-Redis VM (${REDIS_VM}) found"
    UNHEALTHY=$((UNHEALTHY+1))
  elif [[ "$VM_STATE" == "RUNNING" ]]; then
    success "docker-Redis VM: ${VM_STATE} (${REDIS_VM} @ ${REDIS_VM_ZONE})"; HEALTHY=$((HEALTHY+1))
  else
    warn "docker-Redis VM: ${VM_STATE} (not RUNNING yet — ${REDIS_VM})"; WARNINGS=$((WARNINGS+1))
  fi
fi

# ─── Check 3: Cloud Run Services ─────────────────────────────────────────────
info "Checking Cloud Run services..."
SERVICE_URLS=()

for svc in "${SERVICES[@]}"; do
  # Check if service exists and get URL
  SVC_URL=$(gcloud run services describe "$svc" \
    --project="$PROJECT_ID" \
    --region="$REGION" \
    --format="value(status.url)" 2>/dev/null || echo "")
  
  if [[ -n "$SVC_URL" ]]; then
    # Get last deployment time
    LAST_DEPLOY=$(gcloud run services describe "$svc" \
      --project="$PROJECT_ID" \
      --region="$REGION" \
      --format="value(spec.template.metadata.creationTimestamp)" 2>/dev/null || echo "unknown")
    
    success "${svc}: ${SVC_URL} (deployed: ${LAST_DEPLOY})"
    SERVICE_URLS+=("$svc:$SVC_URL")
    HEALTHY=$((HEALTHY+1))
  else
    error "${svc}: NOT DEPLOYED"
    UNHEALTHY=$((UNHEALTHY+1))
  fi
done

# ─── Check 4: HTTP Endpoint Health ───────────────────────────────────────────
info "Checking service endpoints..."

# Check proxy health endpoint
PROXY_URL=$(gcloud run services describe token-proxy \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --format="value(status.url)" 2>/dev/null || echo "")

if [[ -n "$PROXY_URL" ]]; then
  HTTP_STATUS=$(curl -s -o /dev/null -w "%{http_code}" "${PROXY_URL}/health" 2>/dev/null || echo "000")
  if [[ "$HTTP_STATUS" == "200" ]]; then
    success "token-proxy /health: HTTP ${HTTP_STATUS}"
    HEALTHY=$((HEALTHY+1))
  else
    warn "token-proxy /health: HTTP ${HTTP_STATUS} (may still be warming up)"
    WARNINGS=$((WARNINGS+1))
  fi
fi

# Check Langfuse
LANGFUSE_URL=$(gcloud run services describe langfuse-svc \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --format="value(status.url)" 2>/dev/null || echo "")

if [[ -n "$LANGFUSE_URL" ]]; then
  HTTP_STATUS=$(curl -s -o /dev/null -w "%{http_code}" "${LANGFUSE_URL}/api/public/health" 2>/dev/null || echo "000")
  # 401/403 = the service is UP but deployed private (--no-allow-unauthenticated, the default
  # commercial posture) → Cloud Run's IAM rejects the unauthenticated curl. That's EXPECTED and
  # healthy, not a warning. Only a non-2xx/non-auth code (000/5xx/404) is a real problem.
  if [[ "$HTTP_STATUS" == "200" ]]; then
    success "langfuse-svc: HTTP ${HTTP_STATUS}"
    HEALTHY=$((HEALTHY+1))
  elif [[ "$HTTP_STATUS" == "401" || "$HTTP_STATUS" == "403" ]]; then
    success "langfuse-svc: HTTP ${HTTP_STATUS} (up; private — auth required, as expected)"
    HEALTHY=$((HEALTHY+1))
  else
    warn "langfuse-svc: HTTP ${HTTP_STATUS} (expected 200, or 401/403 if private)"
    WARNINGS=$((WARNINGS+1))
  fi
fi

# Check LLMLingua sidecar
LLMLINGUA_URL=$(gcloud run services describe llmlingua-svc \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --format="value(status.url)" 2>/dev/null || echo "")

if [[ -n "$LLMLINGUA_URL" ]]; then
  HTTP_STATUS=$(curl -s -o /dev/null -w "%{http_code}" "${LLMLINGUA_URL}/health" 2>/dev/null || echo "000")
  if [[ "$HTTP_STATUS" == "200" ]]; then
    success "llmlingua-svc: HTTP ${HTTP_STATUS}"
    HEALTHY=$((HEALTHY+1))
  else
    warn "llmlingua-svc: HTTP ${HTTP_STATUS}"
    WARNINGS=$((WARNINGS+1))
  fi
fi

# Check RouteLLM sidecar
ROUTELLM_URL=$(gcloud run services describe routellm-svc \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --format="value(status.url)" 2>/dev/null || echo "")

if [[ -n "$ROUTELLM_URL" ]]; then
  HTTP_STATUS=$(curl -s -o /dev/null -w "%{http_code}" "${ROUTELLM_URL}/health" 2>/dev/null || echo "000")
  # routellm-svc's /health is a POST route (src/routellm-sidecar/app.py), so a GET probe returns
  # 404/405 even though the service is UP — that's reachable, not a failure. It may also be
  # deployed --ingress=internal (denial-of-wallet: it holds an OpenAI key) → 403 to an external
  # curl. So any of 200/403/404/405 means "up"; only 000/5xx is a real problem.
  case "$HTTP_STATUS" in
    200|403|404|405)
      success "routellm-svc: reachable (HTTP ${HTTP_STATUS}; up — /health is POST-only / internal-ingress)"
      HEALTHY=$((HEALTHY+1)) ;;
    *)
      warn "routellm-svc: HTTP ${HTTP_STATUS}"
      WARNINGS=$((WARNINGS+1)) ;;
  esac
fi

# Check Qdrant (only when enabled — otherwise G07 uses pgvector fallback)
if [[ "$ENABLE_QDRANT" == "true" ]]; then
  QDRANT_URL=$(gcloud run services describe token-opt-qdrant \
    --project="$PROJECT_ID" \
    --region="$REGION" \
    --format="value(status.uri)" 2>/dev/null || echo "")

  if [[ -n "$QDRANT_URL" ]]; then
    # Qdrant has a health endpoint at /healthz or we can check root
    HTTP_STATUS=$(curl -s -o /dev/null -w "%{http_code}" "${QDRANT_URL}" 2>/dev/null || echo "000")
    if [[ "$HTTP_STATUS" == "200" ]] || [[ "$HTTP_STATUS" == "403" ]]; then
      # 403 is OK for internal services (they block external ingress)
      success "token-opt-qdrant: Reachable (${HTTP_STATUS})"
      HEALTHY=$((HEALTHY+1))
    else
      warn "token-opt-qdrant: HTTP ${HTTP_STATUS}"
      WARNINGS=$((WARNINGS+1))
    fi
  fi
fi

# Check Prometheus (only when self-hosted observability is enabled)
if [[ "$ENABLE_SELF_HOSTED_OBS" == "true" ]]; then
  PROM_URL=$(gcloud run services describe token-opt-prometheus \
    --project="$PROJECT_ID" \
    --region="$REGION" \
    --format="value(status.uri)" 2>/dev/null || echo "")

  if [[ -n "$PROM_URL" ]]; then
    HTTP_STATUS=$(curl -s -o /dev/null -w "%{http_code}" "${PROM_URL}/-/healthy" 2>/dev/null || echo "000")
    if [[ "$HTTP_STATUS" == "200" ]]; then
      success "token-opt-prometheus: HTTP ${HTTP_STATUS}"
      HEALTHY=$((HEALTHY+1))
    elif [[ "$HTTP_STATUS" == "403" ]]; then
      success "token-opt-prometheus: Reachable (internal service)"
      HEALTHY=$((HEALTHY+1))
    else
      warn "token-opt-prometheus: HTTP ${HTTP_STATUS}"
      WARNINGS=$((WARNINGS+1))
    fi
  fi
fi

# ─── Check 5: Secret Manager ─────────────────────────────────────────────────
info "Checking Secret Manager..."
SECRETS_COUNT=$(gcloud secrets list \
  --project="$PROJECT_ID" \
  --format="value(name)" 2>/dev/null | wc -l || echo "0")

if [[ "$SECRETS_COUNT" -gt 0 ]]; then
  success "Secret Manager: ${SECRETS_COUNT} secrets configured"
  HEALTHY=$((HEALTHY+1))
else
  warn "Secret Manager: No secrets found"
  WARNINGS=$((WARNINGS+1))
fi

# Check critical secrets exist. Includes the commercial BYOK secrets:
# tenant-key-encryption-key = the BYOK master key (Fernet); database-url = the DB DSN
# stored as a secret (item 9) rather than a plaintext env var.
# NOTE: llm-key-openai is NOT here — under strict BYOK (SKIP_PLATFORM_KEYS=true, the commercial
# default) the deploy DELIBERATELY seeds no platform provider key (every tenant brings its own),
# so its absence is correct, not a failure. It's checked separately below as expected-absent.
CRITICAL_SECRETS=("token-opt-db-password" "tenant-key-encryption-key" "database-url")
for secret in "${CRITICAL_SECRETS[@]}"; do
  if gcloud secrets versions access latest --secret="$secret" --project="$PROJECT_ID" &>/dev/null; then
    success "Secret ${secret}: Has active version"
    HEALTHY=$((HEALTHY+1))
  else
    warn "Secret ${secret}: No active version"
    WARNINGS=$((WARNINGS+1))
  fi
done

# Platform provider key: present on a non-BYOK deploy, ABSENT under strict BYOK. Either is fine —
# report it informationally without ever failing the check.
if gcloud secrets versions access latest --secret="llm-key-openai" --project="$PROJECT_ID" &>/dev/null; then
  success "Secret llm-key-openai: present (platform provider key configured)"
  HEALTHY=$((HEALTHY+1))
else
  success "Secret llm-key-openai: absent (strict BYOK — no platform key, as expected)"
  HEALTHY=$((HEALTHY+1))
fi

# ─── BYOK hardening posture (commercial) ─────────────────────────────────────
# Confirms the master key is KMS-wrapped and the KMS key ring exists — i.e. reading
# the Secret Manager value alone does not yield plaintext (decrypt is a separable
# IAM grant). Absent/plaintext is a WARN (a valid OSS/unhardened deploy), not fatal.
info "Checking BYOK hardening posture..."
KMS_ENV=$(gcloud run services describe token-proxy --region="$REGION" --project="$PROJECT_ID" \
  --format='value(spec.template.spec.containers[0].env)' 2>/dev/null | grep -c TENANT_KEY_KMS_KEY || true)
if [[ "${KMS_ENV:-0}" -ge 1 ]]; then
  # Env-var presence proves KMS unwrap is CONFIGURED, not that the stored secret is actually
  # ciphertext — the behavioural proof (decrypt round-trip) is key-security-harness.sh --gcp stage 11.
  success "BYOK master key: TENANT_KEY_KMS_KEY set (KMS unwrap configured; wrap not behaviourally verified here — see key-security-harness.sh --gcp stage 11)"
  HEALTHY=$((HEALTHY+1))
else
  warn "BYOK master key: plaintext (TENANT_KEY_KMS_KEY not set — enable_kms_master_key=false / HARDEN_KMS=false)"
  WARNINGS=$((WARNINGS+1))
fi
if gcloud kms keys list --location="$REGION" --keyring=token-opt-byok --project="$PROJECT_ID" &>/dev/null; then
  success "KMS key ring token-opt-byok: present (decrypt is a separable grant)"
  HEALTHY=$((HEALTHY+1))
else
  warn "KMS key ring token-opt-byok: absent (KMS envelope not enabled)"
  WARNINGS=$((WARNINGS+1))
fi

# ─── Check 6: GCS Bucket ─────────────────────────────────────────────────────
info "Checking GCS bucket..."
CONFIG_BUCKET=$(gcloud storage buckets list \
  --project="$PROJECT_ID" \
  --filter="name~token-opt-config" \
  --format="value(name)" 2>/dev/null | head -1 || echo "")

if [[ -n "$CONFIG_BUCKET" ]]; then
  success "GCS bucket: ${CONFIG_BUCKET}"
  
  # Check for config.yaml
  if gsutil ls "gs://${CONFIG_BUCKET}/config/config.yaml" &>/dev/null; then
    success "config.yaml: Present in GCS"
    HEALTHY=$((HEALTHY+1))
  else
    warn "config.yaml: NOT found in GCS"
    WARNINGS=$((WARNINGS+1))
  fi
  
  HEALTHY=$((HEALTHY+1))
else
  error "GCS bucket: NOT FOUND"
  UNHEALTHY=$((UNHEALTHY+1))
fi

# ─── Summary ─────────────────────────────────────────────────────────────────
echo ""
echo -e "${BLUE}══════════════════════════════════════════════════════${NC}"
if [[ $UNHEALTHY -eq 0 ]]; then
  echo -e "${GREEN}✓ ALL SYSTEMS HEALTHY${NC}"
  echo -e "${GREEN}  Healthy: ${HEALTHY}${NC}"
  echo -e "${YELLOW}  Warnings: ${WARNINGS}${NC}"
  echo ""
  echo -e "${GREEN}Ready for testing!${NC}"
  echo "  Proxy URL: ${PROXY_URL:-"Not deployed"}"
  echo "  Langfuse:  ${LANGFUSE_URL:-"Not deployed"}"
  echo "  Grafana:   $(gcloud run services describe grafana-svc --project="$PROJECT_ID" --region="$REGION" --format="value(status.url)" 2>/dev/null || echo "Not deployed")"
  exit 0
else
  echo -e "${RED}✗ HEALTH CHECK FAILED${NC}"
  echo -e "${GREEN}  Healthy: ${HEALTHY}${NC}"
  echo -e "${YELLOW}  Warnings: ${WARNINGS}${NC}"
  echo -e "${RED}  Unhealthy: ${UNHEALTHY}${NC}"
  echo ""
  echo -e "${YELLOW}To restore missing components:${NC}"
  echo "  1. Start infrastructure:  ./scripts/gcp/start-gcp.sh --project ${PROJECT_ID}"
  echo "  2. Deploy services:       ./scripts/gcp/gcp-deploy.sh --project ${PROJECT_ID} --skip-infra"
  echo "  3. Or full redeploy:      ./scripts/gcp/gcp-deploy.sh --project ${PROJECT_ID}"
  exit 1
fi
