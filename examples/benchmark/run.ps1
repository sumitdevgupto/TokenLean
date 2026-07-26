# One-command token-savings benchmark (Windows / PowerShell).
#
# Self-contained: checks prerequisites, creates config + a proxy key if missing,
# starts (and can rebuild) the local stack, then runs the benchmark. Depends only
# on the repo's docker-compose.yml + config template - not on scripts/.
#
#   .\examples\benchmark\run.ps1                  # run (starts stack if needed)
#   .\examples\benchmark\run.ps1 --rebuild        # rebuild images first (REQUIRED the first
#                                                 #   time after updating proxy code, e.g. the
#                                                 #   G06 routing fix this benchmark relies on)
#   .\examples\benchmark\run.ps1 --quality-check  # also assert each answer's curated facts
#                                                 #   (proves the savings did not hurt quality)
#   .\examples\benchmark\run.ps1 --limit 5        # pass-through args go to run_benchmark.py

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo = (Resolve-Path (Join-Path $here "..\..")).Path
Set-Location $repo

function Info($m) { Write-Host "[benchmark] $m" -ForegroundColor Cyan }
function Die($m)  { Write-Host "[benchmark] ERROR: $m" -ForegroundColor Red; exit 1 }

# Separate launcher-only flags (--rebuild, --keep-cache, --ab) from the runner's args.
#   --ab  run the true A/B harness (run_ab.py: proxy vs direct-to-provider on
#         provider-billed tokens) instead of the single-arm counterfactual.
$rebuild = $false; $keepCache = $false; $runAb = $false; $passArgs = @()
foreach ($a in $args) {
    if     ($a -eq "--rebuild")    { $rebuild = $true }
    elseif ($a -eq "--keep-cache") { $keepCache = $true }
    elseif ($a -eq "--ab")         { $runAb = $true }
    else   { $passArgs += $a }
}

# 1. Docker present + running ---------------------------------------------------
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { Die "Docker not found. Install Docker Desktop and retry." }
docker info *> $null
if ($LASTEXITCODE -ne 0) { Die "Docker daemon not running. Start Docker Desktop and retry." }

# 2. Proxy config - create from template on first run --------------------------
if (-not (Test-Path "config/config.yaml")) {
    if (-not (Test-Path "config/config.yaml.template")) { Die "config/config.yaml.template is missing." }
    Copy-Item "config/config.yaml.template" "config/config.yaml"
    Info "created config/config.yaml from template"
}

# 3. .env + the provider key the proxy uses (LLM_KEY_OPENAI) -------------------
if (-not (Test-Path ".env")) { Die ".env not found at repo root. Copy .env.template -> .env and set LLM_KEY_OPENAI." }
$envtext = Get-Content ".env" -Raw
$openai = [regex]::Match($envtext, '(?m)^\s*LLM_KEY_OPENAI=(.+)$').Groups[1].Value.Trim()
if (-not $openai) { Die "LLM_KEY_OPENAI is empty in .env - the proxy needs it for real OpenAI calls. Set LLM_KEY_OPENAI=sk-... (you can reuse your OPENAI_API_KEY value)." }

# 4. Proxy API key: env -> .env ROI_PROXY_API_KEY_* -> generate (first run) -----
$key = $env:PROXY_API_KEY
# Set PROXY_API_KEY=tok-... in .env to run with a fixed key and pass nothing at the CLI.
if (-not $key) { $key = [regex]::Match($envtext, '(?m)^\s*(?:export\s+)?PROXY_API_KEY=(.+)$').Groups[1].Value.Trim().Trim('"').Trim("'") }
if (-not $key) { $key = [regex]::Match($envtext, '(?m)^\s*(?:export\s+)?ROI_PROXY_API_KEY_\w+=(tok-\S+)\s*$').Groups[1].Value }
if ((-not $key) -and (-not (Test-Path "config/local-keys.json"))) {
    Info "no proxy key found - generating a local one"
    $bytes = New-Object 'System.Byte[]' 24
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $key = "tok-" + (($bytes | ForEach-Object { $_.ToString("x2") }) -join "")
    $sha = [System.Security.Cryptography.SHA256]::Create()
    $hash = (($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($key)) | ForEach-Object { $_.ToString("x2") }) -join "")
    # New-format admin key: admin scope lets run_benchmark.py select the tenant via
    # the X-Tenant-ID header (post key-authoritative tenancy). A legacy
    # {"hash":"admin"} string key would resolve to "default" and break the
    # benchmark's t:<tenant>: namespacing + clear-cache cleanup.
    "{`"$hash`": {`"tenant_id`": `"bench`", `"tier`": `"enterprise`", `"admin`": true}}" | Set-Content -Path "config/local-keys.json" -Encoding ascii
    Info "wrote config/local-keys.json (proxy loads it on start)"
    $rebuild = $true   # force a (re)start so the proxy picks up the new key
}
if (-not $key) { Die "No proxy key found and config/local-keys.json already exists (hashes are one-way). Set `$env:PROXY_API_KEY, add ROI_PROXY_API_KEY_* to .env, or run: bash scripts/local/deploy-local.sh" }

