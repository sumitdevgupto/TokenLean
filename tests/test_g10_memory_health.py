"""
G10 Memory — Mem0/Zep Health-Check Warning Tests

Tests the one-time health-check warning emitted in process_request() when
mem0_enabled or zep_enabled is true but the corresponding URL env var is absent.

Key behaviours verified:
- mem0_enabled=true + no MEM0_API_URL → warning logged once
- zep_enabled=true + no ZEP_API_URL  → warning logged once
- Both enabled + no URLs             → both warnings logged
- Both enabled + URLs present        → no warning logged
- Warning only fires once per G10Memory instance (not every request)
- G10 disabled entirely              → no warning, no processing
"""
import logging
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from middleware import RequestContext
from middleware.g10_memory import G10Memory


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _ctx(mem0_enabled=False, zep_enabled=False, messages=None):
    ctx = MagicMock(spec=RequestContext)
    ctx.messages = messages or [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "Hello"},
    ]
    ctx.params = {}
    ctx.config = {
        "groups": {
            "G10_memory": {
                "enabled": True,
                "sliding_window_turns": 6,
                "summary_model": "gpt-4o-mini",
                "skills_enabled": False,
                "mem0_enabled": mem0_enabled,
                "zep_enabled": zep_enabled,
            }
        }
    }
    ctx.request_id = "test-g10"
    ctx.model = "gpt-4o-mini"
    ctx.savings = MagicMock()
    ctx.savings.add_step = MagicMock()
    ctx.redis_prefix = ""
    return ctx


def _patch_session(return_value=None):
    """Patch _apply_session_state so we don't need real Redis."""
    return patch(
        "middleware.g10_memory._apply_sliding_window",
        new_callable=AsyncMock,
        return_value=return_value,
    )


# ---------------------------------------------------------------------------
# Health-check warning tests
# ---------------------------------------------------------------------------

