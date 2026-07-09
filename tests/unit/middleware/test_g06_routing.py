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


def _faithful_cost(input_tokens, output_tokens, model):
    """Per-model cost estimate ($/1M in, $/1M out) mirroring the real pricing spread —
    for tests that exercise the cost/requested-model escalation guards, since the unit
    pricing table maps every model to one default rate."""
    m = model or ""
    if "mini" in m:
        cin, cout = 0.15, 0.60
    elif "4-5" in m:
        cin, cout = 75.0, 150.0
    else:  # gpt-4o and friends
        cin, cout = 5.0, 15.0
    return input_tokens * cin / 1e6 + output_tokens * cout / 1e6


def _mk_resp(model, content="An adequate answer.", finish_reason="stop"):
    """Build a MagicMock litellm response whose model_dump() carries the fields the
    cascade's response-confidence heuristic reads (choices[0].finish_reason / content)."""
    resp = MagicMock()
    resp.model_dump.return_value = {
        "choices": [{"finish_reason": finish_reason, "message": {"content": content}}],
        "model": model,
    }
    return resp


# A ~100-word prompt with no complex/simple keywords → _classify_heuristic == "medium".
# This is the DS9 shape: a substantive but non-complex query the cheap tier handles.
_MEDIUM_PROMPT = (
    "Our nightly export job moved about forty thousand customer records into the "
    "reporting warehouse and then paused for roughly nine minutes before finishing "
    "the remaining batches without any operator action taken at all. The dashboard "
    "showed the queue depth climbing steadily and then draining back down to zero on "
    "its own while the on call engineer was still reading the very first alert page. "
    "Walk me through what the most likely sequence of events was here and whether the "
    "pause should be treated as expected backpressure behaviour or a genuine incident "
    "worth escalating to the wider platform team for a deeper follow up review tomorrow."
)


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

    # Distinct pricing so the cost-floor guard can tell gpt-4o (pricey) from gpt-4o-mini
    # (cheap) — mirrors the live config's pricing table (unit tests have none by default).
    _PRICING = {
        "gpt-4o-mini": {"input": 0.00015, "output": 0.0006},
        "gpt-4o": {"input": 0.005, "output": 0.015},
        "gpt-4-5": {"input": 0.075, "output": 0.15},
        "default": {"input": 0.005, "output": 0.015},
    }

    async def test_cost_floor_reverts_upward_route_to_requested(self, make_ctx):
        # B2: a cheap-requested query mis-classified as "medium" must NOT route up to a
        # pricier tier (denial-of-wallet by over-escalation). The medium prompt classifies
        # medium → gpt-4o, but the caller asked for gpt-4o-mini, so the cost-floor guard
        # reverts to gpt-4o-mini.
        ctx = make_ctx([{"role": "user", "content": _MEDIUM_PROMPT}], model="gpt-4o-mini")
        ctx.config["groups"]["G6_routing"]["classifier"] = "heuristic"
        ctx.config["groups"]["G6_routing"]["tiers"] = {
            "simple": ["gpt-4o-mini"],
            "medium": ["gpt-4o"],
            "complex": ["gpt-4-5"],
        }
        from middleware.g06_routing import G06Routing
        with patch("config_loader.get_pricing_table", return_value=self._PRICING):
            ctx = await G06Routing().process_request(ctx)
        assert ctx.routed_model == "gpt-4o-mini"
        assert "cost_floor" in (ctx.savings.routing_mode or "")

    async def test_allow_escalation_above_requested_opt_in(self, make_ctx):
        # With the opt-in flag set, upward routing is permitted again (quality-upgrade mode).
        ctx = make_ctx([{"role": "user", "content": _MEDIUM_PROMPT}], model="gpt-4o-mini")
        ctx.config["groups"]["G6_routing"]["classifier"] = "heuristic"
        ctx.config["groups"]["G6_routing"]["allow_escalation_above_requested"] = True
        ctx.config["groups"]["G6_routing"]["tiers"] = {
            "simple": ["gpt-4o-mini"],
            "medium": ["gpt-4o"],
            "complex": ["gpt-4-5"],
        }
        from middleware.g06_routing import G06Routing
        with patch("config_loader.get_pricing_table", return_value=self._PRICING):
            ctx = await G06Routing().process_request(ctx)
        assert ctx.routed_model == "gpt-4o"

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
            # Must classify complex (hits "analyze") so the classified-tier cap permits
            # escalation past tier1; a short 4-word prompt would classify simple and be capped.
            [{"role": "user", "content": "Analyze and architect a strategy for this complex system."}],
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


