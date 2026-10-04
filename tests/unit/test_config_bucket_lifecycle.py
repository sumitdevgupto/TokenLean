"""
Guard: the config bucket's lifecycle rules must never delete a live config object.

The GCS bucket "config" in infra/main.tf holds two kinds of object: timestamped exports
under backups/, and the live config under config/ — config.yaml, local-keys.json (the
proxy key store), bypass rules, tool registry — which deploys upload and nothing else
refreshes. A Delete rule that is neither prefix-scoped away from config/ nor limited to
noncurrent versions removes the live config N days after each deploy, and the next cold
start boots with an empty config.

Regression: 2026-09-25 — the bucket carried one `Delete, age = 90` rule with no scope.
"""
import re
from pathlib import Path

MAIN_TF = Path(__file__).parent.parent.parent / "infra" / "main.tf"


def _block_from(text: str, open_brace: int) -> str:
    depth = 0
    for j in range(open_brace, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return text[open_brace:j + 1]
    raise AssertionError("unterminated HCL block")


def _config_bucket() -> str:
    text = MAIN_TF.read_text(encoding="utf-8")
    header = re.search(r'resource\s+"google_storage_bucket"\s+"config"\s*\{', text)
    assert header, "google_storage_bucket.config not found in infra/main.tf"
    return _block_from(text, header.end() - 1)


def _lifecycle_rules(bucket: str):
    return [_block_from(bucket, m.end() - 1)
            for m in re.finditer(r"^\s*lifecycle_rule\s*\{", bucket, re.M)]


def test_config_bucket_still_prunes_something():
    assert _lifecycle_rules(_config_bucket())


def test_no_delete_rule_can_reach_a_live_config_object():
    for rule in _lifecycle_rules(_config_bucket()):
        if not re.search(r'type\s*=\s*"Delete"', rule):
            continue
        noncurrent_only = re.search(r'with_state\s*=\s*"ARCHIVED"', rule)
        prefix = re.search(r"matches_prefix\s*=\s*\[([^\]]*)\]", rule)
        assert noncurrent_only or prefix, f"unscoped Delete rule:\n{rule}"
        if prefix and not noncurrent_only:
            prefixes = re.findall(r'"([^"]*)"', prefix.group(1))
            assert prefixes, f"empty matches_prefix:\n{rule}"
            reaching = [p for p in prefixes if not p or "config/".startswith(p)]
            assert not reaching, f"prefix {reaching} matches live config/ objects:\n{rule}"
