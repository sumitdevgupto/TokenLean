"""Unit tests for G12 — Reasoning suppression prompt injection (G12.2/G12.3)."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

import pytest
from providers.anthropic_adapter import AnthropicAdapter


@pytest.mark.asyncio
class TestG12ReasoningSuppression:
    async def test_low_effort_injects_suppression(self, make_ctx):
        """Low effort should append suppression prompt to last system message."""
        ctx = make_ctx(
            [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "Solve this math problem."},
            ],
            model="o1",
        )
        ctx.config["groups"]["G12_reasoning"]["default_effort"] = "low"

        from middleware.g12_reasoning_budget import G12ReasoningBudget
        ctx = await G12ReasoningBudget().process_request(ctx)

        # reasoning_effort should be set
        assert ctx.params.get("reasoning_effort") == "low"
        # Suppression prompt should be appended to system message
        system_msgs = [m for m in ctx.messages if m.get("role") == "system"]
        assert len(system_msgs) == 1
        assert "[BUDGET]" in system_msgs[0]["content"]
        assert "final answer only" in system_msgs[0]["content"].lower()

    async def test_medium_effort_injects_suppression(self, make_ctx):
        """Medium effort should append medium suppression prompt."""
        ctx = make_ctx(
            [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "Explain quantum computing."},
            ],
            model="claude-sonnet-4-5",
        )
        ctx.provider_adapter = AnthropicAdapter()
        ctx.config["groups"]["G12_reasoning"]["default_effort"] = "medium"

        from middleware.g12_reasoning_budget import G12ReasoningBudget
        ctx = await G12ReasoningBudget().process_request(ctx)

        # Claude thinking budget should be set
        assert "thinking" in ctx.params
        # Suppression prompt should be present
        system_msgs = [m for m in ctx.messages if m.get("role") == "system"]
        assert "[BUDGET]" in system_msgs[0]["content"]
        assert "minimal" in system_msgs[0]["content"].lower()

    async def test_high_effort_no_suppression(self, make_ctx):
        """High effort should NOT inject suppression — allow full reasoning."""
        ctx = make_ctx(
            [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "Prove Fermat's Last Theorem."},
            ],
            model="o1",
        )
        ctx.config["groups"]["G12_reasoning"]["default_effort"] = "high"

        from middleware.g12_reasoning_budget import G12ReasoningBudget
        ctx = await G12ReasoningBudget().process_request(ctx)

        # reasoning_effort should be high
        assert ctx.params.get("reasoning_effort") == "high"
        # No suppression prompt should be present
        system_msgs = [m for m in ctx.messages if m.get("role") == "system"]
        assert "[BUDGET]" not in system_msgs[0]["content"]
        assert system_msgs[0]["content"] == "You are a helpful assistant."

    async def test_no_system_message_prepends_suppression(self, make_ctx):
        """If there is no system message, suppression should be prepended."""
        ctx = make_ctx(
            [
                {"role": "user", "content": "What is 2+2?"},
            ],
            model="o1",
        )
        ctx.config["groups"]["G12_reasoning"]["default_effort"] = "low"

        from middleware.g12_reasoning_budget import G12ReasoningBudget
        ctx = await G12ReasoningBudget().process_request(ctx)

        # A new system message should be prepended
        assert ctx.messages[0]["role"] == "system"
        assert "[BUDGET]" in ctx.messages[0]["content"]

    async def test_claude_model_low_effort_suppression_and_budget(self, make_ctx):
        """Claude at low effort should get both suppression + thinking budget."""
        ctx = make_ctx(
            [
                {"role": "system", "content": "Be concise."},
                {"role": "user", "content": "Summarise the report."},
            ],
            model="claude-haiku-4-5",
        )
        ctx.provider_adapter = AnthropicAdapter()
        ctx.config["groups"]["G12_reasoning"]["default_effort"] = "low"

        from middleware.g12_reasoning_budget import G12ReasoningBudget
        ctx = await G12ReasoningBudget().process_request(ctx)

        # Claude thinking budget
        assert "thinking" in ctx.params
        # Suppression prompt
        system_msgs = [m for m in ctx.messages if m.get("role") == "system"]
        assert "[BUDGET]" in system_msgs[0]["content"]

    async def test_missing_suppression_config_skips_injection(self, make_ctx):
        """If reasoning_suppression_prompts is not configured, no prompt should be injected."""
        ctx = make_ctx(
            [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "Hello."},
            ],
            model="o1",
        )
        ctx.config["groups"]["G12_reasoning"]["default_effort"] = "low"
        # Remove suppression config entirely
        del ctx.config["groups"]["G12_reasoning"]["reasoning_suppression_prompts"]

        from middleware.g12_reasoning_budget import G12ReasoningBudget
        ctx = await G12ReasoningBudget().process_request(ctx)

        # reasoning_effort still set
        assert ctx.params.get("reasoning_effort") == "low"
        # But no suppression prompt
        system_msgs = [m for m in ctx.messages if m.get("role") == "system"]
        assert "[BUDGET]" not in system_msgs[0]["content"]


@pytest.mark.asyncio
class TestG12BaselineImmutability:
    """A3 — `baseline_tokens` is the immutable ingress count and must survive
    G12 suppression (which appends prompt text) + G06 routing. Regression guard
    for the A1 fix that removed `ctx.savings.baseline_tokens = tokens_after`.
    """

    async def test_baseline_unchanged_through_g12_suppression_and_g06(self, make_ctx):
        from savings.calculator import count_request_tokens
        from middleware.g12_reasoning_budget import G12ReasoningBudget
        from middleware.g06_routing import G06Routing

        ctx = make_ctx(
            [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "What is the capital of France?"},
            ],
            model="o1",
        )
        ctx.config["groups"]["G12_reasoning"]["default_effort"] = "low"

        expected_baseline = count_request_tokens(
            ctx.original_messages, ctx.model, ctx.params.get("tools")
        )
        assert ctx.savings.baseline_tokens == expected_baseline

        # G12 suppression appends prompt text → messages grow (the exact path that
        # used to clobber baseline_tokens). Baseline must stay put.
        ctx = await G12ReasoningBudget().process_request(ctx)
        system_msgs = [m for m in ctx.messages if m.get("role") == "system"]
        assert "[BUDGET]" in system_msgs[0]["content"]
        assert ctx.savings.baseline_tokens == expected_baseline

        # G06 routing may change routed_model but never the ingress baseline.
        ctx = await G06Routing().process_request(ctx)
        assert ctx.savings.baseline_tokens == expected_baseline

    async def test_baseline_includes_tools_and_survives_g12(self, minimal_config):
        import copy
        from middleware import RequestContext
        from savings.calculator import count_request_tokens, count_messages_tokens
        from middleware.g12_reasoning_budget import G12ReasoningBudget

        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Book a flight to Paris."},
        ]
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "book_flight",
                    "description": "Book a flight to a destination",
                    "parameters": {
                        "type": "object",
                        "properties": {"destination": {"type": "string"}},
                    },
                },
            }
        ]
        cfg = copy.deepcopy(minimal_config)
        cfg["groups"]["G12_reasoning"]["default_effort"] = "low"
        # RequestContext.create computes baseline via count_request_tokens (incl. tools)
        ctx = RequestContext.create(
            request_id="req-a3-tools",
            user_id="u1",
            messages=messages,
            model="o1",
            params={"tools": tools},
            config=cfg,
        )

        expected = count_request_tokens(messages, "o1", tools)
        # Tool definitions genuinely add to the ingress baseline.
        assert expected > count_messages_tokens(messages, "o1")
        assert ctx.savings.baseline_tokens == expected

        ctx = await G12ReasoningBudget().process_request(ctx)
        system_msgs = [m for m in ctx.messages if m.get("role") == "system"]
        assert "[BUDGET]" in system_msgs[0]["content"]
        # Baseline (incl. tool tokens) is untouched despite suppression growth.
        assert ctx.savings.baseline_tokens == expected
