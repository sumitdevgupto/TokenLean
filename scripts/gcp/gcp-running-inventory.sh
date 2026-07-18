#!/usr/bin/env bash
# =============================================================================
# gcp-running-inventory.sh — read-only, project-wide inventory of every
#                            cost-bearing GCP resource (across ALL regions/zones)
# =============================================================================
# Answers one question: "What is still deployed, and what is costing me money?"
#
# Unlike check-gcp-status.sh (which probes only the fixed token-opt-* names in a
# single region), this sweeps the WHOLE project across ALL regions/zones and
# groups results by cost behaviour, so nothing an ad-hoc deploy left behind can
# hide. It is READ-ONLY (only *list/describe* calls) — safe to run from any shell
# (WSL, Git Bash, Cloud Shell); no host-shell guard needed, it never mutates GCP.
#
# Cost buckets:
#   💸 BILLS CONTINUOUSLY  — Cloud SQL (RUNNABLE), Compute VMs (RUNNING),
#                            Memorystore Redis, Serverless VPC connectors,
#                            reserved-but-idle external IPs, forwarding rules/LBs,
#                            Cloud NAT/routers
#   😴 SCALE-TO-ZERO       — Cloud Run services (₹0 when idle), Cloud Run jobs
#                            (bill only while a job is executing)
#   📦 STORAGE (small)     — persistent disks, GCS buckets, Artifact Registry,
#                            Secret Manager, KMS keys
#
# Usage:
#   bash scripts/gcp/gcp-running-inventory.sh [--project ID] [--region REGION] [--asset]
#     --project ID     GCP project (default: GCP_PROJECT_ID in .env.gcp, else gcloud config)
#     --region REGION  region for the regional-only checks (VPC connector, NAT)
#                      (default: GCP_REGION in .env.gcp, else asia-south1). Everything
#                      else is swept across ALL regions regardless.
#     --asset          also print a full Cloud Asset Inventory dump (needs the
#                      cloudasset.googleapis.com API; the closest thing to "show me
#                      literally everything")
#
# Exit codes: 0 = nothing billing continuously; 1 = at least one continuous biller found.
# =============================================================================
set -uo pipefail

# ─── Never block on a gcloud prompt ──────────────────────────────────────────
# A disabled API (e.g. vpcaccess / cloudasset) makes gcloud prompt
# "API not enabled. Enable and retry? (y/N)" on stdin — with no visible prompt
# under some terminals the script just appears to HANG there forever. Forcing
# non-interactive mode auto-answers "no", so every list call returns immediately
# (empty) instead of stalling. (Matches teardown-gcp.sh / prepare-gcp-deploy-host.sh.)
export CLOUDSDK_CORE_DISABLE_PROMPTS=1

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
PROJECT=""
REGION=""
ASSET=false

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'

# ─── Load .env.gcp (fallback .env) — SAME project/region source as the deploy +
#     teardown scripts, so this inspects the SAME project the stack lives in. ─────
ENV_FILE="${REPO_ROOT}/.env.gcp"
[[ -f "$ENV_FILE" ]] || ENV_FILE="${REPO_ROOT}/.env"
# Strip CRLF on the fly: Windows-edited env files carry \r, which bash would append
# to every value (GCP_REGION=asia-south1$'\r') and corrupt gcloud args.
[[ -f "$ENV_FILE" ]] && { set -a; source <(tr -d '\r' < "$ENV_FILE"); set +a; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project) PROJECT="$2"; shift 2 ;;
    --region)  REGION="$2";  shift 2 ;;
    --asset)   ASSET=true;   shift ;;
    --help)    sed -n '2,42p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

# Resolve: flag > .env.gcp > gcloud config / default
[[ -z "$PROJECT" ]] && PROJECT="${GCP_PROJECT_ID:-}"
[[ -z "$PROJECT" ]] && PROJECT=$(gcloud config get-value project 2>/dev/null)
[[ -z "$PROJECT" ]] && { echo -e "${RED}No GCP project. Use --project, set GCP_PROJECT_ID in .env.gcp, or: gcloud config set project ID${NC}" >&2; exit 2; }
[[ -z "$REGION" ]]  && REGION="${GCP_REGION:-asia-south1}"

