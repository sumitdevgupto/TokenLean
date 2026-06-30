"""
G17 Loop Control - x-token-opt-state Header Tests

Tests the x-token-opt-state HTTP header:
- Header format (base64-encoded JSON)
- Header presence in response
- State serialization/deserialization
"""
import base64
import json
import pytest
from middleware.g17_loop_control import InterAgentState


class TestInterAgentStateHeader:
    """Test InterAgentState header serialization."""

    def test_to_header_value_base64_encoded(self):
        """Test that to_header_value produces base64-encoded JSON."""
        state = InterAgentState(
            token_budget_remaining=500,
            workflow_turn=3,
            max_iterations=5,
            confidence_score=0.85,
            wall_clock_elapsed_seconds=1.5,
            stop_reason="high_confidence",
        )

        header_value = state.to_header_value()

        # Should be valid base64
        decoded = base64.b64decode(header_value)
        data = json.loads(decoded.decode("utf-8"))

        assert data["token_budget_remaining"] == 500
        assert data["workflow_turn"] == 3
        assert data["max_iterations"] == 5
        assert data["confidence_score"] == 0.85
        assert data["wall_clock_elapsed_seconds"] == 1.5
        assert data["stop_reason"] == "high_confidence"

    def test_from_header_value_parsing(self):
        """Test that from_header_value correctly parses header."""
        original = InterAgentState(
            token_budget_remaining=1000,
            workflow_turn=1,
            max_iterations=10,
            confidence_score=None,
            wall_clock_elapsed_seconds=None,
            stop_reason=None,
        )

        header_value = original.to_header_value()
        parsed = InterAgentState.from_header_value(header_value)

        assert parsed.token_budget_remaining == 1000
        assert parsed.workflow_turn == 1
        assert parsed.max_iterations == 10
        assert parsed.confidence_score is None
        assert parsed.wall_clock_elapsed_seconds is None
        assert parsed.stop_reason is None

    def test_round_trip_serialization(self):
        """Test that serialize -> deserialize preserves all fields."""
        original = InterAgentState(
            token_budget_remaining=750,
            workflow_turn=2,
            max_iterations=5,
            confidence_score=0.92,
            wall_clock_elapsed_seconds=3.14,
            stop_reason="budget_low",
        )

        header_value = original.to_header_value()
        restored = InterAgentState.from_header_value(header_value)

        assert restored.token_budget_remaining == original.token_budget_remaining
        assert restored.workflow_turn == original.workflow_turn
        assert restored.max_iterations == original.max_iterations
        assert restored.confidence_score == original.confidence_score
        assert restored.wall_clock_elapsed_seconds == original.wall_clock_elapsed_seconds
        assert restored.stop_reason == original.stop_reason

    def test_header_value_is_compact(self):
        """Test that header value is compact (no extra whitespace)."""
        state = InterAgentState(
            token_budget_remaining=500,
            workflow_turn=1,
            max_iterations=5,
        )

        header_value = state.to_header_value()
        decoded = base64.b64decode(header_value)
        json_str = decoded.decode("utf-8")

        # Should use compact separators (no spaces after commas/colons)
        assert ", " not in json_str
        assert ": " not in json_str


class TestG17HeaderInResponse:
    """Test x-token-opt-state header in HTTP response."""

    @pytest.mark.asyncio
    async def test_header_present_in_response(self, client):
        """Test that response includes x-token-opt-state header."""
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": "Hello"}],
                "workflow_id": "test-workflow-123",  # Add workflow_id to trigger G17
            },
            headers={"Authorization": "Bearer test-key"},
        )

        assert response.status_code == 200
        assert "x-token-opt-state" in response.headers

    @pytest.mark.asyncio
    async def test_header_contains_valid_state(self, client):
        """Test that header contains valid InterAgentState."""
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": "Hello"}],
                "workflow_id": "test-workflow-456",  # Add workflow_id to trigger G17
            },
            headers={"Authorization": "Bearer test-key"},
        )

        header_value = response.headers.get("x-token-opt-state")
        assert header_value is not None

        # Should be parseable
        state = InterAgentState.from_header_value(header_value)
        assert state.token_budget_remaining >= 0
        assert state.workflow_turn >= 1
        assert state.max_iterations >= 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
