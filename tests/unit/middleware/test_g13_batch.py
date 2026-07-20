"""Unit tests for G13 — Batch Processing & Compact Notation."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.asyncio
class TestG13Batch:
    async def test_disabled_passes_through(self, make_ctx):
        ctx = make_ctx()
        ctx.config["groups"]["G13_batch"]["enabled"] = False
        original = [m.copy() for m in ctx.messages]
        from middleware.g13_batch import G13Batch
        ctx = await G13Batch().process_request(ctx)
        assert ctx.messages == original

    async def test_no_structured_data_unchanged(self, make_ctx):
        ctx = make_ctx([{"role": "user", "content": "Plain text question here."}])
        original_content = ctx.messages[0]["content"]
        from middleware.g13_batch import G13Batch
        ctx = await G13Batch().process_request(ctx)
        assert ctx.messages[0]["content"] == original_content

    async def test_batch_topic_defers_request(self, make_ctx):
        ctx = make_ctx(
            [{"role": "user", "content": "Classify this text."}],
            params={"batch_topic": "classification"},
        )
        from middleware.g13_batch import G13Batch
        ctx = await G13Batch().process_request(ctx)
        assert ctx.batch_deferred is True

    async def test_toon_notation_applied_to_json_in_message(self, make_ctx):
        # TOON only fires when a system message contains 'schema' AND '|'
        json_data = '[{"name": "Alice", "age": 30}, {"name": "Bob", "age": 25}, {"name": "Carol", "age": 35}]'
        ctx = make_ctx([
            {"role": "system", "content": "schema:name|age"},
            {"role": "user", "content": f"Analyse this data: {json_data}"},
        ])
        tokens_before = ctx.current_token_count
        from middleware.g13_batch import G13Batch
        ctx = await G13Batch().process_request(ctx)
        # If TOON notation applied, token count should be ≤ before
        assert ctx.current_token_count <= tokens_before

    async def test_step_saving_recorded_when_toon_applied(self, make_ctx):
        big_array = str([{"id": i, "value": f"item-{i}", "status": "active"} for i in range(10)])
        # Need system message with 'schema' and '|' to trigger TOON
        ctx = make_ctx([
            {"role": "system", "content": "schema:id|value|status"},
            {"role": "user", "content": big_array},
        ])
        from middleware.g13_batch import G13Batch
        ctx = await G13Batch().process_request(ctx)
        for s in ctx.savings.step_savings:
            if s.group == "G13":
                assert s.tokens_after <= s.tokens_before

    async def test_no_system_schema_no_toon(self, make_ctx):
        # Without system message containing schema|, TOON should NOT fire
        json_data = '[{"name": "Alice", "age": 30}, {"name": "Bob", "age": 25}, {"name": "Carol", "age": 35}]'
        ctx = make_ctx([{"role": "user", "content": json_data}])
        original_content = ctx.messages[0]["content"]
        from middleware.g13_batch import G13Batch
        ctx = await G13Batch().process_request(ctx)
        assert ctx.messages[-1]["content"] == original_content


@pytest.mark.asyncio
class TestBatchBaselineTokensAttribution:
    """Regression: /v1/batch/results has no RequestContext at poll time, so
    baseline_tokens must be captured at accumulate-time and threaded through the flush
    lane to _store_batch_result for the poller's x-tokenlean-* headers to be accurate."""

    async def test_accumulate_includes_baseline_tokens(self, make_ctx):
        from middleware.g13_batch import _accumulate
        ctx = make_ctx([{"role": "user", "content": "hi"}])
        ctx.savings.baseline_tokens = 77
        mock_redis = AsyncMock()
        mock_redis.xadd = AsyncMock()
        with patch("middleware.g13_batch._get_redis", return_value=mock_redis):
            await _accumulate(ctx, "topic1")
        _stream, fields = mock_redis.xadd.await_args.args
        payload = json.loads(fields["payload"])
        assert payload["baseline_tokens"] == 77

    async def test_flush_loop_threads_baseline_tokens_into_stored_result(self):
        from middleware import g13_batch
        items = [{"request_id": "r0", "messages": [{"role": "user", "content": "hi"}],
                  "params": {}, "model": "gpt-4o-mini", "baseline_tokens": 500,
                  "tenant_id": "acme"}]
        fake_resp = MagicMock()
        fake_resp.model_dump.return_value = {"id": "c0", "usage": {"prompt_tokens": 120}}
        with patch("config_loader.get_provider_model_prefixes", return_value={"gpt-4o-mini": "openai"}), \
             patch("config_loader.get_providers", return_value=[]), \
             patch("providers.build_litellm_call", return_value=("gpt-4o-mini", {})), \
             patch("providers.key_resolver.resolve_provider_key",
                   new=AsyncMock(return_value="sk-test")), \
             patch("litellm.acompletion", new=AsyncMock(return_value=fake_resp)), \
             patch.object(g13_batch, "_store_batch_result", new=AsyncMock()) as store:
            await g13_batch._flush_batch_loop("topic1", items, {})
        store.assert_awaited_once()
        request_id_arg, stored = store.await_args.args
        assert request_id_arg == "r0"
        assert stored["status"] == "completed"
        assert stored["baseline_tokens"] == 500

    async def test_flush_loop_missing_baseline_tokens_defaults_to_zero(self):
        from middleware import g13_batch
        items = [{"request_id": "r0", "messages": [{"role": "user", "content": "hi"}],
                  "params": {}, "model": "gpt-4o-mini", "tenant_id": "acme"}]
        fake_resp = MagicMock()
        fake_resp.model_dump.return_value = {"id": "c0"}
        with patch("config_loader.get_provider_model_prefixes", return_value={"gpt-4o-mini": "openai"}), \
             patch("config_loader.get_providers", return_value=[]), \
             patch("providers.build_litellm_call", return_value=("gpt-4o-mini", {})), \
             patch("providers.key_resolver.resolve_provider_key",
                   new=AsyncMock(return_value="sk-test")), \
             patch("litellm.acompletion", new=AsyncMock(return_value=fake_resp)), \
             patch.object(g13_batch, "_store_batch_result", new=AsyncMock()) as store:
            await g13_batch._flush_batch_loop("topic1", items, {})
        stored = store.await_args.args[1]
        assert stored["baseline_tokens"] == 0