echo ""
echo -e "${BOLD}╔════════════════════════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}║  GCP RUNNING INVENTORY — what is deployed & what costs money        ║${NC}"
echo -e "${BOLD}╚════════════════════════════════════════════════════════════════════╝${NC}"
echo -e "Project: ${BOLD}${PROJECT}${NC}   Regional checks use: ${BOLD}${REGION}${NC}   (all other checks sweep ALL regions)"

BILLERS=()   # continuously-billing resources (the ones that actually cost real money)
MINOR=()     # small ongoing costs (storage / secrets / KMS — cents, kept by design)

# count non-empty lines robustly (trailing newline / CRLF safe)
_count() { grep -c . 2>/dev/null | tr -d '[:space:]'; }

# ─── helper: list a resource; RED + rows if present, GREEN if none ───────────
#   $1 label  $2 "costing"|"idle"  $3.. the gcloud list command (value(name) for the
#   count, then the same command with a table format for display).
# Because different resources need different flags/formats, each section calls
# gcloud directly below rather than through one over-generic wrapper.

echo ""
echo -e "${BOLD}══ 💸 BILLS CONTINUOUSLY (stop these to cut cost) ══${NC}"

# 1. Cloud SQL (all regions). RUNNABLE = billing compute; STOPPED = storage only.
echo -e "\n${BOLD}── Cloud SQL instances ──${NC}"
SQL=$(gcloud sql instances list --project="$PROJECT" \
      --format="value(name,state,region,settings.tier)" 2>/dev/null || echo "")
if [[ -n "$SQL" ]]; then
  gcloud sql instances list --project="$PROJECT" \
    --format="table(name, state, region, settings.tier, settings.activationPolicy)" 2>/dev/null
  while IFS=$'\t' read -r name state region tier; do
    [[ -z "$name" ]] && continue
    if [[ "$state" == "RUNNABLE" ]]; then
      echo -e "  ${RED}❌ ${name}: RUNNABLE — billing compute${NC}"
      BILLERS+=("Cloud SQL '${name}' (RUNNABLE, ${tier:-?}, ${region})")
    else
      echo -e "  ${YELLOW}⚠  ${name}: ${state} — storage only (~\$2/mo)${NC}"
    fi
  done <<< "$SQL"
else
  echo -e "  ${GREEN}✅ none${NC}"
fi

# 2. Compute Engine VMs (ALL zones). RUNNING = billing; TERMINATED = disk only.
echo -e "\n${BOLD}── Compute Engine VMs (all zones) ──${NC}"
VMS=$(gcloud compute instances list --project="$PROJECT" \
      --format="value(name,status,zone,machineType.basename())" 2>/dev/null || echo "")
if [[ -n "$VMS" ]]; then
  gcloud compute instances list --project="$PROJECT" \
    --format="table(name, status, zone, machineType.basename())" 2>/dev/null
  while IFS=$'\t' read -r name status zone mtype; do
    [[ -z "$name" ]] && continue
    if [[ "$status" == "RUNNING" ]]; then
      echo -e "  ${RED}❌ ${name}: RUNNING — billing compute${NC}"
      BILLERS+=("Compute VM '${name}' (RUNNING, ${mtype:-?}, ${zone})")
    else
      echo -e "  ${YELLOW}⚠  ${name}: ${status} — boot disk only (~pennies/mo)${NC}"
    fi
  done <<< "$VMS"
else
  echo -e "  ${GREEN}✅ none${NC}"
fi

# 3. Memorystore Redis (ALL regions). Any instance bills continuously.
echo -e "\n${BOLD}── Memorystore Redis (all regions) ──${NC}"
REDIS=$(gcloud redis instances list --project="$PROJECT" --region=- \
        --format="value(name)" 2>/dev/null || echo "")
if [[ -n "$REDIS" ]]; then
  gcloud redis instances list --project="$PROJECT" --region=- \
    --format="table(name, state, sizeGb, tier, region)" 2>/dev/null
  echo -e "  ${RED}❌ Memorystore present — bills continuously (~\$32/mo+)${NC}"
  BILLERS+=("Memorystore Redis ($(echo "$REDIS" | _count) instance(s))")
else
  echo -e "  ${GREEN}✅ none${NC}"
fi

