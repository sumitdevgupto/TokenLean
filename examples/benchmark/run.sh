#!/usr/bin/env bash
#
# One-command token-savings benchmark (Linux / macOS).
#
# Self-contained: checks prerequisites, creates config + a proxy key if missing,
# starts (and can rebuild) the local stack, then runs the benchmark. Depends only
# on the repo's docker-compose.yml + config template — not on scripts/.
#
#   ./examples/benchmark/run.sh                  # run (starts stack if needed; the proxy image
#                                                #   is always rebuilt from your checkout - a
#                                                #   cached no-op when nothing changed)
#   ./examples/benchmark/run.sh --rebuild        # rebuild EVERY image, not just the proxy
#   ./examples/benchmark/run.sh --quality-check  # also assert each answer's curated facts
#                                                #   (proves the savings did not hurt quality)
#   ./examples/benchmark/run.sh --limit 5        # pass-through args go to run_benchmark.py
#   ./examples/benchmark/run.sh --no-pin-config  # measure the LIVE config as-is. By default
#                                                #   the launcher PINS a known-good config that
#                                                #   enables the six groups this benchmark
#                                                #   measures, so the result never depends on
#                                                #   whatever groups happen to be toggled on.
#
# Exit status is the runner's: 0 = complete (gate passed if asked), 1 = nothing measured,
# 2 = quality gate failed, 3 = INCOMPLETE (a request failed - not a result).
# With --ab: 2 = fact regression, 3 = spend cap, 4 = asked-for lever never fired, 5 = incomplete.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
cd "$REPO"

info() { printf '\033[36m[benchmark]\033[0m %s\n' "$1"; }
die()  { printf '\033[31m[benchmark] ERROR:\033[0m %s\n' "$1" >&2; exit 1; }

# Separate launcher-only flags (--rebuild, --keep-cache, --no-pin-config, --ab)
# from the runner's pass-through args.
#   --ab  run the true A/B harness (run_ab.py: proxy vs direct-to-provider on
#         provider-billed tokens) instead of the single-arm counterfactual.
#   --restore  put back a config left pinned by a run that was killed, then exit.
REBUILD=0; KEEP_CACHE=0; PIN_CONFIG=1; RUN_AB=0; RESTORE_ONLY=0; ARGS=()
for a in "$@"; do
  case "$a" in
    --rebuild)        REBUILD=1 ;;
    --keep-cache)     KEEP_CACHE=1 ;;
    --no-pin-config)  PIN_CONFIG=0 ;;
    --ab)             RUN_AB=1 ;;
    --restore)        RESTORE_ONLY=1 ;;
    *)                ARGS+=("$a") ;;
  esac
done

# Backups live INSIDE the directory config/.config.yaml.prepin/, one PID-named file per run, so
# overlapping runs never share one. The DIRECTORY is what .gitignore excludes. The 2026-09-17
# rename put them at `config/.config.yaml.prepin.<pid>` - a sibling FILE that ignore line does
# not match - and checkin-push.sh stages the public repo with `git add -A`, so a leftover or
# in-flight backup of the operator's live config was one routine commit away from the public
# repo (2026-09-18). A run that is KILLED never fires the EXIT trap, so the pinned config stays
# in place; the next run's restore_stranded() finds ANY run's leftover backup and recovers it.
BACKUP_DIR="config/.config.yaml.prepin"
ORIG_BACKUP="$BACKUP_DIR/$$.yaml"

