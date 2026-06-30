"""Unit tests for G28 — Contextual Content Reuse (CCR).

Focus: the system-role guard. In a pass-through chat completion there is no agent
loop to resolve a [CCR:ref] via headroom_retrieve, so G28 must never replace the
system instruction by default (doing so strips the policy/facts the answer needs).
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

import pytest

# A block comfortably over the default min_tokens (300) regardless of the estimator.
_BIG = "Policy: eu-west and eu-central are GDPR-compliant for EU data residency. " * 100


class TestProcessMessagesSystemGuard:
    """Direct tests for _process_messages role gating."""

    def test_system_role_preserved_by_default(self):
        from middleware.g28_ccr import _process_messages
        messages = [
            {"role": "system", "content": _BIG},
            {"role": "user", "content": _BIG},
        ]
        new_msgs, before, after = _process_messages(messages, None, 300, "gpt-4o-mini", 3600)
        # System instruction is preserved verbatim...
        assert new_msgs[0]["content"] == _BIG
        assert "[CCR:" not in new_msgs[0]["content"]
        # ...while the user block (over threshold) is replaced by a compact reference.
        assert new_msgs[1]["content"].startswith("[CCR:")
        assert after < before

    def test_system_role_compressed_when_opted_in(self):
        from middleware.g28_ccr import _process_messages
        messages = [{"role": "system", "content": _BIG}]
        new_msgs, before, after = _process_messages(
            messages, None, 300, "gpt-4o-mini", 3600, compress_system=True
        )
        assert new_msgs[0]["content"].startswith("[CCR:")
        assert after < before

    def test_short_content_left_untouched(self):
        from middleware.g28_ccr import _process_messages
        messages = [{"role": "user", "content": "hello there"}]
        new_msgs, before, after = _process_messages(messages, None, 300, "gpt-4o-mini", 3600)
        assert new_msgs[0]["content"] == "hello there"
        assert before == after


@pytest.mark.asyncio
class TestG28ProcessRequest:
    async def test_disabled_is_noop(self, make_ctx):
        ctx = make_ctx([{"role": "system", "content": _BIG}], model="gpt-4o-mini")
        ctx.config["groups"]["G28_ccr"] = {"enabled": False}
        original = list(ctx.messages)
        from middleware.g28_ccr import G28CCR
        ctx = await G28CCR().process_request(ctx)
        assert ctx.messages == original

    async def test_system_prompt_preserved_by_default(self, make_ctx):
        ctx = make_ctx(
            [{"role": "system", "content": _BIG},
             {"role": "user", "content": "Which regions are GDPR compliant?"}],
            model="gpt-4o-mini",
        )
        ctx.config["groups"]["G28_ccr"] = {"enabled": True, "min_tokens": 300}
        from middleware.g28_ccr import G28CCR
        ctx = await G28CCR().process_request(ctx)
        assert ctx.messages[0]["role"] == "system"
        assert ctx.messages[0]["content"] == _BIG  # verbatim, not a [CCR:...] reference

    async def test_compress_system_prompt_flag_wired(self, make_ctx):
        ctx = make_ctx([{"role": "system", "content": _BIG}], model="gpt-4o-mini")
        ctx.config["groups"]["G28_ccr"] = {
            "enabled": True, "min_tokens": 300, "compress_system_prompt": True,
        }
        from middleware.g28_ccr import G28CCR
        ctx = await G28CCR().process_request(ctx)
        assert ctx.messages[0]["content"].startswith("[CCR:")

    async def test_per_tenant_override_deep_merges(self, make_ctx):
        # A tenant flips compress_system_prompt without re-declaring the block; the
        # base keys (enabled/min_tokens) must survive the merge or G28 would no-op.
        ctx = make_ctx([{"role": "system", "content": _BIG}], model="gpt-4o-mini")
        ctx.config["groups"]["G28_ccr"] = {"enabled": True, "min_tokens": 300}
        ctx.config.setdefault("tenants", {})["acme"] = {
            "groups": {"G28_ccr": {"compress_system_prompt": True}}
        }
        ctx.tenant_id = "acme"
        from middleware.g28_ccr import G28CCR
        ctx = await G28CCR().process_request(ctx)
        assert ctx.messages[0]["content"].startswith("[CCR:")
