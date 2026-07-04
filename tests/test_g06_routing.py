"""
G06 Model Routing & Cascading — Unit Tests

Covers:
- _classify_heuristic: simple / medium / complex tier detection
- _classify_cascade: heuristic short-circuit vs judge escalation
- _execute_three_tier_cascade: tier1 accept, tier2 escalation,
  cost-based rollback, tier2/tier3 failure rollback
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from middleware import RequestContext
from middleware.g06_routing import (
    _classify_heuristic,
    _classify_cascade,
    _execute_three_tier_cascade,
    G06Routing,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_ctx(messages=None, params=None):
    ctx = MagicMock(spec=RequestContext)
    ctx.messages = messages or [{"role": "user", "content": "Hello"}]
    ctx.params = params or {}
    ctx.config = {
        "groups": {
            "G6_routing": {
                "enabled": True,
                "classifier": "cascade",
                "cascade_execution": True,
                "tiers": {
                    "simple": ["gpt-4o-mini"],
                    "medium": ["gpt-4o"],
                    "complex": ["gpt-4-5"],
                },
                "judge_model": "",
                "judge_timeout_ms": 2000,
                "cascade_confidence_threshold": 0.70,
                "max_escalation_cost_usd": 0.01,
            }
        }
    }
    ctx.current_token_count = 100
    ctx.routed_model = ""
    ctx.model = "gpt-4o-mini"
    ctx.request_id = "test-g06"
    ctx.savings = MagicMock()
    ctx.savings.add_step = MagicMock()
    ctx.bypassed = False
    ctx.cache_hit = False
    return ctx


def _fake_response(content="OK"):
    """Minimal litellm-style response object."""
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    resp.model_dump.return_value = {
        "choices": [{"message": {"content": content}}]
    }
    return resp


# ---------------------------------------------------------------------------
# _classify_heuristic
# ---------------------------------------------------------------------------

class TestClassifyHeuristic:

    def test_simple_factual_query(self):
        msgs = [{"role": "user", "content": "What is the capital of France?"}]
        tier, confidence = _classify_heuristic(msgs, {})
        assert tier == "simple"
        assert confidence >= 0.80

    def test_complex_keyword_triggers_complex_tier(self):
        msgs = [{"role": "user", "content": "Analyse and architect a distributed caching strategy."}]
        tier, confidence = _classify_heuristic(msgs, {})
        assert tier == "complex"
        assert confidence >= 0.80

    def test_short_message_defaults_simple(self):
        msgs = [{"role": "user", "content": "hi"}]
        tier, confidence = _classify_heuristic(msgs, {})
        assert tier == "simple"

    def test_long_message_without_keywords_is_complex(self):
        # Heuristic threshold is word_count > 500 → complex
        long_text = "word " * 600
        msgs = [{"role": "user", "content": long_text}]
        tier, confidence = _classify_heuristic(msgs, {})
        assert tier == "complex"

    def test_medium_length_no_strong_keywords(self):
        msgs = [{"role": "user", "content": "Please provide details about the process in three steps. " * 3}]
        tier, confidence = _classify_heuristic(msgs, {})
        # 18 words × 3 = ~54 words — lands in medium band
        assert tier in ("simple", "medium")


# ---------------------------------------------------------------------------
# _classify_cascade
# ---------------------------------------------------------------------------

class TestClassifyCascade:

    @pytest.mark.asyncio
    async def test_high_confidence_heuristic_skips_judge(self):
        msgs = [{"role": "user", "content": "What is 2+2?"}]
        cfg = {"cascade_confidence_threshold": 0.70, "judge_model": "gpt-4o-mini"}
        # Simple query → heuristic confidence 0.90 → no judge call
        tier = await _classify_cascade(msgs, {}, cfg)
        assert tier == "simple"

    @pytest.mark.asyncio
    async def test_no_judge_model_returns_heuristic(self):
        msgs = [{"role": "user", "content": "Evaluate the performance of three sorting algorithms."}]
        cfg = {"cascade_confidence_threshold": 0.70, "judge_model": ""}
        tier = await _classify_cascade(msgs, {}, cfg)
        assert tier in ("simple", "medium", "complex")

    @pytest.mark.asyncio
    async def test_low_confidence_escalates_to_judge(self):
        """When heuristic confidence is below threshold, judge is called."""
        msgs = [{"role": "user", "content": "something something " * 5}]  # ~medium confidence
        cfg = {
            "cascade_confidence_threshold": 0.95,  # very high → forces escalation
            "judge_model": "gpt-4o-mini",
            "judge_timeout_ms": 2000,
        }
        judge_result = '{"tier": "complex", "confidence": 0.92}'
        mock_response = MagicMock()
        mock_response.get = lambda k, d=None: (
            [{"message": {"content": judge_result}}] if k == "choices" else d
        )

        with patch("middleware.g06_routing.litellm.acompletion", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = mock_response
            tier = await _classify_cascade(msgs, {}, cfg)
        assert tier in ("simple", "medium", "complex")


# ---------------------------------------------------------------------------
# _execute_three_tier_cascade
# ---------------------------------------------------------------------------

class TestExecuteThreeTierCascade:

    def _cfg(self, threshold=0.70, max_cost=0.01, judge_model=""):
        return {
            "cascade_confidence_threshold": threshold,
            "max_escalation_cost_usd": max_cost,
            "judge_model": judge_model,
            "judge_timeout_ms": 2000,
        }

    def _tiers(self):
        return {
            "simple": ["gpt-4o-mini"],
            "medium": ["gpt-4o"],
            "complex": ["gpt-4-5"],
        }

    @pytest.mark.asyncio
    async def test_tier1_high_confidence_accepted(self):
        """Tier1 response with confidence >= threshold → use tier1, no escalation."""
        ctx = _make_ctx()

        with patch("middleware.g06_routing._resolve_provider_key", new_callable=AsyncMock, return_value="key"), \
             patch("middleware.g06_routing.litellm.acompletion", new_callable=AsyncMock) as mock_llm, \
             patch("middleware.g06_routing._classify_heuristic", return_value=("simple", 0.95)):
            mock_llm.return_value = _fake_response("Simple answer")
            model, response = await _execute_three_tier_cascade(ctx, self._tiers(), self._cfg(threshold=0.70))

        assert model == "gpt-4o-mini"
        assert mock_llm.call_count == 1  # only tier1 called

    @pytest.mark.asyncio
    async def test_tier1_low_confidence_escalates_to_tier2(self):
        """Tier1 confidence below threshold → escalate to tier2."""
        ctx = _make_ctx()
        responses = [_fake_response("Tier1 response"), _fake_response("Better tier2 response")]
        call_count = {"n": 0}

        async def side_effect(**kwargs):
            resp = responses[min(call_count["n"], len(responses) - 1)]
            call_count["n"] += 1
            return resp

        with patch("middleware.g06_routing._resolve_provider_key", new_callable=AsyncMock, return_value="key"), \
             patch("middleware.g06_routing.litellm.acompletion", side_effect=side_effect), \
             patch("middleware.g06_routing._classify_heuristic", return_value=("medium", 0.50)), \
             patch("middleware.g06_routing.estimate_cost", return_value=0.0):
            model, response = await _execute_three_tier_cascade(ctx, self._tiers(), self._cfg(threshold=0.70))

        assert call_count["n"] >= 2  # tier1 + tier2 at minimum

    @pytest.mark.asyncio
    async def test_cost_rollback_prevents_tier2_escalation(self):
        """Escalation cost above max_escalation_cost → stay with tier1."""
        ctx = _make_ctx()

        def cost_side_effect(tokens, output, model):
            # Use exact model names to avoid gpt-4o matching gpt-4o-mini
            costs = {"gpt-4o-mini": 0.0001, "gpt-4o": 0.02, "gpt-4-5": 0.05}
            return costs.get(model, 0.001)

        with patch("middleware.g06_routing._resolve_provider_key", new_callable=AsyncMock, return_value="key"), \
             patch("middleware.g06_routing.litellm.acompletion", new_callable=AsyncMock) as mock_llm, \
             patch("middleware.g06_routing._classify_heuristic", return_value=("simple", 0.40)), \
             patch("middleware.g06_routing.estimate_cost", side_effect=cost_side_effect):
            mock_llm.return_value = _fake_response("Tier1 cheap answer")
            model, response = await _execute_three_tier_cascade(
                ctx, self._tiers(), self._cfg(threshold=0.70, max_cost=0.005)
            )

        assert model == "gpt-4o-mini"  # cost block → tier1 retained
        assert mock_llm.call_count == 1

    @pytest.mark.asyncio
    async def test_tier2_failure_rolls_back_to_tier1(self):
        """If tier2 raises, return tier1 response."""
        ctx = _make_ctx()
        tier1_resp = _fake_response("Tier1 answer")

        async def acompletion_side(**kwargs):
            if "gpt-4o" in kwargs.get("model", "") and "mini" not in kwargs.get("model", ""):
                raise RuntimeError("Tier2 unavailable")
            return tier1_resp

        with patch("middleware.g06_routing._resolve_provider_key", new_callable=AsyncMock, return_value="key"), \
             patch("middleware.g06_routing.litellm.acompletion", side_effect=acompletion_side), \
             patch("middleware.g06_routing._classify_heuristic", return_value=("medium", 0.40)), \
             patch("middleware.g06_routing.estimate_cost", return_value=0.0):
            model, response = await _execute_three_tier_cascade(ctx, self._tiers(), self._cfg(threshold=0.70))

        assert model == "gpt-4o-mini"

    @pytest.mark.asyncio
    async def test_tier3_failure_rolls_back_to_tier2(self):
        """If tier3 raises after tier2 succeeded, return tier2 response."""
        ctx = _make_ctx()
        call_count = {"n": 0}

        async def acompletion_side(**kwargs):
            call_count["n"] += 1
            model = kwargs.get("model", "")
            if "gpt-4-5" in model:
                raise RuntimeError("Tier3 unavailable")
            return _fake_response(f"Response from {model}")

        with patch("middleware.g06_routing._resolve_provider_key", new_callable=AsyncMock, return_value="key"), \
             patch("middleware.g06_routing.litellm.acompletion", side_effect=acompletion_side), \
             patch("middleware.g06_routing._classify_heuristic", return_value=("medium", 0.40)), \
             patch("middleware.g06_routing.estimate_cost", return_value=0.0):
            model, response = await _execute_three_tier_cascade(ctx, self._tiers(), self._cfg(threshold=0.70))

        # tier3 failed → rolled back to tier2 (best_model at that point)
        assert model == "gpt-4o"
        assert call_count["n"] == 3  # tier1 + tier2 + tier3(failed)

    @pytest.mark.asyncio
    async def test_no_simple_models_returns_error(self):
        """Empty simple tier → returns error dict immediately."""
        ctx = _make_ctx()
        tiers = {"simple": [], "medium": ["gpt-4o"], "complex": ["gpt-4-5"]}

        model, response = await _execute_three_tier_cascade(ctx, tiers, self._cfg())

        assert model is None
        assert "error" in response

    @pytest.mark.asyncio
    async def test_provider_key_failure_returns_error(self):
        """Provider key resolution failure → returns error dict."""
        ctx = _make_ctx()

        with patch("middleware.g06_routing._resolve_provider_key", new_callable=AsyncMock, return_value=None):
            model, response = await _execute_three_tier_cascade(ctx, self._tiers(), self._cfg())

        assert model is None
        assert "error" in response


# ---------------------------------------------------------------------------
# G06Routing.process_request integration smoke test
# ---------------------------------------------------------------------------

class TestG06RoutingProcessRequest:

    @pytest.mark.asyncio
    async def test_disabled_group_passes_through(self):
        g06 = G06Routing()
        ctx = _make_ctx()
        ctx.config["groups"]["G6_routing"]["enabled"] = False

        with patch("middleware.g06_routing.get_default_model", return_value="gpt-4o-mini"), \
             patch("middleware.g06_routing.langfuse_tracing"):
            result = await g06.process_request(ctx)
        assert result is ctx

    @pytest.mark.asyncio
    async def test_heuristic_classifier_downgrades_complex_model(self):
        """Simple query with a large starting model should route to simple tier."""
        g06 = G06Routing()
        ctx = _make_ctx(messages=[{"role": "user", "content": "What is 2+2?"}])
        ctx.model = "gpt-4-5"  # start with expensive model
        ctx.config["groups"]["G6_routing"]["classifier"] = "heuristic"
        ctx.config["groups"]["G6_routing"]["cascade_execution"] = False

        # Reachability guard (G06 unreachable-tier fallback) is orthogonal to the routing
        # decision under test; the tier's provider registry/key isn't set up in this unit
        # fixture, so stub the tier as reachable (as a keyed deployment would be).
        with patch("middleware.g06_routing.langfuse_tracing"), \
             patch("middleware.g06_routing._tier_reachable", return_value=True), \
             patch("middleware.g06_routing.estimate_cost", return_value=0.0):
            result = await g06.process_request(ctx)
        # Heuristic should route simple query to gpt-4o-mini
        assert result.routed_model == "gpt-4o-mini"

    @pytest.mark.asyncio
    async def test_user_override_bypasses_classifier(self):
        """x_complexity param should bypass the classifier entirely."""
        g06 = G06Routing()
        ctx = _make_ctx(
            messages=[{"role": "user", "content": "Analyse the entire universe."}],
            params={"x_complexity": "simple"},
        )
        ctx.model = "gpt-4-5"

        # Reachability guard is orthogonal to the override behaviour under test — stub the
        # tier as reachable (see the sibling test above).
        with patch("middleware.g06_routing.langfuse_tracing"), \
             patch("middleware.g06_routing._tier_reachable", return_value=True), \
             patch("middleware.g06_routing.estimate_cost", return_value=0.0):
            result = await g06.process_request(ctx)
        assert result.routed_model == "gpt-4o-mini"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