# 4. Serverless VPC Access connectors (regional — checks the resolved REGION).
#    A connector holds min 2 e2-micro instances → ~\$8/mo+ even when idle.
echo -e "\n${BOLD}── Serverless VPC connectors (region: ${REGION}) ──${NC}"
VPCC=$(gcloud compute networks vpc-access connectors list \
        --region="$REGION" --project="$PROJECT" \
        --format="value(name)" 2>/dev/null || echo "")
if [[ -n "$VPCC" ]]; then
  gcloud compute networks vpc-access connectors list --region="$REGION" --project="$PROJECT" \
    --format="table(name, state, network, minInstances, maxInstances)" 2>/dev/null
  echo -e "  ${RED}❌ VPC connector present — bills continuously (min instances, ~\$8/mo+)${NC}"
  BILLERS+=("Serverless VPC connector ($(echo "$VPCC" | _count) in ${REGION})")
else
  echo -e "  ${GREEN}✅ none in ${REGION}${NC} (add --region to check another)"
fi

# 5. Reserved EXTERNAL IPs (regional + global). RESERVED-but-not-IN_USE = wasted spend.
#    ONLY external addresses bill — INTERNAL reserved ranges (e.g. the Cloud SQL
#    Private Service Access range 'token-opt-sql-psa') are FREE, so exclude them.
echo -e "\n${BOLD}── Reserved external IPs (all regions + global) ──${NC}"
IPS=$(gcloud compute addresses list --project="$PROJECT" \
      --format="value(name,status,address,region,addressType)" 2>/dev/null || echo "")
if [[ -n "$IPS" ]]; then
  gcloud compute addresses list --project="$PROJECT" \
    --format="table(name, status, address, region, addressType)" 2>/dev/null
  # Idle biller = EXTERNAL + RESERVED (not IN_USE). INTERNAL rows are informational (free).
  IDLE_IPS=$(echo "$IPS" | awk -F'\t' '$5=="EXTERNAL" && $2=="RESERVED"' | _count)
  if [[ "${IDLE_IPS:-0}" -gt 0 ]]; then
    echo -e "  ${RED}❌ ${IDLE_IPS} EXTERNAL RESERVED (idle) IP(s) — unattached static IPs bill hourly${NC}"
    BILLERS+=("${IDLE_IPS} idle reserved external IP(s)")
  else
    echo -e "  ${GREEN}✅ no idle external IPs${NC} (any INTERNAL rows above are the free Cloud SQL PSA range)"
  fi
else
  echo -e "  ${GREEN}✅ none${NC}"
fi

# 6. Forwarding rules / load balancers (any = an LB front-end, bills continuously).
echo -e "\n${BOLD}── Forwarding rules / load balancers (all regions + global) ──${NC}"
FWD=$(gcloud compute forwarding-rules list --project="$PROJECT" \
      --format="value(name)" 2>/dev/null || echo "")
if [[ -n "$FWD" ]]; then
  gcloud compute forwarding-rules list --project="$PROJECT" \
    --format="table(name, IPAddress, target, region)" 2>/dev/null
  echo -e "  ${RED}❌ forwarding rule(s) present — load balancer front-ends bill continuously${NC}"
  BILLERS+=("$(echo "$FWD" | _count) forwarding rule(s)/LB")
else
  echo -e "  ${GREEN}✅ none${NC}"
fi

# 7. Cloud NAT / routers (Cloud NAT bills per-gateway + data).
echo -e "\n${BOLD}── Cloud Routers / NAT (region: ${REGION}) ──${NC}"
ROUTERS=$(gcloud compute routers list --project="$PROJECT" --regions="$REGION" \
          --format="value(name)" 2>/dev/null || echo "")
if [[ -n "$ROUTERS" ]]; then
  gcloud compute routers list --project="$PROJECT" --regions="$REGION" \
    --format="table(name, region, network)" 2>/dev/null
  echo -e "  ${YELLOW}⚠  router(s) present — Cloud NAT (if configured) bills per-gateway + egress${NC}"
else
  echo -e "  ${GREEN}✅ none in ${REGION}${NC}"
fi

echo ""
echo -e "${BOLD}══ 😴 SCALE-TO-ZERO (deployed, but ₹0 while idle) ══${NC}"

# 8. Cloud Run services (ALL regions). Deployed = listed; bills only while serving.
echo -e "\n${BOLD}── Cloud Run services (all regions) ──${NC}"
RUN_SVC=$(gcloud run services list --project="$PROJECT" --region=- \
          --format="value(metadata.name)" 2>/dev/null || echo "")
