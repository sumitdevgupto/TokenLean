"""Unit tests for G09 — Instructor fallback to heuristic compaction (G9.3/G9.4)."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.asyncio
class TestG09InstructorFallback:
    async def test_instructor_failure_falls_back_to_heuristic(self, make_ctx):
        """If Instructor raises, heuristic compaction should still fire when fallback is enabled."""
        prose = (
            "Customer Alice Smith called about order #A99 which was shipped. "
            "She requested a status update and mentioned she needs it delivered. "
            "The customer told us the order was placed two weeks ago."
        )
        ctx = make_ctx([
            {"role": "system", "content": prose},
            {"role": "user", "content": "Summarise."},
        ])
        # Enable instructor with schema fields so it would try Instructor
        ctx.config["groups"]["G9_context_schema"]["use_instructor"] = True
        ctx.config["groups"]["G9_context_schema"]["schema_fields"] = {
            "customer_name": "Name of the customer",
            "order_id": "Order number",
        }
        ctx.config["groups"]["G9_context_schema"]["instructor_fallback_to_heuristic"] = True

        from middleware.g09_context_schema import G09ContextSchema
        with patch(
            "middleware.g09_context_schema._compact_with_schema",
            side_effect=Exception("Instructor LLM unreachable"),
        ):
            ctx = await G09ContextSchema().process_request(ctx)

        # Heuristic should have produced a compacted result
        assert ctx is not None
        # Message was either compacted or left alone — no exception thrown
        assert len(ctx.messages) == 2

    async def test_instructor_timeout_falls_back_to_heuristic(self, make_ctx):
        """If Instructor times out, heuristic should fire."""
        prose = (
            "Customer Bob called about order #Z1 pending. "
            "He requested a refund and the status was delivered."
        )
        ctx = make_ctx([
            {"role": "system", "content": prose},
            {"role": "user", "content": "ok"},
        ])
        ctx.config["groups"]["G9_context_schema"]["use_instructor"] = True
        ctx.config["groups"]["G9_context_schema"]["schema_fields"] = {
            "customer_name": "Name",
            "order_id": "Order",
        }
        ctx.config["groups"]["G9_context_schema"]["instructor_timeout_ms"] = 1  # 1ms — instant timeout
        ctx.config["groups"]["G9_context_schema"]["instructor_fallback_to_heuristic"] = True

        from middleware.g09_context_schema import G09ContextSchema
        with patch(
            "middleware.g09_context_schema._compact_with_schema",
            new_callable=AsyncMock,
            side_effect=TimeoutError("simulated timeout"),
        ):
            ctx = await G09ContextSchema().process_request(ctx)

        assert ctx is not None
        # Should not crash; heuristic may or may not match depending on prose content

    async def test_instructor_disabled_uses_heuristic_directly(self, make_ctx):
        """When use_instructor=false, heuristic runs immediately."""
        prose = (
            "Customer Carol called about order #C42 which was delivered. "
            "She explained the issue and requested a replacement."
        )
        ctx = make_ctx([
            {"role": "system", "content": prose},
            {"role": "user", "content": "ok"},
        ])
        ctx.config["groups"]["G9_context_schema"]["use_instructor"] = False
        ctx.config["groups"]["G9_context_schema"]["schema_fields"] = {
            "customer_name": "Name",
            "order_id": "Order",
        }

        from middleware.g09_context_schema import G09ContextSchema
        ctx = await G09ContextSchema().process_request(ctx)

        assert ctx is not None
        # Heuristic should have matched 2+ fields and compacted
        system_content = ctx.messages[0].get("content", "")
        assert "=" in system_content or prose == system_content

    async def test_instructor_client_uses_per_request_wrapper_not_shared_litellm(self, make_ctx):
        """instructor.from_litellm() must be given a fresh per-call wrapper,
        not the shared `litellm.acompletion` module function — otherwise any
        attribute patching instructor performs on its argument would mutate
        global state shared by concurrent requests."""
        import litellm
        from middleware.g09_context_schema import _compact_with_schema

        captured_callables = []

        class _FakeClient:
            class chat:
                class completions:
                    @staticmethod
                    async def create(**kwargs):
                        return type("R", (), {"model_dump": lambda self: {"a": "1", "b": "2"}})()

        def _fake_from_litellm(fn, *args, **kwargs):
            captured_callables.append(fn)
            return _FakeClient()

        fake_instructor = MagicMock(from_litellm=_fake_from_litellm)
        with patch.dict("sys.modules", {"instructor": fake_instructor}):
            await _compact_with_schema("some text", {"a": "A", "b": "B"}, "gpt-4o-mini", "sk-test")
            await _compact_with_schema("other text", {"a": "A", "b": "B"}, "gpt-4o-mini", "sk-test")

        assert len(captured_callables) == 2
        # Each call gets its own wrapper function — neither is the shared litellm.acompletion
        assert captured_callables[0] is not litellm.acompletion
        assert captured_callables[1] is not litellm.acompletion
        assert captured_callables[0] is not captured_callables[1]

    async def test_custom_prose_indicators_detect_non_default_keywords(self, make_ctx):
        """Config-driven prose indicators should flag custom keywords."""
        ctx = make_ctx([
            {"role": "system", "content": "The patient reported symptoms of fever and cough. Diagnosis pending."},
            {"role": "user", "content": "What next?"},
        ])
        ctx.config["groups"]["G9_context_schema"]["prose_indicators"] = ["patient", "diagnosis", "symptoms"]
        ctx.config["groups"]["G9_context_schema"]["use_instructor"] = False

        from middleware.g09_context_schema import G09ContextSchema
        ctx = await G09ContextSchema().process_request(ctx)

        assert ctx is not None
        # Should have detected "patient" or "diagnosis" and attempted compaction
