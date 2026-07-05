#!/usr/bin/env bash
# =============================================================================
# precheck-local-deployment.sh — Validate all dependencies before local deploy
# =============================================================================
# Usage:
#   ./scripts/local/precheck-local-deployment.sh
#
# Checks:
#   - Docker daemon running
#   - Docker Compose available (V1 or V2)
#   - Required files exist (.env, config/config.yaml, docker-compose.yml)
#   - Required directories exist (src/proxy, src/llmlingua-sidecar, etc.)
#   - Required ports are free (4000, 8080, 8081, 6333, 9998, 3000, 3100)
#   - Environment variables are set (not placeholders)
#   - YAML files are valid
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
PASS="${GREEN}✓${NC}"; FAIL="${RED}✗${NC}"; WARN="${YELLOW}!${NC}"

ERRORS=0
WARNINGS=0

info()    { echo -e "${BLUE}[CHECK]${NC} $*"; }
success() { echo -e "${PASS} $*"; }
error()   { echo -e "${FAIL} $*"; ERRORS=$((ERRORS + 1)); }
warn()    { echo -e "${WARN} $*"; WARNINGS=$((WARNINGS + 1)); }

# ─── Check 1: Docker daemon ───────────────────────────────────────────────────
info "Checking Docker daemon..."
if docker info &>/dev/null; then
    DOCKER_VERSION=$(docker version --format '{{.Server.Version}}' 2>/dev/null || echo "unknown")
    success "Docker running (v${DOCKER_VERSION})"
else
    error "Docker is not running. Start Docker Desktop."
fi

# ─── Check 2: Docker Compose ──────────────────────────────────────────────────
info "Checking Docker Compose..."
if docker compose version &>/dev/null; then
    DC="docker compose"
    DC_VERSION=$(docker compose version --short 2>/dev/null || echo "unknown")
    success "Docker Compose V2 plugin (v${DC_VERSION})"
elif docker-compose version &>/dev/null; then
    DC="docker-compose"
    DC_VERSION=$(docker-compose version --short 2>/dev/null || echo "unknown")
    success "Docker Compose V1 (v${DC_VERSION})"
else
    error "Docker Compose not found. Install Docker Compose."
    DC="docker-compose"  # fallback for subsequent checks
fi

# ─── Check 3: Required files ──────────────────────────────────────────────────
info "Checking required files..."
REQUIRED_FILES=(
    "docker-compose.yml"
    "config/config.yaml"
    ".env"
)

for file in "${REQUIRED_FILES[@]}"; do
    if [[ -f "${REPO_ROOT}/${file}" ]]; then
        success "${file} exists"
    else
        error "${file} missing at ${REPO_ROOT}/${file}"
    fi
done

# ─── Check 4: Required directories ────────────────────────────────────────────
info "Checking required directories..."
REQUIRED_DIRS=(
    "src/proxy"
    "src/llmlingua-sidecar"
    "src/routellm-sidecar"
    "src/tika-sidecar"
    "src/doc-pipeline"
    "scripts"
    "config"
)

for dir in "${REQUIRED_DIRS[@]}"; do
    if [[ -d "${REPO_ROOT}/${dir}" ]]; then
        success "${dir}/ exists"
    else
        error "${dir}/ missing"
    fi
done

# ─── Check 5: YAML validity ─────────────────────────────────────────────────────
info "Checking YAML validity..."
if command -v python3 &>/dev/null; then
    if python3 -c "import yaml; yaml.safe_load(open('${REPO_ROOT}/docker-compose.yml'))" 2>/dev/null; then
        success "docker-compose.yml is valid YAML"
    else
        error "docker-compose.yml has YAML syntax errors"
    fi
    
    if python3 -c "import yaml; yaml.safe_load(open('${REPO_ROOT}/config/config.yaml'))" 2>/dev/null; then
        success "config/config.yaml is valid YAML"
    else
        error "config/config.yaml has YAML syntax errors"
    fi
else
    warn "python3 not found — skipping YAML validation"
fi

# ─── Check 6: Port availability ─────────────────────────────────────────────────
info "Checking port availability..."
REQUIRED_PORTS=(4000 8080 8081 6333 9998 3000 3100)
PORT_ERRORS=0

# Ports published by our OWN already-running stack are not conflicts on a
# re-deploy — `docker compose up -d` will reconcile those containers in place.
# Collect them so we don't false-fail when the stack is already up.
OWN_PORTS=""
if command -v docker &>/dev/null && docker info >/dev/null 2>&1; then
    OWN_PORTS="$(docker ps --format '{{.Names}} {{.Ports}}' 2>/dev/null \
        | grep -E '(^| )token-opt-' \
        | grep -oE '(0\.0\.0\.0|127\.0\.0\.1|\[::\]|\*):[0-9]+' \
        | grep -oE '[0-9]+$' | sort -u | tr '\n' ' ')"
fi

for port in "${REQUIRED_PORTS[@]}"; do
    if [[ " ${OWN_PORTS} " == *" ${port} "* ]]; then
        success "Port ${port} is in use by our own token-opt-* container (stack already running) — OK"
        continue
    fi
    if command -v nc &>/dev/null; then
        # nc available (Linux/Mac)
        if nc -z localhost "${port}" 2>/dev/null; then
            error "Port ${port} is already in use"
            PORT_ERRORS=$((PORT_ERRORS + 1))
        else
            success "Port ${port} is free"
        fi
    elif command -v lsof &>/dev/null; then
        # lsof available (Mac/Linux)
        if lsof -Pi ":${port}" -sTCP:LISTEN -t &>/dev/null; then
            error "Port ${port} is already in use"
            PORT_ERRORS=$((PORT_ERRORS + 1))
        else
            success "Port ${port} is free"
        fi
    elif command -v netstat &>/dev/null; then
        # netstat available
        if netstat -tuln 2>/dev/null | grep -q ":${port}"; then
            error "Port ${port} is already in use"
            PORT_ERRORS=$((PORT_ERRORS + 1))
        else
            success "Port ${port} is free"
        fi
    else
        warn "Cannot check port ${port} (no nc, lsof, or netstat available)"
    fi