if [[ -n "$RUN_SVC" ]]; then
  gcloud run services list --project="$PROJECT" --region=- \
    --format="table(metadata.name, region, status.url)" 2>/dev/null
  echo -e "  ${BLUE}ℹ️  $(echo "$RUN_SVC" | _count) service(s) deployed — ₹0 when idle (auto scale-to-zero)${NC}"
  echo -e "     ${YELLOW}(only removed by teardown-gcp.sh; scaling to zero is automatic)${NC}"
else
  echo -e "  ${GREEN}✅ none${NC}"
fi

# 9. Cloud Run jobs (ALL regions). Bill only during an execution.
echo -e "\n${BOLD}── Cloud Run jobs (all regions) ──${NC}"
RUN_JOB=$(gcloud run jobs list --project="$PROJECT" --region=- \
          --format="value(metadata.name)" 2>/dev/null || echo "")
if [[ -n "$RUN_JOB" ]]; then
  gcloud run jobs list --project="$PROJECT" --region=- \
    --format="table(metadata.name, region)" 2>/dev/null
  echo -e "  ${BLUE}ℹ️  $(echo "$RUN_JOB" | _count) job(s) defined — bill only while executing${NC}"
else
  echo -e "  ${GREEN}✅ none${NC}"
fi

echo ""
echo -e "${BOLD}══ 📦 STORAGE / MISC (small ongoing cost) ══${NC}"

# 10. Persistent disks (ALL zones) — billed even when the VM is stopped/deleted.
echo -e "\n${BOLD}── Persistent disks (all zones) ──${NC}"
DISKS=$(gcloud compute disks list --project="$PROJECT" \
        --format="value(name)" 2>/dev/null || echo "")
if [[ -n "$DISKS" ]]; then
  gcloud compute disks list --project="$PROJECT" \
    --format="table(name, sizeGb, type.basename(), zone.basename(), users.basename())" 2>/dev/null
  echo -e "  ${YELLOW}⚠  $(echo "$DISKS" | _count) disk(s) — billed by size even if unattached${NC}"
  MINOR+=("$(echo "$DISKS" | _count) persistent disk(s) — billed by GB")
else
  echo -e "  ${GREEN}✅ none${NC}"
fi

# 11. GCS buckets (project-wide).
echo -e "\n${BOLD}── GCS buckets ──${NC}"
BKTS=$(gcloud storage buckets list --project="$PROJECT" \
       --format="value(name)" 2>/dev/null || echo "")
if [[ -n "$BKTS" ]]; then
  echo "$BKTS" | sed 's/^/    • /'
  echo -e "  ${YELLOW}⚠  $(echo "$BKTS" | _count) bucket(s) — storage billed by GB (tf-state + config kept by design)${NC}"
  MINOR+=("$(echo "$BKTS" | _count) GCS bucket(s) — storage billed by GB")
else
  echo -e "  ${GREEN}✅ none${NC}"
fi

# 12. Artifact Registry repos (Docker images — storage cost).
echo -e "\n${BOLD}── Artifact Registry repos ──${NC}"
AR=$(gcloud artifacts repositories list --project="$PROJECT" \
     --format="value(name)" 2>/dev/null || echo "")
if [[ -n "$AR" ]]; then
  gcloud artifacts repositories list --project="$PROJECT" \
    --format="table(name, format, location, sizeBytes)" 2>/dev/null
  echo -e "  ${YELLOW}⚠  image storage billed by GB (removed only by teardown --full / --delete-images)${NC}"
  MINOR+=("Artifact Registry image storage")
else
  echo -e "  ${GREEN}✅ none${NC}"
fi

# 13. Secret Manager secrets (tiny cost per active secret version).
echo -e "\n${BOLD}── Secret Manager secrets ──${NC}"
SECRETS=$(gcloud secrets list --project="$PROJECT" --format="value(name)" 2>/dev/null | _count)
SECRETS=${SECRETS:-0}
if [[ "$SECRETS" -gt 0 ]]; then
  echo -e "  ${YELLOW}⚠  ${SECRETS} secret(s) — ~\$0.06/active version/mo (kept unless teardown --full)${NC}"
  MINOR+=("${SECRETS} Secret Manager secret(s) — ~\$0.06/version/mo")
