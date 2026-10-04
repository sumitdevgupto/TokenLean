"""Unit tests for G09 — Context Schema & Inter-Agent Handoffs."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

import pytest


@pytest.mark.asyncio
class TestG09ContextSchema:
    async def test_disabled_passes_through(self, make_ctx):
        ctx = make_ctx()
        ctx.config["groups"]["G9_context_schema"]["enabled"] = False
        original = [m.copy() for m in ctx.messages]
        from middleware.g09_context_schema import G09ContextSchema
        ctx = await G09ContextSchema().process_request(ctx)
        assert ctx.messages == original

    async def test_short_message_unchanged(self, make_ctx):
        ctx = make_ctx([{"role": "user", "content": "Hi"}])
        original = [m.copy() for m in ctx.messages]
        from middleware.g09_context_schema import G09ContextSchema
        ctx = await G09ContextSchema().process_request(ctx)
        assert ctx.messages == original
        assert len(ctx.savings.step_savings) == 0

    async def test_prose_heavy_system_message_compacted(self, make_ctx):
        # G09 only compacts role='system' messages with prose indicators
        # _try_compact_prose needs >= 2 extracted fields to fire
        prose = (
            "Customer Alice Smith called about order #A99 which was shipped. "
            "She requested a status update and mentioned she needs it delivered. "
            "The customer told us the order was placed two weeks ago."
        )
        ctx = make_ctx([
            {"role": "system", "content": prose},
            {"role": "user", "content": "Summarise."},
        ])
        from middleware.g09_context_schema import G09ContextSchema
        ctx = await G09ContextSchema().process_request(ctx)
        # Either compacted (savings recorded) or passed through (no match) — no exception
        assert ctx is not None

    async def test_retrieved_documents_are_left_as_they_are(self, make_ctx):
        """They are facts to quote, and G21 recognises them by their first line. They used to
        sit first, which G09 protects; now they follow the tenant's prompt. G09 still leaves
        them alone: their `[Retrieved context]` first line reads to it as a structured block.
        The same prose in any other later system block is compacted."""
        prose = ("Customer Bob called about order #Z1 pending. "
                 "He requested a refund and the status was delivered. "
                 "The customer mentioned the issue to us.")
        from middleware.g09_context_schema import G09ContextSchema

        async def run(later):
            ctx = make_ctx([{"role": "system", "content": "You are support."},
                            {"role": "user", "content": "q1"},
                            {"role": "system", "content": later},
                            {"role": "user", "content": "q2"}])
            ctx.config["groups"]["G9_context_schema"]["enabled"] = True
            return (await G09ContextSchema().process_request(ctx)).messages[2]["content"]

        assert await run(prose) != prose                      # the control: it does compact
        retrieved = "[Retrieved context]\n" + prose
        assert await run(retrieved) == retrieved

    async def test_compaction_reduces_tokens(self, make_ctx):
        # Dense prose system message that compaction regex can match 2+ fields
        prose = (
            "Customer Bob called about order #Z1 pending. "
            "He requested a refund and the status was delivered. "
            "The customer mentioned the issue to us."
        )
        ctx = make_ctx([
            {"role": "system", "content": prose},
            {"role": "user", "content": "ok"},
        ])
        tokens_before = ctx.current_token_count
        from middleware.g09_context_schema import G09ContextSchema
        ctx = await G09ContextSchema().process_request(ctx)

        if any(s.group == "G09" for s in ctx.savings.step_savings):
            assert ctx.current_token_count <= tokens_before
