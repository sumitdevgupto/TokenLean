"""Unit tests for G06 — Model Routing & Cascading."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

import pytest
from unittest.mock import MagicMock, Mock, patch


class FakeClient:
    """Minimal async context manager that avoids AsyncMock coroutine leaks."""
    def __init__(self, resp):
        self._resp = resp
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        return False
    async def post(self, *args, **kwargs):
        return self._resp


class FakeClientError(FakeClient):
    def __init__(self):
        self._resp = None
    async def post(self, *args, **kwargs):
        raise Exception("sidecar error")


@pytest.fixture(autouse=True)
def _g06_provider_keys():
    """Routing now checks tier-model provider reachability (P8 hardening). Provide a providers
    table + keys by default so these tests exercise classification, not key-gating; the no-key
    path has dedicated reachability tests (TestG06TierReachability) that override these."""
    providers = [
        {"name": "openai", "model_prefixes": ["gpt", "o1", "o3", "o4", "chatgpt"]},
        {"name": "anthropic", "model_prefixes": ["claude"]},
    ]
    with patch("middleware.g06_routing.get_providers", return_value=providers), \
         patch("auth.api_key_manager.get_llm_provider_key", return_value="sk-test"):
        yield


@pytest.mark.asyncio
class TestG06Routing:
    async def test_disabled_no_routing(self, make_ctx):
        ctx = make_ctx()
        ctx.config["groups"]["G6_routing"]["enabled"] = False
        original_model = ctx.routed_model
        from middleware.g06_routing import G06Routing
        ctx = await G06Routing().process_request(ctx)
        assert ctx.routed_model == original_model

    async def test_simple_query_routes_to_mini(self, make_ctx):
        ctx = make_ctx(
            [{"role": "user", "content": "What is Python?"}],
            model="gpt-4o",
        )
        from middleware.g06_routing import G06Routing
        ctx = await G06Routing().process_request(ctx)
        assert ctx.routed_model == "gpt-4o-mini"

    async def test_complex_query_routes_to_full_model(self, make_ctx):
        ctx = make_ctx(
            [{"role": "user", "content": "Please analyse and evaluate the architectural trade-offs between microservices and monolith."}],
            model="gpt-4o-mini",
        )
        ctx.config["groups"]["G6_routing"]["tiers"] = {
            "simple": ["gpt-4o-mini"],
            "medium": ["gpt-4o"],
            "complex": ["gpt-4-5"],
        }
        from middleware.g06_routing import G06Routing
        ctx = await G06Routing().process_request(ctx)
        assert ctx.routed_model == "gpt-4-5"

    async def test_routing_records_step_saving(self, make_ctx):
        ctx = make_ctx([{"role": "user", "content": "What is 2+2?"}], model="gpt-4o")
        from middleware.g06_routing import G06Routing
        ctx = await G06Routing().process_request(ctx)
        if ctx.routed_model != ctx.model:
            assert any(s.group == "G06" for s in ctx.savings.step_savings)

    async def test_already_cheapest_no_step_saved(self, make_ctx):
        ctx = make_ctx([{"role": "user", "content": "What is 2+2?"}], model="gpt-4o-mini")
        from middleware.g06_routing import G06Routing
        ctx = await G06Routing().process_request(ctx)
        # If simple tier is gpt-4o-mini and model is already gpt-4o-mini, no saving
        if ctx.routed_model == ctx.model:
            g06_steps = [s for s in ctx.savings.step_savings if s.group == "G06"]
            assert len(g06_steps) == 0

    async def test_empty_tiers_skips(self, make_ctx):
        ctx = make_ctx([{"role": "user", "content": "hi"}])
        ctx.config["groups"]["G6_routing"]["tiers"] = {}
        from middleware.g06_routing import G06Routing
        original = ctx.model
        ctx = await G06Routing().process_request(ctx)
        assert ctx.routed_model == original

    async def test_heuristic_classifier_explicit(self, make_ctx):
        ctx = make_ctx(
            [{"role": "user", "content": "Please analyse the architecture."}],
            model="gpt-4o",
        )
        ctx.config["groups"]["G6_routing"]["classifier"] = "heuristic"
        ctx.config["groups"]["G6_routing"]["tiers"] = {
            "simple": ["gpt-4o-mini"],
            "medium": ["gpt-4o"],
            "complex": ["gpt-4-5"],
        }
        from middleware.g06_routing import G06Routing
        ctx = await G06Routing().process_request(ctx)
        assert ctx.routed_model == "gpt-4-5"
        assert ctx.savings.routing_mode == "heuristic"

    async def test_llm_judge_calls_litellm(self, make_ctx):
        ctx = make_ctx(
            [{"role": "user", "content": "What is 2+2?"}],
            model="gpt-4o",
        )
        ctx.config["groups"]["G6_routing"]["classifier"] = "llm_judge"
        ctx.config["groups"]["G6_routing"]["judge_model"] = "gpt-4o-mini"
        async def mock_acompletion(*args, **kwargs):
            return {
                "choices": [
                    {"message": {"content": '{"tier":"simple","confidence":0.95}'}}
                ]
            }
        with patch("middleware.g06_routing.litellm.acompletion", mock_acompletion):
            from middleware.g06_routing import G06Routing
            ctx = await G06Routing().process_request(ctx)
        assert ctx.routed_model == "gpt-4o-mini"
        assert ctx.savings.routing_mode == "llm_judge"

    async def test_llm_judge_fallback_on_error(self, make_ctx):
        ctx = make_ctx(
            [{"role": "user", "content": "What is 2+2?"}],
            model="gpt-4o",
        )
        ctx.config["groups"]["G6_routing"]["classifier"] = "llm_judge"
        ctx.config["groups"]["G6_routing"]["judge_model"] = "gpt-4o-mini"
        async def mock_acompletion(*args, **kwargs):
            raise Exception("judge error")
        with patch("middleware.g06_routing.litellm.acompletion", mock_acompletion):
            from middleware.g06_routing import G06Routing
            ctx = await G06Routing().process_request(ctx)
        # Falls back to heuristic: simple query → simple tier
        assert ctx.routed_model == "gpt-4o-mini"
        assert ctx.savings.routing_mode == "llm_judge"

    async def test_llm_judge_fallback_on_timeout(self, make_ctx):
        ctx = make_ctx(
            [{"role": "user", "content": "What is 2+2?"}],
            model="gpt-4o",
        )
        ctx.config["groups"]["G6_routing"]["classifier"] = "llm_judge"
        ctx.config["groups"]["G6_routing"]["judge_model"] = "gpt-4o-mini"
        import asyncio
        from unittest.mock import patch
        def mock_wait_for(coro, timeout):
            if hasattr(coro, 'close'):
                coro.close()
            raise asyncio.TimeoutError
        with patch(
            "middleware.g06_routing.asyncio.wait_for",
            side_effect=mock_wait_for,
        ):
            from middleware.g06_routing import G06Routing
            ctx = await G06Routing().process_request(ctx)
        assert ctx.routed_model == "gpt-4o-mini"
        assert ctx.savings.routing_mode == "llm_judge"

    async def test_cascade_high_confidence_skips_judge(self, make_ctx):
        # Complex keyword gives confidence 0.90 ≥ 0.70 threshold → judge never called
        ctx = make_ctx(
            [{"role": "user", "content": "Analyse the system design."}],
            model="gpt-4o",
        )
        ctx.config["groups"]["G6_routing"]["classifier"] = "cascade"
        ctx.config["groups"]["G6_routing"]["judge_model"] = "gpt-4o-mini"
        ctx.config["groups"]["G6_routing"]["tiers"] = {
            "simple": ["gpt-4o-mini"],
            "medium": ["gpt-4o"],
            "complex": ["gpt-4-5"],
        }
        from middleware.g06_routing import G06Routing
        ctx = await G06Routing().process_request(ctx)
        assert ctx.routed_model == "gpt-4-5"
        assert ctx.savings.routing_mode == "cascade"

    async def test_cascade_low_confidence_calls_judge(self, make_ctx):
        # Ambiguous text (80–500 words, no keywords) → medium tier, confidence 0.50 < 0.70
        ambiguous = " ".join(["word"] * 100)
        ctx = make_ctx(
            [{"role": "user", "content": ambiguous}],
            model="gpt-4o",
        )
        ctx.config["groups"]["G6_routing"]["classifier"] = "cascade"
        ctx.config["groups"]["G6_routing"]["judge_model"] = "gpt-4o-mini"
        ctx.config["groups"]["G6_routing"]["tiers"] = {
            "simple": ["gpt-4o-mini"],
            "medium": ["gpt-4o"],
            "complex": ["gpt-4-5"],
        }
        async def mock_acompletion(*args, **kwargs):
            return {
                "choices": [
                    {"message": {"content": '{"tier":"simple","confidence":0.85}'}}
                ]
            }
        with patch("middleware.g06_routing.litellm.acompletion", mock_acompletion):
            from middleware.g06_routing import G06Routing
            ctx = await G06Routing().process_request(ctx)
        assert ctx.routed_model == "gpt-4o-mini"  # judge overrode medium → simple
        assert ctx.savings.routing_mode == "cascade"

    async def test_cascade_no_judge_model_skips_llm(self, make_ctx):
        ambiguous = " ".join(["word"] * 100)
        ctx = make_ctx(
            [{"role": "user", "content": ambiguous}],
            model="gpt-4o",
        )
        ctx.config["groups"]["G6_routing"]["classifier"] = "cascade"
        ctx.config["groups"]["G6_routing"]["judge_model"] = ""
        from middleware.g06_routing import G06Routing
        ctx = await G06Routing().process_request(ctx)
        # Falls back to heuristic medium tier
        assert ctx.routed_model == "gpt-4o"
        assert ctx.savings.routing_mode == "cascade"

    async def test_cascade_is_default_when_no_classifier_key(self, make_ctx):
        ambiguous = " ".join(["word"] * 100)
        ctx = make_ctx(
            [{"role": "user", "content": ambiguous}],
            model="gpt-4o",
        )
        # No classifier key in config → defaults to cascade
        ctx.config["groups"]["G6_routing"].pop("classifier", None)
        ctx.config["groups"]["G6_routing"]["judge_model"] = ""
        from middleware.g06_routing import G06Routing
        ctx = await G06Routing().process_request(ctx)
        # Default cascade with no judge_model → heuristic medium → gpt-4o
        assert ctx.routed_model == "gpt-4o"
        assert ctx.savings.routing_mode == "cascade"

    async def test_user_complexity_override_simple(self, make_ctx):
        # Even with classifier=routellm, user override bypasses everything
        ctx = make_ctx(
            [{"role": "user", "content": "Analyse the system design."}],
            model="gpt-4o",
        )
        ctx.config["groups"]["G6_routing"]["classifier"] = "routellm"
        ctx.config["groups"]["G6_routing"]["tiers"] = {
            "simple": ["gpt-4o-mini"],
            "medium": ["gpt-4o"],
            "complex": ["gpt-4-5"],
        }
        ctx.params["complexity"] = "simple"
        async def mock_routellm(*args, **kwargs):
            return "simple"
        with patch("middleware.g06_routing._classify_routellm", mock_routellm):
            from middleware.g06_routing import G06Routing
            ctx = await G06Routing().process_request(ctx)
        assert ctx.routed_model == "gpt-4o-mini"
        assert ctx.savings.routing_mode == "user_override"

    async def test_user_complexity_override_complex(self, make_ctx):
        ctx = make_ctx(
            [{"role": "user", "content": "What is 2+2?"}],
            model="gpt-4o",
        )
        ctx.config["groups"]["G6_routing"]["tiers"] = {
            "simple": ["gpt-4o-mini"],
            "medium": ["gpt-4o"],
            "complex": ["gpt-4-5"],
        }
        ctx.params["x_complexity"] = "complex"
        from middleware.g06_routing import G06Routing
        ctx = await G06Routing().process_request(ctx)
        assert ctx.routed_model == "gpt-4-5"
        assert ctx.savings.routing_mode == "user_override"

    async def test_user_complexity_invalid_falls_through(self, make_ctx):
        ctx = make_ctx(
            [{"role": "user", "content": "What is 2+2?"}],
            model="gpt-4o",
        )
        ctx.params["complexity"] = "banana"
        from middleware.g06_routing import G06Routing
        ctx = await G06Routing().process_request(ctx)
        # Falls through to default cascade → heuristic (no judge) → simple tier
        assert ctx.routed_model == "gpt-4o-mini"
        assert ctx.savings.routing_mode == "cascade"

    async def test_unknown_classifier_defaults_to_cascade(self, make_ctx):
        ctx = make_ctx(
            [{"role": "user", "content": "What is 2+2?"}],
            model="gpt-4o",
        )
        ctx.config["groups"]["G6_routing"]["classifier"] = "nonexistent"
        ctx.config["groups"]["G6_routing"]["judge_model"] = ""
        from middleware.g06_routing import G06Routing
        ctx = await G06Routing().process_request(ctx)
        # Unknown classifier → defaults to cascade → heuristic (no judge) → simple
        assert ctx.routed_model == "gpt-4o-mini"
        assert ctx.savings.routing_mode == "nonexistent"

    async def test_routellm_calls_sidecar(self, make_ctx):
        ctx = make_ctx(
            [{"role": "user", "content": "What is 2+2?"}],
            model="gpt-4o",
        )
        ctx.config["groups"]["G6_routing"]["classifier"] = "routellm"
        ctx.config["groups"]["G6_routing"]["routellm"] = {
            "url": "http://routellm-svc:8081",
            "router": "mf",
            "threshold": 0.11593,
            "weak_model": "gpt-4o-mini",
            "strong_model": "gpt-4-1106-preview",
            "model_map": {"weak": "simple", "strong": "complex"},
        }
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"routed_model": "gpt-4o-mini", "confidence": 0.85, "reason": "below_threshold"}
        mock_resp.raise_for_status = MagicMock()
        with patch("middleware.g06_routing.httpx.AsyncClient", return_value=FakeClient(mock_resp)):
            from middleware.g06_routing import G06Routing
            ctx = await G06Routing().process_request(ctx)
        assert ctx.routed_model == "gpt-4o-mini"
        assert ctx.savings.routing_mode == "routellm"

    async def test_routellm_fallback_on_error(self, make_ctx):
        ctx = make_ctx(
            [{"role": "user", "content": "What is 2+2?"}],
            model="gpt-4o",
        )
        ctx.config["groups"]["G6_routing"]["classifier"] = "routellm"
        ctx.config["groups"]["G6_routing"]["routellm"] = {
            "url": "http://routellm-svc:8081",
            "router": "mf",
            "threshold": 0.11593,
            "weak_model": "gpt-4o-mini",
            "strong_model": "gpt-4-1106-preview",
            "model_map": {"weak": "simple", "strong": "complex"},
        }
        with patch("middleware.g06_routing.httpx.AsyncClient", return_value=FakeClientError()):
            from middleware.g06_routing import G06Routing
            ctx = await G06Routing().process_request(ctx)
        # Falls back to heuristic → simple tier
        assert ctx.routed_model == "gpt-4o-mini"
        assert ctx.savings.routing_mode == "routellm"

    async def test_routellm_mapping_configurable(self, make_ctx):
        ctx = make_ctx(
            [{"role": "user", "content": "What is 2+2?"}],
            model="gpt-4o",
        )
        ctx.config["groups"]["G6_routing"]["classifier"] = "routellm"
        ctx.config["groups"]["G6_routing"]["routellm"] = {
            "url": "http://routellm-svc:8081",
            "router": "mf",
            "threshold": 0.11593,
            "weak_model": "gpt-4o-mini",
            "strong_model": "gpt-4-1106-preview",
            "model_map": {"weak": "simple", "strong": "medium"},  # strong → medium, not complex
        }
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"routed_model": "gpt-4-1106-preview", "confidence": 0.85, "reason": "above_threshold"}
        mock_resp.raise_for_status = MagicMock()
        with patch("middleware.g06_routing.httpx.AsyncClient", return_value=FakeClient(mock_resp)):
            from middleware.g06_routing import G06Routing
            ctx = await G06Routing().process_request(ctx)
        # strong_model mapped to medium tier
        assert ctx.routed_model == "gpt-4o"
        assert ctx.savings.routing_mode == "routellm"


@pytest.mark.asyncio
class TestG06CascadeExecution:
    """Tests for _execute_three_tier_cascade() — the true cascade execution path."""

    async def test_cascade_execution_tier1_high_confidence_returns_immediately(self, make_ctx):
        """Tier 1 succeeds with high confidence — return immediately without escalation."""
        ctx = make_ctx(
            [{"role": "user", "content": "What is 2+2?"}],
            model="gpt-4o",
        )
        ctx.config["groups"]["G6_routing"]["classifier"] = "cascade"
        ctx.config["groups"]["G6_routing"]["cascade_execution"] = True
        ctx.config["groups"]["G6_routing"]["cascade_confidence_threshold"] = 0.70
        ctx.config["groups"]["G6_routing"]["judge_model"] = ""
        tiers = {
            "simple": ["gpt-4o-mini"],
            "medium": ["gpt-4o"],
            "complex": ["gpt-4-5"],
        }

        async def mock_acompletion(*args, **kwargs):
            model = kwargs.get("model", "")
            if "mini" in model:
                # Tier 1 returns high confidence answer
                mock_resp = MagicMock()
                mock_resp.model_dump.return_value = {
                    "choices": [{"message": {"content": "4"}}],
                    "model": "gpt-4o-mini",
                }
                return mock_resp
            raise Exception("Should not call higher tiers")

        with patch("middleware.g06_routing.litellm.acompletion", mock_acompletion), \
             patch("middleware.g06_routing._resolve_provider_key", return_value="mock-key"):
            from middleware.g06_routing import G06Routing
            ctx = await G06Routing().process_request(ctx)

        assert ctx.routed_model == "gpt-4o-mini"
        assert ctx.savings.routing_mode == "cascade_execution"
        assert hasattr(ctx, "cascade_response")

    async def test_cascade_execution_escalates_to_tier2_when_tier1_low_confidence(self, make_ctx):
        """Tier 1 has low confidence — escalate to tier 2."""
        ctx = make_ctx(
            [{"role": "user", "content": "Explain the differences between Python 2 and 3."}],
            model="gpt-4o",
        )
        ctx.config["groups"]["G6_routing"]["classifier"] = "cascade"
        ctx.config["groups"]["G6_routing"]["cascade_execution"] = True
        ctx.config["groups"]["G6_routing"]["cascade_confidence_threshold"] = 0.90  # High threshold
        ctx.config["groups"]["G6_routing"]["judge_model"] = "gpt-4o-mini"
        tiers = {
            "simple": ["gpt-4o-mini"],
            "medium": ["gpt-4o"],
            "complex": ["gpt-4-5"],
        }

        call_count = 0
        async def mock_acompletion(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            model = kwargs.get("model", "")
            mock_resp = MagicMock()
            mock_resp.model_dump.return_value = {
                "choices": [{"message": {"content": f"Answer from {model}"}}],
                "model": model,
            }

            if "mini" in model and call_count == 1:
                # Tier 1 response
                return mock_resp
            elif "gpt-4o" in model and "mini" not in model and call_count == 2:
                # Tier 2 response (escalation)
                mock_resp.model_dump.return_value = {
                    "choices": [{"message": {"content": "Detailed comparison from gpt-4o"}}],
                    "model": "gpt-4o",
                }
                return mock_resp
            return mock_resp

        # Mock the judge to return low confidence for tier 1
        async def mock_judge(*args, **kwargs):
            return 0.50  # Below 0.90 threshold

        with patch("middleware.g06_routing.litellm.acompletion", mock_acompletion), \
             patch("middleware.g06_routing._evaluate_response_confidence", mock_judge), \
             patch("middleware.g06_routing._resolve_provider_key", return_value="mock-key"):
            from middleware.g06_routing import G06Routing
            ctx = await G06Routing().process_request(ctx)

        assert ctx.routed_model == "gpt-4o"  # Escalated to tier 2
        assert call_count >= 2  # Both tier 1 and tier 2 called

    async def test_cascade_execution_rolls_back_on_tier3_failure(self, make_ctx):
        """Tier 3 fails — roll back to best previous tier (tier 2)."""
        ctx = make_ctx(
            [{"role": "user", "content": "Complex architectural analysis required."}],
            model="gpt-4o",
        )
        ctx.config["groups"]["G6_routing"]["classifier"] = "cascade"
        ctx.config["groups"]["G6_routing"]["cascade_execution"] = True
        ctx.config["groups"]["G6_routing"]["cascade_confidence_threshold"] = 0.90
        ctx.config["groups"]["G6_routing"]["judge_model"] = "gpt-4o-mini"
        tiers = {
            "simple": ["gpt-4o-mini"],
            "medium": ["gpt-4o"],
            "complex": ["gpt-4-5"],
        }

        call_count = 0
        async def mock_acompletion(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            model = kwargs.get("model", "")

            if "mini" in model and call_count == 1:
                # Tier 1
                mock_resp = MagicMock()
                mock_resp.model_dump.return_value = {"choices": [{"message": {"content": "T1"}}], "model": model}
                return mock_resp
            elif "gpt-4o" in model and "mini" not in model and call_count == 2:
                # Tier 2 succeeds
                mock_resp = MagicMock()
                mock_resp.model_dump.return_value = {"choices": [{"message": {"content": "T2"}}], "model": model}
                return mock_resp
            elif "gpt-4-5" in model or "4-5" in model:
                # Tier 3 fails
                raise Exception("Tier 3 API error")
            return MagicMock()

        # Judge returns low confidence for tier 1, then high for tier 2
        judge_calls = 0
        async def mock_judge(*args, **kwargs):
            nonlocal judge_calls
            judge_calls += 1
            if judge_calls == 1:
                return 0.50  # Tier 1 low confidence
            return 0.95  # Tier 2 high confidence

        with patch("middleware.g06_routing.litellm.acompletion", mock_acompletion), \
             patch("middleware.g06_routing._evaluate_response_confidence", mock_judge), \
             patch("middleware.g06_routing._resolve_provider_key", return_value="mock-key"):
            from middleware.g06_routing import G06Routing
            ctx = await G06Routing().process_request(ctx)

        # Should roll back to tier 2 (the best response before tier 3 failure)
        assert ctx.routed_model == "gpt-4o"

    async def test_cascade_execution_respects_cost_threshold(self, make_ctx):
        """Escalation cost exceeds threshold — stay at tier 1."""
        ctx = make_ctx(
            [{"role": "user", "content": "What is Python?"}],
            model="gpt-4o",
        )
        ctx.config["groups"]["G6_routing"]["classifier"] = "cascade"
        ctx.config["groups"]["G6_routing"]["cascade_execution"] = True
        ctx.config["groups"]["G6_routing"]["cascade_confidence_threshold"] = 0.50  # Low threshold
        ctx.config["groups"]["G6_routing"]["max_escalation_cost_usd"] = 0.0001  # Very low — essentially disable escalation
        ctx.config["groups"]["G6_routing"]["judge_model"] = ""
        tiers = {
            "simple": ["gpt-4o-mini"],
            "medium": ["gpt-4o"],
            "complex": ["gpt-4-5"],
        }

        async def mock_acompletion(*args, **kwargs):
            model = kwargs.get("model", "")
            if "mini" in model:
                mock_resp = MagicMock()
                mock_resp.model_dump.return_value = {"choices": [{"message": {"content": "Simple answer"}}], "model": model}
                return mock_resp
            raise Exception("Should not escalate due to cost threshold")

        with patch("middleware.g06_routing.litellm.acompletion", mock_acompletion):
            from middleware.g06_routing import G06Routing
            ctx = await G06Routing().process_request(ctx)

        assert ctx.routed_model == "gpt-4o-mini"

    async def test_cascade_execution_no_simple_models_returns_error(self, make_ctx):
        """No simple models configured — cascade cannot start."""
        ctx = make_ctx(
            [{"role": "user", "content": "What is 2+2?"}],
            model="gpt-4o",
        )
        ctx.config["groups"]["G6_routing"]["classifier"] = "cascade"
        ctx.config["groups"]["G6_routing"]["cascade_execution"] = True
        ctx.config["groups"]["G6_routing"]["tiers"] = {
            "simple": [],  # Empty!
            "medium": ["gpt-4o"],
            "complex": ["gpt-4-5"],
        }

        from middleware.g06_routing import G06Routing
        ctx = await G06Routing().process_request(ctx)

        # Falls back to standard classification
        assert ctx.savings.routing_mode == "cascade_fallback"

    async def test_cascade_execution_tier1_failure_returns_none(self, make_ctx):
        """Tier 1 fails completely — cascade returns error, falls back."""
        ctx = make_ctx(
            [{"role": "user", "content": "What is 2+2?"}],
            model="gpt-4o",
        )
        ctx.config["groups"]["G6_routing"]["classifier"] = "cascade"
        ctx.config["groups"]["G6_routing"]["cascade_execution"] = True
        tiers = {
            "simple": ["gpt-4o-mini"],
            "medium": ["gpt-4o"],
            "complex": ["gpt-4-5"],
        }

        async def mock_acompletion(*args, **kwargs):
            raise Exception("Tier 1 API completely down")

        with patch("middleware.g06_routing.litellm.acompletion", mock_acompletion):
            from middleware.g06_routing import G06Routing
            ctx = await G06Routing().process_request(ctx)

        # Falls back to standard classification
        assert "fallback" in ctx.savings.routing_mode


class TestG06TierReachability:
    """P8 hardening: routing must not hand main.py a tier whose provider has no credential."""

    def test_tier_provider_startswith_not_substring(self):
        # openrouter/openai/... must resolve to openrouter, not openai (the substring trap).
        from middleware.g06_routing import _tier_provider
        provs = [
            {"name": "openai", "model_prefixes": ["gpt"]},
            {"name": "openrouter", "model_prefixes": ["openrouter/"]},
        ]
        with patch("middleware.g06_routing.get_providers", return_value=provs):
            assert _tier_provider("openrouter/openai/gpt-oss-120b:free") == "openrouter"
            assert _tier_provider("gpt-4o-mini") == "openai"

    def test_reachable_when_key_present(self):
        from middleware.g06_routing import _tier_reachable
        with patch("middleware.g06_routing.get_providers",
                   return_value=[{"name": "openai", "model_prefixes": ["gpt"]}]), \
             patch("auth.api_key_manager.get_llm_provider_key", return_value="sk"):
            assert _tier_reachable("gpt-4o-mini") is True

    def test_unreachable_when_no_key_and_key_required(self):
        from middleware.g06_routing import _tier_reachable
        adapter = Mock()
        adapter.requires_api_key.return_value = True
        with patch("middleware.g06_routing.get_providers",
                   return_value=[{"name": "openai", "model_prefixes": ["gpt"]}]), \
             patch("auth.api_key_manager.get_llm_provider_key", return_value=None), \
             patch("providers.get_adapter", return_value=adapter):
            assert _tier_reachable("gpt-4o-mini") is False

    def test_reachable_via_ambient_creds_when_key_not_required(self):
        # Bedrock/Vertex: no bearer key, but reachable via ambient creds (requires_api_key=False).
        from middleware.g06_routing import _tier_reachable
        adapter = Mock()
        adapter.requires_api_key.return_value = False
        with patch("middleware.g06_routing.get_providers",
                   return_value=[{"name": "bedrock", "model_prefixes": ["bedrock/"]}]), \
             patch("auth.api_key_manager.get_llm_provider_key", return_value=None), \
             patch("providers.get_adapter", return_value=adapter):
            assert _tier_reachable("bedrock/amazon.nova-micro-v1:0") is True


@pytest.mark.asyncio
class TestG06UnreachableTierGuard:
    """The per-request guard: unreachable routed tier → fallback to requested (default) or 503 (error)."""

    def _tiers_cfg(self, ctx, mode):
        g = ctx.config["groups"]["G6_routing"]
        g["enabled"] = True
        g["classifier"] = "cascade"
        g["cascade_execution"] = False
        g["on_unreachable_tier"] = mode
        g["tiers"] = {"simple": ["gpt-4o-mini"], "medium": ["gpt-4o"], "complex": ["gpt-4o"]}

    async def test_fallback_serves_requested_model(self, make_ctx):
        # Simple prompt routes to the gpt-4o-mini tier, but that tier is unreachable → serve
        # the caller's own model (claude-haiku-4-5, which is reachable).
        ctx = make_ctx([{"role": "user", "content": "What is Python?"}], model="claude-haiku-4-5")
        self._tiers_cfg(ctx, "fallback")
        from middleware.g06_routing import G06Routing
        with patch("middleware.g06_routing._tier_reachable",
                   side_effect=lambda m: m == "claude-haiku-4-5"):
            ctx = await G06Routing().process_request(ctx)
        assert ctx.routed_model == "claude-haiku-4-5"

    async def test_error_mode_keeps_unreachable_tier(self, make_ctx):
        # In error mode the unreachable tier is kept so main.py's provider-key guard 503s.
        ctx = make_ctx([{"role": "user", "content": "What is Python?"}], model="claude-haiku-4-5")
        self._tiers_cfg(ctx, "error")
        from middleware.g06_routing import G06Routing
        with patch("middleware.g06_routing._tier_reachable",
                   side_effect=lambda m: m == "claude-haiku-4-5"):
            ctx = await G06Routing().process_request(ctx)
        assert ctx.routed_model == "gpt-4o-mini"
