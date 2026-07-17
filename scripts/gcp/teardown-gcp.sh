#!/bin/bash
# =============================================================================
# teardown-gcp.sh — Complete GCP teardown (base + commercial)
# =============================================================================
# Usage: ./scripts/gcp/teardown-gcp.sh [--project PROJECT_ID] [--region REGION]
#                                      [--full] [--delete-images]
#
# Deletes all GCP resources created by gcp-deploy.sh AND deploy-commercial-gcp.sh:
#   - Cloud SQL instance
#   - Redis — BOTH backends (docker GCE VM, the commercial DEFAULT; Memorystore, opt-in)
#   - All Cloud Run services, incl. portal-svc (commercial)
#   - Cloud Run Jobs (doc-pipeline, finetune-pipeline, docs-seed)
#   - KMS key ring (if BYOK hardening was enabled) — key itself has prevent_destroy,
#     so this only removes the ring binding/ring if empty; see Step 5 note
#   - Artifact Registry images (optional)
#
# DEFAULT (paused/cheap) — keeps GCS bucket for config backups + Secret Manager
# secrets + service accounts + the Terraform state (minimal cost). Use this when
# you intend to re-deploy the SAME environment.
#
# --full (TRUE clean slate — "as if first run") — ALSO removes everything the
# default keeps AND resets Terraform remote state:
#   - Terraform remote state object (gs://<project>-tf-state/<prefix>/default.tfstate)
#   - config GCS bucket (token-opt-config-*)
#   - Secret Manager secrets (tokenlean-backup-* + any token-opt/llm-key/routellm/db)
#   - Service accounts (token-opt-proxy-sa, routellm-sidecar-sa, token-opt-ingest-push-sa)
#   - Artifact Registry repo (implies --delete-images)
#
#   ★ WHY --full EXISTS: the default teardown deletes live infra via `gcloud`
#     but NEVER touches Terraform state. That desyncs state from reality — a
#     later `terraform apply` then tries to READ the now-deleted (tombstoned)
#     Cloud SQL instance and fails with a 403 notAuthorized. --full wipes the
#     stale state so the next deploy plans a genuine clean create. (This is the
#     fix for the "partial teardown" 403 seen 2026-07-15.)
# =============================================================================
set -euo pipefail
# ── Host-shell guard ────────────────────────────────────────────────────────────
# GCP deploy-family operations must run from WSL Ubuntu / Linux / Cloud Shell — NEVER
# Git Bash or any Windows shell (Terraform local-exec via cmd.exe, psql/docker host
# tooling, CRLF-corrupted env sourcing). Abort up front with the fix. (Read-only checks
# like check-gcp-status.sh / post-deploy-check.sh are intentionally NOT guarded — they
# work from any shell.)
case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*)
    echo "ERROR: run this from WSL Ubuntu (or Linux/Cloud Shell), NOT Git Bash/Windows." >&2
    echo "  In WSL:  cd /mnt/d/token-optimisation && bash $0 $*" >&2
    exit 2 ;;
esac

# ─── Force a UTF-8 locale ─────────────────────────────────────────────────────
# This script contains UTF-8 box-drawing characters (─ ║ ╔) in its banners. Under
# a non-UTF-8 locale (C/POSIX, or a Latin-1 Windows codepage in some Git Bash
# setups) bash mis-decodes those multibyte chars and errors with
# "$'\200──': command not found" on the banner/comment lines. Pinning a UTF-8
# locale here makes the script parse identically regardless of the caller's shell.
if locale -a 2>/dev/null | grep -qiE '^(C\.UTF-?8|C\.utf8)$'; then
  export LC_ALL=C.UTF-8 LANG=C.UTF-8
elif locale -a 2>/dev/null | grep -qiE '^en_US\.(UTF-?8|utf8)$'; then
  export LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