@pytest.mark.asyncio
class TestG06CascadeOverEscalationGuards:
    """Guards against the cascade over-escalating cheap-answerable queries to expensive
    tiers (the DS9 regression). Covers: response-derived confidence in the no-judge path,
    the classified-tier cap, and the cost/requested-model escalation guards."""

    @staticmethod
    def _cfg(ctx, **overrides):
        g = ctx.config["groups"]["G6_routing"]
        g["classifier"] = "cascade"
        g["cascade_execution"] = True
        g["judge_model"] = ""
        g["cascade_confidence_threshold"] = 0.70
        g["tiers"] = {"simple": ["gpt-4o-mini"], "medium": ["gpt-4o"], "complex": ["gpt-4-5"]}
        g.update(overrides)
        return g

    async def _run(self, ctx, mock_acompletion, judge=None, cost_fn=None):
        patches = [
            patch("middleware.g06_routing.litellm.acompletion", mock_acompletion),
            patch("middleware.g06_routing._resolve_provider_key", return_value="mock-key"),
        ]
        if judge is not None:
            patches.append(patch("middleware.g06_routing._evaluate_response_confidence", judge))
        if cost_fn is not None:
            # The unit-test pricing table maps every model to the same default rate, so the
            # cost guards can't differentiate tiers. Inject faithful per-model pricing.
            patches.append(patch("middleware.g06_routing.estimate_cost", cost_fn))
        from contextlib import ExitStack
        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            from middleware.g06_routing import G06Routing
            return await G06Routing().process_request(ctx)

    # ── No-judge response-derived confidence ──────────────────────────────────

    async def test_cascade_no_judge_medium_query_stays_tier1_when_response_adequate(self, make_ctx):
        """DS9 regression: a medium query whose cheap-tier answer is adequate must NOT escalate."""
        ctx = make_ctx([{"role": "user", "content": _MEDIUM_PROMPT}], model="gpt-4o-mini")
        self._cfg(ctx)
        calls = 0
        async def mock(*a, **k):
            nonlocal calls; calls += 1
            return _mk_resp(k.get("model", ""), "A clear, complete answer.", "stop")
        ctx = await self._run(ctx, mock)
        assert ctx.routed_model == "gpt-4o-mini"
        assert calls == 1  # served by the cheap tier, no escalation

    async def test_cascade_no_judge_escalates_on_truncated_response(self, make_ctx):
        """Tier1 truncated (finish_reason=length) → escalate to tier2."""
        ctx = make_ctx([{"role": "user", "content": _MEDIUM_PROMPT}], model="gpt-4o")
        self._cfg(ctx)
        async def mock(*a, **k):
            m = k.get("model", "")
            if "mini" in m:
                return _mk_resp(m, "partial...", "length")
            if "4-5" in m:
                raise AssertionError("tier3 must not be reached for a medium query")
            return _mk_resp(m, "Full tier2 answer.", "stop")
        ctx = await self._run(ctx, mock)
        assert ctx.routed_model == "gpt-4o"

    async def test_cascade_no_judge_escalates_on_refusal_response(self, make_ctx):
        """Tier1 refusal opening → low confidence → escalate to tier2."""
        ctx = make_ctx([{"role": "user", "content": _MEDIUM_PROMPT}], model="gpt-4o")
        self._cfg(ctx)
        async def mock(*a, **k):
            m = k.get("model", "")
            if "mini" in m:
                return _mk_resp(m, "I'm sorry, I can't help with that.", "stop")
            return _mk_resp(m, "Full tier2 answer.", "stop")
        ctx = await self._run(ctx, mock)
        assert ctx.routed_model == "gpt-4o"

    async def test_cascade_no_judge_empty_response_escalates(self, make_ctx):
        """Tier1 empty content → confidence 0.0 → escalate to tier2."""
        ctx = make_ctx([{"role": "user", "content": _MEDIUM_PROMPT}], model="gpt-4o")
        self._cfg(ctx)
        async def mock(*a, **k):
            m = k.get("model", "")
            if "mini" in m:
                return _mk_resp(m, "", "stop")
            return _mk_resp(m, "Full tier2 answer.", "stop")
        ctx = await self._run(ctx, mock)
        assert ctx.routed_model == "gpt-4o"

    async def test_cascade_tier1_forwards_tools_and_keeps_tool_call(self, make_ctx):
        """Regression (DS3/DS7/DS13): a tool-requiring request that stays at tier1 must still
        receive its tools, and its tool-call response must short-circuit — not escalate, not
        get dropped. Before the fix, tier1 omitted `tools` and returned a tool-less answer."""
        tools = [{"type": "function", "function": {"name": "get_logs", "parameters": {}}}]
        ctx = make_ctx([{"role": "user", "content": _MEDIUM_PROMPT}], model="gpt-4o-mini",
                       params={"tools": tools})
        self._cfg(ctx)
        seen_tools = {}
        calls = 0
        async def mock(*a, **k):
            nonlocal calls; calls += 1
            m = k.get("model", "")
            seen_tools[m] = k.get("tools")
            resp = MagicMock()
            resp.model_dump.return_value = {
                "choices": [{"finish_reason": "tool_calls", "message": {
                    "content": None,
                    "tool_calls": [{"id": "c1", "function": {"name": "get_logs"}}]}}],
                "model": m,
            }
            return resp
        ctx = await self._run(ctx, mock)
        assert ctx.routed_model == "gpt-4o-mini"          # stayed at tier1
        assert calls == 1                                  # no escalation
        assert seen_tools.get("gpt-4o-mini") == tools      # tier1 actually received the tools

    # ── Classified-tier cap ───────────────────────────────────────────────────

    async def test_cascade_cap_blocks_complex_tier_for_medium_query(self, make_ctx):
        """Medium query, tier1+tier2 both weak → capped at tier2, never reaches complex tier."""
        ctx = make_ctx([{"role": "user", "content": _MEDIUM_PROMPT}], model="gpt-4o")
        self._cfg(ctx)
        async def mock(*a, **k):
            m = k.get("model", "")
            if "4-5" in m:
                raise AssertionError("complex tier must be capped out for a medium query")
            return _mk_resp(m, "weak...", "length")  # both tier1 and tier2 truncated
        ctx = await self._run(ctx, mock)
        assert ctx.routed_model == "gpt-4o"  # best available under the cap

    async def test_cascade_cap_blocks_all_escalation_for_simple_query(self, make_ctx):
        """Simple query, even a weak tier1 answer → stays at tier1 (cap at simple)."""
        ctx = make_ctx([{"role": "user", "content": "What is Python?"}], model="gpt-4o")
        self._cfg(ctx)
        calls = 0
        async def mock(*a, **k):
            nonlocal calls; calls += 1
            m = k.get("model", "")
            if "mini" not in m:
                raise AssertionError("no escalation allowed for a simple query")
            return _mk_resp(m, "weak...", "length")
        ctx = await self._run(ctx, mock)
        assert ctx.routed_model == "gpt-4o-mini"
        assert calls == 1

    async def test_cascade_cap_disabled_restores_full_escalation(self, make_ctx):
        """cap off + allow-above + generous budget → medium query can reach the complex tier."""
        ctx = make_ctx([{"role": "user", "content": _MEDIUM_PROMPT}], model="gpt-4o")
        self._cfg(
            ctx,
            cascade_cap_to_classified_tier=False,
            allow_escalation_above_requested=True,
            max_escalation_cost_usd=100.0,
        )
        async def mock(*a, **k):
            m = k.get("model", "")
            if "4-5" in m:
                return _mk_resp(m, "Deep complex answer.", "stop")
            return _mk_resp(m, "weak...", "length")  # tier1 + tier2 truncated
        ctx = await self._run(ctx, mock)
        assert ctx.routed_model == "gpt-4-5"

    async def test_cascade_cap_applies_to_judge_path(self, make_ctx):
        """A flaky judge that always scores low still can't push a medium query past its tier."""
        ctx = make_ctx([{"role": "user", "content": _MEDIUM_PROMPT}], model="gpt-4o")
        self._cfg(ctx, judge_model="gpt-4o-mini", cascade_confidence_threshold=0.90)
        async def mock(*a, **k):
            m = k.get("model", "")
            if "4-5" in m:
                raise AssertionError("complex tier must be capped out even on the judge path")
            return _mk_resp(m, "answer", "stop")
        async def judge(*a, **k):
            return 0.40  # always below threshold
        ctx = await self._run(ctx, mock, judge=judge)
        assert ctx.routed_model == "gpt-4o"

    # ── x_complexity override ─────────────────────────────────────────────────

    async def test_x_complexity_override_bypasses_cascade(self, make_ctx):
        """An explicit x_complexity override routes directly, without running the cascade."""
        ctx = make_ctx([{"role": "user", "content": _MEDIUM_PROMPT}], model="gpt-4o")
        self._cfg(ctx)
        ctx.params["x_complexity"] = "complex"
        calls = 0
        async def mock(*a, **k):
            nonlocal calls; calls += 1
            return _mk_resp(k.get("model", ""))
        ctx = await self._run(ctx, mock)
        assert ctx.routed_model == "gpt-4-5"
        assert ctx.savings.routing_mode == "user_override"
        assert calls == 0  # cascade never ran

    # ── Cost / requested-model guards ─────────────────────────────────────────

    async def test_cost_guard_includes_expected_output_cost_and_returns_best(self, make_ctx):
        """Complex query, large max_tokens → complex tier blocked by output-inclusive cost,
        returns the best tier reached (tier2), not tier1."""
        ctx = make_ctx(
            [{"role": "user", "content": "Analyze and architect a strategy for this complex system."}],
            model="gpt-4o",
        )
        self._cfg(ctx, max_escalation_cost_usd=1.0)  # per-step delta not the limiter here
        ctx.params["max_tokens"] = 4096  # amplifies the complex tier's output cost
        async def mock(*a, **k):
            m = k.get("model", "")
            if "4-5" in m:
                raise AssertionError("complex tier blocked by cost guard, must not be called")
            return _mk_resp(m, "weak...", "length")  # tier1 + tier2 both weak
        ctx = await self._run(ctx, mock, cost_fn=_faithful_cost)
        assert ctx.routed_model == "gpt-4o"  # best reached, not rolled back to tier1

    async def test_cost_guard_blocks_escalation_above_requested_model(self, make_ctx):
        """Caller requested the cheap model → cascade won't escalate above it by default."""
        ctx = make_ctx(
            [{"role": "user", "content": "Analyze and architect a strategy for this complex system."}],
            model="gpt-4o-mini",
        )
        self._cfg(ctx)
        calls = 0
        async def mock(*a, **k):
            nonlocal calls; calls += 1
            m = k.get("model", "")
            if "mini" not in m:
                raise AssertionError("must not escalate above the requested gpt-4o-mini")
            return _mk_resp(m, "weak...", "length")
        ctx = await self._run(ctx, mock, cost_fn=_faithful_cost)
        assert ctx.routed_model == "gpt-4o-mini"
        assert calls == 1

    async def test_allow_escalation_above_requested_true_permits_escalation(self, make_ctx):
        """With the knob on, escalation above the requested model is allowed."""
        ctx = make_ctx(
            [{"role": "user", "content": "Analyze and architect a strategy for this complex system."}],
            model="gpt-4o-mini",
        )
        self._cfg(ctx, allow_escalation_above_requested=True)
        async def mock(*a, **k):
            m = k.get("model", "")
            if "mini" in m:
                return _mk_resp(m, "weak...", "length")
            return _mk_resp(m, "Full tier2 answer.", "stop")
        ctx = await self._run(ctx, mock, cost_fn=_faithful_cost)
        assert ctx.routed_model == "gpt-4o"


