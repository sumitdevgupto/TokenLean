#!/usr/bin/env bash
# =============================================================================
# prepare-gcp-deploy-host.sh — one-shot WSL/Linux prep for the GCP deploy.
#                              Does NOT deploy.
# =============================================================================
# Run this ONCE (idempotent — safe to re-run) inside WSL Ubuntu / a Linux host
# before `scripts/gcp/gcp-deploy.sh`. It installs + verifies every prerequisite
# the deploy needs, then prints a clear ALL-OK / NOT-READY verdict and the next
# command. It NEVER touches GCP resources (only installs local tooling + runs the
# read-only pre-deploy check).
#
# WHY a Linux host: gcp-deploy.sh runs Terraform `local-exec` schema migrations
# that are bash + `psql` and tunnel to Cloud SQL via the Cloud SQL Auth Proxy
# (cloud-sql-proxy). Windows Git Bash / cmd.exe cannot run them (Terraform
# launches local-exec via cmd.exe there, and Windows lacks psql). Use WSL Ubuntu
# (repo visible at /mnt/<drive>/...) or GCP Cloud Shell.
#
# What it does:
#   1. Confirms it's on Linux/WSL (not Git Bash / cmd) and in the repo root
#   2. Installs (if missing): psql, python3, cloud-sql-proxy, gcloud, terraform
#   3. Verifies Docker is reachable (Docker Desktop WSL integration)
#   4. Ensures gcloud auth + ADC + project — DRIVES the interactive login for you
#      (launches `gcloud auth login` / `application-default login` when missing), so a
#      SINGLE run can reach ✅ ALL OK. The login itself is browser-based (you approve it).
#   5. Confirms the config files exist (terraform.tfvars, keys.yaml, .env.gcp) + CRLF-fixes env files
#   6. Runs the read-only ./scripts/gcp/pre-deploy-check.sh
#   → prints ✅ ALL OK + the next command, or ❌ with the exact fix.
#
# The login step is interactive by design (browser / device-code) — no script can do it
# headless. On a non-TTY (CI/piped) or with --no-auth, the script does NOT drive the login;
# it only flags what's missing and leaves it to the real deploy.
#
# Usage:
#   bash scripts/gcp/prepare-gcp-deploy-host.sh              # install + drive login + verify → ✅ ALL OK
#   bash scripts/gcp/prepare-gcp-deploy-host.sh --no-install # verify only, install nothing
#   bash scripts/gcp/prepare-gcp-deploy-host.sh --no-auth    # don't drive login; only flag if missing
#
# Exit codes: 0 = ALL OK (ready to deploy); 1 = one or more blockers remain.
# =============================================================================
set -uo pipefail   # NOT -e: we tally failures and print a full report, not abort on first.

export CLOUDSDK_CORE_DISABLE_PROMPTS=1   # never let a gcloud API-enable prompt block us

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
cd "$REPO_ROOT"

CSP_VERSION="v2.11.0"   # cloud-sql-proxy release to install if missing
NO_INSTALL=false
NO_AUTH=false           # --no-auth → don't drive the interactive gcloud login; only flag if missing (CI/non-TTY)
while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-install) NO_INSTALL=true; shift ;;
    --no-auth)    NO_AUTH=true; shift ;;
    --help) sed -n '2,39p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1"; exit 2 ;;
  esac
done

# Interactive login only makes sense on a real terminal. Auto-disable auth-driving when
# stdin isn't a TTY (CI, piped) — there it degrades to flag-only, same as --no-auth.
[[ -t 0 ]] || NO_AUTH=true

# ─── Pretty output ────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'
info() { echo -e "${BLUE}[PREP]${NC} $*"; }
ok()   { echo -e "  ${GREEN}✅${NC} $*"; }
bad()  { echo -e "  ${RED}❌${NC} $*"; }
warn() { echo -e "  ${YELLOW}⚠${NC}  $*"; }