PROJECT_ID=""
REGION=""   # resolved below: --region flag > GCP_REGION (.env.gcp) > asia-south1
SQL_INSTANCE="token-opt-pg"
REDIS_INSTANCE="token-opt-redis"        # Memorystore instance name (redis_backend=memorystore)
REDIS_VM="token-opt-redis-vm"           # GCE VM name (redis_backend=docker, THE DEFAULT)
DELETE_IMAGES=false
FULL=false                              # --full → true clean slate (state + secrets + SAs + buckets + AR)
TF_STATE_PREFIX="token-opt"             # backend prefix (matches gcp-deploy.sh -backend-config prefix=)
ARTIFACT_REPO="token-opt"               # Artifact Registry repo id (matches infra artifact_registry_repo default)
# Service accounts created by the deploy (deleted only in --full).
SERVICE_ACCOUNTS=(
  "token-opt-proxy-sa"
  "routellm-sidecar-sa"
  "token-opt-ingest-push-sa"
)

# ─── Never block on a gcloud prompt ──────────────────────────────────────────
# A disabled API (e.g. cloudkms) makes gcloud prompt "enable and retry? (y/N)" on
# stdin; with output redirected the prompt is invisible and the script appears to
# hang. CLOUDSDK_CORE_DISABLE_PROMPTS=1 forces every gcloud invocation to run
# non-interactively (auto-"no"), so no step can stall waiting for a keypress.
export CLOUDSDK_CORE_DISABLE_PROMPTS=1

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()    { echo -e "${BLUE}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# ─── Load .env.gcp (fallback .env) — SAME source of GCP_PROJECT_ID / GCP_REGION as
#     the deploy scripts, so a teardown without flags targets the SAME project/region
#     the stack was deployed into (else it silently defaults to asia-south1 and misses
#     resources in another region). ─────────────────────────────────────────────
ENV_FILE="${REPO_ROOT}/.env.gcp"
[[ -f "$ENV_FILE" ]] || ENV_FILE="${REPO_ROOT}/.env"
if [[ -f "$ENV_FILE" ]]; then
  # Strip CRLF on the fly: env files edited on Windows carry \r, which bash would
  # append to every value (e.g. GCP_REGION=asia-south1$'\r') and corrupt gcloud args.
  set -a; source <(tr -d '\r' < "$ENV_FILE"); set +a
fi

# ─── Argument parsing ─────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --project)  PROJECT_ID="$2"; shift 2 ;;
    --region)   REGION="$2"; shift 2 ;;
    --delete-images) DELETE_IMAGES=true; shift ;;
    --full)     FULL=true; DELETE_IMAGES=true; shift ;;   # true clean slate; AR repo delete implies image delete
    --help)
      echo "Usage: ./scripts/gcp/teardown-gcp.sh [--project PROJECT_ID] [--region REGION] [--full] [--delete-images]"
      echo ""
      echo "Options:"
      echo "  --project ID       GCP project ID (default: GCP_PROJECT_ID in .env.gcp, else gcloud config)"
      echo "  --region REGION    GCP region (default: GCP_REGION in .env.gcp, else asia-south1)"
      echo "  --delete-images    Also delete Docker images from Artifact Registry"
      echo "  --full             TRUE clean slate: also reset Terraform state + delete secrets,"
      echo "                     service accounts, config bucket, and Artifact Registry repo."
      echo "                     Use to re-run 'as if the first time'. (implies --delete-images)"
      echo "  --help             Show this help"
      exit 0 ;;
    *) error "Unknown option: $1" ;;
  esac
done

# ─── Resolve project (flag > GCP_PROJECT_ID > gcloud config) ──────────────────
[[ -z "$PROJECT_ID" ]] && PROJECT_ID="${GCP_PROJECT_ID:-}"
if [[ -z "$PROJECT_ID" ]]; then
  PROJECT_ID=$(gcloud config get-value project 2>/dev/null)
  [[ -z "$PROJECT_ID" ]] && error "No GCP project set. Use --project, set GCP_PROJECT_ID in .env.gcp, or: gcloud config set project PROJECT_ID"
fi
# ─── Resolve region (flag > GCP_REGION > asia-south1) ─────────────────────────
[[ -z "$REGION" ]] && REGION="${GCP_REGION:-asia-south1}"