done

if [[ ${PORT_ERRORS} -gt 0 ]]; then
    error "${PORT_ERRORS} required ports are in use. Stop conflicting services first."
fi

# ─── Check 7: Environment variables ───────────────────────────────────────────
info "Checking environment variables..."

# Load .env if exists
if [[ -f "${REPO_ROOT}/.env" ]]; then
    # shellcheck source=/dev/null
    set -a
    source "${REPO_ROOT}/.env"
    set +a
fi

# Check critical variables
if [[ -z "${OPENAI_API_KEY:-}" ]] || [[ "${OPENAI_API_KEY}" == "sk-..." ]] || [[ "${OPENAI_API_KEY}" == "sk-" ]]; then
    error "OPENAI_API_KEY not set or is placeholder in .env"
else
    success "OPENAI_API_KEY is set"
fi

if [[ -z "${LLM_KEY_OPENAI:-}" ]] || [[ "${LLM_KEY_OPENAI}" == "sk-..." ]] || [[ "${LLM_KEY_OPENAI}" == "sk-" ]]; then
    warn "LLM_KEY_OPENAI not set or is placeholder (proxy may fail)"
else
    success "LLM_KEY_OPENAI is set"
fi

if [[ -z "${REDIS_URL:-}" ]]; then
    warn "REDIS_URL not set (will use default)"
else
    success "REDIS_URL is set"
fi

if [[ -z "${DATABASE_URL:-}" ]]; then
    warn "DATABASE_URL not set (will use default)"
else
    success "DATABASE_URL is set"
fi

# ─── Check 8: Docker build contexts ────────────────────────────────────────────
info "Checking Docker build contexts..."
DOCKERFILES=(
    "src/proxy/Dockerfile"
    "src/llmlingua-sidecar/Dockerfile"
    "src/routellm-sidecar/Dockerfile"
    "src/tika-sidecar/Dockerfile"
)

for df in "${DOCKERFILES[@]}"; do
    if [[ -f "${REPO_ROOT}/${df}" ]]; then
        success "${df} exists"
    else
        error "${df} missing — cannot build image"
    fi
done

# ─── Check 9: Disk space ────────────────────────────────────────────────────────
info "Checking disk space..."
if command -v df &>/dev/null; then
    # Get available space in GB (works on Linux/Mac)
    AVAILABLE_GB=$(df -BG "${REPO_ROOT}" 2>/dev/null | awk 'NR==2 {print $4}' | tr -d 'G' || echo "0")
    if [[ "${AVAILABLE_GB}" -lt 5 ]]; then
        error "Low disk space: ${AVAILABLE_GB}GB available (need at least 5GB)"
    elif [[ "${AVAILABLE_GB}" -lt 10 ]]; then
        warn "Disk space: ${AVAILABLE_GB}GB available (recommend 10GB+)"
    else
        success "Disk space: ${AVAILABLE_GB}GB available"
    fi
else
    warn "Cannot check disk space (df not available)"
fi

# ─── Check 10: Memory ───────────────────────────────────────────────────────────
info "Checking available memory..."
if [[ -f /proc/meminfo ]]; then
    # Linux
    MEM_AVAILABLE_KB=$(grep MemAvailable /proc/meminfo 2>/dev/null | awk '{print $2}' || echo "0")
    MEM_AVAILABLE_GB=$((MEM_AVAILABLE_KB / 1024 / 1024))
    if [[ ${MEM_AVAILABLE_GB} -lt 4 ]]; then
        error "Low memory: ~${MEM_AVAILABLE_GB}GB available (need at least 4GB, recommend 8GB+)"
    elif [[ ${MEM_AVAILABLE_GB} -lt 8 ]]; then
        warn "Memory: ~${MEM_AVAILABLE_GB}GB available (recommend 8GB+)"
    else
        success "Memory: ~${MEM_AVAILABLE_GB}GB available"
    fi
elif command -v vm_stat &>/dev/null; then
    # macOS
    warn "Cannot accurately check memory on macOS — ensure 8GB+ RAM available"
else
    warn "Cannot check memory availability"
fi

# ─── Summary ────────────────────────────────────────────────────────────────────
echo ""
echo "╔════════════════════════════════════════════════════════════════╗"
if [[ ${ERRORS} -eq 0 ]]; then
    echo -e "║  ${GREEN}ALL CHECKS PASSED${NC} — Ready for local deployment              ║"
    echo "╠════════════════════════════════════════════════════════════════╣"
    echo -e "║  Run: ${GREEN}./scripts/local/deploy-local.sh${NC}                            ║"
    if [[ ${WARNINGS} -gt 0 ]]; then
        echo "║                                                                ║"
        echo -e "║  ${YELLOW}Warnings: ${WARNINGS}${NC} (non-blocking)                         ║"
    fi
else
    echo -e "║  ${RED}CHECKS FAILED${NC} — Fix errors before deploying                  ║"
    echo "╠════════════════════════════════════════════════════════════════╣"
    echo -e "║  ${RED}Errors: ${ERRORS}${NC}                                                ║"
    if [[ ${WARNINGS} -gt 0 ]]; then
        echo -e "║  ${YELLOW}Warnings: ${WARNINGS}${NC}                                          ║"
    fi
fi
echo "╚════════════════════════════════════════════════════════════════╝"

exit ${ERRORS}