else
  echo -e "  ${GREEN}✅ none${NC}"
fi

# 14. KMS key rings (BYOK master key — prevent_destroy, kept by design).
echo -e "\n${BOLD}── KMS key rings (region: ${REGION}) ──${NC}"
if gcloud services list --enabled --project="$PROJECT" --format="value(config.name)" 2>/dev/null | grep -q '^cloudkms.googleapis.com$'; then
  KR=$(gcloud kms keyrings list --location="$REGION" --project="$PROJECT" \
       --format="value(name)" 2>/dev/null || echo "")
  if [[ -n "$KR" ]]; then
    echo "$KR" | sed 's|.*/keyRings/|    • |'
    echo -e "  ${YELLOW}⚠  BYOK key ring present — ~\$0.06/key-version/mo; KEPT on purpose (prevent_destroy)${NC}"
    MINOR+=("BYOK KMS key ring — ~\$0.06/key-version/mo (prevent_destroy)")
  else
    echo -e "  ${GREEN}✅ none in ${REGION}${NC}"
  fi
else
  echo -e "  ${GREEN}✅ Cloud KMS API not enabled — no keys${NC}"
fi

# ─── Optional: full Cloud Asset Inventory dump ───────────────────────────────
if [[ "$ASSET" == true ]]; then
  echo ""
  echo -e "${BOLD}══ 🗂  FULL CLOUD ASSET INVENTORY (every resource) ══${NC}"
  if ! gcloud asset search-all-resources --scope="projects/${PROJECT}" \
        --format="table(assetType, displayName, location)" 2>/dev/null; then
    echo -e "  ${YELLOW}⚠  cloudasset API not enabled. Enable once:${NC}"
    echo -e "     gcloud services enable cloudasset.googleapis.com --project=${PROJECT}"
  fi
fi

# ─── Verdict / cost summary ──────────────────────────────────────────────────
echo ""
echo -e "${BOLD}════════════════════════════════════════════════════════════════════${NC}"
echo -e "${BOLD}  COST SUMMARY — what is incurring cost${NC}"
echo -e "${BOLD}════════════════════════════════════════════════════════════════════${NC}"

# Tier 1: continuous billers (real money).
if [[ ${#BILLERS[@]} -gt 0 ]]; then
  echo -e "${RED}${BOLD}💸 BILLING CONTINUOUSLY (real cost — stop these):${NC}"
  for b in "${BILLERS[@]}"; do echo -e "   ${RED}•${NC} ${b}"; done
else
  echo -e "${GREEN}${BOLD}✅ BILLING CONTINUOUSLY: nothing.${NC}"
fi

# Tier 2: small ongoing costs (cents — storage/secrets/KMS, kept by design).
echo ""
if [[ ${#MINOR[@]} -gt 0 ]]; then
  echo -e "${YELLOW}${BOLD}📦 SMALL ONGOING COST (cents/mo — kept by design):${NC}"
  for m in "${MINOR[@]}"; do echo -e "   ${YELLOW}•${NC} ${m}"; done
  echo -e "   ${BLUE}→ removed only by:${NC} ${BOLD}teardown-gcp.sh --full${NC} (KMS key always kept: prevent_destroy)"
else
  echo -e "${GREEN}${BOLD}📦 SMALL ONGOING COST: none.${NC}"
fi

# Bottom line + next action.
echo ""
if [[ ${#BILLERS[@]} -eq 0 ]]; then
  echo -e "${GREEN}${BOLD}➤ Bottom line: no meaningful spend.${NC} Any Cloud Run services are ₹0 while idle; only cents of storage/KMS remain."
  RC=0
else
  echo -e "${RED}${BOLD}➤ Bottom line: real money is being spent (see 💸 above).${NC}"
  echo -e "   To pause (keep the deploy):   ${BOLD}bash scripts/gcp/stop-gcp.sh${NC}"
  echo -e "   To delete everything:         ${BOLD}bash scripts/gcp/teardown-gcp.sh --full${NC}"
  echo -e "   ${YELLOW}(both must run from WSL / Linux / Cloud Shell)${NC}"
  RC=1
fi
echo -e "${BOLD}════════════════════════════════════════════════════════════════════${NC}"
exit $RC