echo -e "${RED}"
echo "╔════════════════════════════════════════════════════════════════╗"
echo "║   ⚠️  TEARDOWN: DELETE ALL GCP RESOURCES ⚠️                  ║"
echo "╠════════════════════════════════════════════════════════════════╣"
echo "║  This will PERMANENTLY DELETE:                                 ║"
echo "║    • Cloud SQL database (token-opt-pg)                        ║"
echo "║    • Redis — docker VM or Memorystore, whichever exists       ║"
echo "║    • All Cloud Run services (incl. portal-svc)                ║"
echo "║    • Cloud Run Jobs (doc-pipeline, finetune, docs-seed)        ║"
if [[ "$FULL" == true ]]; then
echo "║                                                                ║"
echo "║   🔥 --full CLEAN SLATE — ALSO deletes:                       ║"
echo "║    • Terraform remote state (next deploy = fresh create)      ║"
echo "║    • Secret Manager secrets (incl. tokenlean-backup-*)        ║"
echo "║    • Service accounts (proxy-sa, routellm-sa, ingest-push-sa) ║"
echo "║    • config GCS bucket + Artifact Registry repo               ║"
echo "║    ★ tf-state BUCKET kept (empty); KMS key material kept       ║"
else
echo "║                                                                ║"
echo "║  ★ GCS bucket, Secret Manager, service accounts, TF state     ║"
echo "║    KEPT (minimal cost). Use --full for a true clean slate.    ║"
echo "║    (KMS key has prevent_destroy — see script)                 ║"
fi
echo "╚════════════════════════════════════════════════════════════════╝"
echo -e "${NC}"
echo "Project: ${PROJECT_ID}"
echo "Region:  ${REGION}"
echo "Mode:    $([[ "$FULL" == true ]] && echo 'FULL clean slate (state + secrets + SAs + buckets + AR)' || echo 'default (keeps state/secrets/SAs/buckets)')"
echo ""

# ─── Confirm ──────────────────────────────────────────────────────────────────
echo -en "${YELLOW}Are you ABSOLUTELY SURE? Type 'destroy' to confirm: ${NC}"
read -r confirm
[[ "$confirm" != "destroy" ]] && { info "Aborted."; exit 0; }

# ─── Step 1: Delete Cloud Run Services ───────────────────────────────────────
echo ""
info "Step 1/5: Deleting Cloud Run services..."

SERVICES=(
  "token-proxy"
  "portal-svc"
  "llmlingua-svc"
  "routellm-svc"
  "langfuse-svc"
  "grafana-svc"
  "tika-svc"
  "token-opt-qdrant"
  "token-opt-prometheus"
  "token-opt-alertmanager"
)

for service in "${SERVICES[@]}"; do
  info "  → Deleting ${service}..."
  if gcloud run services describe "${service}" --region="${REGION}" --project="${PROJECT_ID}" &>/dev/null; then
    gcloud run services delete "${service}" \
      --region="${REGION}" \
      --project="${PROJECT_ID}" \
      --quiet 2>/dev/null && success "    Deleted ${service}" || warn "    Failed to delete ${service}"
  else
    success "    ${service} not found (already deleted)"
  fi
done

# ─── Step 1b: Delete Cloud Run Jobs ───────────────────────────────────────────
info "Step 1b/5: Deleting Cloud Run Jobs..."

JOBS=("doc-pipeline-job" "finetune-pipeline-job" "docs-seed-job" "qdrant-seeder" "schema-migrate-job")
for job in "${JOBS[@]}"; do
  info "  → Deleting ${job}..."
  if gcloud run jobs describe "${job}" --region="${REGION}" --project="${PROJECT_ID}" &>/dev/null; then
    gcloud run jobs delete "${job}" \
      --region="${REGION}" \
      --project="${PROJECT_ID}" \
      --quiet 2>/dev/null && success "    Deleted ${job}" || warn "    Failed to delete ${job}"
  else
    success "    ${job} not found (already deleted)"
  fi
done

# Safety sweep: delete ANY remaining Cloud Run job in the region (catches jobs
# added by future deploy steps that aren't in the static list above). --full only.
if [[ "$FULL" == true ]]; then
  LEFTOVER_JOBS=$(gcloud run jobs list --region="${REGION}" --project="${PROJECT_ID}" --format="value(metadata.name)" 2>/dev/null | grep . || echo "")
  if [[ -n "$LEFTOVER_JOBS" ]]; then
    while IFS= read -r job; do
      [[ -z "$job" ]] && continue
      info "  → (sweep) Deleting leftover job ${job}..."
      gcloud run jobs delete "${job}" --region="${REGION}" --project="${PROJECT_ID}" --quiet 2>/dev/null \
        && success "    Deleted ${job}" || warn "    Failed to delete ${job}"
    done <<< "$LEFTOVER_JOBS"
  fi
