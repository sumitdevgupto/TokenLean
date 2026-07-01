#!/usr/bin/env bash
# =============================================================================
# live_run_check.sh — one-stop LOCAL live provider test for TokenLean.
#
#   1. Preflight  — checks Docker, proxy health, config.yaml, local-keys.json,
#                   the proxy Bearer key, and each provider's API key in .env.
#   2. Auto-key   — if no proxy key is found, GENERATES an ephemeral one, reloads
#                   the proxy so it picks it up, and uses it (default; --no-auto-key
#                   to disable). Existing tenant keys are preserved.
#   3. Run        — sends a real /v1/chat/completions round-trip per provider,
#                   using that provider's cheapest lead model from config.yaml.
#   4. Response   — prints HTTP code, routed_model, savings% and PASS/FAIL/SKIP,
#                   and exits non-zero if any *attempted* provider failed.
#
# Covers the plan's live-verification halves P3/P5/P7/P11 (per-provider round-trip
# + cost/routing resolution). Targeted checks P4/P8/P9 are documented in the plan.
#
# Usage:
#   ./scripts/local/live_run_check.sh                 # all providers, auto-key
#   ./scripts/local/live_run_check.sh aws             # AWS Bedrock only
#   ./scripts/local/live_run_check.sh anthropic gemini
#   PROXY_KEY=tok-xxxx ./scripts/local/live_run_check.sh openai   # bring your own key
#
# Providers: openai anthropic gemini mistral deepseek xai cohere groq
#            bedrock(=aws) azure openrouter opencode   (or 'all')
#
# Options:
#   --no-auto-key      do NOT generate a key; require PROXY_KEY / manual generation
#   --tenant NAME      tenant id for the auto-generated key (default: livecheck)
#   --no-restart       after auto-generating, don't restart the proxy (relies on the
#                      300s key-cache TTL instead — the key may not work immediately)
#   -h | --help        show this header
#
# Env:
#   PROXY_KEY   proxy Bearer token (tok-...). If unset, read from .env, else auto-generated.
#   PROXY       proxy base URL (default http://localhost:4000)
# =============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$ROOT"

ENV_FILE="$ROOT/.env"
CONFIG="$ROOT/config/config.yaml"
KEYS="$ROOT/config/local-keys.json"
PROXY="${PROXY:-http://localhost:4000}"
PY="$(command -v python || command -v python3 || true)"

# ── colours (only when attached to a TTY) ────────────────────────────────────
if [[ -t 1 ]]; then R=$'\e[31m'; G=$'\e[32m'; Y=$'\e[33m'; B=$'\e[34m'; DIM=$'\e[2m'; NC=$'\e[0m'
else R=; G=; Y=; B=; DIM=; NC=; fi
ok()   { echo "  ${G}✓${NC} $*"; }
bad()  { echo "  ${R}✗${NC} $*"; }
note() { echo "  ${Y}!${NC} $*"; }
dc()   { docker compose "$@" 2>/dev/null || docker-compose "$@"; }   # v2 plugin, else v1

# ── provider → required key var(s); lead model resolved from config.yaml ──────
declare -A KEYVAR=(
  [openai]=LLM_KEY_OPENAI      [anthropic]=LLM_KEY_ANTHROPIC  [gemini]=LLM_KEY_GEMINI
  [mistral]=LLM_KEY_MISTRAL    [deepseek]=LLM_KEY_DEEPSEEK     [xai]=LLM_KEY_XAI
  [cohere]=LLM_KEY_COHERE      [groq]=LLM_KEY_GROQ             [azure]=LLM_KEY_AZURE
  [openrouter]=LLM_KEY_OPENROUTER  [opencode]=LLM_KEY_OPENCODE
  [bedrock]="AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_REGION_NAME"
)
declare -A FALLBACK_MODEL=(
  [openai]=gpt-4o-mini            [anthropic]=claude-haiku-4-5
  [gemini]=gemini-2.5-flash-lite  [mistral]=ministral-3b-latest
  [deepseek]=deepseek-chat        [xai]=grok-3-mini
  [cohere]=command-r7b-12-2024    [groq]=groq/llama-3.1-8b-instant
  [bedrock]=bedrock/amazon.nova-micro-v1:0
  [openrouter]=openrouter/openai/gpt-oss-120b:free  [opencode]=opencode/mimo-v2.5
)
ALL="openai anthropic gemini mistral deepseek xai cohere groq bedrock openrouter opencode"

# ── parse flags + provider args ──────────────────────────────────────────────
AUTO_KEY=1; DO_RESTART=1; GEN_TENANT=livecheck; PROVIDERS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)      sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
    --no-auto-key)  AUTO_KEY=0;;
    --no-restart)   DO_RESTART=0;;
    --tenant)       GEN_TENANT="${2:?--tenant needs a value}"; shift;;
    --tenant=*)     GEN_TENANT="${1#*=}";;
    -*)             echo "Unknown option: $1"; echo "Try --help"; exit 2;;
    *)              PROVIDERS+=("${1,,}");;
  esac
  shift
