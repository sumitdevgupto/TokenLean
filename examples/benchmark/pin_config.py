#!/usr/bin/env python3
"""Write the benchmark's pinned proxy config. Shared by run.sh and run.ps1.

The benchmark only measures techniques it can credit honestly black-box, and it can only do
that if those groups are actually enabled. Rather than trust whatever config happens to be
loaded (a config with these groups OFF silently reports a gutted pipeline), the launchers pin a
config derived from config.yaml.template (the calibrated baseline) with the six measured groups
force-enabled: G01 compression, G05 cache, G06 routing, G08 lazy tools, G19 pruning, G22 dedup.
G28 CCR is force-DISABLED: in a pass-through chat completion (no agent loop) it replaces an
over-threshold system prompt with a CCR reference token the model can't resolve, shredding the
policy facts the answers depend on - and it isn't one of the six measured techniques anyway.

This lived as a heredoc inside run.sh until 2026-09-18, so run.ps1 - the README's Windows
quick-start - had no pin at all and measured whatever config.yaml held. On a fresh clone that is
the template, whose G01 sidecar URL (`llmlingua-svc`) does not resolve in the compose stack, so
a Windows viewer could not reproduce the published number. One file, two launchers.

Observability is the one thing carried over from the operator's own config: whether Langfuse
tracing is on. It changes no token count, and the template ships it off, so pinning the template
value switched tracing OFF for every benchmark run and left the Langfuse-backed dashboards empty
for exactly the traffic an operator runs the benchmark to look at.

    python examples/benchmark/pin_config.py [--operator-config PATH] [--providers openai]
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
TEMPLATE = REPO / "config" / "config.yaml.template"
OUT = REPO / "config" / "config.yaml"

# Reporting-only G18 keys that follow the operator's config instead of the template. Nothing here
# may change what is sent to a provider - a key that could move a measured number must never be
# inherited from a local config, or the benchmark stops measuring what a customer runs.
OBSERVABILITY_CARRY_OVER = ("langfuse_enabled", "capture_trace_content")


def build(template: dict, operator: dict | None, providers: str = "openai") -> tuple[dict, list[str]]:
    """Return (pinned_config, notes). Pure - no I/O - so it can be unit-tested directly."""
    c = template
    notes: list[str] = []
    groups = c.setdefault('groups', {})
    # The six techniques this benchmark measures black-box.
    enable = ['G1_compression', 'G5_cache', 'G6_routing', 'G8_tools',
              'G19_headroom', 'g22_deduplication']
    # G28 CCR shreds an over-threshold system prompt into an unresolvable reference
    # token in pass-through mode (no agent loop to retrieve it) - keep it off.
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
    # Agentic lever (G08/G16 tool-catalogue pruning). Safe to enable globally: the
    # standard/cache workloads carry no tools and <=32-token system prompts, so G16 is a no-op
    # there; it only bites on the --workload agentic tool-heavy episodes.
    _block('G8_tools').update({'enabled': True, 'max_tools_per_agent': 20})
    # The system-prompt cap is deliberately NOT pinned here (2026-09-06). It used to be set to
    # 800 against a shipped default of 4096, and every BFCL episode carries a ~1,046-token system
    # prompt - so the cap fired on all 15 and contributed 7.22 percentage points of agentic
    # savings that nobody running a default install would ever see. This benchmark's whole claim
    # is that a skeptic can reproduce it, so it now runs the shipped default.
    _block('G16_agent_arch').update({'enabled': True, 'max_tools_per_agent': 20})
    # Provider-aware G06 routing. The template's tiers are OpenAI-only (simple->gpt-4o-mini, ...),
    # so a non-OpenAI `--ab --providers <p>` request would be silently rerouted to gpt-4o-mini and
    # the A/B would compare two different models. Set the tiers to the *target* provider's own
    # model ladder so G06 cascades within that provider. The OpenAI (default) path is left
    # untouched so its calibrated numbers stay byte-identical; a mixed/`all` run disables G06
    # (one static tier map can't route each provider within its own family).
    try:
        sys.path.insert(0, str(HERE))
        from run_ab import g06_pin_plan, resolve_providers
        action, tiers = g06_pin_plan(resolve_providers(providers))
    except Exception as exc:                       # fail safe: keep template tiers
        action, tiers = 'keep', None
        notes.append(f"note: G06 provider-aware pin skipped ({exc!r}); keeping template tiers")
    # The benchmark drives its OWN routing via the flat `tiers` map + g06_pin_plan (models
    # guaranteed present in prices.json), so drop the template's per-provider ladders - their
    # declared models aren't necessarily priced in the benchmark's prices.json and would crash
    # arm-B pricing on an escalation. Production keeps tiers_by_provider; the harness doesn't.
    _block('G6_routing').pop('tiers_by_provider', None)
    if action == 'disable':
        _block('G6_routing')['enabled'] = False
        notes.append(f"G06 routing: DISABLED for mixed providers '{providers}' (pass-through - no misroute)")
    elif action == 'tiers':
        _block('G6_routing')['tiers'] = tiers
        notes.append(f"G06 routing: tiers set to '{providers}' ladder {tiers}")
    else:
        notes.append(f"G06 routing: template tiers kept (providers='{providers}')")
    # G00 rate-limiting is a production THROUGHPUT guard, not a token-savings lever. The
    # --workload cache burst fires ~500 requests back-to-back (well over the template's 60/min
    # default), so the un-pinned limit 429s most of arm B and corrupts the measurement. Lift the
    # ceiling for the controlled benchmark burst so every request reaches the pipeline (savings
    # are unaffected - throttled vs served changes nothing about per-request token counts).
    rl = c.setdefault('rate_limit', {})
    rl['enabled'] = True
    rl.setdefault('default', {}).update({'requests_per_minute': 100000,
                                         'requests_per_hour': 1000000})
    # G01 LLMLingua sidecar URL: the template defaults to the DEPLOYED service name
    # (`llmlingua-svc`, the GCP/Cloud-Run convention), but the local docker-compose.yml names the
    # service `llmlingua` (no -svc alias) - so G01's compression calls silently DNS-fail here and
    # the prose-compression lever never fires. Point it at the compose service.
    _block('G1_compression')['sidecar_url'] = 'http://llmlingua:8080/compress'
    c.setdefault('services', {})['llmlingua_url'] = 'http://llmlingua:8080/compress'
    # Observability follows the operator (see module docstring) - reporting only.
    op_g18 = (((operator or {}).get('groups') or {}).get('G18_observability') or {})
    carried = []
    for key in OBSERVABILITY_CARRY_OVER:
        if key in op_g18:
            _block('G18_observability')[key] = op_g18[key]
            carried.append(f"{key}={op_g18[key]!r}")
    if carried:
        notes.append("observability kept from your config: " + ", ".join(carried))
    if created:
        notes.append("note: created missing group blocks: " + ", ".join(created))
    return c, notes


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--template", default=str(TEMPLATE))
    ap.add_argument("--operator-config", default="",
                    help="the operator's pre-pin config.yaml (its backup), for the observability "
                         "carry-over. Omit on a first run with no config.")
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--providers", default=os.environ.get("AB_PROVIDERS", "openai"))
    args = ap.parse_args()

    template = yaml.safe_load(Path(args.template).read_text(encoding="utf-8")) or {}
    operator = None
    if args.operator_config and Path(args.operator_config).exists():
        try:
            operator = yaml.safe_load(Path(args.operator_config).read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            print(f"  note: could not read {args.operator_config} ({exc}); observability "
                  "follows the template")
    pinned, notes = build(template, operator, args.providers)
    with open(args.out, "w", encoding="utf-8") as fh:
        yaml.safe_dump(pinned, fh, sort_keys=False)
    for n in notes:
        print("  " + n)
    print("  pinned config written; six groups + G16 agentic pruning enabled, G28 CCR disabled,")
    print("  G00 burst headroom raised, G01 LLMLingua sidecar pointed at the local compose service")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
