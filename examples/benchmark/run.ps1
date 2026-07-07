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

# Separate launcher-only flags (--rebuild, --keep-cache) from run_benchmark.py args.
$rebuild = $false; $keepCache = $false; $passArgs = @()
foreach ($a in $args) {
    if     ($a -eq "--rebuild")    { $rebuild = $true }
    elseif ($a -eq "--keep-cache") { $keepCache = $true }
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

# 6. Clear the benchmark tenant's prior-run keys (only its own data) -----------
$benchTenant = if ($env:BENCHMARK_TENANT) { $env:BENCHMARK_TENANT } else { "bench" }
if (-not $keepCache) {
    & (Join-Path $here "clear-cache.ps1") $benchTenant
} else {
    Info "keeping existing cache (--keep-cache)"
}

# 7. Run (under the dedicated benchmark tenant) --------------------------------
Info "running benchmark..."
python examples/benchmark/run_benchmark.py --api-key $key --tenant $benchTenant @passArgs