# The first leftover backup from ANY run, in every layout this launcher has ever written.
stranded_backup() {
  local b
  for b in "$BACKUP_DIR"/*.yaml config/.config.yaml.prepin.* "$BACKUP_DIR"; do
    if [ -f "$b" ]; then printf '%s\n' "$b"; return 0; fi
  done
  return 1
}

# Self-heal: a leftover backup means that run did not restore. Put the operator's config back
# BEFORE taking a new backup, so the pinned config is never mistaken for the original.
restore_stranded() {
  local backup
  backup="$(stranded_backup)" || return 0
  info "found $backup — a previous run did not restore; recovering your config first"
  mv -f "$backup" config/config.yaml || die "could not restore $backup — move it back to config/config.yaml by hand"
  rmdir "$BACKUP_DIR" 2>/dev/null || true
}

if [ "$RESTORE_ONLY" = 1 ]; then
  # Any run's backup, not this process's: $ORIG_BACKUP carries THIS run's PID, so a killed
  # run's backup is never at that path and the old `-f "$ORIG_BACKUP"` test made --restore
  # report "nothing to restore" in exactly the case it exists for (2026-09-18).
  if stranded_backup >/dev/null; then
    restore_stranded
    docker compose restart proxy >/dev/null 2>&1 || true
    info "config restored."
  else
    info "nothing to restore — no leftover backup under $BACKUP_DIR/."
  fi
  exit 0
fi

# Extract the A/B target provider(s) so the config pin below can route them
# correctly. G06's default tiers are OpenAI-only, so without this a non-OpenAI
# `--ab --providers <p>` gets silently rerouted to gpt-4o-mini (the A/B then
# compares two different models). Supports `--providers x` and `--providers=x`.
AB_PROVIDERS="openai"
for ((i=0; i<${#ARGS[@]}; i++)); do
  case "${ARGS[$i]}" in
    --providers)   AB_PROVIDERS="${ARGS[$((i+1))]:-openai}" ;;
    --providers=*) AB_PROVIDERS="${ARGS[$i]#--providers=}" ;;
  esac
done
export AB_PROVIDERS

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

# 4. Proxy API key: env $PROXY_API_KEY -> .env PROXY_API_KEY= -> .env
#    ROI_PROXY_API_KEY_* -> generate (first run). Set PROXY_API_KEY=tok-... in .env
#    to run with a fixed key and pass nothing at the command line.
key="${PROXY_API_KEY:-}"
[ -n "$key" ] || key="$(grep -E '^[[:space:]]*(export[[:space:]]+)?PROXY_API_KEY=' .env 2>/dev/null | head -1 | cut -d= -f2- | tr -d '[:space:]' | sed -E 's/^"(.*)"$/\1/; s/^'"'"'(.*)'"'"'$/\1/' || true)"
[ -n "$key" ] || key="$(grep -hoE 'ROI_PROXY_API_KEY_[A-Z_]+=tok-[A-Za-z0-9]+' .env 2>/dev/null | grep -oE 'tok-[A-Za-z0-9]+' | head -1 || true)"
if [ -z "$key" ] && [ ! -f config/local-keys.json ]; then
  info "no proxy key found - generating a local one"
  key="tok-$(openssl rand -hex 24)"
  hash="$(printf '%s' "$key" | sha256sum | awk '{print $1}')"
  # New-format admin key: admin scope lets run_benchmark.py select the tenant via
  # the X-Tenant-ID header (post key-authoritative tenancy). A legacy
  # {"hash":"admin"} string key would resolve to the "default" tenant and break
  # the benchmark's t:<tenant>: namespacing + clear-cache cleanup.
  printf '{"%s": {"tenant_id": "BENC-STG-01", "tier": "enterprise", "admin": true}}\n' "$hash" > config/local-keys.json
  info "wrote config/local-keys.json (proxy loads it on start)"
  REBUILD=1   # force a (re)start so the proxy picks up the new key
fi
[ -n "$key" ] || die "No proxy key found and config/local-keys.json already exists (hashes are one-way). Set PROXY_API_KEY, add ROI_PROXY_API_KEY_* to .env, or run: bash scripts/local/deploy-local.sh"

# 5. Pin the config, THEN bring the stack up --------------------------------------
# Order matters on a cold stack (2026-09-18). The launcher used to start the stack on the
# operator's config, wait for health, THEN pin and restart the proxy - two cold starts, the
# first one's warm-up thrown away (and on a fresh volume, its first model download with it).
# Pinning first means a stack that is not running starts exactly once, on the pinned config.
# A proxy that was already running still needs one restart to load it; nothing else does.
#
# What is pinned and why: examples/benchmark/pin_config.py (shared with run.ps1). The
# original config is restored and the proxy reloaded on exit, even on failure or Ctrl-C.
# Opt out with --no-pin-config to measure the live config as-is.
healthy() { curl -fsS http://localhost:4000/health >/dev/null 2>&1; }
proxy_started_at() { docker inspect -f '{{.State.StartedAt}}' token-opt-proxy 2>/dev/null || true; }
wait_healthy() {
  local tries="$1" ok=0
  for _ in $(seq 1 "$tries"); do if healthy; then ok=1; break; fi; sleep 3; done
  [ "$ok" = 1 ]
}
PINNED=0
restore_config() {
  local rc=$?
  if [ "$PINNED" = 1 ]; then
    PINNED=0
    info "restoring original proxy config + reloading proxy..."
    if [ -f "$ORIG_BACKUP" ]; then
      mv -f "$ORIG_BACKUP" config/config.yaml || info "config restore failed (pinned config left in place; rerun to self-heal from $ORIG_BACKUP)"
      rmdir "$BACKUP_DIR" 2>/dev/null || true
    else
      info "no pre-existing config.yaml — leaving benchmark config in place (matches first-run)"
    fi
    docker compose restart proxy >/dev/null 2>&1 || info "proxy restart on restore failed (non-fatal)"
    for _ in $(seq 1 30); do if healthy; then break; fi; sleep 2; done
  fi
  exit "$rc"
}
# INT/TERM as well as EXIT: a bare `trap ... EXIT` does not fire for every kill, and the
# whole point of the stable backup is that the paths we cannot trap still recover.
trap restore_config EXIT INT TERM

if [ "$PIN_CONFIG" = 1 ]; then
  [ -f config/config.yaml.template ] || die "config/config.yaml.template missing — cannot pin benchmark config (use --no-pin-config to skip)."
  restore_stranded
  if [ -f config/config.yaml ]; then
    { mkdir -p "$BACKUP_DIR" && cp config/config.yaml "$ORIG_BACKUP"; } \
      || die "could not write $ORIG_BACKUP — refusing to pin without a recoverable backup."
  fi
  info "pinning benchmark config (enabling G01/G05/G06/G08/G19/G22; disabling G28 CCR)..."
  PINNED=1   # before the write: a half-written pin must still be restored on exit
  python examples/benchmark/pin_config.py --operator-config "$ORIG_BACKUP" --providers "$AB_PROVIDERS" \
    || die "failed to generate pinned benchmark config."
fi

# The proxy image is rebuilt from the checked-out source on every run. Docker's layer cache
# makes that a no-op when nothing changed; without it a stack built before a `git pull`
# keeps measuring the old code with nothing to say so (2026-09-18: the calibration run was
# four proxy commits behind). A failed build (e.g. offline) warns and uses what exists.
before="$(proxy_started_at)"
if [ "$REBUILD" = 1 ]; then
  info "building + (re)starting stack (docker compose up -d --build)..."
  docker compose up -d --build || die "docker compose up failed. Try: bash scripts/local/deploy-local.sh"
else
  info "building the proxy image from your checkout (cached no-op if unchanged)..."
  docker compose build --quiet proxy \
    || info "WARNING: proxy image build failed - measuring the EXISTING image, which may not match your checkout."
  info "starting stack (docker compose up -d)..."
  docker compose up -d || die "docker compose up failed. Try: bash scripts/local/deploy-local.sh"
fi
info "waiting for proxy health..."
wait_healthy 40 || die "proxy did not become healthy in ~2min. Check: docker compose logs proxy"
after="$(proxy_started_at)"
if [ "$PINNED" = 1 ] && [ -n "$before" ] && [ "$before" = "$after" ]; then
  # Same process as before the pin: it is still running the operator's config.
  info "reloading the already-running proxy to load the pinned config..."
  docker compose restart proxy >/dev/null 2>&1 || die "proxy restart failed while pinning config."
  wait_healthy 40 || die "proxy did not become healthy after pinning config. Check: docker compose logs proxy"
fi
if [ "$PINNED" = 1 ]; then info "proxy healthy (pinned benchmark config active)"; else info "proxy healthy"; fi

# 6. Clear the RUN's tenant prior-run keys (only its own data) -----------------
# The flush must target the tenant the run ACTUALLY executes under, not the label
# we pass as X-Tenant-ID. An admin key honours our X-Tenant-ID (= BENCH_TENANT); a
# non-admin key (e.g. a real business tenant's tok- key set as PROXY_API_KEY)
# IGNORES it and runs under the key's OWN tenant. Flushing BENCH_TENANT in that
# case leaves the real namespace un-cleared, so cold mode reads stale cache hits.
# Resolve the effective tenant from the key hash against whichever key store is
# live: the OSS blob (config/local-keys.json) or the commercial Postgres proxy_keys.
BENCH_TENANT="${BENCHMARK_TENANT:-bench}"
resolve_effective_tenant() {
  local k="$1" fb="$2" h t
  h="$(printf '%s' "$k" | sha256sum | awk '{print $1}')"
  # 1. OSS blob store. admin key → our X-Tenant-ID (=fallback) wins; else its tenant.
  if [ -f config/local-keys.json ]; then
    t="$(python - "$h" "$fb" <<'PY' 2>/dev/null || true
import json, sys
h, fb = sys.argv[1], sys.argv[2]
try:
    store = json.load(open('config/local-keys.json'))
except Exception:
    raise SystemExit(0)
e = store.get(h)
if isinstance(e, dict):
    print(fb if e.get('admin') else e.get('tenant_id', fb))
elif isinstance(e, str):
    print(e)
PY
)"
    if [ -n "$t" ]; then printf '%s\n' "$t"; return 0; fi
  fi
  # 2. Commercial Postgres proxy_keys store (same service/creds as clear-cache.sh).
  local row tid adm
  row="$(docker compose exec -T postgres psql -U token_opt -d token_opt -tAF'|' -c \
       "SELECT tenant_id, admin FROM proxy_keys WHERE key_hash = '$h';" 2>/dev/null \
       | tr -d '[:space:]' | head -1 || true)"
  if [ -n "$row" ]; then
    tid="${row%%|*}"; adm="${row##*|}"
    if [ "$adm" = "t" ]; then printf '%s\n' "$fb"; else printf '%s\n' "$tid"; fi
    return 0
  fi
  # 3. Unknown key store → fall back to the label (prior behaviour).
  printf '%s\n' "$fb"
}
FLUSH_TENANT="$(resolve_effective_tenant "$key" "$BENCH_TENANT")"
if [ "$KEEP_CACHE" = 0 ]; then
  [ "$FLUSH_TENANT" = "$BENCH_TENANT" ] || \
    info "proxy key runs under tenant '$FLUSH_TENANT' (not '$BENCH_TENANT') — flushing its namespace so cold mode is genuinely cold"
  bash "$HERE/clear-cache.sh" "$FLUSH_TENANT" || info "cache clear skipped (continuing)"
else
  info "keeping existing cache (--keep-cache)"
fi

# 7. Run (under the dedicated benchmark tenant) --------------------------------
if [ "$RUN_AB" = 1 ]; then
  # The A/B direct arm (arm A) calls providers via litellm, which reads keys from
  # the process environment. Load EVERY provider credential in .env so
  # `--ab --providers all` can reach each provider's direct arm — not just OpenAI.
  #   * LLM_KEY_<PROVIDER>  -> run_ab.py mirrors these to the native litellm vars
  #   * AZURE_/AWS_ extras  -> passed through as-is (azure endpoint, bedrock region)
  # Only these allow-listed names are exported; the rest of .env is left untouched.
  while IFS='=' read -r _name _value; do
    _name="$(printf '%s' "$_name" | sed -E 's/^[[:space:]]*(export[[:space:]]+)?//; s/[[:space:]]*$//')"
    case "$_name" in
      LLM_KEY_*|AZURE_API_BASE|AZURE_API_VERSION|AWS_ACCESS_KEY_ID|AWS_SECRET_ACCESS_KEY|AWS_REGION_NAME|AWS_SESSION_TOKEN)
        _value="$(printf '%s' "$_value" | tr -d '\r' | sed -E 's/^"(.*)"$/\1/; s/^'"'"'(.*)'"'"'$/\1/')"
        export "$_name=$_value" ;;
    esac
  done < .env
  # Mirror the proxy's LLM_KEY_OPENAI to OPENAI_API_KEY so arm A can authenticate.
  export OPENAI_API_KEY="${OPENAI_API_KEY:-$openai}"
  info "running A/B benchmark (proxy vs direct)..."
  RUNNER=(python examples/benchmark/run_ab.py)
else
  info "running benchmark..."
  RUNNER=(python examples/benchmark/run_benchmark.py)
fi
# The sidecar URL goes BEFORE the pass-through args, so a --sidecar-url of your own wins.
# The exit status is captured explicitly and handed to the EXIT trap, which restores the
# config and then exits with it - a failed or incomplete run must never read as success.
rc=0
"${RUNNER[@]}" --api-key "$key" --tenant "$BENCH_TENANT" \
  --sidecar-url "http://localhost:8080/compress" ${ARGS[@]+"${ARGS[@]}"} || rc=$?
[ "$rc" = 0 ] || info "run finished with exit status $rc (see the runner's output above)"
exit "$rc"