class TestG10MemoryHealthWarning:

    @pytest.mark.asyncio
    async def test_mem0_enabled_without_url_warns(self, caplog):
        """mem0_enabled=true + no MEM0_API_URL → warning logged."""
        g10 = G10Memory()
        ctx = _ctx(mem0_enabled=True, zep_enabled=False)

        with patch.dict("os.environ", {}, clear=False), \
             patch("middleware.g10_memory._MEM0_API_URL", ""), \
             patch("middleware.g10_memory._ZEP_API_URL", ""), \
             _patch_session():
            with caplog.at_level(logging.WARNING, logger="middleware.g10_memory"):
                await g10.process_request(ctx)

        assert any("MEM0_API_URL" in r.message for r in caplog.records)
        assert not any("ZEP_API_URL" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_zep_enabled_without_url_warns(self, caplog):
        """zep_enabled=true + no ZEP_API_URL → warning logged."""
        g10 = G10Memory()
        ctx = _ctx(mem0_enabled=False, zep_enabled=True)

        with patch("middleware.g10_memory._MEM0_API_URL", ""), \
             patch("middleware.g10_memory._ZEP_API_URL", ""), \
             _patch_session():
            with caplog.at_level(logging.WARNING, logger="middleware.g10_memory"):
                await g10.process_request(ctx)

        assert any("ZEP_API_URL" in r.message for r in caplog.records)
        assert not any("MEM0_API_URL" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_both_enabled_without_urls_warns_for_both(self, caplog):
        """Both enabled + no URLs → both warnings emitted."""
        g10 = G10Memory()
        ctx = _ctx(mem0_enabled=True, zep_enabled=True)

        with patch("middleware.g10_memory._MEM0_API_URL", ""), \
             patch("middleware.g10_memory._ZEP_API_URL", ""), \
             _patch_session():
            with caplog.at_level(logging.WARNING, logger="middleware.g10_memory"):
                await g10.process_request(ctx)

        assert any("MEM0_API_URL" in r.message for r in caplog.records)
        assert any("ZEP_API_URL" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_urls_present_suppresses_warnings(self, caplog):
        """Both enabled with URLs configured → no health warning logged."""
        g10 = G10Memory()
        ctx = _ctx(mem0_enabled=True, zep_enabled=True)

        with patch("middleware.g10_memory._MEM0_API_URL", "http://mem0.internal"), \
             patch("middleware.g10_memory._ZEP_API_URL", "http://zep.internal"), \
             _patch_session():
            with caplog.at_level(logging.WARNING, logger="middleware.g10_memory"):
                await g10.process_request(ctx)

        health_warnings = [
            r for r in caplog.records
            if "MEM0_API_URL" in r.message or "ZEP_API_URL" in r.message
        ]
        assert health_warnings == []

    @pytest.mark.asyncio
    async def test_warning_fires_only_once_per_instance(self, caplog):
        """Health warning is emitted on the first request only, not subsequent ones."""
        g10 = G10Memory()
        ctx = _ctx(mem0_enabled=True, zep_enabled=False)

        with patch("middleware.g10_memory._MEM0_API_URL", ""), \
             patch("middleware.g10_memory._ZEP_API_URL", ""), \
             _patch_session():
            with caplog.at_level(logging.WARNING, logger="middleware.g10_memory"):
                await g10.process_request(ctx)
                await g10.process_request(ctx)
                await g10.process_request(ctx)

        mem0_warnings = [r for r in caplog.records if "MEM0_API_URL" in r.message]
        assert len(mem0_warnings) == 1

    @pytest.mark.asyncio
    async def test_fresh_instance_warns_again(self, caplog):
        """A new G10Memory instance starts with _health_warned=False, so warns again."""
        ctx = _ctx(mem0_enabled=True, zep_enabled=False)

        with patch("middleware.g10_memory._MEM0_API_URL", ""), \
             patch("middleware.g10_memory._ZEP_API_URL", ""), \
             _patch_session():
            with caplog.at_level(logging.WARNING, logger="middleware.g10_memory"):
                await G10Memory().process_request(ctx)
                await G10Memory().process_request(ctx)

        mem0_warnings = [r for r in caplog.records if "MEM0_API_URL" in r.message]
        assert len(mem0_warnings) == 2  # one per instance

    @pytest.mark.asyncio
    async def test_neither_enabled_no_warning(self, caplog):
        """Both disabled (defaults) → no health warning, no spurious noise."""
        g10 = G10Memory()
        ctx = _ctx(mem0_enabled=False, zep_enabled=False)

        with patch("middleware.g10_memory._MEM0_API_URL", ""), \
             patch("middleware.g10_memory._ZEP_API_URL", ""), \
             _patch_session():
            with caplog.at_level(logging.WARNING, logger="middleware.g10_memory"):
                await g10.process_request(ctx)

        health_warnings = [
            r for r in caplog.records
            if "MEM0_API_URL" in r.message or "ZEP_API_URL" in r.message
        ]
        assert health_warnings == []

    @pytest.mark.asyncio
    async def test_g10_disabled_skips_health_check(self, caplog):
        """Group disabled → process_request returns immediately, health check never runs."""
        g10 = G10Memory()
        ctx = _ctx(mem0_enabled=True, zep_enabled=True)
        ctx.config["groups"]["G10_memory"]["enabled"] = False

        with patch("middleware.g10_memory._MEM0_API_URL", ""), \
             patch("middleware.g10_memory._ZEP_API_URL", ""):
            with caplog.at_level(logging.WARNING, logger="middleware.g10_memory"):
                result = await g10.process_request(ctx)

        assert result is ctx
        health_warnings = [
            r for r in caplog.records
            if "MEM0_API_URL" in r.message or "ZEP_API_URL" in r.message
        ]
        assert health_warnings == []
        assert g10._health_warned is False  # flag not set when group is disabled

    @pytest.mark.asyncio
    async def test_health_warned_flag_set_after_first_request(self):
        """_health_warned flag is True after the first enabled request."""
        g10 = G10Memory()
        assert g10._health_warned is False

        ctx = _ctx(mem0_enabled=False, zep_enabled=False)
        with patch("middleware.g10_memory._MEM0_API_URL", ""), \
             patch("middleware.g10_memory._ZEP_API_URL", ""), \
             _patch_session():
            await g10.process_request(ctx)

        assert g10._health_warned is True