fi

# ─── Step 2: Delete Cloud SQL ─────────────────────────────────────────────────
echo ""
info "Step 2/5: Deleting Cloud SQL..."

# NOTE: on a TOMBSTONED instance (deleted but name still reserved), `describe`
# can return 403 rather than a clean 404 — treat both as "nothing live to delete".
SQL_LIVE=$(gcloud sql instances list --project="${PROJECT_ID}" \
  --filter="name:${SQL_INSTANCE}" --format="value(name)" 2>/dev/null || echo "")
if [[ -n "$SQL_LIVE" ]]; then
  info "  → Disabling deletion protection..."
  gcloud sql instances patch "${SQL_INSTANCE}" \
    --no-deletion-protection \
    --project="${PROJECT_ID}" \
    --quiet 2>/dev/null || warn "    Could not disable protection (may already be disabled)"

  info "  → Deleting SQL instance..."
  gcloud sql instances delete "${SQL_INSTANCE}" \
    --project="${PROJECT_ID}" \
    --quiet && success "  Deleted Cloud SQL" || warn "  Failed to delete Cloud SQL"
else
  success "  Cloud SQL not live (already deleted or tombstoned)"
fi

# ─── Step 3: Delete Redis — BOTH backends (docker VM is the commercial default) ─
echo ""
info "Step 3/5: Deleting Redis (docker VM + Memorystore, whichever exists)..."

VM_ZONE="${REGION}-a"
if gcloud compute instances describe "${REDIS_VM}" --zone="${VM_ZONE}" --project="${PROJECT_ID}" &>/dev/null; then
  gcloud compute instances delete "${REDIS_VM}" \
    --zone="${VM_ZONE}" \
    --project="${PROJECT_ID}" \
    --quiet && success "  Deleted Redis VM (${REDIS_VM})" || warn "  Failed to delete Redis VM"
else
  success "  Redis VM not found (already deleted, or Memorystore backend in use)"
fi

if gcloud redis instances describe "${REDIS_INSTANCE}" --region="${REGION}" --project="${PROJECT_ID}" &>/dev/null; then
  gcloud redis instances delete "${REDIS_INSTANCE}" \
    --region="${REGION}" \
    --project="${PROJECT_ID}" \
    --quiet && success "  Deleted Memorystore Redis" || warn "  Failed to delete Memorystore Redis"
else
  success "  Memorystore Redis not found (already deleted, or docker VM backend in use)"
fi

# ─── Step 3b: KMS — BYOK master-key envelope (if hardening was enabled) ────────
# The crypto key itself has `prevent_destroy = true` in Terraform (losing it makes
# every stored tenant provider key permanently unrecoverable) — teardown deliberately
# does NOT delete google_kms_crypto_key.master_key. This step only reports its
# presence so it's not mistaken for an orphaned resource; delete it explicitly and
# knowingly via `terraform destroy` (after removing prevent_destroy) if truly needed.
echo ""
info "Step 3c/5: Checking KMS BYOK key ring (not deleted — see note)..."
# --quiet: if the KMS API is DISABLED, gcloud otherwise prompts "enable and retry? (y/N)"
# on stdin — and &>/dev/null hides the prompt text but does NOT stop it blocking, so the
# script appears to hang here. --quiet auto-answers "no" (never blocks). We also short-circuit
# when the API is off, since a disabled KMS API means there can be no key ring anyway.
if gcloud services list --enabled --project="${PROJECT_ID}" --format="value(config.name)" 2>/dev/null | grep -q "^cloudkms.googleapis.com$" \
   && gcloud kms keys list --location="${REGION}" --keyring=token-opt-byok --project="${PROJECT_ID}" --quiet &>/dev/null; then
  warn "  KMS key ring 'token-opt-byok' present — KEPT intentionally (prevent_destroy on the crypto key; deleting it makes all stored BYOK provider keys unrecoverable). Minimal cost (~\$0.06/key-version/month)."
else
  success "  No KMS key ring found (hardening was not enabled, or already removed)"
fi

