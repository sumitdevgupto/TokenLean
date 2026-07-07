#!/usr/bin/env bash
# =============================================================================
# issue-key.sh — Issue or revoke proxy API keys stored in Secret Manager
# =============================================================================
# Usage:
#   ./scripts/issue-key.sh issue  --user <user_id>  [--project ID] [--region REGION]
#   ./scripts/issue-key.sh revoke --user <user_id>  [--project ID]
#   ./scripts/issue-key.sh list                     [--project ID]
#
# The proxy key secret in Secret Manager is a JSON object mapping the SHA-256 of
# each raw key to its tenant metadata (new format):
#   { "<sha256_of_key>": {"tenant_id": "...", "tier": "...", "admin": true|absent}, ... }
# Legacy string entries ({ "<hash>": "<user_id>" }) are still accepted by the
# proxy but resolve to the "default" tenant — always issue new-format keys.
#
# Developers receive ONLY the raw key — never the LLM provider keys.
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()    { echo -e "${BLUE}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

COMMAND=""
USER_ID=""
TENANT_ID=""
TIER="free"
ADMIN="false"
PROJECT_ID=""
SECRET_NAME="${PROXY_KEYS_SECRET_NAME:-token-proxy-api-keys}"

# ─── Load .env if present ─────────────────────────────────────────────────────
ENV_FILE="${REPO_ROOT}/.env"
if [[ -f "$ENV_FILE" ]]; then
  set -a; source "$ENV_FILE"; set +a
fi

# ─── Argument parsing ─────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    issue|revoke|list) COMMAND="$1"; shift ;;
    --user)    USER_ID="$2";    shift 2 ;;
    --tenant)  TENANT_ID="$2";  shift 2 ;;
    --tier)    TIER="$2";       shift 2 ;;
    --admin)   ADMIN="true";    shift ;;
    --project) PROJECT_ID="$2"; shift 2 ;;
    --secret)  SECRET_NAME="$2"; shift 2 ;;
    --help)
      sed -n '/^# Usage:/,/^# ===/p' "$0" | head -10
      exit 0 ;;
    *) error "Unknown option: $1" ;;
  esac
done

[[ -z "$COMMAND" ]] && error "Command required: issue | revoke | list"

# ─── Resolve project ──────────────────────────────────────────────────────────
if [[ -z "$PROJECT_ID" ]]; then
  PROJECT_ID="${GCP_PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
fi
[[ -z "$PROJECT_ID" ]] && error "No GCP project set. Use --project or: gcloud config set project PROJECT_ID"

# ─── Helpers ──────────────────────────────────────────────────────────────────
fetch_keys_json() {
  gcloud secrets versions access latest \
    --secret="$SECRET_NAME" --project="$PROJECT_ID" 2>/dev/null || echo "{}"
}

store_keys_json() {
  local json="$1"
  if gcloud secrets describe "$SECRET_NAME" --project="$PROJECT_ID" &>/dev/null; then
    echo -n "$json" | gcloud secrets versions add "$SECRET_NAME" \
      --data-file=- --project="$PROJECT_ID" &>/dev/null
  else
    echo -n "$json" | gcloud secrets create "$SECRET_NAME" \
      --project="$PROJECT_ID" --replication-policy=automatic --data-file=- &>/dev/null
  fi
}

# ─── Commands ─────────────────────────────────────────────────────────────────
cmd_issue() {
  # --tenant is preferred; --user is accepted as an alias for the tenant id so
  # existing callers (e.g. gcp-deploy.sh `issue --user admin`) keep working.
  local tenant="${TENANT_ID:-$USER_ID}"
  [[ -z "$tenant" ]] && error "--tenant (or --user) is required for issue"

  # Pricing tier is bound to the key and flows into usage_events.pricing_tier →
  # invoicing. Two tiers only — free (self-host / $0 floor) or enterprise (managed SaaS).
  # Reject typos here so a bad tier never silently bills at the wrong card.
  case "$TIER" in
    free|enterprise) ;;
    *) error "--tier must be free|enterprise (got '${TIER}')" ;;
  esac

  local raw_key
  raw_key="tok-$(openssl rand -hex 24)"
  local key_hash
  key_hash=$(echo -n "$raw_key" | sha256sum | awk '{print $1}')

  local existing_json
  existing_json=$(fetch_keys_json)

  # Warn if this tenant already has a key (new-format dict or legacy string).
  if echo "$existing_json" | TENANT="$tenant" python3 -c "
