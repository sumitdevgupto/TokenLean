# clear-cache.ps1 — delete ONLY the keys the benchmark inserted, nothing else.
#
# The benchmark runs under a dedicated tenant (default "bench", via the X-Tenant-ID
# header in run_benchmark.py), so every key it creates is namespaced under
# "t:<tenant>:". Deleting that prefix removes exactly the benchmark's data and
# leaves every other tenant's data and all global state untouched.
#
# Usage: clear-cache.ps1 [tenant]   (default tenant: bench)

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo = (Resolve-Path (Join-Path $here "..\..")).Path
Set-Location $repo

$tenant = if ($args.Count -ge 1) { $args[0] } else { "bench" }

# Single-quoted (literal) so PowerShell does not interpolate the shell $vars;
# tenant is passed as $1 to the inner sh.
$cmd = 'prefix="t:$1:"; keys=$(redis-cli KEYS "${prefix}*"); n=$(printf "%s" "$keys" | grep -c .); if [ "$n" -gt 0 ]; then printf "%s\n" "$keys" | xargs redis-cli del >/dev/null; fi; echo "[clear-cache] deleted $n key(s) under ${prefix}* (benchmark tenant only)"'
docker compose exec -T redis sh -c $cmd sh $tenant

# The L2 semantic cache lives in Postgres (cache_l2), not Redis — purge this
# tenant's rows too, or a prior run's stored answers replay on the next run.
docker compose exec -T postgres psql -U token_opt -d token_opt -c "DELETE FROM cache_l2 WHERE tenant_id = '$tenant';" *> $null
if ($LASTEXITCODE -eq 0) {
    Write-Host "[clear-cache] cleared Postgres L2 rows for tenant '$tenant'"
} else {
    Write-Host "[clear-cache] Postgres L2 clear skipped (table absent or psql unavailable)"
}
