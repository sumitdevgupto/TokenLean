"""The public A/B benchmark must measure the config a customer actually gets.

E22, 2026-09-06. `examples/benchmark/run.sh` pins a known-good config before running so the
result does not depend on whatever groups happen to be toggled on locally. That is sound — but
one of the pinned values had drifted away from the shipped default: `max_system_prompt_tokens`
was pinned to **800** while `config.yaml.template` ships **4096**, and every BFCL agentic
episode carries a ~1,046-token system prompt. So the cap fired on all 15 episodes in the
benchmark and would fire on none of them for a customer running defaults. It was worth roughly
half the published agentic figure (~20% → ~12% once removed) and it damped the run-to-run
spread, which widened from a claimed 19–25% to a measured 7–22%.

This is the same harness/product divergence found in the pitch harness the same day (E14), but
on the harness whose entire claim is that a skeptic can reproduce it. A number nobody can
reproduce with defaults is worse for trust than a smaller number, so the pins are now checked
against the template rather than trusted.

The test compares every numeric/string knob the launcher pins against the shipped template.
`enabled` flags are exempt by design — pinning which groups are ON is the launcher's stated
purpose, and the README discloses the set. Anything else needs a written reason in `DELIBERATE`.
"""
import pathlib
import re

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
RUN_SH = ROOT / "examples" / "benchmark" / "run.sh"
TEMPLATE = ROOT / "config" / "config.yaml.template"

# Knobs the benchmark may legitimately pin away from the template default.
# Each entry MUST carry a reason; an entry that no longer differs is reported as stale so the
# exception list cannot quietly outlive its justification.
DELIBERATE: dict[tuple[str, str], str] = {
    ("G1_compression", "sidecar_url"): (
        "container-network address for the compose stack the benchmark starts; the template "
        "carries the deployment-neutral value. Not a tuning knob."
    ),
}

_UPDATE = re.compile(r"_block\(\s*'([A-Za-z0-9_]+)'\s*\)\.update\(\s*\{(.*?)\}\s*\)", re.S)
_ASSIGN = re.compile(r"_block\(\s*'([A-Za-z0-9_]+)'\s*\)\[\s*'([A-Za-z0-9_]+)'\s*\]\s*=\s*([^\n]+)")
_KV = re.compile(r"'([A-Za-z0-9_]+)'\s*:\s*([^,}]+)")


def _literal(raw: str):
    raw = raw.strip().rstrip(",").strip()
    if raw in ("True", "False"):
        return raw == "True"
    try:
        return int(raw)
    except ValueError:
        pass
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
        return raw[1:-1]
    return None  # a computed expression — not a literal pin, nothing to compare


def _pinned() -> dict[tuple[str, str], object]:
    src = RUN_SH.read_text(encoding="utf-8")
    # Only the config-pinning heredoc assigns through _block(...); comments are stripped so a
    # commented-out example never registers as a live pin.
    src = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
    out: dict[tuple[str, str], object] = {}
    for group, body in _UPDATE.findall(src):
        for key, raw in _KV.findall(body):
            out[(group, key)] = _literal(raw)
    for group, key, raw in _ASSIGN.findall(src):
        out[(group, key)] = _literal(raw)
    return out


def _template_groups() -> dict:
    return (yaml.safe_load(TEMPLATE.read_text(encoding="utf-8")) or {}).get("groups", {}) or {}


class TestTheBenchmarkMeasuresTheShippedConfig:
    def test_it_still_pins_something(self):
        """Guard the guard: if the parse silently matched nothing, every assertion below would
        pass vacuously and the check would be worthless."""
        assert len(_pinned()) >= 4, f"parsed only {_pinned()!r} — the pin parser has drifted"

    def test_no_pinned_knob_differs_from_the_shipped_default(self):
        groups = _template_groups()
        drift = []
        for (group, key), value in sorted(_pinned().items()):
            if key == "enabled" or value is None:
                continue          # group selection is the launcher's job; non-literals aren't pins
            if (group, key) in DELIBERATE:
                continue
            block = groups.get(group)
            if not isinstance(block, dict) or key not in block:
                continue          # not a template knob (e.g. a benchmark-only routing map)
            if block[key] != value:
                drift.append(f"{group}.{key}: benchmark pins {value!r}, shipped ships {block[key]!r}")
        assert not drift, (
            "the public benchmark would measure a config no customer runs:\n  "
            + "\n  ".join(drift)
            + "\nFix the pin, or add it to DELIBERATE with the reason it must differ."
        )

    def test_the_system_prompt_cap_is_not_pinned_at_all(self):
        """The specific drift that caused E22. Leaving it unpinned means the benchmark inherits
        whatever the shipped default is, so this cannot silently diverge again."""
        assert ("G16_agent_arch", "max_system_prompt_tokens") not in _pinned(), (
            "pinning G16's system-prompt cap makes the agentic figure depend on a cap a default "
            "install does not apply — it was worth ~8 points of a ~20% published number (E22)"
        )

    def test_declared_exceptions_are_still_real(self):
        """A stale exception is a hole that looks like a decision."""
        groups = _template_groups()
        pinned = _pinned()
        stale = [
            f"{g}.{k}"
            for (g, k) in DELIBERATE
            if (g, k) in pinned
            and isinstance(groups.get(g), dict)
            and k in groups[g]
            and groups[g][k] == pinned[(g, k)]
        ]
        assert not stale, f"DELIBERATE entries no longer differ from the template: {stale} — drop them"
