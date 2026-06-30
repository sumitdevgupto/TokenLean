#!/bin/bash
#
# Promptfoo Evaluation Step for CI/CD
# 
# Runs automated prompt quality evaluations using Promptfoo.
# Designed to run in Cloud Build or GitHub Actions.
#
# Usage:
#   bash ci/promptfoo-eval.sh --config tests/promptfoo-config.yaml
#

set -e

echo "========================================="
echo "Promptfoo Prompt Evaluation"
echo "========================================="

# Run from repo root so relative fixture paths resolve regardless of caller CWD.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "$REPO_ROOT"

# Configuration
CONFIG_FILE="${PROMPTFOO_CONFIG:-tests/promptfoo-config.yaml}"
OUTPUT_DIR="${PROMPTFOO_OUTPUT:-reports/promptfoo}"
PROXY_URL="${PROXY_URL:-http://localhost:4000}"
PROXY_KEY="${PROXY_API_KEY:-}"

# Point promptfoo's OpenAI provider at the proxy (OpenAI-compatible) endpoint.
# The provider reads OPENAI_BASE_URL + OPENAI_API_KEY from the environment, so
# the same config file works for both the local stack and a deployed proxy URL.
export OPENAI_BASE_URL="${PROXY_URL%/}/v1"

# Keep the eval fully local: no anonymous usage telemetry, no update pings.
export PROMPTFOO_DISABLE_TELEMETRY=1
export PROMPTFOO_DISABLE_UPDATE=1

# promptfoo@latest needs Node ^20.20 || >=22.22. If your Node is older, pin a
# compatible release, e.g. PROMPTFOO_VERSION=0.117.0
PROMPTFOO_VERSION="${PROMPTFOO_VERSION:-latest}"

# ─── Resolve a proxy caller key (tok-...) from .env / .env.gcp ──────────────────
# Proxy keys are tenant-scoped: ROI_PROXY_API_KEY_<TENANT> (uppercased tenant).
# Default tenant NOVA_MED; override with PROMPTFOO_TENANT=SHOP_BOT (etc), or set
# PROXY_API_KEY directly to bypass resolution. The local build sources .env and
# the GCP deploy sources .env.gcp, so the correct key is usually already in the
# environment; the file scan is a fallback for running this script standalone.
PROMPTFOO_TENANT="${PROMPTFOO_TENANT:-NOVA_MED}"
if [ -z "$PROXY_KEY" ]; then
    _keyvar="ROI_PROXY_API_KEY_${PROMPTFOO_TENANT}"
    PROXY_KEY="${!_keyvar:-}"
fi
if [ -z "$PROXY_KEY" ]; then
    for _ef in "$REPO_ROOT/.env" "$REPO_ROOT/.env.gcp"; do
        [ -f "$_ef" ] || continue
        _line="$(grep -E "^(export[[:space:]]+)?ROI_PROXY_API_KEY_${PROMPTFOO_TENANT}=" "$_ef" | tail -1 || true)"
        if [ -n "$_line" ]; then
            PROXY_KEY="${_line#*=}"
            PROXY_KEY="${PROXY_KEY%\"}"; PROXY_KEY="${PROXY_KEY#\"}"
            PROXY_KEY="${PROXY_KEY%\'}"; PROXY_KEY="${PROXY_KEY#\'}"
            echo "Resolved proxy key for tenant '${PROMPTFOO_TENANT}' from ${_ef##*/}"
            break
        fi
    done
fi

# Check dependencies
if ! command -v npx &> /dev/null; then
    echo "Installing promptfoo via npx..."
    npm install -g promptfoo
fi

# Ensure output directory exists
mkdir -p "$OUTPUT_DIR"

# Check if config exists
if [ ! -f "$CONFIG_FILE" ]; then
    echo "⚠️  Promptfoo config not found: $CONFIG_FILE"
    echo "Creating default configuration..."
    
    mkdir -p "$(dirname "$CONFIG_FILE")"
    # Minimal fallback only — the committed tests/promptfoo-config.yaml is the
    # canonical config; this is written solely if that file has been removed.
    # Auth/base URL come from OPENAI_BASE_URL / OPENAI_API_KEY (exported above).
    cat > "$CONFIG_FILE" << 'EOF'
description: Token Optimisation Proxy - Prompt Quality Evals (fallback)
providers:
  - openai:gpt-4o-mini
prompts:
  - data/prompts/customer-support.txt
tests: data/promptfoo-tests.yaml
defaultTest:
  assert:
    - type: is-json
EOF
fi

# Run evaluations
echo ""
echo "Running Promptfoo evaluations..."
echo "Config: $CONFIG_FILE"
echo "Output: $OUTPUT_DIR"
echo ""

if [ -n "$PROXY_KEY" ]; then
    export OPENAI_API_KEY="$PROXY_KEY"
fi
if [ -z "${OPENAI_API_KEY:-}" ]; then
    echo "⚠️  No API key set. Export PROXY_API_KEY (a tok-... proxy key the proxy"
    echo "    accepts) — requests through the proxy will 401 without it."
fi

# Execute promptfoo. NOTE: no --share — results stay LOCAL. --share would upload
# your prompts and model responses to the public promptfoo sharing service.
npx "promptfoo@${PROMPTFOO_VERSION}" eval \
    --config "$CONFIG_FILE" \
    --output "$OUTPUT_DIR/results.json" \
    --verbose

# Check results
echo ""
echo "========================================="
echo "Evaluation Results"
echo "========================================="

if [ -f "$OUTPUT_DIR/results.json" ]; then
    # Parse results (if jq available)
    if command -v jq &> /dev/null; then
        TOTAL=$(jq '.results | length' "$OUTPUT_DIR/results.json")
        PASSED=$(jq '[.results[] | select(.success == true)] | length' "$OUTPUT_DIR/results.json")
        FAILED=$((TOTAL - PASSED))
        
        echo "Total tests: $TOTAL"
        echo "Passed: $PASSED"
        echo "Failed: $FAILED"
        echo ""
        
        if [ "$FAILED" -gt 0 ]; then
            echo "❌ $FAILED test(s) failed"
            
            # Show failures
            jq -r '.results[] | select(.success == false) | "  - \(.description): \(.failReason // "Unknown")"' "$OUTPUT_DIR/results.json"
            
            exit 1
        else
            echo "✅ All tests passed"
        fi
    else
        echo "Results saved to: $OUTPUT_DIR/results.json"
        echo "Install jq for detailed parsing"
    fi
    
    # Inspect results locally — no external upload.
    echo ""
    echo "Raw results JSON: $OUTPUT_DIR/results.json"
    echo "Open the local results viewer with: npx promptfoo@${PROMPTFOO_VERSION} view"
else
    echo "❌ No results file generated"
    exit 1
fi

echo ""
echo "========================================="
echo "Promptfoo evaluation complete"
echo "========================================="
