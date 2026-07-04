"""Unit tests for G04 — Rules-Based Bypass."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.asyncio
class TestG04Bypass:
    async def test_disabled_passes_through(self, make_ctx):
        ctx = make_ctx([{"role": "user", "content": "hello"}])
        ctx.config["groups"]["G4_bypass"]["enabled"] = False
        from middleware.g04_bypass import G04Bypass
        ctx = await G04Bypass().process_request(ctx)
        assert ctx.bypassed is False

    async def test_keyword_match_sets_bypassed(self, make_ctx):
        ctx = make_ctx([{"role": "user", "content": "hello there"}])
        from middleware.g04_bypass import G04Bypass
        ctx = await G04Bypass().process_request(ctx)
        assert ctx.bypassed is True
        assert ctx.savings.bypassed is True

    async def test_no_match_not_bypassed(self, make_ctx):
        ctx = make_ctx([{"role": "user", "content": "What is quantum computing?"}])
        from middleware.g04_bypass import G04Bypass
        ctx = await G04Bypass().process_request(ctx)
        assert ctx.bypassed is False

    async def test_db_cache_ttl_seconds_caches_within_window(self, make_ctx):
        # db_cache_ttl_seconds (now config-driven) keeps DB rules cached within the window.
        # Deterministic clock: each time.time() call advances by 1s, so the two requests are
        # a few seconds apart — well inside a 3600s window.
        import middleware.g04_bypass as mod
        db_rule = [{"name": "x", "keywords": ["zzz"], "confidence_threshold": 0.9}]
        clock = [1_700_000_000.0]

        def _tick():
            clock[0] += 1.0
            return clock[0]

        with patch.object(mod, "_load_rules_from_db", new=AsyncMock(return_value=db_rule)) as m, \
                patch.object(mod.time, "time", side_effect=_tick):
            bypass = mod.G04Bypass()
            for content in ("unrelated one", "unrelated two"):
                ctx = make_ctx([{"role": "user", "content": content}])
                ctx.config["groups"]["G4_bypass"]["database_first"] = True
                ctx.config["groups"]["G4_bypass"]["db_cache_ttl_seconds"] = 3600
                await bypass.process_request(ctx)
            assert m.call_count == 1  # cached within the TTL window

    async def test_db_cache_ttl_zero_refetches_each_call(self, make_ctx):
        # With ttl=0 every request re-fetches. Deterministic clock guarantees the second
        # request's timestamp is strictly greater than the first load's, so (now - last) > 0.
        import middleware.g04_bypass as mod
        db_rule = [{"name": "x", "keywords": ["zzz"], "confidence_threshold": 0.9}]
        clock = [1_700_000_000.0]

        def _tick():
            clock[0] += 1.0
            return clock[0]

        with patch.object(mod, "_load_rules_from_db", new=AsyncMock(return_value=db_rule)) as m, \
                patch.object(mod.time, "time", side_effect=_tick):
            bypass = mod.G04Bypass()
            for content in ("unrelated a", "unrelated b"):
                ctx = make_ctx([{"role": "user", "content": content}])
                ctx.config["groups"]["G4_bypass"]["database_first"] = True
                ctx.config["groups"]["G4_bypass"]["db_cache_ttl_seconds"] = 0
                await bypass.process_request(ctx)
            assert m.call_count == 2  # TTL 0 → re-fetch each call

    async def test_bypass_records_step_saving(self, make_ctx):
        ctx = make_ctx([{"role": "user", "content": "hi there"}])
        from middleware.g04_bypass import G04Bypass
        ctx = await G04Bypass().process_request(ctx)
        assert len(ctx.savings.step_savings) == 1
        step = ctx.savings.step_savings[0]
        assert step.group == "G04"
        assert step.tokens_after == 0

    async def test_bypass_sets_cache_response(self, make_ctx):
        ctx = make_ctx([{"role": "user", "content": "hello world"}])
        from middleware.g04_bypass import G04Bypass
        ctx = await G04Bypass().process_request(ctx)
        assert ctx.cache_response is not None
        choices = ctx.cache_response.get("choices", [])
        assert len(choices) == 1
        assert "Hello! How can I help?" in choices[0]["message"]["content"]

    async def test_backend_url_rule_calls_httpx(self, make_ctx):
        ctx = make_ctx([{"role": "user", "content": "order status please"}])
        ctx.config["groups"]["G4_bypass"]["rules"] = [
            {
                "name": "order-status",
                "keywords": ["order status"],
                "backend_url": "http://mock-api/status",
            }
        ]
        from unittest.mock import MagicMock
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"response": "Your order is shipped."}
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_cls.return_value = mock_client

            from middleware.g04_bypass import G04Bypass
            ctx = await G04Bypass().process_request(ctx)

        assert ctx.bypassed is True

    async def test_no_user_message_skips(self, make_ctx):
        ctx = make_ctx([{"role": "system", "content": "You are helpful."}])
        from middleware.g04_bypass import G04Bypass
        ctx = await G04Bypass().process_request(ctx)
        assert ctx.bypassed is False

    async def test_regex_pattern_match(self, make_ctx):
        ctx = make_ctx([{"role": "user", "content": "order #12345"}])
        ctx.config["groups"]["G4_bypass"]["rules"] = [
            {
                "name": "order-re",
                "patterns": [r"order\s+#\d+"],
                "static_response": "Order found.",
            }
        ]
        from middleware.g04_bypass import G04Bypass
        ctx = await G04Bypass().process_request(ctx)
        assert ctx.bypassed is True


class TestBypassRuleConfidence:
    """Direct unit tests for BypassRule.matches() — the weighted confidence
    formula (40% keyword / 60% pattern) that drives bypass decisions."""

    def test_keywords_and_patterns_weighted_average(self):
        from middleware.g04_bypass import BypassRule

        rule = BypassRule({
            "name": "mixed",
            "keywords": ["hello", "hi", "hey", "yo"],
            "patterns": [r"order\s+#\d+"],
            "confidence_threshold": 0.5,
        })

        # 1/4 keywords hit (0.25), 1/1 pattern hits (1.0)
        # weighted: 0.25*0.4 + 1.0*0.6 = 0.7
        matched, confidence = rule.matches("hello, order #999")
        assert confidence == pytest.approx(0.7)
        assert matched is True

    def test_keywords_only_uses_keyword_confidence_directly(self):
        from middleware.g04_bypass import BypassRule

        rule = BypassRule({
            "name": "kw-only",
            "keywords": ["alpha", "beta"],
            "confidence_threshold": 0.5,
        })

        matched, confidence = rule.matches("alpha seen but nothing else")
        # 1/2 keywords hit
        assert confidence == pytest.approx(0.5)
        assert matched is True  # exactly at threshold

    def test_patterns_only_uses_pattern_confidence_directly(self):
        from middleware.g04_bypass import BypassRule

        rule = BypassRule({
            "name": "pattern-only",
            "patterns": [r"foo\d+", r"bar\d+"],
            "confidence_threshold": 0.6,
        })

        matched, confidence = rule.matches("foo123 but no bar here")
        # 1/2 patterns hit
        assert confidence == pytest.approx(0.5)
        assert matched is False  # below 0.6 threshold

    def test_below_threshold_does_not_match(self):
        from middleware.g04_bypass import BypassRule

        rule = BypassRule({
            "name": "strict",
            "keywords": ["alpha", "beta", "gamma", "delta"],
            "confidence_threshold": 0.9,
        })

        # only 1/4 keywords hit => confidence 0.25 < 0.9
        matched, confidence = rule.matches("alpha only")
        assert confidence == pytest.approx(0.25)
        assert matched is False

    def test_no_keywords_or_patterns_never_matches(self):
        from middleware.g04_bypass import BypassRule

        rule = BypassRule({"name": "empty", "confidence_threshold": 0.0})

        matched, confidence = rule.matches("anything at all")
        assert matched is False
        assert confidence == 0.0

    def test_case_insensitive_keyword_and_pattern_matching(self):
        from middleware.g04_bypass import BypassRule

        rule = BypassRule({
            "name": "case-insensitive",
            "keywords": ["HELLO"],
            "patterns": [r"ORDER\s+#\d+"],
            "confidence_threshold": 0.5,
        })

        matched, confidence = rule.matches("Hello, this is order #42")
        assert confidence == pytest.approx(1.0)
        assert matched is True


@pytest.mark.asyncio
class TestLoadRulesFromDb:
    async def test_no_database_url_returns_empty_list(self, monkeypatch):
        monkeypatch.delenv("DATABASE_URL", raising=False)

        from middleware.g04_bypass import _load_rules_from_db
        rules = await _load_rules_from_db()
        assert rules == []

    async def test_db_rows_are_parsed_into_rule_dicts(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql://test/db")

        mock_row = {
            "rule_id": "r1",
            "name": "order-status",
            "category": "support",
            "keywords": json.dumps(["order", "status"]),
            "patterns": json.dumps([r"order\s+#\d+"]),
            "backend_url": "http://mock-api/status",
            "static_response": None,
            "confidence_threshold": 0.7,
            "enabled": True,
        }
        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(return_value=[mock_row])
        mock_conn.close = AsyncMock()

        with patch("asyncpg.connect", new_callable=AsyncMock, return_value=mock_conn):
            from middleware.g04_bypass import _load_rules_from_db
            rules = await _load_rules_from_db()

        assert len(rules) == 1
        assert rules[0]["rule_id"] == "r1"
        assert rules[0]["keywords"] == ["order", "status"]
        assert rules[0]["patterns"] == [r"order\s+#\d+"]
        mock_conn.close.assert_awaited_once()

    async def test_db_connection_error_returns_empty_list(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql://test/db")

        with patch("asyncpg.connect", new_callable=AsyncMock, side_effect=Exception("connection refused")):
            from middleware.g04_bypass import _load_rules_from_db
            rules = await _load_rules_from_db()

        assert rules == []


@pytest.mark.asyncio
class TestLoadRulesPrefersDatabase:
    async def test_database_rules_take_precedence_over_config(self, make_ctx):
        ctx = make_ctx([{"role": "user", "content": "order #777 please"}])
        ctx.config["groups"]["G4_bypass"]["rules"] = [
            {"name": "config-rule", "keywords": ["nevermatch"], "static_response": "from-config"}
        ]
        ctx.config["groups"]["G4_bypass"]["database_first"] = True

        db_rules = [{
            "rule_id": "db-1",
            "name": "db-rule",
            "category": "general",
            "keywords": [],
            "patterns": [r"order\s+#\d+"],
            "backend_url": None,
            "static_response": "from-database",
            "confidence_threshold": 0.5,
        }]

        with patch("middleware.g04_bypass._load_rules_from_db", new_callable=AsyncMock, return_value=db_rules):
            from middleware.g04_bypass import G04Bypass
            ctx = await G04Bypass().process_request(ctx)

        assert ctx.bypassed is True
        assert ctx.cache_response["choices"][0]["message"]["content"] == "from-database"

    async def test_db_cache_not_reloaded_within_ttl(self, make_ctx):
        """Once DB rules are loaded, a second call within the 60s TTL must
        not hit the database again."""
        ctx1 = make_ctx([{"role": "user", "content": "order #1 please"}])
        ctx2 = make_ctx([{"role": "user", "content": "order #2 please"}])
        for ctx in (ctx1, ctx2):
            ctx.config["groups"]["G4_bypass"]["database_first"] = True

        db_rules = [{
            "rule_id": "db-1",
            "name": "db-rule",
            "category": "general",
            "keywords": [],
            "patterns": [r"order\s+#\d+"],
            "backend_url": None,
            "static_response": "from-database",
            "confidence_threshold": 0.5,
        }]

        with patch("middleware.g04_bypass._load_rules_from_db", new_callable=AsyncMock, return_value=db_rules) as mock_load:
            from middleware.g04_bypass import G04Bypass
            bypass = G04Bypass()
            await bypass.process_request(ctx1)
            await bypass.process_request(ctx2)

        mock_load.assert_awaited_once()