# 5. Ensure the stack is up (build so code changes are picked up) ---------------
function Test-ProxyHealthy {
    try { return (Invoke-WebRequest -Uri "http://localhost:4000/health" -TimeoutSec 3 -UseBasicParsing).StatusCode -eq 200 }
    catch { return $false }
}
if ($rebuild) {
    Info "building + (re)starting stack (docker compose up -d --build)..."
    docker compose up -d --build
    if ($LASTEXITCODE -ne 0) { Die "docker compose up failed. Try: bash scripts/local/deploy-local.sh" }
} elseif (Test-ProxyHealthy) {
    Info "proxy already healthy on :4000 (pass --rebuild to pick up code changes)"
} else {
    Info "starting stack (docker compose up -d) - builds images only if missing..."
    docker compose up -d
    if ($LASTEXITCODE -ne 0) { Die "docker compose up failed. Try: bash scripts/local/deploy-local.sh" }
}
if (-not (Test-ProxyHealthy)) {
    Info "waiting for proxy health..."
    $ok = $false
    for ($i = 0; $i -lt 40; $i++) { if (Test-ProxyHealthy) { $ok = $true; break }; Start-Sleep -Seconds 3 }
    if (-not $ok) { Die "proxy did not become healthy in ~2min. Check: docker compose logs proxy" }
}
Info "proxy healthy"

# 6. Clear the RUN's tenant prior-run keys (only its own data) -----------------
# Flush the tenant the run ACTUALLY executes under, not the X-Tenant-ID label. An
# admin key honours our X-Tenant-ID (= $benchTenant); a non-admin key (e.g. a real
# business tenant's tok- key set as PROXY_API_KEY) IGNORES it and runs under the
# key's OWN tenant, so flushing $benchTenant would leave that namespace un-cleared
# and cold mode would read stale cache hits. Resolve the effective tenant from the
# key hash against whichever store is live: OSS blob or commercial Postgres.
$benchTenant = if ($env:BENCHMARK_TENANT) { $env:BENCHMARK_TENANT } else { "bench" }
function Resolve-EffectiveTenant($k, $fb) {
    $sha = [System.Security.Cryptography.SHA256]::Create()
    $h = (($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($k)) | ForEach-Object { $_.ToString("x2") }) -join "")
    # 1. OSS blob store. admin key -> our X-Tenant-ID (=fallback) wins; else its tenant.
    if (Test-Path "config/local-keys.json") {
        try {
            $store = Get-Content "config/local-keys.json" -Raw | ConvertFrom-Json
            $e = $store.$h
            if ($e) {
                if ($e -is [string]) { return $e }
                if ($e.admin) { return $fb } else { if ($e.tenant_id) { return $e.tenant_id } else { return $fb } }
            }
        } catch { }
    }
    # 2. Commercial Postgres proxy_keys store (same service/creds as clear-cache.ps1).
    $row = (docker compose exec -T postgres psql -U token_opt -d token_opt -tAF'|' -c "SELECT tenant_id, admin FROM proxy_keys WHERE key_hash = '$h';" 2>$null | Select-Object -First 1)
    if ($LASTEXITCODE -eq 0 -and $row) {
        $parts = ($row -replace '\s', '').Split('|')
        if ($parts.Count -ge 2) {
            if ($parts[1] -eq 't') { return $fb } else { return $parts[0] }
        }
    }
    # 3. Unknown key store -> fall back to the label (prior behaviour).
    return $fb
}
$flushTenant = Resolve-EffectiveTenant $key $benchTenant
if (-not $keepCache) {
    if ($flushTenant -ne $benchTenant) {
        Info "proxy key runs under tenant '$flushTenant' (not '$benchTenant') - flushing its namespace so cold mode is genuinely cold"
    }
    & (Join-Path $here "clear-cache.ps1") $flushTenant
} else {
    Info "keeping existing cache (--keep-cache)"
}

# 7. Run (under the dedicated benchmark tenant) --------------------------------
if ($runAb) {
    # The A/B direct arm (arm A) calls providers via litellm, which reads keys from
    # the process environment. Load EVERY provider credential in .env so
    # `--ab --providers all` can reach each provider's direct arm - not just OpenAI.
    #   LLM_KEY_<PROVIDER> -> run_ab.py mirrors these to native litellm vars
    #   AZURE_/AWS_ extras -> passed through (azure endpoint, bedrock region)
    foreach ($line in (Get-Content ".env")) {
        if ($line -match '^\s*(?:export\s+)?(LLM_KEY_[A-Z0-9_]+|AZURE_API_BASE|AZURE_API_VERSION|AWS_ACCESS_KEY_ID|AWS_SECRET_ACCESS_KEY|AWS_REGION_NAME|AWS_SESSION_TOKEN)\s*=\s*(.*)$') {
            $n = $matches[1]; $v = $matches[2].Trim().Trim('"').Trim("'")
            Set-Item -Path "Env:$n" -Value $v
        }
    }
    # Mirror the proxy's LLM_KEY_OPENAI to OPENAI_API_KEY so arm A can authenticate.
    if (-not $env:OPENAI_API_KEY) { $env:OPENAI_API_KEY = $openai }
    Info "running A/B benchmark (proxy vs direct)..."
    python examples/benchmark/run_ab.py --api-key $key --tenant $benchTenant @passArgs
} else {
    Info "running benchmark..."
    python examples/benchmark/run_benchmark.py --api-key $key --tenant $benchTenant @passArgs
}