# ─── Step 4: FULL clean slate — state reset + secrets + SAs + config bucket ────
# This is the piece the default teardown OMITS. Without it, `gcloud`-deleted infra
# leaves Terraform state referencing dead resources → next `apply` 403s on the
# tombstoned SQL instance. --full makes the project deployable "as if the first run".
if [[ "$FULL" == true ]]; then
  echo ""
  info "Step 4/5: FULL clean slate..."

  # 4a. Reset Terraform remote state — delete the state OBJECT, keep the (versioned) bucket.
  #     The bucket is versioned, so this is recoverable; the next deploy re-inits fresh.
  TF_STATE_BUCKET="${PROJECT_ID}-tf-state"
  TF_STATE_OBJECT="gs://${TF_STATE_BUCKET}/${TF_STATE_PREFIX}/default.tfstate"
  info "  → Resetting Terraform state (${TF_STATE_OBJECT})..."
  if gcloud storage ls "${TF_STATE_OBJECT}" &>/dev/null; then
    gcloud storage rm "${TF_STATE_OBJECT}" --quiet 2>/dev/null \
      && success "    Terraform state reset (bucket kept, versioned/recoverable)" \
      || warn "    Could not delete state object"
  else
    success "    No Terraform state object found (already clean)"
  fi

  # 4b. Delete Secret Manager secrets — deploy-created + tokenlean-backup-*.
  #     Match by known prefixes so we never touch unrelated project secrets.
  info "  → Deleting Secret Manager secrets (token-opt / llm-key / routellm / db / tokenlean-backup)..."
  SECRETS=$(gcloud secrets list --project="${PROJECT_ID}" --format="value(name)" 2>/dev/null \
    | grep -E '^(tokenlean-backup-|token-opt|llm-key-|routellm-|token-proxy-api-keys$|db-password$|grafana-admin|prometheus-|alertmanager-|langfuse-)' || echo "")
  if [[ -n "$SECRETS" ]]; then
    while IFS= read -r secret; do
      [[ -z "$secret" ]] && continue
      gcloud secrets delete "$secret" --project="${PROJECT_ID}" --quiet 2>/dev/null \
        && success "    Deleted secret ${secret}" || warn "    Failed to delete secret ${secret}"
    done <<< "$SECRETS"
  else
    success "    No matching secrets found (already clean)"
  fi

  # 4c. Delete the config GCS bucket (token-opt-config-*). NOT the tf-state bucket.
  info "  → Deleting config GCS bucket(s) (token-opt-config-*)..."
  CONFIG_BUCKETS=$(gcloud storage buckets list --project="${PROJECT_ID}" --format="value(name)" 2>/dev/null \
    | grep -E '^token-opt-config-' || echo "")
  if [[ -n "$CONFIG_BUCKETS" ]]; then
    while IFS= read -r bkt; do
      [[ -z "$bkt" ]] && continue
      gcloud storage rm -r "gs://${bkt}" --quiet 2>/dev/null \
        && success "    Deleted bucket ${bkt}" || warn "    Failed to delete bucket ${bkt}"
    done <<< "$CONFIG_BUCKETS"
  else
    success "    No config bucket found (already clean)"
  fi

  # 4d. Delete service accounts created by the deploy.
  info "  → Deleting service accounts..."
  for sa in "${SERVICE_ACCOUNTS[@]}"; do
    SA_EMAIL="${sa}@${PROJECT_ID}.iam.gserviceaccount.com"
    if gcloud iam service-accounts describe "$SA_EMAIL" --project="${PROJECT_ID}" &>/dev/null; then
      gcloud iam service-accounts delete "$SA_EMAIL" --project="${PROJECT_ID}" --quiet 2>/dev/null \
        && success "    Deleted SA ${sa}" || warn "    Failed to delete SA ${sa}"
    else
      success "    SA ${sa} not found (already deleted)"
    fi
  done
else
  echo ""
  info "Step 4/5: Skipping FULL clean slate (Terraform state, secrets, service accounts, config bucket KEPT). Use --full for a true clean slate."
fi

