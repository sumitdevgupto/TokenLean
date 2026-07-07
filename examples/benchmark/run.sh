#!/usr/bin/env bash
#
# One-command token-savings benchmark (Linux / macOS).
#
# Self-contained: checks prerequisites, creates config + a proxy key if missing,
# starts (and can rebuild) the local stack, then runs the benchmark. Depends only
# on the repo's docker-compose.yml + config template — not on scripts/.
#
#   ./examples/benchmark/run.sh                  # run (starts stack if needed)
#   ./examples/benchmark/run.sh --rebuild        # rebuild images first (REQUIRED the first
#                                                #   time after updating proxy code, e.g. the
#                                                #   G06 routing fix this benchmark relies on)
#   ./examples/benchmark/run.sh --quality-check  # also assert each answer's curated facts
#                                                #   (proves the savings did not hurt quality)
#   ./examples/benchmark/run.sh --limit 5        # pass-through args go to run_benchmark.py
#   ./examples/benchmark/run.sh --no-pin-config  # measure the LIVE config as-is. By default
#                                                #   the launcher PINS a known-good config that
#                                                #   enables the six groups this benchmark
#                                                #   measures, so the result never depends on
#                                                #   whatever groups happen to be toggled on.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
cd "$REPO"

info() { printf '\033[36m[benchmark]\033[0m %s\n' "$1"; }
die()  { printf '\033[31m[benchmark] ERROR:\033[0m %s\n' "$1" >&2; exit 1; }

# Separate launcher-only flags (--rebuild, --keep-cache, --no-pin-config) from
# run_benchmark.py args.
REBUILD=0; KEEP_CACHE=0; PIN_CONFIG=1; ARGS=()
for a in "$@"; do
  case "$a" in
    --rebuild)        REBUILD=1 ;;
    --keep-cache)     KEEP_CACHE=1 ;;
    --no-pin-config)  PIN_CONFIG=0 ;;
    *)                ARGS+=("$a") ;;
  esac
done

# 1. Docker present + running ---------------------------------------------------
command -v docker >/dev/null 2>&1 || die "Docker not found. Install Docker and retry."
docker info >/dev/null 2>&1       || die "Docker daemon not running. Start it and retry."

# 2. Proxy config — create from template on first run --------------------------
if [ ! -f config/config.yaml ]; then
  [ -f config/config.yaml.template ] || die "config/config.yaml.template is missing."
  cp config/config.yaml.template config/config.yaml
  info "created config/config.yaml from template"
fi

# 3. .env + the provider key the proxy uses (LLM_KEY_OPENAI) -------------------
[ -f .env ] || die ".env not found at repo root. Copy .env.template -> .env and set LLM_KEY_OPENAI."
openai="$(grep -E '^[[:space:]]*LLM_KEY_OPENAI=' .env | head -1 | cut -d= -f2- | tr -d '[:space:]')"
[ -n "$openai" ] || die "LLM_KEY_OPENAI is empty in .env - the proxy needs it for real OpenAI calls. Set LLM_KEY_OPENAI=sk-... (you can reuse your OPENAI_API_KEY value)."

# 4. Proxy API key: env -> .env ROI_PROXY_API_KEY_* -> generate (first run) -----
key="${PROXY_API_KEY:-}"
[ -n "$key" ] || key="$(grep -hoE 'ROI_PROXY_API_KEY_[A-Z_]+=tok-[A-Za-z0-9]+' .env 2>/dev/null | grep -oE 'tok-[A-Za-z0-9]+' | head -1 || true)"
if [ -z "$key" ] && [ ! -f config/local-keys.json ]; then
  info "no proxy key found - generating a local one"
  key="tok-$(openssl rand -hex 24)"
  hash="$(printf '%s' "$key" | sha256sum | awk '{print $1}')"
  # New-format admin key: admin scope lets run_benchmark.py select the tenant via
  # the X-Tenant-ID header (post key-authoritative tenancy). A legacy
  # {"hash":"admin"} string key would resolve to the "default" tenant and break
  # the benchmark's t:<tenant>: namespacing + clear-cache cleanup.
  printf '{"%s": {"tenant_id": "bench", "tier": "enterprise", "admin": true}}\n' "$hash" > config/local-keys.json
  info "wrote config/local-keys.json (proxy loads it on start)"
  REBUILD=1   # force a (re)start so the proxy picks up the new key
fi
[ -n "$key" ] || die "No proxy key found and config/local-keys.json already exists (hashes are one-way). Set PROXY_API_KEY, add ROI_PROXY_API_KEY_* to .env, or run: bash scripts/local/deploy-local.sh"