FAILURES=()   # human-readable "what to do" lines for anything not ready
fail() { FAILURES+=("$1"); }

echo -e "${BOLD}"
echo "╔══════════════════════════════════════════════════════════════════╗"
echo "║   WSL/Linux PREP — GCP deploy prerequisites (steps 1–6)          ║"
echo "╚══════════════════════════════════════════════════════════════════╝"
echo -e "${NC}"
info "Repo: ${REPO_ROOT}"
[[ "$NO_INSTALL" == true ]] && info "Mode: --no-install (verify only)"

# Load .env.gcp so we know the target project/region (and can pass them explicitly).
GCP_PROJECT_ID=""; GCP_REGION=""
if [[ -f "${REPO_ROOT}/.env.gcp" ]]; then
  set -a; # shellcheck disable=SC1091
  source "${REPO_ROOT}/.env.gcp" 2>/dev/null || true
  set +a
fi
PROJECT_ID="${GCP_PROJECT_ID:-token-optimisation}"
REGION="${GCP_REGION:-asia-south1}"

# ─── Helper: is a command on PATH? ───────────────────────────────────────────
have() { command -v "$1" &>/dev/null; }

# =============================================================================
echo ""; echo -e "${BOLD}─── 1. Environment (Linux/WSL, not Git Bash) ───${NC}"
# =============================================================================
UNAME="$(uname -s 2>/dev/null || echo unknown)"
if [[ "$UNAME" == "Linux" ]]; then
  if grep -qiE 'microsoft|wsl' /proc/version 2>/dev/null; then
    ok "Running under WSL (Linux) — correct host for this deploy"
  else
    ok "Running under Linux — fine (native Linux or a Linux runner)"
  fi
else
  bad "Not Linux (uname=${UNAME}). This deploy needs WSL/Linux — Git Bash/cmd cannot run it."
  fail "Open WSL Ubuntu first:  wsl -d Ubuntu   then re-run this from /mnt/d/token-optimisation"
fi

# apt present? (needed for the auto-install path)
APT=false
have apt-get && APT=true
[[ "$APT" == false && "$NO_INSTALL" == false ]] && warn "apt-get not found — auto-install disabled; will only verify."

# sudo available (installs need it)
SUDO=""
if have sudo; then SUDO="sudo"; fi

# =============================================================================
echo ""; echo -e "${BOLD}─── 2. Required CLI tools ───${NC}"
# =============================================================================

# psql (postgresql-client) — migrations run through it
if have psql; then
  ok "psql present ($(psql --version 2>/dev/null | head -1))"
elif [[ "$NO_INSTALL" == false && "$APT" == true ]]; then
  info "Installing postgresql-client (psql)…"
  $SUDO apt-get update -qq && $SUDO apt-get install -y -qq postgresql-client \
    && ok "psql installed" || { bad "psql install failed"; fail "Install psql:  sudo apt-get install -y postgresql-client"; }
else
  bad "psql missing"; fail "Install psql:  sudo apt-get install -y postgresql-client"
fi

# python3 — used by some deploy helpers
if have python3; then
  ok "python3 present ($(python3 --version 2>/dev/null))"
elif [[ "$NO_INSTALL" == false && "$APT" == true ]]; then
  info "Installing python3…"
  $SUDO apt-get install -y -qq python3 && ok "python3 installed" \
    || { bad "python3 install failed"; fail "Install python3:  sudo apt-get install -y python3"; }
else
  bad "python3 missing"; fail "Install python3:  sudo apt-get install -y python3"
fi

# cloud-sql-proxy — the Auth Proxy that reaches the private-IP Cloud SQL for migrations
if have cloud-sql-proxy; then
  ok "cloud-sql-proxy present ($(cloud-sql-proxy --version 2>/dev/null | head -1))"
