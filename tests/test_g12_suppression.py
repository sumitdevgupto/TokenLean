"""
G12 Reasoning Budget - Suppression Prompt Tests

Tests reasoning suppression injection by effort level:
- Low effort: "Give final answer only. No explanation."
- Medium effort: "Be concise. Brief explanation only if necessary."
- High effort: No suppression (default)
"""
import pytest
from unittest.mock import MagicMock
from middleware import RequestContext
from middleware.g12_reasoning_budget import G12ReasoningBudget, _inject_suppression


class TestG12SuppressionByEffort:
    """Test suppression prompt injection by effort level."""

    @pytest.fixture
    def g12(self):
        return G12ReasoningBudget()

    @pytest.fixture
    def ctx_low_effort(self):
        """Create context with low effort (maximum suppression)."""
        ctx = MagicMock(spec=RequestContext)
        ctx.config = {
            "groups": {
                "G12_reasoning": {
                    "enabled": True,
                    "default_effort": "low",
                    "effort_map": {
                        "low": {"tokens": 1000},
                        "medium": {"tokens": 2000},
                        "high": {"tokens": 4000}
                    },
                    "provider_params": [
                        {"model_fragment": "gpt-4o", "param_key": "max_tokens"}
                    ],
                    "reasoning_suppression_prompts": {
                        "low": "Give final answer only. No explanation.",
                        "medium": "Be concise. Brief explanation only if necessary.",
                    }
                }
            }
        }
        ctx.messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "What is 2+2?"}
        ]
        ctx.current_token_count = 50
        ctx.model = "gpt-4o-mini"
        ctx.routed_model = "gpt-4o-mini"
        ctx.request_id = "test-low-001"
        ctx.savings = MagicMock()
        ctx.savings.add_step = MagicMock()
        ctx.params = {}
        return ctx

    @pytest.fixture
    def ctx_medium_effort(self):
        """Create context with medium effort."""
        ctx = MagicMock(spec=RequestContext)
        ctx.config = {
            "groups": {
                "G12_reasoning": {
                    "enabled": True,
                    "default_effort": "medium",
                    "effort_map": {
                        "low": {"tokens": 1000},
                        "medium": {"tokens": 2000},
                        "high": {"tokens": 4000}
                    },
                    "provider_params": [
                        {"model_fragment": "gpt-4o", "param_key": "max_tokens"}
                    ],
                    "reasoning_suppression_prompts": {
                        "low": "Give final answer only. No explanation.",
                        "medium": "Be concise. Brief explanation only if necessary.",
                    }
                }
            }
        }
        ctx.messages = [
            {"role": "system", "content": "You are a helpful assistant."}
        ]
        ctx.current_token_count = 50
        ctx.model = "gpt-4o"
        ctx.routed_model = "gpt-4o"
        ctx.request_id = "test-med-001"
        ctx.savings = MagicMock()
        ctx.savings.add_step = MagicMock()
        ctx.params = {}
        return ctx

    @pytest.fixture
    def ctx_high_effort(self):
        """Create context with high effort (no suppression)."""
        ctx = MagicMock(spec=RequestContext)
        ctx.config = {
            "groups": {
                "G12_reasoning": {
                    "enabled": True,
                    "default_effort": "high",
                    "effort_map": {
                        "low": {"tokens": 1000},
                        "medium": {"tokens": 2000},
                        "high": {"tokens": 4000}
                    },
                    "provider_params": [
                        {"model_fragment": "gpt-4o", "param_key": "max_tokens"}
                    ],
                    "reasoning_suppression_prompts": {
                        "low": "Give final answer only. No explanation.",
                        "medium": "Be concise. Brief explanation only if necessary.",
                    }
                }
            }
        }
        ctx.messages = [
            {"role": "system", "content": "You are a helpful assistant."}
        ]
        ctx.current_token_count = 50
        ctx.model = "o1"
        ctx.routed_model = "o1"
        ctx.request_id = "test-high-001"
        ctx.savings = MagicMock()
        ctx.savings.add_step = MagicMock()
        ctx.params = {}
        return ctx

    @pytest.mark.asyncio
    async def test_low_effort_suppression_injected(self, g12, ctx_low_effort):
        """Test low effort gets maximum suppression."""
        result = await g12.process_request(ctx_low_effort)

        # Find system message
        system_msgs = [m for m in result.messages if m.get("role") == "system"]
        assert len(system_msgs) > 0
        assert "Give final answer only" in system_msgs[0].get("content", "")

    @pytest.mark.asyncio
    async def test_medium_effort_suppression_injected(self, g12, ctx_medium_effort):
        """Test medium effort gets moderate suppression."""
        result = await g12.process_request(ctx_medium_effort)

        system_msgs = [m for m in result.messages if m.get("role") == "system"]
        assert len(system_msgs) > 0
        assert "Be concise" in system_msgs[0].get("content", "")

    @pytest.mark.asyncio
    async def test_high_effort_no_suppression(self, g12, ctx_high_effort):
        """Test high effort has no suppression prompt."""
        original_content = ctx_high_effort.messages[0].get("content")
        result = await g12.process_request(ctx_high_effort)

        system_msgs = [m for m in result.messages if m.get("role") == "system"]
        # High effort doesn't inject suppression
        assert len(system_msgs) == 1
        assert system_msgs[0].get("content") == original_content