# ─── Step 5: Delete Artifact Registry (repo in --full, else images if requested) ─
if [[ "$FULL" == true ]]; then
  echo ""
  info "Step 5/5: Deleting Artifact Registry repo '${ARTIFACT_REPO}' (--full)..."
  if gcloud artifacts repositories describe "${ARTIFACT_REPO}" \
       --location="${REGION}" --project="${PROJECT_ID}" &>/dev/null; then
    gcloud artifacts repositories delete "${ARTIFACT_REPO}" \
      --location="${REGION}" --project="${PROJECT_ID}" --quiet 2>/dev/null \
      && success "  Deleted Artifact Registry repo (all images)" \
      || warn "  Failed to delete Artifact Registry repo"
  else
    success "  Artifact Registry repo not found (already deleted)"
  fi
elif [[ "$DELETE_IMAGES" == true ]]; then
  echo ""
  info "Step 5/5: Deleting Artifact Registry images..."

  REGISTRY="${REGION}-docker.pkg.dev/${PROJECT_ID}/${ARTIFACT_REPO}"

  for image in proxy proxy-commercial portal llmlingua-sidecar doc-pipeline finetune-pipeline routellm-sidecar tika-sidecar; do
    info "  → Deleting ${image}..."
    gcloud artifacts docker images delete "${REGISTRY}/${image}" \
      --delete-tags \
      --quiet 2>/dev/null && success "    Deleted ${image}" || warn "    Failed or not found: ${image}"
  done
else
  echo ""
  info "Step 5/5: Skipping Artifact Registry (use --delete-images to remove images, or --full to remove the repo)"
fi

# ─── Summary ──────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}╔════════════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║   TEARDOWN COMPLETE                                            ║${NC}"
echo -e "${GREEN}╠════════════════════════════════════════════════════════════════╣${NC}"
if [[ "$FULL" == true ]]; then
echo -e "${GREEN}║${NC} FULL clean slate — project is deployable as if the first run.  ${GREEN}║${NC}"
echo -e "${GREEN}║${NC} Kept (intentionally): tf-state BUCKET (empty), KMS key material.${GREEN}║${NC}"
echo -e "${GREEN}║${NC}                                                               ${GREEN}║${NC}"
echo -e "${GREEN}║${NC} Next: re-run the deploy from scratch —                        ${GREEN}║${NC}"
echo -e "${GREEN}║${NC}   sh scripts/commercial/run-gcp-commercial-lifecycle.sh                  ${GREEN}║${NC}"
else
echo -e "${GREEN}║${NC} Remaining GCP resources (minimal cost):                        ${GREEN}║${NC}"
echo -e "${GREEN}║${NC}   • GCS bucket (config backups) + tf-state                    ${GREEN}║${NC}"
echo -e "${GREEN}║${NC}   • Secret Manager secrets + service accounts                 ${GREEN}║${NC}"
echo -e "${GREEN}║${NC}   ⚠ Terraform state KEPT — a re-deploy reuses it. For a true   ${GREEN}║${NC}"
echo -e "${GREEN}║${NC}     clean slate (avoids the 403-tombstone), re-run with --full.${GREEN}║${NC}"
fi
echo -e "${GREEN}╚════════════════════════════════════════════════════════════════╝${NC}"
echo ""

# ─── Verify ───────────────────────────────────────────────────────────────────
echo "Verification:"
echo "------------"
CLEAN=true
RUNNING_SQL=$(gcloud sql instances list --project="${PROJECT_ID}" --filter="name:${SQL_INSTANCE}" --format="value(name)" 2>/dev/null || echo "")
RUNNING_REDIS_MS=$(gcloud redis instances list --region="${REGION}" --project="${PROJECT_ID}" --filter="name:${REDIS_INSTANCE}" --format="value(name)" 2>/dev/null || echo "")
RUNNING_REDIS_VM=$(gcloud compute instances list --project="${PROJECT_ID}" --filter="name:${REDIS_VM}" --format="value(name)" 2>/dev/null || echo "")
# NOTE: pipe through `grep -c .` (counts non-empty lines only) + tr-strip so a
# trailing newline / CRLF doesn't produce a multiline "0\n0" that breaks -eq.
RUNNING_SERVICES=$(gcloud run services list --region="${REGION}" --project="${PROJECT_ID}" --format="value(metadata.name)" 2>/dev/null | grep -c . | tr -d '[:space:]' || echo "0")
RUNNING_JOBS=$(gcloud run jobs list --region="${REGION}" --project="${PROJECT_ID}" --format="value(metadata.name)" 2>/dev/null | grep -c . | tr -d '[:space:]' || echo "0")
RUNNING_SERVICES=${RUNNING_SERVICES:-0}; RUNNING_JOBS=${RUNNING_JOBS:-0}

