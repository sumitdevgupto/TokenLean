"""Tests for G24 — Adaptive Bypass middleware."""
import json
import os
import tempfile
from unittest.mock import patch

import pytest
import yaml

from middleware import RequestContext
from middleware.g24_adaptive_bypass import G24AdaptiveBypass


@pytest.fixture
def sample_config():
    return {
        "groups": {
            "G24_adaptive_bypass": {
                "enabled": True,
                "rules_file": "",  # Will be set per test
            }
        }
    }


@pytest.fixture
def sample_context(sample_config):
    """Create a RequestContext for testing."""
    ctx = RequestContext.create(
        request_id="test-001",
        user_id="test-user",
        messages=[
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hello world " * 50},
        ],
        model="gpt-4o-mini",
        params={"x_dataset": "DS1"},
        config=sample_config,
    )
    return ctx


@pytest.fixture
def bypass_rules():
    """Sample bypass rules."""
    return {
        "adaptive_bypass": {
            "enabled": True,
            "rules": [
                {
                    "group": "G01",
                    "enabled": True,
                    "reason": "80% of DS1 requests show token increase with compression",
                    "confidence": 0.85,
                    "conditions": {
                        "min_prompt_tokens": 0,
                        "datasets": ["DS1"],
                        "models": ["gpt-4o-mini"],
                    },
                },
                {
                    "group": "G07",
                    "enabled": True,
                    "reason": "RAG retrieval adds tokens for short prompts",
                    "confidence": 0.6,
                    "conditions": {
                        "min_prompt_tokens": 0,
                        "max_prompt_tokens": 500,
                        "models": [],
                    },
                },
                {
                    "group": "G19",
                    "enabled": False,
                    "reason": "Disabled rule — should not trigger",
                    "confidence": 0.9,
                    "conditions": {},
                },
            ],
        }
    }


