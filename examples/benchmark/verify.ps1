# Tenant self-verification (Windows / PowerShell) - preview YOUR savings before
# first prod traffic, against your ALREADY-LIVE TokenLean proxy. No Docker.
#
# One command after `git clone` + `cd TokenLean\examples\benchmark`:
#
#   .\verify.ps1 --proxy-url https://<your-proxy>.run.app --api-key tok-... --provider-key sk-...
#
# Creates a throwaway venv, installs deps, runs the TRUE A/B (direct-to-provider
# vs through your proxy, compared on provider-billed usage), prints the table.
#
#   --provider-key KEY  your OpenAI key for the direct arm (sets OPENAI_API_KEY);
#                       required unless a provider key is already in the environment
#   --providers LIST    default 'openai'; 'all' or a comma list (needs each key in env)
#   ...any other run_ab.py flag (--mode cold, --limit 10, --max-spend-per-provider 0.5, --judge)
#
# Always does the true A/B, so a provider key is required (onboarding never gives
# you one - BYOK tenants already have theirs). Only the bundled PUBLIC dataset is
# sent to the provider, never your data.

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
function Info($m) { Write-Host "[verify] $m" -ForegroundColor Cyan }
function Die($m)  { Write-Host "[verify] ERROR: $m" -ForegroundColor Red; exit 1 }

# Split out --provider-key (consumed); capture --proxy-url/--api-key; pass the rest.
$proxyUrl = ""; $apiKey = ""; $providerKey = ""; $pass = @()
for ($i = 0; $i -lt $args.Count; $i++) {
    switch ($args[$i]) {
        "--provider-key" { $providerKey = $args[++$i] }
        "--proxy-url"    { $proxyUrl = $args[++$i]; $pass += @("--proxy-url", $proxyUrl) }
        "--api-key"      { $apiKey = $args[++$i]; $pass += @("--api-key", $apiKey) }
        default          { $pass += $args[$i] }
    }
}

if (-not $proxyUrl) { Die "missing --proxy-url (your live proxy base URL, e.g. https://xxx.run.app)" }
if (-not $apiKey)   { Die "missing --api-key (your tenant proxy key, tok-...)" }

if ($providerKey) { $env:OPENAI_API_KEY = $providerKey }
$keyVars = @("OPENAI_API_KEY","LLM_KEY_OPENAI","ANTHROPIC_API_KEY","LLM_KEY_ANTHROPIC","GEMINI_API_KEY",
             "LLM_KEY_GEMINI","MISTRAL_API_KEY","LLM_KEY_MISTRAL","GROQ_API_KEY","LLM_KEY_GROQ",
             "DEEPSEEK_API_KEY","LLM_KEY_DEEPSEEK","XAI_API_KEY","LLM_KEY_XAI","COHERE_API_KEY",
             "LLM_KEY_COHERE","AZURE_API_KEY","AWS_ACCESS_KEY_ID")
$haveKey = $false
foreach ($v in $keyVars) { if ([Environment]::GetEnvironmentVariable($v)) { $haveKey = $true; break } }
if (-not $haveKey) { Die "no provider key found. This flow does a TRUE A/B and needs your own provider key. Pass --provider-key sk-... (OpenAI) or set LLM_KEY_<PROVIDER> and use --providers." }

$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) { $py = (Get-Command python3 -ErrorAction SilentlyContinue).Source }
if (-not $py) { Die "python not found. Install Python 3.10+ and retry." }

$venv = Join-Path $here ".venv-verify"
$vpy = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $venv)) {
    Info "creating venv + installing deps (httpx, litellm)..."
    & $py -m venv $venv
    & (Join-Path $venv "Scripts\pip.exe") install --quiet --upgrade pip | Out-Null
    & (Join-Path $venv "Scripts\pip.exe") install --quiet httpx litellm | Out-Null
}

if (-not (Test-Path (Join-Path $here "public_dataset.jsonl"))) {
    Info "building dataset artifacts (fixture)..."
    & $vpy (Join-Path $here "build_public_dataset.py") | Out-Null
}

Info "running A/B against your live proxy (bundled public dataset only - none of your data is sent)..."
# --require-direct: hard-fail if a selected provider lacks a key (always-A/B).
# --no-cache-flush: cannot flush a live proxy; bundled items are new to your tenant
#   cache namespace so cold=misses / replay=hits holds. No --tenant: the proxy key
#   self-identifies your tenant.
& $vpy (Join-Path $here "run_ab.py") --require-direct --no-cache-flush --mode both @pass
exit $LASTEXITCODE
