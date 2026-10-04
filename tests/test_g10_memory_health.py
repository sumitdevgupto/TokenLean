"""
G10 Memory — Mem0 Health-Check Warning Tests

Tests the one-time health-check warning emitted in process_request() when
mem0_enabled is true but Mem0 cannot run, and when a config still sets
zep_enabled (Zep support was removed: its client was never checked against the
library it imported, and nothing could test it).

Key behaviours verified:
- mem0_enabled=true + no MEM0_API_URL → warning logged once
- mem0_enabled=true + URL, no MEM0_API_KEY → warning logged
- mem0 enabled + URL present          → no warning logged
- Enabled + client library missing    → import warning logged
- zep_enabled set                      → one warning: Zep is gone
- Warning only fires once per G10Memory instance (not every request)
- G10 disabled entirely                → no warning, no processing
"""
import logging
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from middleware import RequestContext
from middleware.g10_memory import G10Memory


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _ctx(mem0_enabled=False, messages=None, **g10):
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
                **g10,
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
        ctx = _ctx(mem0_enabled=True)

        with patch.dict("os.environ", {}, clear=False), \
             patch("middleware.g10_memory._MEM0_API_URL", ""), \
             _patch_session():
            with caplog.at_level(logging.WARNING, logger="middleware.g10_memory"):
                await g10.process_request(ctx)

        assert any("MEM0_API_URL" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_a_config_that_still_turns_zep_on_is_told_it_is_gone(self, caplog):
        """zep_enabled → one warning that Zep support was removed, and nothing tries to run it."""
        g10 = G10Memory()
        ctx = _ctx(zep_enabled=True)

        with patch("middleware.g10_memory._MEM0_API_URL", ""), _patch_session():
            with caplog.at_level(logging.WARNING, logger="middleware.g10_memory"):
                await g10.process_request(ctx)
                await g10.process_request(ctx)

        zep = [r for r in caplog.records if "zep_enabled" in r.message]
        assert len(zep) == 1 and "removed" in zep[0].message
        assert not any("MEM0_API_URL" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_url_present_suppresses_warnings(self, caplog, monkeypatch):
        """Enabled with URL and key configured → no health warning logged."""
        monkeypatch.setenv("MEM0_API_KEY", "set")
        g10 = G10Memory()
        ctx = _ctx(mem0_enabled=True)

        with patch("middleware.g10_memory._MEM0_API_URL", "http://mem0.internal"), \
             _patch_session():
            with caplog.at_level(logging.WARNING, logger="middleware.g10_memory"):
                await g10.process_request(ctx)

        assert [r for r in caplog.records if "MEM0_API" in r.message] == []

    @pytest.mark.asyncio
    async def test_mem0_with_a_url_but_no_key_warns(self, caplog, monkeypatch):
        """mem0_enabled=true + MEM0_API_URL but no MEM0_API_KEY → warning logged (the
        Mem0 client is never built without a key); with the key, none."""
        monkeypatch.delenv("MEM0_API_KEY", raising=False)
        with patch("middleware.g10_memory._MEM0_API_URL", "https://api.mem0.ai"), \
             _patch_session():
            with caplog.at_level(logging.WARNING, logger="middleware.g10_memory"):
                await G10Memory().process_request(_ctx(mem0_enabled=True))
            assert any("MEM0_API_KEY" in r.message for r in caplog.records)

            caplog.clear()
            monkeypatch.setenv("MEM0_API_KEY", "set")
            with caplog.at_level(logging.WARNING, logger="middleware.g10_memory"):
                await G10Memory().process_request(_ctx(mem0_enabled=True))
            assert not any("MEM0_API_KEY" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_warning_fires_only_once_per_instance(self, caplog):
        """Health warning is emitted on the first request only, not subsequent ones."""
        g10 = G10Memory()
        ctx = _ctx(mem0_enabled=True)

        with patch("middleware.g10_memory._MEM0_API_URL", ""), _patch_session():
            with caplog.at_level(logging.WARNING, logger="middleware.g10_memory"):
                await g10.process_request(ctx)
                await g10.process_request(ctx)
                await g10.process_request(ctx)

        mem0_warnings = [r for r in caplog.records if "MEM0_API_URL" in r.message]
        assert len(mem0_warnings) == 1

    @pytest.mark.asyncio
    async def test_fresh_instance_warns_again(self, caplog):
        """A new G10Memory instance starts with _health_warned=False, so warns again."""
        ctx = _ctx(mem0_enabled=True)

        with patch("middleware.g10_memory._MEM0_API_URL", ""), _patch_session():
            with caplog.at_level(logging.WARNING, logger="middleware.g10_memory"):
                await G10Memory().process_request(ctx)
                await G10Memory().process_request(ctx)

        mem0_warnings = [r for r in caplog.records if "MEM0_API_URL" in r.message]
        assert len(mem0_warnings) == 2  # one per instance

    @pytest.mark.asyncio
    async def test_not_enabled_no_warning(self, caplog):
        """Mem0 off (the default) → no health warning, no spurious noise."""
        g10 = G10Memory()
        ctx = _ctx(mem0_enabled=False)

        with patch("middleware.g10_memory._MEM0_API_URL", ""), _patch_session():
            with caplog.at_level(logging.WARNING, logger="middleware.g10_memory"):
                await g10.process_request(ctx)

        assert [r for r in caplog.records if "MEM0_API" in r.message or "zep" in r.message] == []

    @pytest.mark.asyncio
    async def test_enabled_without_its_client_library_warns(self, caplog):
        """The URL can be set and the memory still never run: the client library must
        import too. Say so, rather than leave the operator thinking memory is on."""
        g10 = G10Memory()
        ctx = _ctx(mem0_enabled=True)

        with patch("middleware.g10_memory._MEM0_API_URL", "http://mem0.internal"), \
             patch("middleware.g10_memory._mem0_available", False), \
             _patch_session():
            with caplog.at_level(logging.WARNING, logger="middleware.g10_memory"):
                await g10.process_request(ctx)

        assert [r.message for r in caplog.records if "client library" in r.message] == [
            "G10: mem0_enabled=true but the mem0 client library could not be "
            "imported, so this memory is inactive."]

    @pytest.mark.asyncio
    async def test_a_loadable_client_library_raises_no_import_warning(self, caplog):
        g10 = G10Memory()
        ctx = _ctx(mem0_enabled=True)

        with patch("middleware.g10_memory._MEM0_API_URL", "http://mem0.internal"), \
             patch("middleware.g10_memory._mem0_available", True), \
             patch.object(G10Memory, "_get_mem0", return_value=None), \
             _patch_session():
            with caplog.at_level(logging.WARNING, logger="middleware.g10_memory"):
                await g10.process_request(ctx)

        assert not [r for r in caplog.records if "client library" in r.message]

    @pytest.mark.asyncio
    async def test_g10_disabled_skips_health_check(self, caplog):
        """Group disabled → process_request returns immediately, health check never runs."""
        g10 = G10Memory()
        ctx = _ctx(mem0_enabled=True, zep_enabled=True)
        ctx.config["groups"]["G10_memory"]["enabled"] = False

        with patch("middleware.g10_memory._MEM0_API_URL", ""):
            with caplog.at_level(logging.WARNING, logger="middleware.g10_memory"):
                result = await g10.process_request(ctx)

        assert result is ctx
        assert [r for r in caplog.records if "MEM0_API_URL" in r.message or "zep" in r.message] == []
        assert g10._health_warned is False  # flag not set when group is disabled

    @pytest.mark.asyncio
    async def test_health_warned_flag_set_after_first_request(self):
        """_health_warned flag is True after the first enabled request."""
        g10 = G10Memory()
        assert g10._health_warned is False

        ctx = _ctx(mem0_enabled=False)
        with patch("middleware.g10_memory._MEM0_API_URL", ""), _patch_session():
            await g10.process_request(ctx)

        assert g10._health_warned is True