class TestCompactJsonToToon:
    """Boundary tests for the lowered TOON array-length trigger threshold
    (now >= 2 identical-key items, previously >= 3)."""

    def test_two_item_array_triggers_toon(self):
        from middleware.g13_batch import _compact_json_to_toon
        json_data = '[{"name": "Alice", "age": 30}, {"name": "Bob", "age": 25}]'
        content = f"Analyse this data: {json_data}"
        result = _compact_json_to_toon(content)
        assert "schema:name|age" in result
        assert "Alice|30" in result
        assert "Bob|25" in result

    def test_single_item_array_does_not_trigger_toon(self):
        from middleware.g13_batch import _compact_json_to_toon
        json_data = '[{"name": "Alice", "age": 30, "city": "Paris", "role": "engineer"}]'
        content = f"Analyse this data: {json_data}"
        result = _compact_json_to_toon(content)
        assert result == content

    def test_mixed_key_two_item_array_does_not_trigger_toon(self):
        from middleware.g13_batch import _compact_json_to_toon
        json_data = '[{"name": "Alice", "age": 30}, {"name": "Bob", "city": "Paris"}]'
        content = f"Analyse this data: {json_data}"
        result = _compact_json_to_toon(content)
        assert result == content

    def test_three_item_array_still_triggers_toon(self):
        from middleware.g13_batch import _compact_json_to_toon
        json_data = '[{"name": "Alice", "age": 30}, {"name": "Bob", "age": 25}, {"name": "Carol", "age": 35}]'
        content = f"Analyse this data: {json_data}"
        result = _compact_json_to_toon(content)
        assert "schema:name|age" in result


