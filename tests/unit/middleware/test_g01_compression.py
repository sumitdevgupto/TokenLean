"""Unit tests for G01 — Prompt Compression via LLMLingua-2 sidecar."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, patch


class TestLayeredPromptComposer:
    """Tests for the consolidated base→role→task→dynamic layer composer
    (migrated from the now-removed g01_layered_composer.py)."""

    def test_compose_all_layers(self):
        from middleware.g01_compression import LayeredPromptComposer

        composer = LayeredPromptComposer({
            "layers": {
                "base": "Company: Acme Corp\nTone: Professional",
                "role": "Role: Customer Support Agent\nGoal: Resolve issues quickly",
                "task": "Task: Process refund request",
                "dynamic": "Order: #12345\nAmount: $99.99",
            },
        })

        composed = composer.compose({})

        assert "Acme Corp" in composed
        assert "Customer Support Agent" in composed
        assert "Process refund" in composed
        assert "Order: #12345" in composed

    def test_static_compression_removes_duplicate_role_lines(self):
        from middleware.g01_compression import LayeredPromptComposer

        composer = LayeredPromptComposer({
            "layers": {
                "base": "Tone: Professional\nBe helpful",
                "role": "Tone: Professional\nGoal: Resolve issues",
                "task": "Task: Help",
            },
            "build_time_compression": True,
        })

        composed = composer.compose({})

        assert composed.count("Tone: Professional") == 1
        assert "Goal: Resolve issues" in composed
        assert "Task: Help" in composed

    def test_static_layers_cached_across_calls(self):
        from middleware.g01_compression import LayeredPromptComposer

        composer = LayeredPromptComposer({
            "layers": {
                "base": "Base prompt here",
                "role": "Role prompt here",
                "task": "{task}",
            },
            "build_time_compression": True,
        })

        composed1 = composer.compose({"task": "Task: A"})
        composed2 = composer.compose({"task": "Task: B"})

        static1 = composed1.split("Task: A")[0].strip()
        static2 = composed2.split("Task: B")[0].strip()
        assert static1 == static2
        # Cache is reused, not recomputed, on the second call
        assert len(composer._compressed_cache) == 1

    def test_dynamic_layers_always_fresh(self):
        from middleware.g01_compression import LayeredPromptComposer

        composer = LayeredPromptComposer({
            "layers": {
                "base": "Base",
                "role": "Role",
                "task": "{task}",
                "dynamic": "Context: {ctx}",
            },
        })

        composed1 = composer.compose({"task": "Task: A", "ctx": "X"})
        composed2 = composer.compose({"task": "Task: B", "ctx": "Y"})

        assert "Task: A" in composed1 and "Context: X" in composed1
        assert "Task: B" in composed2 and "Context: Y" in composed2

    def test_build_time_compression_disabled_keeps_both_layers_verbatim(self):
        from middleware.g01_compression import LayeredPromptComposer

        composer = LayeredPromptComposer({
            "layers": {
                "base": "Tone: Professional",
                "role": "Tone: Professional",
            },
            "build_time_compression": False,
        })

        composed = composer.compose({})

        assert composed.count("Tone: Professional") == 2


@pytest.mark.asyncio
class TestG01Compression:
    async def test_disabled_skips_compression(self, make_ctx):
        ctx = make_ctx()
        ctx.config["groups"]["G1_compression"]["enabled"] = False
        original_messages = ctx.messages[:]

        from middleware.g01_compression import G01Compression
        ctx = await G01Compression().process_request(ctx)

        assert ctx.messages == original_messages
        assert len(ctx.savings.step_savings) == 0

    async def test_short_messages_skipped(self, make_ctx):
        ctx = make_ctx([{"role": "user", "content": "Hi"}])
        ctx.config["groups"]["G1_compression"]["min_tokens_to_compress"] = 1000

        from middleware.g01_compression import G01Compression
        original = ctx.messages[:]
        ctx = await G01Compression().process_request(ctx)
        assert ctx.messages == original

    async def test_compression_saves_tokens(self, make_ctx):
        long_system = "You are a helpful assistant. " * 30
        messages = [
            {"role": "system", "content": long_system},
            {"role": "user", "content": "Summarise."},
        ]
        ctx = make_ctx(messages)
        ctx.config["groups"]["G1_compression"]["min_tokens_to_compress"] = 5
        ctx.config["groups"]["G1_compression"]["compress_system_prompt"] = True  # opt in (system preserved by default)

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"compressed": "S."}  # shorter than original
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client_cls.return_value = mock_client

            from middleware.g01_compression import G01Compression
            ctx = await G01Compression().process_request(ctx)

        assert len(ctx.savings.step_savings) == 1
        step = ctx.savings.step_savings[0]
        assert step.group == "G01"
        assert step.tokens_after < step.tokens_before

    async def test_user_messages_not_compressed_by_default(self, make_ctx):
        long_user = "Please summarise this document. " * 30
        messages = [{"role": "user", "content": long_user}]
        ctx = make_ctx(messages)
        ctx.config["groups"]["G1_compression"]["min_tokens_to_compress"] = 5
        original_messages = [m.copy() for m in ctx.messages]

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"compressed": "Summarise."}
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client_cls.return_value = mock_client

            from middleware.g01_compression import G01Compression
            ctx = await G01Compression().process_request(ctx)

        # Default behavior unchanged: user messages are left verbatim
        assert ctx.messages == original_messages
        assert len(ctx.savings.step_savings) == 0

    async def test_user_messages_compressed_when_opted_in(self, make_ctx):
        long_user = "Please summarise this document. " * 30
        messages = [{"role": "user", "content": long_user}]
        ctx = make_ctx(messages)
        ctx.config["groups"]["G1_compression"]["min_tokens_to_compress"] = 5
        ctx.config["groups"]["G1_compression"]["compress_user_messages"] = True

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"compressed": "Summarise."}
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client_cls.return_value = mock_client

            from middleware.g01_compression import G01Compression
            ctx = await G01Compression().process_request(ctx)

        assert ctx.messages[0]["content"] == "Summarise."
        assert len(ctx.savings.step_savings) == 1
        step = ctx.savings.step_savings[0]
        assert step.group == "G01"
        assert step.tokens_after < step.tokens_before

    async def test_sidecar_error_falls_back_gracefully(self, make_ctx):
        long_system = "You are a helpful assistant. " * 30
        messages = [{"role": "system", "content": long_system}, {"role": "user", "content": "ok"}]
        ctx = make_ctx(messages)
        ctx.config["groups"]["G1_compression"]["min_tokens_to_compress"] = 5
        original_messages = [m.copy() for m in ctx.messages]

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(side_effect=Exception("connection refused"))
            mock_client_cls.return_value = mock_client

            from middleware.g01_compression import G01Compression
            ctx = await G01Compression().process_request(ctx)

        # Messages should be unchanged on error
        assert ctx.messages == original_messages
        assert len(ctx.savings.step_savings) == 0


# ─── T14: Kompress-v2-base fallback ─────────────────────────────────────────

class TestKompressV2Fallback:

    def test_is_log_error_content_detects_timestamps(self):
        from middleware.g01_compression import _is_log_error_content
        log = "2024-01-15 12:34:56 INFO Starting service\n2024-01-15 12:34:57 DEBUG Connected"
        assert _is_log_error_content(log) is True

    def test_is_log_error_content_detects_level_prefixes(self):
        from middleware.g01_compression import _is_log_error_content
        assert _is_log_error_content("[ERROR] Connection refused") is True
        assert _is_log_error_content("WARN: disk usage high") is True

    def test_is_log_error_content_detects_traceback(self):
        from middleware.g01_compression import _is_log_error_content
        tb = "Traceback (most recent call last):\n  File 'app.py', line 42\nValueError: bad value"
        assert _is_log_error_content(tb) is True

    def test_is_log_error_content_natural_language_returns_false(self):
        from middleware.g01_compression import _is_log_error_content
        prose = "Please summarise the following document for a business audience."
        assert _is_log_error_content(prose) is False

    def test_kompress_compress_returns_none_when_transformers_unavailable(self):
        from unittest.mock import patch
        from middleware.g01_compression import _kompress_compress
        import middleware.g01_compression as mod

        with patch.object(mod, "_kompress_loaded", False), \
             patch.object(mod, "_kompress_pipe", None), \
             patch("builtins.__import__", side_effect=ImportError("no transformers")):
            # Reset load state so _get_kompress_pipe tries again
            mod._kompress_loaded = False
            result = _kompress_compress("ERROR: test", "microsoft/Kompress-v2-base")
        assert result is None
        # Restore state so other tests are unaffected
        mod._kompress_loaded = False
        mod._kompress_pipe = None
        mod._kompress_pipe_model = None

    def test_kompress_compress_uses_pipeline_result(self):
        from unittest.mock import MagicMock, patch
        from middleware.g01_compression import _kompress_compress
        import middleware.g01_compression as mod

        mock_pipe = MagicMock(return_value=[{"generated_text": "compressed"}])
        original_text = "ERROR: connection refused\n" * 20  # longer than "compressed"

        with patch.object(mod, "_kompress_pipe", mock_pipe), \
             patch.object(mod, "_kompress_loaded", True), \
             patch.object(mod, "_kompress_pipe_model", "microsoft/Kompress-v2-base"):
            result = _kompress_compress(original_text, "microsoft/Kompress-v2-base")

        assert result == "compressed"
        mock_pipe.assert_called_once()

    def test_kompress_not_called_for_natural_language(self):
        """Kompress fallback must NOT run for non-log content."""
        from unittest.mock import patch, MagicMock
        import middleware.g01_compression as mod

        mock_pipe = MagicMock()

        with patch.object(mod, "_kompress_pipe", mock_pipe), \
             patch.object(mod, "_kompress_loaded", True):
            from middleware.g01_compression import _is_log_error_content, _kompress_compress
            prose = "You are a helpful assistant that summarises documents."
            # Simulate what process_request does: only call Kompress if _is_log_error_content
            if _is_log_error_content(prose):
                _kompress_compress(prose, "microsoft/Kompress-v2-base")

        mock_pipe.assert_not_called()

    @pytest.mark.asyncio
    async def test_kompress_fallback_fires_when_sidecar_down(self, make_ctx):
        """When LLMLingua sidecar is down and content is log/error, Kompress compresses."""
        log_content = "2024-01-15T10:00:00 ERROR db connection pool exhausted\n" * 15
        messages = [{"role": "system", "content": log_content}, {"role": "user", "content": "summarise"}]
        ctx = make_ctx(messages)
        ctx.config["groups"]["G1_compression"].update({
            "min_tokens_to_compress": 1,
            "min_chars_to_compress": 1,
            "kompress_enabled": True,
            "kompress_model": "microsoft/Kompress-v2-base",
            "compress_system_prompt": True,  # opt in (system preserved by default)
        })

        import middleware.g01_compression as mod
        mock_pipe = MagicMock(return_value=[{"generated_text": "db pool exhausted (compressed)"}])

        with patch("httpx.AsyncClient") as mock_http:
            # Sidecar unavailable
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(side_effect=Exception("sidecar down"))
            mock_http.return_value = mock_client

            with patch.object(mod, "_kompress_pipe", mock_pipe), \
                 patch.object(mod, "_kompress_loaded", True), \
                 patch.object(mod, "_kompress_pipe_model", "microsoft/Kompress-v2-base"):
                from middleware.g01_compression import G01Compression
                ctx = await G01Compression().process_request(ctx)

        assert ctx.messages[0]["content"] == "db pool exhausted (compressed)"
        step = ctx.savings.step_savings[0]
        assert step.group == "G01"

    @pytest.mark.asyncio
    async def test_kompress_disabled_via_config(self, make_ctx):
        """kompress_enabled=False prevents Kompress from running even for log content."""
        log_content = "2024-01-15T10:00:00 ERROR crash\n" * 15
        messages = [{"role": "system", "content": log_content}, {"role": "user", "content": "ok"}]
        ctx = make_ctx(messages)
        ctx.config["groups"]["G1_compression"].update({
            "min_tokens_to_compress": 1,
            "min_chars_to_compress": 1,
            "kompress_enabled": False,
        })

        import middleware.g01_compression as mod
        mock_pipe = MagicMock(return_value=[{"generated_text": "should not appear"}])

        with patch("httpx.AsyncClient") as mock_http:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(side_effect=Exception("sidecar down"))
            mock_http.return_value = mock_client

            with patch.object(mod, "_kompress_pipe", mock_pipe), \
                 patch.object(mod, "_kompress_loaded", True), \
                 patch.object(mod, "_kompress_pipe_model", "microsoft/Kompress-v2-base"):
                from middleware.g01_compression import G01Compression
                ctx = await G01Compression().process_request(ctx)

        mock_pipe.assert_not_called()
        # Content unchanged because both sidecar and Kompress are disabled/down
        assert ctx.messages[0]["content"] == log_content


# ─── 5e: digit preservation (force_reserve_digit) ───────────────────────────
#
# LLMLingua-2 otherwise treats digits as low-information and can silently drop
# one — corrupting a date/ID/amount that a downstream tool call depends on
# (DS3: an incident date "2023-10-18" became a wrong "2023-10-02" `since=`
# window). The proxy must pass force_reserve_digit through to the sidecar.

@pytest.mark.asyncio
class TestG01ForceReserveDigit:

    async def _capture_sidecar_payload(self, ctx):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"compressed": "S."}  # shorter than original
        mock_resp.raise_for_status = MagicMock()
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client_cls.return_value = mock_client
            from middleware.g01_compression import G01Compression
            await G01Compression().process_request(ctx)
        assert mock_client.post.await_count >= 1, "sidecar was not called"
        return mock_client.post.call_args.kwargs["json"]

    def _ctx_with_dated_system(self, make_ctx):
        long_system = "Investigate the incident that occurred on 2023-10-18 in detail. " * 10
        ctx = make_ctx([
            {"role": "system", "content": long_system},
            {"role": "user", "content": "go"},
        ])
        ctx.config["groups"]["G1_compression"]["min_tokens_to_compress"] = 5
        ctx.config["groups"]["G1_compression"]["compress_system_prompt"] = True
        return ctx

    async def test_force_reserve_digit_sent_to_sidecar_by_default(self, make_ctx):
        ctx = self._ctx_with_dated_system(make_ctx)
        payload = await self._capture_sidecar_payload(ctx)
        assert payload["force_reserve_digit"] is True

    async def test_force_reserve_digit_disabled_via_config(self, make_ctx):
        ctx = self._ctx_with_dated_system(make_ctx)
        ctx.config["groups"]["G1_compression"]["force_reserve_digit"] = False
        payload = await self._capture_sidecar_payload(ctx)
        assert payload["force_reserve_digit"] is False


@pytest.mark.asyncio
async def test_call_llmlingua_payload_includes_force_reserve_digit():
    """The sidecar-call helper always sends ratio + force_reserve_digit."""
    from middleware.g01_compression import _call_llmlingua
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"compressed": "x"}
    mock_resp.raise_for_status = MagicMock()
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client
        out = await _call_llmlingua("http://x/compress", "some long text here", 0.5, force_reserve_digit=False)
    assert out == "x"
    payload = mock_client.post.call_args.kwargs["json"]
    assert payload["ratio"] == 0.5
    assert payload["force_reserve_digit"] is False