[[ -z "$RUNNING_SQL" ]] && echo -e "${GREEN}✅ Cloud SQL: Deleted${NC}" || { echo -e "${RED}❌ Cloud SQL: Still exists${NC}"; CLEAN=false; }
[[ -z "$RUNNING_REDIS_MS" && -z "$RUNNING_REDIS_VM" ]] && echo -e "${GREEN}✅ Redis (both backends): Deleted${NC}" || { echo -e "${RED}❌ Redis: Still exists (Memorystore=${RUNNING_REDIS_MS:-none} VM=${RUNNING_REDIS_VM:-none})${NC}"; CLEAN=false; }
[[ "$RUNNING_SERVICES" -eq 0 ]] && echo -e "${GREEN}✅ Cloud Run services: All deleted${NC}" || { echo -e "${RED}❌ Cloud Run services: $RUNNING_SERVICES still exist${NC}"; CLEAN=false; }
[[ "$RUNNING_JOBS" -eq 0 ]] && echo -e "${GREEN}✅ Cloud Run jobs: All deleted${NC}" || { echo -e "${RED}❌ Cloud Run jobs: $RUNNING_JOBS still exist${NC}"; CLEAN=false; }

# --full: also verify state reset + secrets + SAs + config bucket are gone.
if [[ "$FULL" == true ]]; then
  TF_STATE_OBJECT="gs://${PROJECT_ID}-tf-state/${TF_STATE_PREFIX}/default.tfstate"
  gcloud storage ls "${TF_STATE_OBJECT}" &>/dev/null \
    && { echo -e "${RED}❌ Terraform state: Still present${NC}"; CLEAN=false; } \
    || echo -e "${GREEN}✅ Terraform state: Reset${NC}"

  LEFT_SECRETS=$(gcloud secrets list --project="${PROJECT_ID}" --format="value(name)" 2>/dev/null \
    | grep -Ec '^(tokenlean-backup-|token-opt|llm-key-|routellm-|token-proxy-api-keys$|db-password$|grafana-admin|prometheus-|alertmanager-|langfuse-)' | tr -d '[:space:]' || echo "0")
  LEFT_SECRETS=${LEFT_SECRETS:-0}
  [[ "$LEFT_SECRETS" -eq 0 ]] && echo -e "${GREEN}✅ Secrets: Deleted${NC}" || { echo -e "${RED}❌ Secrets: $LEFT_SECRETS still exist${NC}"; CLEAN=false; }

  LEFT_CONFIG_BKT=$(gcloud storage buckets list --project="${PROJECT_ID}" --format="value(name)" 2>/dev/null | grep -cE '^token-opt-config-' | tr -d '[:space:]' || echo "0")
  LEFT_CONFIG_BKT=${LEFT_CONFIG_BKT:-0}
  [[ "$LEFT_CONFIG_BKT" -eq 0 ]] && echo -e "${GREEN}✅ Config bucket: Deleted${NC}" || { echo -e "${RED}❌ Config bucket: still exists${NC}"; CLEAN=false; }

  LEFT_SA=0
  for sa in "${SERVICE_ACCOUNTS[@]}"; do
    gcloud iam service-accounts describe "${sa}@${PROJECT_ID}.iam.gserviceaccount.com" --project="${PROJECT_ID}" &>/dev/null && LEFT_SA=$((LEFT_SA+1))
  done
  [[ "$LEFT_SA" -eq 0 ]] && echo -e "${GREEN}✅ Service accounts: Deleted${NC}" || { echo -e "${RED}❌ Service accounts: $LEFT_SA still exist${NC}"; CLEAN=false; }
fi

echo ""
if [[ "$CLEAN" == true ]]; then
  echo -e "${GREEN}✔ CLEAN SLATE VERIFIED${NC}$([[ "$FULL" == true ]] && echo ' — ready for a first-run deploy.')"
else
  echo -e "${YELLOW}⚠ Some resources remain (see ❌ above). Re-run, or delete them manually.${NC}"
fi