elif [[ "$NO_INSTALL" == false ]]; then
  info "Installing cloud-sql-proxy ${CSP_VERSION}…"
  _csp="/tmp/cloud-sql-proxy.$$"
  if curl -fsSL -o "$_csp" "https://storage.googleapis.com/cloud-sql-connectors/cloud-sql-proxy/${CSP_VERSION}/cloud-sql-proxy.linux.amd64"; then
    chmod +x "$_csp" && $SUDO mv "$_csp" /usr/local/bin/cloud-sql-proxy \
      && ok "cloud-sql-proxy installed" \
      || { bad "cloud-sql-proxy move failed"; fail "Move cloud-sql-proxy to /usr/local/bin (needs sudo)"; }
  else
    bad "cloud-sql-proxy download failed"
    fail "Install cloud-sql-proxy: curl -o cloud-sql-proxy https://storage.googleapis.com/cloud-sql-connectors/cloud-sql-proxy/${CSP_VERSION}/cloud-sql-proxy.linux.amd64 && chmod +x cloud-sql-proxy && sudo mv cloud-sql-proxy /usr/local/bin/"
  fi
else
  bad "cloud-sql-proxy missing"
  fail "Install cloud-sql-proxy (see https://cloud.google.com/sql/docs/postgres/sql-proxy#install)"
fi

# gcloud — must be a WORKING Linux gcloud, not the Windows one leaking in via /mnt/c.
# A WSL PATH usually includes the Windows Google Cloud SDK (/mnt/c/.../google-cloud-sdk),
# which fails under WSL's Python ("running gcloud with Python 3.8, no longer supported").
# So a bare `have gcloud` is NOT enough — we require: (a) it's not the /mnt/c Windows one,
# and (b) `gcloud --version` actually succeeds.
install_linux_gcloud() {
  info "Installing native Linux google-cloud-cli (bundles its own compatible Python)…"
  $SUDO apt-get install -y -qq apt-transport-https ca-certificates gnupg curl >/dev/null 2>&1
  curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg 2>/dev/null | $SUDO gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg 2>/dev/null
  echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" | $SUDO tee /etc/apt/sources.list.d/google-cloud-sdk.list >/dev/null
  $SUDO apt-get update -qq >/dev/null 2>&1
  $SUDO apt-get install -y -qq google-cloud-cli >/dev/null 2>&1
  hash -r 2>/dev/null || true
}

_gcloud_path="$(command -v gcloud 2>/dev/null || true)"
_gcloud_is_windows=false; [[ "$_gcloud_path" == /mnt/c/* || "$_gcloud_path" == /mnt/*/Program\ Files* ]] && _gcloud_is_windows=true
_gcloud_works=false; [[ -n "$_gcloud_path" ]] && gcloud --version &>/dev/null && _gcloud_works=true

if [[ "$_gcloud_works" == true && "$_gcloud_is_windows" == false ]]; then
  ok "gcloud present + working ($(gcloud version 2>/dev/null | head -1))"
else
  # Explain what's wrong before acting.
  if [[ "$_gcloud_is_windows" == true ]]; then
    warn "gcloud on PATH is the WINDOWS SDK (${_gcloud_path}) — it fails under WSL's Python. Installing a native Linux gcloud to shadow it."
  elif [[ -n "$_gcloud_path" && "$_gcloud_works" == false ]]; then
    warn "gcloud found (${_gcloud_path}) but 'gcloud --version' fails (usually WSL Python too old for the Windows SDK). Installing a native Linux gcloud."
  fi

  if [[ "$NO_INSTALL" == false && "$APT" == true ]]; then
    install_linux_gcloud
    # After install, prefer /usr/bin/gcloud. If PATH still resolves the Windows one first,
    # tell the user the exact one-line PATH fix (we can't edit their shell rc for them safely).
    if /usr/bin/gcloud --version &>/dev/null; then
      _resolved="$(command -v gcloud 2>/dev/null || true)"
      if [[ "$_resolved" == "/usr/bin/gcloud" ]]; then
        ok "Linux gcloud installed + first on PATH ($(/usr/bin/gcloud version 2>/dev/null | head -1))"
      else
        warn "Linux gcloud installed at /usr/bin/gcloud, but PATH still resolves '${_resolved}' first."
        fail "Put Linux gcloud ahead of the Windows one — add to ~/.bashrc then reopen the shell:  echo 'export PATH=/usr/bin:\$PATH' >> ~/.bashrc && source ~/.bashrc   (then re-run this script)"
      fi
    else
      bad "Linux gcloud install did not produce a working /usr/bin/gcloud"
      fail "Install gcloud manually: https://cloud.google.com/sdk/docs/install#deb"
    fi
  else
    bad "no working Linux gcloud (${_gcloud_path:-not found})"
    fail "Install Linux gcloud: https://cloud.google.com/sdk/docs/install#deb (the Windows SDK on /mnt/c won't work in WSL)"
  fi