class TestToonGating:
    """Eligibility, net-savings and coverage gates added by the tabular-gating work."""

    def test_nested_array_not_compressed_by_default(self):
        from middleware.g13_batch import _compact_json_to_toon
        content = 'rows [{"id": 1, "meta": {"k": "v"}}, {"id": 2, "meta": {"k": "w"}}]'
        # Nested object values → scalar-only gate leaves the block as JSON.
        assert _compact_json_to_toon(content) == content

    def test_nested_array_compressed_when_allowed(self):
        from middleware.g13_batch import _compact_json_to_toon
        content = 'rows [{"id": 1, "meta": {"k": "v"}}, {"id": 2, "meta": {"k": "w"}}]'
        out = _compact_json_to_toon(
            content, {"toon_allow_nested": True, "toon_require_net_savings": False}
        )
        assert "schema:id|meta" in out

    def test_large_array_over_2000_chars_now_compressed(self):
        import json as _json
        from middleware.g13_batch import _compact_json_to_toon
        data = [{"id": i, "value": f"item-{i}", "status": "active"} for i in range(80)]
        json_str = _json.dumps(data)
        assert len(json_str) > 2000  # the legacy 2000-char ceiling would have skipped this
        content = f"Records: {json_str}"
        result = _compact_json_to_toon(content)
        assert "schema:id|value|status" in result
        assert len(result) < len(content)

    def test_multiple_blocks_all_compressed(self):
        from middleware.g13_batch import _compact_json_to_toon
        a = '[{"x": 1}, {"x": 2}]'
        b = '[{"y": 3}, {"y": 4}]'
        content = f"First {a} then {b}"
        result = _compact_json_to_toon(content)
        assert "schema:x" in result
        assert "schema:y" in result

    def test_min_rows_boundary(self):
        from middleware.g13_batch import _compact_json_to_toon
        content = 'data [{"a": 1}, {"a": 2}]'  # 2-row array
        assert _compact_json_to_toon(content, {"toon_min_rows": 3}) == content
        assert "schema:a" in _compact_json_to_toon(content, {"toon_min_rows": 2})

    def test_uniform_threshold_outlier(self):
        from middleware.g13_batch import _compact_json_to_toon
        content = (
            'rows [{"name": "Alice", "age": 30}, {"name": "Bob", "age": 25}, '
            '{"name": "Carol", "age": 35, "city": "Paris"}]'
        )
        # Strict (1.0): the superset outlier breaks uniformity → left as JSON.
        assert _compact_json_to_toon(content, {"toon_uniform_threshold": 1.0}) == content
        # 0.6: 2/3 rows share the modal key-set → compress with a union header.
        out = _compact_json_to_toon(content, {"toon_uniform_threshold": 0.6})
        assert "schema:name|age|city" in out
        assert "Carol|35|Paris" in out

    def test_net_savings_guard_reverts_when_not_smaller(self, monkeypatch):
        from middleware import g13_batch
        # Force the estimator to report TOON as not strictly smaller → guard reverts.
        monkeypatch.setattr(g13_batch, "estimate_tokens", lambda text, model="": 100)
        json_data = '[{"name": "Alice", "age": 30}, {"name": "Bob", "age": 25}]'
        out = g13_batch._compact_json_to_toon(json_data, {"toon_require_net_savings": True})
        assert out == json_data  # reverted

    def test_net_savings_guard_disabled_applies_even_if_not_smaller(self, monkeypatch):
        from middleware import g13_batch
        monkeypatch.setattr(g13_batch, "estimate_tokens", lambda text, model="": 100)
        json_data = '[{"name": "Alice", "age": 30}, {"name": "Bob", "age": 25}]'
        out = g13_batch._compact_json_to_toon(json_data, {"toon_require_net_savings": False})
        assert "schema:name|age" in out  # applied despite equal size


@pytest.mark.asyncio
class TestToonAutoDetect:
    """Auto-detect mode (no manual `schema:` marker) + per-tenant override."""

    async def test_auto_detect_compresses_without_schema_marker(self, make_ctx):
        json_data = '[{"name": "Alice", "age": 30}, {"name": "Bob", "age": 25}, {"name": "Carol", "age": 35}]'
        ctx = make_ctx([{"role": "user", "content": f"Analyse: {json_data}"}])
        ctx.config["groups"]["G13_batch"]["toon_auto_detect"] = True
        before = ctx.current_token_count
        from middleware.g13_batch import G13Batch
        ctx = await G13Batch().process_request(ctx)
        assert "schema:name|age" in ctx.messages[0]["content"]
        assert ctx.current_token_count <= before

    async def test_auto_detect_off_no_marker_unchanged(self, make_ctx):
        json_data = '[{"name": "Alice", "age": 30}, {"name": "Bob", "age": 25}]'
        ctx = make_ctx([{"role": "user", "content": f"Analyse: {json_data}"}])
        ctx.config["groups"]["G13_batch"]["toon_auto_detect"] = False
        original = ctx.messages[0]["content"]
        from middleware.g13_batch import G13Batch
        ctx = await G13Batch().process_request(ctx)
        assert ctx.messages[0]["content"] == original

    async def test_per_tenant_auto_detect_override(self, make_ctx):
        json_data = '[{"name": "Alice", "age": 30}, {"name": "Bob", "age": 25}]'
        ctx = make_ctx([{"role": "user", "content": f"Analyse: {json_data}"}])
        # Global default off; the tenant turns auto-detect on.
        ctx.tenant_id = "acme"
        ctx.config.setdefault("tenants", {})["acme"] = {
            "groups": {"G13_batch": {"toon_auto_detect": True}}
        }
        from middleware.g13_batch import G13Batch
        ctx = await G13Batch().process_request(ctx)
        assert "schema:name|age" in ctx.messages[0]["content"]