import os, sys, json
d = json.load(sys.stdin)
t = os.environ['TENANT']
def owner(v): return v.get('tenant_id') if isinstance(v, dict) else v
print(any(owner(v) == t for v in d.values()))" 2>/dev/null | grep -q "True"; then
    warn "Tenant '${tenant}' already has a key. Issuing a new one (old key remains valid until revoked)."
  fi

  # Add new key hash → {tenant_id, tier, admin?} mapping (new format).
  local updated_json
  updated_json=$(echo "$existing_json" | KEY_HASH="$key_hash" TENANT="$tenant" TIER="$TIER" ADMIN="$ADMIN" python3 -c "
import os, sys, json, datetime
d = json.load(sys.stdin)
meta = {
    'tenant_id': os.environ['TENANT'],
    'tier': os.environ['TIER'],
    'created': datetime.datetime.utcnow().isoformat() + 'Z',
}
if os.environ.get('ADMIN') == 'true':
    meta['admin'] = True
d[os.environ['KEY_HASH']] = meta
print(json.dumps(d))
")

  store_keys_json "$updated_json"
  success "Key issued for tenant: ${tenant} (tier=${TIER}, admin=${ADMIN})"
  echo ""
  echo -e "${GREEN}╔══════════════════════════════════════════════════════╗${NC}"
  echo -e "${GREEN}║  Proxy API Key — share this with the developer       ║${NC}"
  echo -e "${GREEN}╠══════════════════════════════════════════════════════╣${NC}"
  echo -e "${GREEN}║${NC}  Tenant:  ${tenant}  (tier=${TIER}, admin=${ADMIN})"
  echo -e "${GREEN}║${NC}  Key:     ${raw_key}"
  echo -e "${GREEN}║${NC}"
  echo -e "${GREEN}║${NC}  Usage:"
  echo -e "${GREEN}║${NC}    Authorization: Bearer ${raw_key}"
  echo -e "${GREEN}╚══════════════════════════════════════════════════════╝${NC}"
  warn "Store this key securely — it cannot be retrieved again."
}

cmd_revoke() {
  local tenant="${TENANT_ID:-$USER_ID}"
  [[ -z "$tenant" ]] && error "--tenant (or --user) is required for revoke"

  local existing_json
  existing_json=$(fetch_keys_json)

  local updated_json removed
  updated_json=$(echo "$existing_json" | TENANT="$tenant" python3 -c "
import os, sys, json
d = json.load(sys.stdin)
t = os.environ['TENANT']
def owner(v): return v.get('tenant_id') if isinstance(v, dict) else v
before = len(d)
d = {k: v for k, v in d.items() if owner(v) != t}
print(json.dumps(d))
print(before - len(d), file=sys.stderr)
" 2>/tmp/revoke_count)
  removed=$(cat /tmp/revoke_count)

  if [[ "$removed" -eq 0 ]]; then
    warn "No keys found for tenant '${tenant}' — nothing to revoke."
    exit 0
  fi

  store_keys_json "$updated_json"
  success "Revoked ${removed} key(s) for tenant: ${tenant}"
}

cmd_list() {
  local existing_json
  existing_json=$(fetch_keys_json)

  echo -e "${BLUE}Current proxy key holders (tenants):${NC}"
  echo "$existing_json" | python3 -c "
import sys, json
from collections import Counter
d = json.load(sys.stdin)
if not d:
    print('  (no keys issued)')
else:
    def label(v):
        if isinstance(v, dict):
            t = v.get('tenant_id', '?'); admin = ' [admin]' if v.get('admin') else ''
            return f\"{t} (tier={v.get('tier','?')}){admin}\"
        return f'{v} (legacy)'
    counts = Counter(label(v) for v in d.values())
    for lbl, count in sorted(counts.items()):
        print(f'  {lbl}  ({count} key(s))')
    tenants = {(v.get('tenant_id') if isinstance(v, dict) else v) for v in d.values()}
    print(f'\nTotal: {len(d)} key hash(es) across {len(tenants)} tenant(s)')
"
}

# ─── Dispatch ─────────────────────────────────────────────────────────────────
case "$COMMAND" in
  issue)  cmd_issue  ;;
  revoke) cmd_revoke ;;
  list)   cmd_list   ;;
esac