fi

# Resolve the gcloud binary the rest of this script (and the deploy) should use: prefer a
# working /usr/bin/gcloud, else whatever `gcloud` resolves to IF it actually runs, else empty.
GCLOUD=""
if /usr/bin/gcloud --version &>/dev/null; then
  GCLOUD="/usr/bin/gcloud"
elif command -v gcloud &>/dev/null && gcloud --version &>/dev/null; then
  GCLOUD="$(command -v gcloud)"
fi

# terraform — via HashiCorp apt repo if missing
if have terraform; then
  ok "terraform present ($(terraform version 2>/dev/null | head -1))"
elif [[ "$NO_INSTALL" == false && "$APT" == true ]]; then
  info "Installing terraform via HashiCorp apt repo…"
  # Try the HashiCorp apt repo first.
  $SUDO install -m 0755 -d /usr/share/keyrings 2>/dev/null
  wget -qO - https://apt.releases.hashicorp.com/gpg 2>/dev/null | $SUDO gpg --dearmor -o /usr/share/keyrings/hashicorp-archive-keyring.gpg 2>/dev/null
  _codename="$(lsb_release -cs 2>/dev/null || echo '')"
  echo "deb [signed-by=/usr/share/keyrings/hashicorp-archive-keyring.gpg] https://apt.releases.hashicorp.com ${_codename} main" | $SUDO tee /etc/apt/sources.list.d/hashicorp.list >/dev/null
  $SUDO apt-get update -qq >/dev/null 2>&1
  if $SUDO apt-get install -y -qq terraform >/dev/null 2>&1 && have terraform; then
    ok "terraform installed (apt)"
  else
    # Fallback: direct binary download (works even if the apt repo has no build for this
    # release codename — the common cause of `E: Unable to locate package terraform`).
    warn "apt repo install failed — falling back to direct binary download…"
    _tfver="1.9.8"
    if curl -fsSL -o /tmp/terraform.zip "https://releases.hashicorp.com/terraform/${_tfver}/terraform_${_tfver}_linux_amd64.zip" 2>/dev/null; then
      ( command -v unzip &>/dev/null || $SUDO apt-get install -y -qq unzip >/dev/null 2>&1 )
      if unzip -o -q /tmp/terraform.zip -d /tmp && $SUDO mv /tmp/terraform /usr/local/bin/terraform && $SUDO chmod +x /usr/local/bin/terraform; then
        rm -f /tmp/terraform.zip
        have terraform && ok "terraform installed (binary ${_tfver})" \
          || { bad "terraform still not on PATH after binary install"; fail "Install terraform manually: https://developer.hashicorp.com/terraform/install#linux"; }
      else
        bad "terraform binary unzip/move failed"; fail "Install terraform manually: https://developer.hashicorp.com/terraform/install#linux"
      fi
    else
      bad "terraform binary download failed"; fail "Install terraform manually: https://developer.hashicorp.com/terraform/install#linux"
    fi
  fi
else
  bad "terraform missing"; fail "Install terraform: https://developer.hashicorp.com/terraform/install#linux"