class TestG12SuppressionInjection:
    """Test the _inject_suppression helper function."""

    def test_append_to_existing_system_message(self):
        """Test suppression appends to existing system message."""
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hello"}
        ]
        suppression = "Give final answer only."

        result = _inject_suppression(messages, suppression)

        assert len(result) == 2
        assert result[0]["content"] == "You are helpful.\nGive final answer only."
        assert result[1] == {"role": "user", "content": "Hello"}

    def test_prepend_new_system_message(self):
        """Test suppression prepends new system message when none exists."""
        messages = [
            {"role": "user", "content": "Hello"}
        ]
        suppression = "Give final answer only."

        result = _inject_suppression(messages, suppression)

        assert len(result) == 2
        assert result[0] == {"role": "system", "content": "Give final answer only."}
        assert result[1] == {"role": "user", "content": "Hello"}

    def test_handles_multiple_system_messages(self):
        """Test suppression appends to last system message."""
        messages = [
            {"role": "system", "content": "Old system prompt."},
            {"role": "user", "content": "Hello"},
            {"role": "system", "content": "Current instructions."}
        ]
        suppression = "Be concise."

        result = _inject_suppression(messages, suppression)

        assert len(result) == 3
        assert result[0]["content"] == "Old system prompt."
        assert result[2]["content"] == "Current instructions.\nBe concise."


class TestG12AccuracyTradeoff:
    """Test accuracy vs token tradeoff with suppression."""

    @pytest.mark.asyncio
    async def test_suppression_reduces_output_length(self):
        """Test that suppression configuration is tracked in savings."""
        g12 = G12ReasoningBudget()
        ctx = MagicMock(spec=RequestContext)
        ctx.config = {
            "groups": {
                "G12_reasoning": {
                    "enabled": True,
                    "default_effort": "low",
                    "effort_map": {
                        "low": {"tokens": 1000},
                        "medium": {"tokens": 2000},
                        "high": {"tokens": 4000}
                    },
                    "provider_params": [
                        {"model_fragment": "gpt-4o", "param_key": "max_tokens"}
                    ],
                    "reasoning_suppression_prompts": {
                        "low": "Give final answer only."
                    }
                }
            }
        }
        ctx.messages = [{"role": "system", "content": "You are helpful."}]
        ctx.current_token_count = 100
        ctx.model = "gpt-4o-mini"
        ctx.routed_model = "gpt-4o-mini"
        ctx.request_id = "test-tradeoff-001"
        ctx.savings = MagicMock()
        ctx.savings.add_step = MagicMock()
        ctx.params = {}

        result = await g12.process_request(ctx)

        # Verify savings step recorded the suppression
        ctx.savings.add_step.assert_called_once()
        call_args = ctx.savings.add_step.call_args[0]
        assert "G12" in call_args[0]
        assert "low" in call_args[1]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