# 5. Ensure the stack is up (build so code changes are picked up) ---------------
healthy() { curl -fsS http://localhost:4000/health >/dev/null 2>&1; }
if [ "$REBUILD" = 1 ]; then
  info "building + (re)starting stack (docker compose up -d --build)..."
  docker compose up -d --build || die "docker compose up failed. Try: bash scripts/local/deploy-local.sh"
elif healthy; then
  info "proxy already healthy on :4000 (pass --rebuild to pick up code changes)"
else
  info "starting stack (docker compose up -d) - builds images only if missing..."
  docker compose up -d || die "docker compose up failed. Try: bash scripts/local/deploy-local.sh"
fi
if ! healthy; then
  info "waiting for proxy health..."
  ok=0; for _ in $(seq 1 40); do if healthy; then ok=1; break; fi; sleep 3; done
  [ "$ok" = 1 ] || die "proxy did not become healthy in ~2min. Check: docker compose logs proxy"
fi
info "proxy healthy"

# 5b. Pin a known-good benchmark config ----------------------------------------
# The benchmark only measures techniques it can credit honestly black-box, and it
# can only do that if those groups are actually enabled. Rather than trust whatever
# config happens to be loaded (a config with these groups OFF silently reports a
# gutted pipeline), we pin a config derived from config.yaml.template (the calibrated
# baseline) with the six measured groups force-enabled — G01 compression, G05 cache,
# G06 routing, G08 lazy tools, G19 pruning, G22 dedup. G28 CCR is force-DISABLED: in a
# pass-through chat completion (no agent loop) it replaces an over-threshold system
# prompt with a CCR reference token the model can't resolve, shredding the policy facts
# the answers depend on — and it isn't one of the six measured techniques anyway.
# The original config is restored and the proxy reloaded on exit, even on failure or
# Ctrl-C. Opt out with --no-pin-config to measure the live config as-is.
PINNED=0
ORIG_BACKUP=""
restore_config() {
  local rc=$?
  if [ "$PINNED" = 1 ]; then
    PINNED=0
    info "restoring original proxy config + reloading proxy..."
    if [ -n "$ORIG_BACKUP" ] && [ -f "$ORIG_BACKUP" ]; then
      mv -f "$ORIG_BACKUP" config/config.yaml || info "config restore failed (pinned config left in place)"
    else
      info "no pre-existing config.yaml — leaving benchmark config in place (matches first-run)"
    fi
    docker compose restart proxy >/dev/null 2>&1 || info "proxy restart on restore failed (non-fatal)"
    for _ in $(seq 1 30); do if healthy; then break; fi; sleep 2; done
  fi
  exit "$rc"
}
trap restore_config EXIT

if [ "$PIN_CONFIG" = 1 ]; then
  [ -f config/config.yaml.template ] || die "config/config.yaml.template missing — cannot pin benchmark config (use --no-pin-config to skip)."
  if [ -f config/config.yaml ]; then
    ORIG_BACKUP="$(mktemp)"
    cp config/config.yaml "$ORIG_BACKUP"
  fi
  info "pinning benchmark config (enabling G01/G05/G06/G08/G19/G22; disabling G28 CCR)..."
  python - <<'PY' || die "failed to generate pinned benchmark config."
import yaml
c = yaml.safe_load(open('config/config.yaml.template')) or {}
groups = c.setdefault('groups', {})
# The six techniques this benchmark measures black-box.
enable = ['G1_compression', 'G5_cache', 'G6_routing', 'G8_tools',
          'G19_headroom', 'g22_deduplication']
# G28 CCR shreds an over-threshold system prompt into an unresolvable reference
# token in pass-through mode (no agent loop to retrieve it) — keep it off.
disable = ['G28_ccr']
created = []
def _block(k):
    blk = groups.get(k)
    if not isinstance(blk, dict):
        blk = {}
        groups[k] = blk
        created.append(k)
    return blk
for k in enable:
    _block(k)['enabled'] = True
for k in disable:
    _block(k)['enabled'] = False
yaml.safe_dump(c, open('config/config.yaml', 'w'), sort_keys=False)
if created:
    print("  note: created missing group blocks:", ", ".join(created))
print("  pinned config written; six groups enabled, G28 CCR disabled")
PY
  PINNED=1
  info "reloading proxy to load pinned config..."
  docker compose restart proxy >/dev/null 2>&1 || die "proxy restart failed while pinning config."
  ok=0; for _ in $(seq 1 40); do if healthy; then ok=1; break; fi; sleep 3; done
  [ "$ok" = 1 ] || die "proxy did not become healthy after pinning config. Check: docker compose logs proxy"
  info "pinned benchmark config active"
fi

# 6. Clear the benchmark tenant's prior-run keys (only its own data) -----------
BENCH_TENANT="${BENCHMARK_TENANT:-bench}"
if [ "$KEEP_CACHE" = 0 ]; then
  bash "$HERE/clear-cache.sh" "$BENCH_TENANT" || info "cache clear skipped (continuing)"
else
  info "keeping existing cache (--keep-cache)"
fi

# 7. Run (under the dedicated benchmark tenant) --------------------------------
info "running benchmark..."
python examples/benchmark/run_benchmark.py --api-key "$key" --tenant "$BENCH_TENANT" ${ARGS[@]+"${ARGS[@]}"}