fi

# =============================================================================
echo ""; echo -e "${BOLD}─── 3. Docker (Docker Desktop WSL integration) ───${NC}"
# =============================================================================
if have docker; then
  if docker ps &>/dev/null; then
    ok "docker reachable ($(docker --version 2>/dev/null))"
  else
    bad "docker command present but daemon not reachable"
    fail "Enable Docker Desktop → Settings → Resources → WSL Integration → toggle Ubuntu ON, then verify: docker ps"
  fi
else
  bad "docker not found in WSL"
  fail "Enable Docker Desktop → Settings → Resources → WSL Integration → toggle Ubuntu ON (the deploy builds images)"
fi

# =============================================================================
echo ""; echo -e "${BOLD}─── 4. GCP auth + Application Default Credentials ───${NC}"
# =============================================================================
# Use the resolved working gcloud ($GCLOUD) — NOT a bare `gcloud`, which may be the broken
# Windows SDK on PATH. If no working gcloud, these checks can't run; the §2 install step
# already recorded the fix, so we just note it and move on.
# By default this section DRIVES the interactive login when something's missing (so a single
# run can reach ✅ ALL OK). --no-auth (or a non-TTY) degrades to flag-only: report the exact
# command and let the real deploy surface it. Login is inherently interactive (browser/URL) —
# no script can do it headless.
if [[ -n "$GCLOUD" ]]; then
  # ── gcloud account login ──
  ACTIVE_ACCT="$("$GCLOUD" auth list --filter=status:ACTIVE --format='value(account)' 2>/dev/null | head -1)"
  if [[ -z "$ACTIVE_ACCT" && "$NO_AUTH" == false ]]; then
    info "Not logged in — launching 'gcloud auth login' (approve in your browser)…"
    "$GCLOUD" auth login || true
    ACTIVE_ACCT="$("$GCLOUD" auth list --filter=status:ACTIVE --format='value(account)' 2>/dev/null | head -1)"
  fi
  if [[ -n "$ACTIVE_ACCT" ]]; then
    ok "gcloud authenticated as ${ACTIVE_ACCT}"
  else
    bad "gcloud not authenticated"
    fail "Authenticate (interactive):  gcloud auth login"
  fi

  # ── Application Default Credentials (the Auth Proxy uses these) ──
  if ! "$GCLOUD" auth application-default print-access-token &>/dev/null && [[ "$NO_AUTH" == false ]]; then
    info "ADC not set — launching 'gcloud auth application-default login' (approve in your browser)…"
    "$GCLOUD" auth application-default login || true
  fi
  if "$GCLOUD" auth application-default print-access-token &>/dev/null; then
    ok "Application Default Credentials (ADC) present — Auth Proxy can authenticate"
  else
    bad "ADC not set (cloud-sql-proxy needs it)"
    fail "Set ADC (interactive):  gcloud auth application-default login"
  fi

  # ── Project pinned (non-interactive — just set it) ──
  CUR_PROJECT="$("$GCLOUD" config get-value project 2>/dev/null)"
  if [[ "$CUR_PROJECT" != "$PROJECT_ID" && "$NO_AUTH" == false ]]; then
    info "Setting gcloud project → ${PROJECT_ID}…"
    "$GCLOUD" config set project "$PROJECT_ID" >/dev/null 2>&1 || true
    CUR_PROJECT="$("$GCLOUD" config get-value project 2>/dev/null)"
  fi
  if [[ "$CUR_PROJECT" == "$PROJECT_ID" ]]; then
    ok "gcloud project set to ${PROJECT_ID}"
  elif [[ -n "$CUR_PROJECT" ]]; then
    warn "gcloud project is '${CUR_PROJECT}', expected '${PROJECT_ID}' (from .env.gcp)"
    fail "Set project:  gcloud config set project ${PROJECT_ID}"
  else
    bad "gcloud project not set"
    fail "Set project:  gcloud config set project ${PROJECT_ID}"
  fi