class TestG06ResponseConfidenceHeuristic:
    """Direct unit tests for _heuristic_response_confidence (no LLM call)."""

    def test_scores_by_finish_reason_and_content(self):
        from middleware.g06_routing import _heuristic_response_confidence as h
        def resp(content, finish_reason="stop"):
            return {"choices": [{"finish_reason": finish_reason, "message": {"content": content}}]}
        assert h(resp("A real answer."), {}) == 0.85           # clean stop
        assert h(resp("partial", "length"), {}) == 0.30         # truncated
        assert h(resp("I'm sorry, I can't help."), {}) == 0.40  # refusal opening
        assert h(resp("blocked", "content_filter"), {}) == 0.40 # content filter
        assert h(resp(""), {}) == 0.0                           # empty content
        assert h(resp("   "), {}) == 0.0                        # whitespace only
        assert h({"choices": []}, {}) == 0.0                    # no choices
        assert h(object(), {}) == 0.5                           # unparseable → neutral

    def test_tool_call_response_is_adequate_not_empty(self):
        """A tool-call response (empty content + tool_calls / finish_reason=tool_calls) is a
        valid, complete response — must score ok, never 'empty', so it isn't escalated."""
        from middleware.g06_routing import _heuristic_response_confidence as h
        tc = {"choices": [{"finish_reason": "tool_calls", "message": {
            "content": None, "tool_calls": [{"id": "c1", "function": {"name": "get_logs"}}]}}]}
        assert h(tc, {}) == 0.85
        # tool_calls present even if finish_reason is absent/other
        tc2 = {"choices": [{"message": {"content": "", "tool_calls": [{"id": "c2"}]}}]}
        assert h(tc2, {}) == 0.85

    def test_short_but_adequate_answer_is_not_penalised(self):
        from middleware.g06_routing import _heuristic_response_confidence as h
        resp = {"choices": [{"finish_reason": "stop", "message": {"content": "4"}}]}
        assert h(resp, {}) == 0.85

    def test_custom_scores_from_config_are_honoured(self):
        from middleware.g06_routing import _heuristic_response_confidence as h
        cfg = {"response_confidence": {"ok": 0.99, "truncated": 0.11, "refusal": 0.22, "empty": 0.01}}
        def resp(content, finish_reason="stop"):
            return {"choices": [{"finish_reason": finish_reason, "message": {"content": content}}]}
        assert h(resp("ok"), cfg) == 0.99
        assert h(resp("x", "length"), cfg) == 0.11
        assert h(resp("I cannot do that."), cfg) == 0.22
        assert h(resp(""), cfg) == 0.01


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
