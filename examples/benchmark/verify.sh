#!/usr/bin/env bash
#
# Tenant self-verification (Linux / macOS) — preview YOUR savings before first prod
# traffic, against your ALREADY-LIVE TokenLean proxy. No Docker; no local stack.
#
# One command after `git clone` + `cd TokenLean/examples/benchmark`:
#
#   ./verify.sh --proxy-url https://<your-proxy>.run.app --api-key tok-... --provider-key sk-...
#
# It creates a throwaway venv, installs the harness deps, and runs the TRUE A/B
# (every bundled request fired direct-to-provider AND through your proxy, compared
# on the provider's own billed usage), then prints the savings table.
#
#   --proxy-url URL     your proxy base URL (required)
#   --api-key   tok-... your tenant proxy key (required)
#   --provider-key KEY  your OpenAI key for the direct arm (required unless a
#                       provider key is already in the environment); sets OPENAI_API_KEY
#   --providers LIST    default 'openai'; 'all' or a comma list (needs each key in env)
#   ...any other run_ab.py flag (e.g. --mode cold, --limit 10, --max-spend-per-provider 0.5, --judge)
#
# This flow ALWAYS does the true A/B, so it requires a provider key — onboarding
# never gives you one, so supply your own (BYOK tenants already have it). Only the
# bundled PUBLIC dataset is sent to the provider — never your data.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
info() { printf '\033[36m[verify]\033[0m %s\n' "$1"; }
die()  { printf '\033[31m[verify] ERROR:\033[0m %s\n' "$1" >&2; exit 1; }

# Split out --provider-key (we consume it); everything else passes through.
PROXY_URL=""; API_KEY=""; PROVIDER_KEY=""; PASS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --provider-key) PROVIDER_KEY="${2:-}"; shift 2 ;;
    --proxy-url)    PROXY_URL="${2:-}"; PASS+=("$1" "${2:-}"); shift 2 ;;
    --api-key)      API_KEY="${2:-}"; PASS+=("$1" "${2:-}"); shift 2 ;;
    *)              PASS+=("$1"); shift ;;
  esac
done

[ -n "$PROXY_URL" ] || die "missing --proxy-url (your live proxy base URL, e.g. https://xxx.run.app)"
[ -n "$API_KEY" ]   || die "missing --api-key (your tenant proxy key, tok-...)"

# Provider key for the direct arm. --provider-key sets OpenAI; for other providers
# export LLM_KEY_<PROVIDER> / the native var yourself and pass --providers.
if [ -n "$PROVIDER_KEY" ]; then
  export OPENAI_API_KEY="$PROVIDER_KEY"
fi
have_key=0
for v in OPENAI_API_KEY LLM_KEY_OPENAI ANTHROPIC_API_KEY LLM_KEY_ANTHROPIC GEMINI_API_KEY LLM_KEY_GEMINI \
         MISTRAL_API_KEY LLM_KEY_MISTRAL GROQ_API_KEY LLM_KEY_GROQ DEEPSEEK_API_KEY LLM_KEY_DEEPSEEK \
         XAI_API_KEY LLM_KEY_XAI COHERE_API_KEY LLM_KEY_COHERE AZURE_API_KEY AWS_ACCESS_KEY_ID; do
  if [ -n "${!v:-}" ]; then have_key=1; break; fi
done
[ "$have_key" = 1 ] || die "no provider key found. This flow does a TRUE A/B and needs your own provider key. Pass --provider-key sk-... (OpenAI) or export LLM_KEY_<PROVIDER> and use --providers."

# Python + throwaway venv ------------------------------------------------------
PY="$(command -v python3 || command -v python || true)"
[ -n "$PY" ] || die "python3 not found. Install Python 3.10+ and retry."
VENV="$HERE/.venv-verify"
if [ ! -d "$VENV" ]; then
  info "creating venv + installing deps (httpx, litellm)..."
  "$PY" -m venv "$VENV"
  # shellcheck disable=SC1091
  "$VENV/bin/pip" install --quiet --upgrade pip >/dev/null
  "$VENV/bin/pip" install --quiet httpx litellm >/dev/null
fi
VPY="$VENV/bin/python"

# Ensure the checked-in dataset artifacts exist (build from fixture if a fresh
# clone somehow lacks them — the real recognized-standard build is --hf).
if [ ! -f "$HERE/public_dataset.jsonl" ]; then
  info "building dataset artifacts (fixture)..."
  "$VPY" "$HERE/build_public_dataset.py" >/dev/null || die "dataset build failed."
fi

info "running A/B against your live proxy (no data of yours is sent — bundled public dataset only)..."
# --require-direct: hard-fail if a selected provider lacks a key (always-A/B).
# --no-cache-flush: cannot flush a live proxy; the bundled items are new to your
#   tenant cache namespace, so cold=misses / replay=hits holds naturally.
# NOTE: no --tenant — your proxy key self-identifies your tenant.
exec "$VPY" "$HERE/run_ab.py" --require-direct --no-cache-flush --mode both "${PASS[@]}"