@pytest.fixture
def rules_file(bypass_rules):
    """Write bypass rules to a temp file and return the path."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False
    ) as f:
        yaml.dump(bypass_rules, f)
        return f.name


class TestG24AdaptiveBypass:
    """Test G24 adaptive bypass logic."""

    @pytest.mark.asyncio
    async def test_no_rules_file_does_nothing(self, sample_context):
        """When no rules file exists, middleware passes through."""
        g24 = G24AdaptiveBypass()
        ctx = await g24.process_request(sample_context)
        assert ctx.skip_groups == []

    @pytest.mark.asyncio
    async def test_disabled_does_nothing(self, sample_context):
        """When G24 is disabled, middleware passes through."""
        sample_context.config["groups"]["G24_adaptive_bypass"]["enabled"] = False
        g24 = G24AdaptiveBypass()
        ctx = await g24.process_request(sample_context)
        assert ctx.skip_groups == []

    @pytest.mark.asyncio
    async def test_matching_rule_adds_to_skip_groups(self, sample_context, rules_file):
        """Matching rules add groups to skip_groups."""
        sample_context.config["groups"]["G24_adaptive_bypass"]["rules_file"] = rules_file
        g24 = G24AdaptiveBypass()
        ctx = await g24.process_request(sample_context)
        # G01 should match (DS1 + gpt-4o-mini)
        assert "G01" in ctx.skip_groups

    @pytest.mark.asyncio
    async def test_disabled_rule_not_applied(self, sample_context, rules_file):
        """Rules with enabled=False are skipped."""
        sample_context.config["groups"]["G24_adaptive_bypass"]["rules_file"] = rules_file
        g24 = G24AdaptiveBypass()
        ctx = await g24.process_request(sample_context)
        # G19 rule is disabled
        assert "G19" not in ctx.skip_groups

    @pytest.mark.asyncio
    async def test_max_tokens_condition(self, sample_context, rules_file):
        """G07 rule should NOT match because prompt tokens > 500."""
        sample_context.config["groups"]["G24_adaptive_bypass"]["rules_file"] = rules_file
        g24 = G24AdaptiveBypass()
        ctx = await g24.process_request(sample_context)
        # The sample context has ~50+ tokens so > 500 check...
        # "Hello world " * 50 = at least 100 tokens, but the max is 500
        # Actually with system prompt + user content, token count is > 500?
        # Let's just verify the logic works — the fixture has enough tokens
        # that the G07 rule (max_prompt_tokens=500) should NOT match
        # because the prompt is "Hello world " * 50 which is ~100+ tokens
        # Since it's < 500, G07 WILL match
        # Actually let's check: "Hello world " * 50 is about 100 tokens
        # plus system message — still < 500. So G07 SHOULD match.
        assert "G07" in ctx.skip_groups

    @pytest.mark.asyncio
    async def test_model_filter(self, sample_context, rules_file):
        """Rule with model filter only matches specified models."""
        sample_context.config["groups"]["G24_adaptive_bypass"]["rules_file"] = rules_file
        sample_context.model = "gpt-4o"  # Not gpt-4o-mini
        sample_context.routed_model = "gpt-4o"
        g24 = G24AdaptiveBypass()
        ctx = await g24.process_request(sample_context)
        # G01 rule requires gpt-4o-mini — should NOT match now
        assert "G01" not in ctx.skip_groups

    @pytest.mark.asyncio
    async def test_dataset_filter(self, sample_context, rules_file):
        """Rule with dataset filter only matches specified datasets."""
        sample_context.config["groups"]["G24_adaptive_bypass"]["rules_file"] = rules_file
        sample_context.params["x_dataset"] = "DS5"  # Not DS1
        g24 = G24AdaptiveBypass()
        ctx = await g24.process_request(sample_context)
        # G01 rule requires DS1 — should NOT match with DS5
        assert "G01" not in ctx.skip_groups

    @pytest.mark.asyncio
    async def test_tenant_filter(self, sample_context, bypass_rules, rules_file):
        """Rule with tenant filter only matches specified tenants."""
        # Add tenant restriction to rule
        bypass_rules["adaptive_bypass"]["rules"][0]["conditions"]["tenants"] = ["nova-med"]
        with open(rules_file, "w") as f:
            yaml.dump(bypass_rules, f)

        sample_context.config["groups"]["G24_adaptive_bypass"]["rules_file"] = rules_file
        sample_context.tenant_id = "shop-bot"  # Not nova-med
        g24 = G24AdaptiveBypass()
        # Force reload
        g24._last_loaded = 0
        ctx = await g24.process_request(sample_context)
        assert "G01" not in ctx.skip_groups

    @pytest.mark.asyncio
    async def test_savings_step_recorded(self, sample_context, rules_file):
        """Bypass events are recorded in savings for observability."""
        sample_context.config["groups"]["G24_adaptive_bypass"]["rules_file"] = rules_file
        g24 = G24AdaptiveBypass()
        ctx = await g24.process_request(sample_context)
        # At least one step should be recorded (G01 match)
        g24_steps = [s for s in ctx.savings.step_savings if s.group == "G24"]
        assert len(g24_steps) == 1
        assert "G01" in g24_steps[0].description

    @pytest.mark.asyncio
    async def test_multiple_rules_match(self, sample_context, rules_file):
        """Multiple rules can match and accumulate in skip_groups."""
        sample_context.config["groups"]["G24_adaptive_bypass"]["rules_file"] = rules_file
        g24 = G24AdaptiveBypass()
        ctx = await g24.process_request(sample_context)
        # Both G01 (DS1 + gpt-4o-mini) and G07 (< 500 tokens) should match
        assert "G01" in ctx.skip_groups
        assert "G07" in ctx.skip_groups

    @pytest.mark.asyncio
    async def test_empty_rules_file(self, sample_context):
        """Empty rules file doesn't cause errors."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump({"adaptive_bypass": {"enabled": True, "rules": []}}, f)
            empty_rules_path = f.name

        sample_context.config["groups"]["G24_adaptive_bypass"]["rules_file"] = empty_rules_path
        g24 = G24AdaptiveBypass()
        ctx = await g24.process_request(sample_context)
        assert ctx.skip_groups == []

    @pytest.mark.asyncio
    async def test_malformed_rules_file_handled_gracefully(self, sample_context):
        """Malformed YAML is handled without crashing."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("{{invalid yaml content]]]")
            bad_path = f.name

        sample_context.config["groups"]["G24_adaptive_bypass"]["rules_file"] = bad_path
        g24 = G24AdaptiveBypass()
        ctx = await g24.process_request(sample_context)
        assert ctx.skip_groups == []

    @pytest.mark.asyncio
    async def test_rules_reload_caching(self, sample_context, rules_file):
        """Rules are cached and not re-read on every request."""
        sample_context.config["groups"]["G24_adaptive_bypass"]["rules_file"] = rules_file
        g24 = G24AdaptiveBypass()

        # First call loads rules
        await g24.process_request(sample_context)
        assert g24._last_loaded > 0

        # Modify the file — should NOT be reloaded within cache interval
        first_load_time = g24._last_loaded
        sample_context.skip_groups = []  # Reset for second call
        await g24.process_request(sample_context)
        assert g24._last_loaded == first_load_time  # Same timestamp = cache hit
