#!/usr/bin/env bash
# Local development build helper.
# Usage: bash scripts/local/build-local.sh [SERVICE...] [--promptfoo] [--dspy]
#   No SERVICE args -> build all images and (re)start all services
#   With SERVICE args -> build and restart only the named services
#
# Optional post-build steps (opt-in; run after the stack is up):
#   --promptfoo   Run the Promptfoo prompt-quality eval against the local proxy
#                 (needs Node/npx + tests/promptfoo-config.yaml fixtures; non-fatal)
#   --dspy        Run the DSPy prompt-template optimiser over templates/prompts
#                 (needs the templates/prompts source dir; non-fatal)
#
# Examples:
#   bash scripts/local/build-local.sh
#   bash scripts/local/build-local.sh proxy
#   bash scripts/local/build-local.sh proxy llmlingua
#   bash scripts/local/build-local.sh --promptfoo
#   bash scripts/local/build-local.sh proxy --promptfoo --dspy

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

cd "$PROJECT_ROOT"

# Load .env if it exists (never committed; contains local overrides)
if [ -f ".env" ]; then
    set -a
    # shellcheck disable=SC1091
    . ".env"
    set +a
    echo "[build-local] Loaded .env"
fi

# Parse flags out of the args; everything else is treated as a service name.
RUN_PROMPTFOO=false
RUN_DSPY=false
SERVICES=()
for arg in "$@"; do
    case "$arg" in
        --promptfoo)  RUN_PROMPTFOO=true ;;
        --dspy)       RUN_DSPY=true ;;
        --with-evals) RUN_PROMPTFOO=true; RUN_DSPY=true ;;
        --*) echo "[build-local] Unknown option: $arg" >&2; exit 2 ;;
        *) SERVICES+=("$arg") ;;
    esac
done

# G02 prompt-template token-budget gate — fails the build on a budget violation.
echo "[build-local] Validating G02 prompt-template token budgets..."
bash "$PROJECT_ROOT/scripts/ci/validate-templates.sh"

if [ "${#SERVICES[@]}" -eq 0 ]; then
    echo "[build-local] Building all images..."
    docker-compose build

    echo "[build-local] Starting all services..."
    docker-compose up -d

    echo "[build-local] Stack status:"
    docker-compose ps
else
    echo "[build-local] Building services: ${SERVICES[*]}"
    docker-compose build "${SERVICES[@]}"

    echo "[build-local] Restarting services: ${SERVICES[*]}"
    docker-compose up -d "${SERVICES[@]}"

    echo "[build-local] Service status:"
    docker-compose ps "${SERVICES[@]}"
fi

echo ""
echo "[build-local] Done. Key URLs:"
echo "  Proxy:      http://localhost:4000"
echo "  Jaeger:     http://localhost:16686"
echo "  Grafana:    http://localhost:3000  (admin / see .env)"
echo "  Langfuse:   http://localhost:3001"

# ─── Optional post-build steps (opt-in via flags) ──────────────────────────────
# Run after the stack is up so Promptfoo can reach the live local proxy. These are
# non-fatal: a failure is reported but does not tear down the running stack.
if [ "$RUN_PROMPTFOO" = "true" ]; then
    echo ""
    echo "[build-local] Running Promptfoo quality eval (proxy: http://localhost:4000)..."
    PROXY_URL="http://localhost:4000" bash "$PROJECT_ROOT/ci/promptfoo-eval.sh" \
        || echo "[build-local] WARN: promptfoo eval reported failures (stack is still running)"
fi

if [ "$RUN_DSPY" = "true" ]; then
    echo ""
    echo "[build-local] Running DSPy prompt-template optimisation..."
    bash "$PROJECT_ROOT/ci/dspy-optimize.sh" \
        || echo "[build-local] WARN: dspy optimisation reported issues"
fi
