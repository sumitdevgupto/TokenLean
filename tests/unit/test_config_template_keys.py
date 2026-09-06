"""The shipped config must use the keys the middleware actually reads.

Backlog #6, finally closed 2026-09-06 after a code review showed its cost: the template
shipped G20 under `G20_prompt_optimization`, a key `g20_prompt_optimizer.py` never reads, so
`enabled: true` there was a no-op and every template-based deployment ran with G20 OFF —
while readiness ticked it, because the enable map could not see a key that did not exist
and assumed the group on. The same G09 false tick that morning's fix closed, still live for
G20 through a different door.

This pins the key for the one group that drifted, and the general property for all of them:
every `groups.*` key in the template is one some middleware module reads.
"""
import pathlib
import re

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "config" / "config.yaml.template"
MIDDLEWARE = ROOT / "src" / "proxy" / "middleware"

_GROUP_KEY_READ = re.compile(r'\.get\(\s*"groups"[^)]*\)\s*\.get\(\s*"([A-Za-z0-9_]+)"')
_GROUP_KEY_INDEX = re.compile(r'\[\s*"groups"\s*\]\s*\[\s*"([A-Za-z0-9_]+)"\s*\]')
_GROUP_KEY_RESOLVE = re.compile(r'resolve_group_config\(\s*\w+\s*,\s*"([A-Za-z0-9_]+)"')


def _template_groups():
    return set((yaml.safe_load(TEMPLATE.read_text(encoding="utf-8")) or {}).get("groups", {}))


def _keys_read_by_middleware():
    found = set()
    for path in MIDDLEWARE.glob("*.py"):
        src = path.read_text(encoding="utf-8")
        for rx in (_GROUP_KEY_READ, _GROUP_KEY_INDEX, _GROUP_KEY_RESOLVE):
            found.update(rx.findall(src))
    return found


class TestG20ShipsUnderTheKeyTheProxyReads:
    def test_the_real_key_is_present(self):
        assert "g20_prompt_optimizer" in _template_groups()

    def test_the_dead_key_is_gone(self):
        assert "G20_prompt_optimization" not in _template_groups(), (
            "G20_prompt_optimization is read by nothing — a config block under it is a no-op "
            "that LOOKS enabled (backlog #6)"
        )

    def test_it_ships_off_because_that_is_what_deploys_already_had(self):
        """Renaming the key must not silently turn G20 on: that is a savings-affecting
        change that needs a pitch-test-plan quality proof, and it is a decision, not a
        side effect of fixing a typo."""
        block = (yaml.safe_load(TEMPLATE.read_text(encoding="utf-8")))["groups"]["g20_prompt_optimizer"]
        assert block["enabled"] is False


class TestEveryTemplateGroupKeyIsReadBySomething:
    # Keys the template carries that are read outside `middleware/` or by construction
    # (the trust & safety groups read theirs via helpers this regex does not catch).
    _READ_ELSEWHERE = {
        "G3_doc_pipeline",      # src/doc-pipeline job, not chat middleware
        "G13_batch",            # read via _resolve_toon_cfg / batch helpers
        "G29_pii_redaction", "G30_guardrails", "G31_context_trust", "G32_tool_eligibility",
        "context_editing",      # provider adapter, not a G-group
        "G15_server_compute",   # resolve_group_config via a module constant
        "G28_ccr",              # resolve_group_config via a module constant
        "G26_context_budget",   # resolve_group_config via a module constant
    }

    def test_no_orphan_group_keys(self):
        orphans = _template_groups() - _keys_read_by_middleware() - self._READ_ELSEWHERE
        assert not orphans, (
            f"template ships group block(s) nothing reads: {sorted(orphans)} — a config block "
            f"under a dead key is a no-op that looks enabled. Fix the key, or add it to "
            f"_READ_ELSEWHERE with where it IS read."
        )