else
  bad "no working gcloud — auth checks skipped"
  fail "Resolve gcloud first (§2 above), then re-run — auth needs a working Linux gcloud"
fi

# =============================================================================
echo ""; echo -e "${BOLD}─── 5. Required config files (+ CRLF auto-fix) ───${NC}"
# =============================================================================
# CRLF auto-fix: env files edited on Windows carry \r, which breaks bash `source`
# with "$'\r': command not found". Strip \r in place from the shell-sourced files
# (idempotent — a no-op once clean). Only touches env files, not YAML/tfvars.
for f in .env .env.gcp; do
  if [[ -f "${REPO_ROOT}/${f}" ]] && grep -q $'\r' "${REPO_ROOT}/${f}" 2>/dev/null; then
    if tr -d '\r' < "${REPO_ROOT}/${f}" > "${REPO_ROOT}/${f}.tmp" && mv "${REPO_ROOT}/${f}.tmp" "${REPO_ROOT}/${f}"; then
      ok "${f} — stripped Windows CRLF line endings (would break bash source)"
    else
      warn "${f} has CRLF but auto-fix failed — run:  sed -i 's/\\r\$//' ${f}"
    fi
  fi
done

for f in infra/terraform.tfvars config/keys.yaml .env.gcp; do
  if [[ -f "${REPO_ROOT}/${f}" ]]; then
    ok "${f} present"
  else
    bad "${f} MISSING"
    fail "Create/restore ${f} (gitignored secret/config file — copy from your source of truth)"
  fi
done
# config.yaml is optional (deploy generates from template) — informational only.
if [[ -f "${REPO_ROOT}/config/config.yaml" ]]; then
  ok "config/config.yaml present"
else
  warn "config/config.yaml absent — deploy will generate it from the template (fine)"
fi

# =============================================================================
echo ""; echo -e "${BOLD}─── 6. Pre-deploy check (read-only: auth, billing, IAM, APIs, files) ───${NC}"
# =============================================================================
if [[ -x "${REPO_ROOT}/scripts/gcp/pre-deploy-check.sh" ]] || [[ -f "${REPO_ROOT}/scripts/gcp/pre-deploy-check.sh" ]]; then
  if bash "${REPO_ROOT}/scripts/gcp/pre-deploy-check.sh" --project "$PROJECT_ID" --region "$REGION"; then
    ok "pre-deploy-check passed (VALIDATION PASSED)"
  else
    bad "pre-deploy-check reported errors (see its output above)"
    fail "Fix the pre-deploy-check ERRORs above, then re-run this script"
  fi
else
  bad "scripts/gcp/pre-deploy-check.sh not found"
  fail "Ensure you are in the repo root (${REPO_ROOT}) with the full tree checked out"
fi

# =============================================================================
echo ""; echo -e "${BOLD}════════════════════════════════════════════════════════════════════${NC}"
# =============================================================================
if [[ ${#FAILURES[@]} -eq 0 ]]; then
  echo -e "${GREEN}${BOLD}✅ ALL OK — prerequisites satisfied. Ready to deploy.${NC}"
  echo ""
  echo -e "  Next, run the GCP deploy from this Linux/WSL shell:"
  echo -e "    ${BOLD}bash scripts/gcp/gcp-deploy.sh --project ${PROJECT_ID} --region ${REGION}${NC}"
  echo ""
  echo -e "  (A re-run resumes from existing Terraform state — no full rebuild.)"
  exit 0
else
  echo -e "${RED}${BOLD}❌ NOT READY — ${#FAILURES[@]} item(s) need attention:${NC}"
  echo ""
  for f in "${FAILURES[@]}"; do echo -e "  ${RED}•${NC} ${f}"; done
  echo ""
  echo -e "  Fix the above, then re-run:  ${BOLD}bash scripts/gcp/prepare-gcp-deploy-host.sh${NC}"
  exit 1
fi