done
[[ ${#PROVIDERS[@]} -eq 0 ]] && PROVIDERS=(all)
TARGETS=""
for a in "${PROVIDERS[@]}"; do
  [[ "$a" == aws ]] && a=bedrock
  if [[ "$a" == all ]]; then TARGETS="$ALL"; break; fi
  [[ -n "${KEYVAR[$a]:-}" ]] || { echo "Unknown provider: $a"; echo "Valid: $ALL (or aws, all)"; exit 2; }
  TARGETS="$TARGETS $a"
done

# ── helpers ──────────────────────────────────────────────────────────────────
env_val() {  # print value of $1 from .env (accepts optional 'export '); empty if absent
  [[ -f "$ENV_FILE" ]] || return 0
  grep -E "^(export )?$1=" "$ENV_FILE" | tail -1 | sed -E 's/^(export )?[^=]*=//; s/\r$//; s/^"//; s/"$//'
}
is_set() {   # 0 if var present in .env and not an obvious placeholder
  local v; v="$(env_val "$1")"
  [[ -n "$v" && "$v" != *"..."* && "$v" != your-* && "$v" != changeme* ]]
}
lead_model() {  # $1 provider → lead model from config.yaml, else fallback (relative path: Windows-python safe)
  local m=""
  [[ -n "$PY" && -f "$CONFIG" ]] && m="$("$PY" - "$1" <<'PY' 2>/dev/null
import sys, yaml
p = sys.argv[1]
c = yaml.safe_load(open("config/config.yaml", encoding="utf-8")) or {}
provs = c.get("providers", [])
if isinstance(provs, dict): provs = [dict(name=k, **(v or {})) for k, v in provs.items()]
for e in provs:
    if e.get("name") == p:
        ms = e.get("models") or e.get("model") or []
        if isinstance(ms, str): ms = [ms]
        if ms: print(ms[0])
        break
PY
)"
  [[ -n "$m" ]] && echo "$m" || echo "${FALLBACK_MODEL[$1]:-}"
}

provision_key() {  # generate an ephemeral proxy key, reload proxy, set $PROXY_KEY
  local tmp=".livecheck_key.$$.tmp"     # relative → Windows-python can open it
  note "auto-provisioning a proxy key for tenant '${GEN_TENANT}' (--no-auto-key to disable)…"
  if ! "$PY" pitch-test-plan/scripts/common/generate_proxy_key.py \
        --tenant "$GEN_TENANT" --tier pro \
        --output config/local-keys.json --env-file "$tmp" >/dev/null 2>&1; then
    bad "key generation failed (pitch-test-plan/scripts/common/generate_proxy_key.py)"; rm -f "$tmp"; return 1
  fi
  PROXY_KEY="$(grep -oE 'ROI_PROXY_API_KEY_[A-Z0-9_]+=tok-[a-f0-9]+' "$tmp" | head -1 | sed -E 's/.*=(tok-[a-f0-9]+)/\1/')"
  rm -f "$tmp"
  [[ "$PROXY_KEY" == tok-* ]] || { bad "could not capture the generated key"; return 1; }
  ok "generated key for '${GEN_TENANT}' (${DIM}tok-…redacted${NC}); hash added to config/local-keys.json"
  if (( DO_RESTART )); then
    note "restarting proxy to load the new key (key-cache TTL is 300s otherwise)…"
    dc restart proxy >/dev/null 2>&1 || { bad "proxy restart failed — run: docker compose restart proxy"; return 1; }
    local i; for i in $(seq 1 40); do curl -sf "$PROXY/health" >/dev/null 2>&1 && break; sleep 1; done
    curl -sf "$PROXY/health" >/dev/null 2>&1 && ok "proxy healthy after reload" \
      || { bad "proxy did not become healthy after restart"; return 1; }
  else
    note "--no-restart: relying on the 300s cache TTL; the new key may 401 until it refreshes"
  fi
  return 0
}

# ── 1. Preflight ─────────────────────────────────────────────────────────────
echo "${B}== Preflight ==${NC}"
fail=0
[[ -n "$PY" ]] && ok "python found ($PY)" || { bad "python not found (needed to read config / parse JSON)"; fail=1; }
[[ -f "$ENV_FILE" ]] && ok ".env present" || { bad ".env missing — copy .env.template and fill keys"; fail=1; }
if [[ -f "$CONFIG" ]]; then
  # NB: use a RELATIVE path — native Windows python can't open a git-bash /d/... path.
  if [[ -n "$PY" ]] && "$PY" -c "import yaml; yaml.safe_load(open('config/config.yaml',encoding='utf-8'))" 2>/dev/null; then
    ok "config/config.yaml valid"; else bad "config/config.yaml invalid YAML"; fail=1; fi
else bad "config/config.yaml missing — copy config/config.yaml.template"; fail=1; fi
[[ -f "$KEYS" ]] && ok "config/local-keys.json present" || note "config/local-keys.json missing — will be created on auto-key"
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then ok "Docker daemon running"
else bad "Docker not running — start Docker Desktop, then ./scripts/local/deploy-local.sh --recreate --seed"; fail=1; fi
if curl -sf "$PROXY/health" >/dev/null 2>&1; then ok "proxy healthy at $PROXY"
else bad "proxy unreachable at $PROXY/health — run ./scripts/local/deploy-local.sh --recreate --seed"; fail=1; fi

# proxy Bearer key: env → .env PROXY_KEY → first .env ROI_PROXY_API_KEY_* → auto-generate
PROXY_KEY="${PROXY_KEY:-}"
[[ -z "$PROXY_KEY" ]] && PROXY_KEY="$(env_val PROXY_KEY)"
if [[ -z "$PROXY_KEY" && -f "$ENV_FILE" ]]; then
  first="$(grep -oE '^(export )?ROI_PROXY_API_KEY_[A-Z0-9_]+' "$ENV_FILE" | head -1 | sed 's/^export //' || true)"
  [[ -n "$first" ]] && PROXY_KEY="$(env_val "$first")"
fi
if [[ "$PROXY_KEY" == tok-* ]]; then
  ok "proxy Bearer key found (${DIM}tok-…redacted${NC})"
elif [[ $fail -eq 0 ]] && (( AUTO_KEY )); then
  provision_key || fail=1
else
  bad "no proxy Bearer key (tok-…). Set PROXY_KEY=tok-…, drop --no-auto-key to auto-generate, or:"
  echo "      python pitch-test-plan/scripts/common/generate_proxy_key.py --tenant nova-med --tier pro --env-file .env"
  echo "      ${DIM}(send the plaintext tok-… value, NOT the sha256 hash from local-keys.json)${NC}"
  fail=1
fi

if [[ $fail -ne 0 ]]; then
  echo; echo "${R}Preflight failed — fix the ✗ items above and re-run.${NC}"; exit 1
fi

# ── 2/3. Run round-trips + response ──────────────────────────────────────────
echo
echo "${B}== Live round-trips ==${NC}  ${DIM}(model = provider's lead model in config.yaml)${NC}"
printf "%-11s %-40s %-5s %-26s %-7s %s\n" PROVIDER MODEL HTTP ROUTED_MODEL SAVED% VERDICT
overall=0
for p in $TARGETS; do
  model="$(lead_model "$p")"
  miss=""; for kv in ${KEYVAR[$p]}; do is_set "$kv" || miss="$miss $kv"; done
  if [[ -n "$miss" ]]; then
    printf "%-11s %-40s %-5s %-26s %-7s ${Y}SKIP${NC} ${DIM}(missing:%s)${NC}\n" "$p" "${model:--}" "-" "-" "-" "$miss"; continue
  fi
  if [[ "$p" == azure && -z "$model" ]]; then
    printf "%-11s %-40s %-5s %-26s %-7s ${Y}SKIP${NC} ${DIM}(add azure deployment + api_base/api_version to config)${NC}\n" "$p" "-" "-" "-" "-"; continue
  fi
  body="{\"model\":\"$model\",\"messages\":[{\"role\":\"user\",\"content\":\"Reply with exactly: OK\"}],\"max_tokens\":16}"
  resp="$(curl -s -m 60 -w $'\n%{http_code}' "$PROXY/v1/chat/completions" \
          -H "Authorization: Bearer $PROXY_KEY" -H 'Content-Type: application/json' -d "$body")"
  code="$(tail -1 <<<"$resp")"; jsonb="$(sed '$d' <<<"$resp")"
  if [[ "$code" == 200 ]]; then
    rs="$(JSONB="$jsonb" "$PY" - <<'PY' 2>/dev/null
import os, json
t = (json.loads(os.environ.get("JSONB","{}")) or {}).get("_token_opt", {})
print(t.get("routed_model","?"), t.get("total_pct_saving","?"))
PY
)"
    printf "%-11s %-40s %-5s %-26s %-7s ${G}PASS${NC}\n" "$p" "$model" "$code" "${rs%% *}" "${rs##* }"
  else
    overall=1
    err="$(JSONB="$jsonb" "$PY" - <<'PY' 2>/dev/null
import os, json
d = json.loads(os.environ.get("JSONB","{}")) or {}
e = d.get("error", d); m = e.get("message") if isinstance(e, dict) else str(e)
print((m or "")[:44])
PY
)"
    printf "%-11s %-40s %-5s %-26s %-7s ${R}FAIL${NC} ${DIM}%s${NC}\n" "$p" "$model" "$code" "-" "-" "$err"
  fi
done

echo
if [[ $overall -eq 0 ]]; then
  echo "${G}Done — every attempted provider passed.${NC} ${DIM}(SKIP = missing key / azure not configured)${NC}"
else
  echo "${R}Done — one or more providers FAILED.${NC} Inspect with: ${DIM}docker compose logs -f proxy${NC}"
fi
exit $overall
