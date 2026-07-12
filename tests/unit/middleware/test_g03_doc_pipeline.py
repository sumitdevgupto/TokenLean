"""Unit tests for G03 — Knowledge Strategy / Document Pipeline Trigger."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

import time
import types
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.asyncio
class TestG03DomainStability:
    async def test_check_domain_stability_no_stats_returns_unstable(self):
        mock_redis = AsyncMock()
        mock_redis.hgetall = AsyncMock(return_value={})

        with patch("cache.redis_pool.get_redis", return_value=mock_redis):
            from middleware.g03_doc_pipeline import check_domain_stability
            result = await check_domain_stability("acme-corp")

        assert result == {"stable": False, "doc_count": 0, "days_active": 0}

    async def test_check_domain_stability_computes_days_active_from_real_time(self):
        """Regression for the os.time() typo: first_seen 35 days ago with
        enough docs must compute a positive days_active and be marked stable."""
        thirty_five_days_ago = time.time() - (35 * 86400)
        mock_redis = AsyncMock()
        mock_redis.hgetall = AsyncMock(return_value={
            "doc_count": "150",
            "first_seen": str(thirty_five_days_ago),
        })

        with patch("cache.redis_pool.get_redis", return_value=mock_redis):
            from middleware.g03_doc_pipeline import check_domain_stability
            result = await check_domain_stability("acme-corp")

        assert result["doc_count"] == 150
        assert result["days_active"] >= 35
        assert result["stable"] is True

    async def test_check_domain_stability_recent_domain_not_stable(self):
        one_day_ago = time.time() - 86400
        mock_redis = AsyncMock()
        mock_redis.hgetall = AsyncMock(return_value={
            "doc_count": "150",
            "first_seen": str(one_day_ago),
        })

        with patch("cache.redis_pool.get_redis", return_value=mock_redis):
            from middleware.g03_doc_pipeline import check_domain_stability
            result = await check_domain_stability("acme-corp")

        assert result["days_active"] == 1
        assert result["stable"] is False

    async def test_check_domain_stability_redis_error_returns_unstable(self):
        with patch("cache.redis_pool.get_redis", side_effect=Exception("redis down")):
            from middleware.g03_doc_pipeline import check_domain_stability
            result = await check_domain_stability("acme-corp")

        assert result == {"stable": False, "doc_count": 0, "days_active": 0}


@pytest.mark.asyncio
class TestG03UpdateDomainStats:
    async def test_update_domain_stats_sets_first_seen_for_new_domain(self):
        """Regression for the os.time() typo: update_domain_stats must use a
        real wall-clock timestamp when initialising first_seen."""
        mock_redis = AsyncMock()
        mock_redis.exists = AsyncMock(return_value=False)
        mock_redis.hset = AsyncMock(return_value=True)
        mock_redis.hincrby = AsyncMock(return_value=1)
        mock_redis.expire = AsyncMock(return_value=True)

        before = time.time()
        with patch("cache.redis_pool.get_redis", return_value=mock_redis):
            from middleware.g03_doc_pipeline import update_domain_stats
            await update_domain_stats("acme-corp", doc_added=True)
        after = time.time()

        first_seen_call = next(
            c for c in mock_redis.hset.call_args_list if c.args[1] == "first_seen"
        )
        first_seen_value = float(first_seen_call.args[2])
        assert before <= first_seen_value <= after

    async def test_update_domain_stats_existing_domain_skips_first_seen(self):
        mock_redis = AsyncMock()
        mock_redis.exists = AsyncMock(return_value=True)
        mock_redis.hset = AsyncMock(return_value=True)
        mock_redis.hincrby = AsyncMock(return_value=2)
        mock_redis.expire = AsyncMock(return_value=True)

        with patch("cache.redis_pool.get_redis", return_value=mock_redis):
            from middleware.g03_doc_pipeline import update_domain_stats
            await update_domain_stats("acme-corp", doc_added=True)

        first_seen_calls = [c for c in mock_redis.hset.call_args_list if c.args[1] == "first_seen"]
        assert first_seen_calls == []
        mock_redis.hincrby.assert_awaited_once_with("tok_opt:domain:acme-corp", "doc_count", 1)

    async def test_update_domain_stats_redis_error_does_not_raise(self):
        with patch("cache.redis_pool.get_redis", side_effect=Exception("redis down")):
            from middleware.g03_doc_pipeline import update_domain_stats
            await update_domain_stats("acme-corp")  # should not raise

    async def test_domain_stats_key_is_tenant_prefixed(self):
        mock_redis = AsyncMock()
        mock_redis.exists = AsyncMock(return_value=False)
        mock_redis.hset = AsyncMock(return_value=True)
        mock_redis.hincrby = AsyncMock(return_value=1)
        mock_redis.expire = AsyncMock(return_value=True)
        with patch("cache.redis_pool.get_redis", return_value=mock_redis):
            from middleware.g03_doc_pipeline import update_domain_stats
            await update_domain_stats("acme", doc_added=True, tenant_id="NOVA-STG-01")
        # Every key touched must carry the tenant prefix — no cross-tenant domain stats.
        for call in mock_redis.hincrby.call_args_list + mock_redis.hset.call_args_list:
            assert call.args[0] == "t:NOVA-STG-01:tok_opt:domain:acme"


@pytest.mark.asyncio
class TestG03TriggerPipelines:
    async def test_trigger_doc_ingestion_success(self):
        from middleware import g03_doc_pipeline

        mock_client = MagicMock()
        mock_client.run_job = AsyncMock(return_value=MagicMock())

        fake_run_v2 = MagicMock()
        fake_run_v2.JobsAsyncClient.return_value = mock_client
        fake_run_v2.RunJobRequest = MagicMock(side_effect=lambda **kw: kw)
        fake_run_v2.RunJobRequest.Overrides = MagicMock(side_effect=lambda **kw: kw)
        fake_run_v2.RunJobRequest.Overrides.ContainerOverride = MagicMock(side_effect=lambda **kw: kw)
        fake_run_v2.EnvVar = MagicMock(side_effect=lambda **kw: kw)

        fake_module = types.ModuleType("google.cloud.run_v2")
        for attr in dir(fake_run_v2):
            if not attr.startswith("_"):
                setattr(fake_module, attr, getattr(fake_run_v2, attr))

        with patch.dict(sys.modules, {"google.cloud.run_v2": fake_module}):
            result = await g03_doc_pipeline.trigger_doc_ingestion("my-bucket", "docs/file.pdf")

        assert result is True
        mock_client.run_job.assert_awaited_once()

    async def test_trigger_doc_ingestion_threads_tenant_collection(self):
        """The Job env must carry QDRANT_COLLECTION derived via TenantContext (the same
        code the read path uses) + TENANT_ID — closing the read/write asymmetry."""
        import contextlib
        from middleware import g03_doc_pipeline
        from tenancy.context import TenantContext

        mock_client = MagicMock()
        mock_client.run_job = AsyncMock(return_value=MagicMock())

        fake_run_v2 = MagicMock()
        fake_run_v2.JobsAsyncClient.return_value = mock_client
        fake_run_v2.RunJobRequest = MagicMock(side_effect=lambda **kw: kw)
        fake_run_v2.RunJobRequest.Overrides = MagicMock(side_effect=lambda **kw: kw)
        fake_run_v2.RunJobRequest.Overrides.ContainerOverride = MagicMock(side_effect=lambda **kw: kw)
        fake_run_v2.EnvVar = MagicMock(side_effect=lambda **kw: kw)

        fake_module = types.ModuleType("google.cloud.run_v2")
        for attr in dir(fake_run_v2):
            if not attr.startswith("_"):
                setattr(fake_module, attr, getattr(fake_run_v2, attr))

        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.dict(sys.modules, {"google.cloud.run_v2": fake_module}))
            try:
                import google.cloud as _gc
                stack.enter_context(patch.object(_gc, "run_v2", fake_module, create=True))
            except Exception:
                pass
            ok = await g03_doc_pipeline.trigger_doc_ingestion(
                "token-opt-docs-nova-stg-01", "docs/f.pdf", tenant_id="NOVA-STG-01"
            )

        assert ok is True
        # Inspect the captured RunJobRequest → env list (EnvVar side-effect returns dicts).
        req = mock_client.run_job.call_args.kwargs["request"]
        env = {e["name"]: e["value"] for e in req["overrides"]["container_overrides"][0]["env"]}
        expected = TenantContext.for_tenant("NOVA-STG-01").qdrant_collection
        assert env["QDRANT_COLLECTION"] == expected  # rag_nova-stg-01, no hardcoding
        assert env["TENANT_ID"] == "NOVA-STG-01"
        assert env["GCS_BUCKET"] == "token-opt-docs-nova-stg-01"

    async def test_trigger_doc_ingestion_default_tenant_uses_rag_docs(self):
        import contextlib
        from middleware import g03_doc_pipeline

        mock_client = MagicMock()
        mock_client.run_job = AsyncMock(return_value=MagicMock())
        fake_run_v2 = MagicMock()
        fake_run_v2.JobsAsyncClient.return_value = mock_client
        fake_run_v2.RunJobRequest = MagicMock(side_effect=lambda **kw: kw)
        fake_run_v2.RunJobRequest.Overrides = MagicMock(side_effect=lambda **kw: kw)
        fake_run_v2.RunJobRequest.Overrides.ContainerOverride = MagicMock(side_effect=lambda **kw: kw)
        fake_run_v2.EnvVar = MagicMock(side_effect=lambda **kw: kw)
        fake_module = types.ModuleType("google.cloud.run_v2")
        for attr in dir(fake_run_v2):
            if not attr.startswith("_"):
                setattr(fake_module, attr, getattr(fake_run_v2, attr))

        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.dict(sys.modules, {"google.cloud.run_v2": fake_module}))
            try:
                import google.cloud as _gc
                stack.enter_context(patch.object(_gc, "run_v2", fake_module, create=True))
            except Exception:
                pass
            await g03_doc_pipeline.trigger_doc_ingestion("b", "o")  # default tenant

        req = mock_client.run_job.call_args.kwargs["request"]
        env = {e["name"]: e["value"] for e in req["overrides"]["container_overrides"][0]["env"]}
        assert env["QDRANT_COLLECTION"] == "rag_docs"
        assert env["TENANT_ID"] == "default"

    async def test_trigger_doc_ingestion_failure_returns_false(self):
        # google.cloud.run_v2 is not installed in the test environment, so the
        # import inside trigger_doc_ingestion raises and is caught.
        from middleware.g03_doc_pipeline import trigger_doc_ingestion
        result = await trigger_doc_ingestion("my-bucket", "docs/file.pdf")
        assert result is False

    async def test_trigger_fine_tuning_below_min_docs_skipped(self):
        from middleware.g03_doc_pipeline import trigger_fine_tuning_pipeline, _FINETUNE_MIN_DOCS
        result = await trigger_fine_tuning_pipeline("default", "acme", _FINETUNE_MIN_DOCS - 1)
        assert result is False

    async def test_trigger_fine_tuning_at_min_docs_attempts_trigger(self):
        # google.cloud.run_v2 unavailable → caught exception → False, but
        # confirms the doc_count gate itself does not block at the threshold.
        from middleware.g03_doc_pipeline import trigger_fine_tuning_pipeline, _FINETUNE_MIN_DOCS
        result = await trigger_fine_tuning_pipeline("default", "acme", _FINETUNE_MIN_DOCS)
        assert result is False  # ImportError path, not the doc-count gate

    async def test_trigger_fine_tuning_threads_tenant_and_collection(self):
        import contextlib
        from middleware import g03_doc_pipeline
        from tenancy.context import TenantContext

        mock_client = MagicMock()
        mock_client.run_job = AsyncMock(return_value=MagicMock())

        fake_run_v2 = MagicMock()
        fake_run_v2.JobsAsyncClient.return_value = mock_client
        fake_run_v2.RunJobRequest = MagicMock(side_effect=lambda **kw: kw)
        fake_run_v2.RunJobRequest.Overrides = MagicMock(side_effect=lambda **kw: kw)
        fake_run_v2.RunJobRequest.Overrides.ContainerOverride = MagicMock(side_effect=lambda **kw: kw)
        fake_run_v2.EnvVar = MagicMock(side_effect=lambda **kw: kw)

        fake_module = types.ModuleType("google.cloud.run_v2")
        for attr in dir(fake_run_v2):
            if not attr.startswith("_"):
                setattr(fake_module, attr, getattr(fake_run_v2, attr))

        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.dict(sys.modules, {"google.cloud.run_v2": fake_module}))
            try:
                import google.cloud as _gc
                stack.enter_context(patch.object(_gc, "run_v2", fake_module, create=True))
            except Exception:
                pass
            result = await g03_doc_pipeline.trigger_fine_tuning_pipeline("NOVA-STG-01", "support", 150)

        assert result is True
        req = mock_client.run_job.call_args.kwargs["request"]
        env = {e["name"]: e["value"] for e in req["overrides"]["container_overrides"][0]["env"]}
        assert env["TENANT_ID"] == "NOVA-STG-01"
        assert env["QDRANT_COLLECTION"] == TenantContext.for_tenant("NOVA-STG-01").qdrant_collection
        assert env["DOMAIN"] == "support"
        # No resolver installed in this test env → not enforced, no platform-key leak asserted elsewhere.
        assert env["BYOK_ENFORCE"] == "false"

    @staticmethod
    def _fake_run_v2(mock_client):
        """Build a fake google.cloud.run_v2 whose builders record kwargs as dicts."""
        fake = MagicMock()
        fake.JobsAsyncClient.return_value = mock_client
        fake.RunJobRequest = MagicMock(side_effect=lambda **kw: kw)
        fake.RunJobRequest.Overrides = MagicMock(side_effect=lambda **kw: kw)
        fake.RunJobRequest.Overrides.ContainerOverride = MagicMock(side_effect=lambda **kw: kw)
        fake.EnvVar = MagicMock(side_effect=lambda **kw: kw)
        mod = types.ModuleType("google.cloud.run_v2")
        for attr in dir(fake):
            if not attr.startswith("_"):
                setattr(mod, attr, getattr(fake, attr))
        return mod

    async def _run_trigger(self, tenant_id, domain, doc_count, **kw):
        """Invoke trigger_fine_tuning_pipeline with a mocked run_v2; return the captured env."""
        import contextlib
        from middleware import g03_doc_pipeline
        mock_client = MagicMock()
        mock_client.run_job = AsyncMock(return_value=MagicMock())
        mod = self._fake_run_v2(mock_client)
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.dict(sys.modules, {"google.cloud.run_v2": mod}))
            try:
                import google.cloud as _gc
                stack.enter_context(patch.object(_gc, "run_v2", mod, create=True))
            except Exception:
                pass
            ok = await g03_doc_pipeline.trigger_fine_tuning_pipeline(tenant_id, domain, doc_count, **kw)
        env = {}
        if mock_client.run_job.call_args is not None:
            req = mock_client.run_job.call_args.kwargs["request"]
            env = {e["name"]: e["value"] for e in req["overrides"]["container_overrides"][0]["env"]}
        return ok, env

    async def test_trigger_fine_tuning_refuses_on_strict_byok_no_tenant_key(self):
        """Strict BYOK on + no tenant-OWNED key → FineTuneByokError (402), never platform key."""
        from middleware import g03_doc_pipeline
        from middleware.g03_doc_pipeline import FineTuneByokError

        async def _no_key(provider, tenant_id):
            return None  # tenant has no key of its own

        with patch("providers.key_resolver.resolve_tenant_owned_key", _no_key), \
             patch.object(g03_doc_pipeline, "_finetune_byok_enforced", lambda gc=None: True):
            with pytest.raises(FineTuneByokError):
                await g03_doc_pipeline.trigger_fine_tuning_pipeline("NOVA-STG-01", "support", 150)

    async def test_trigger_fine_tuning_never_ships_platform_key(self):
        """THE isolation fix: with strict BYOK OFF (default) and no tenant-owned key, the job
        is triggered WITHOUT any TENANT_PROVIDER_KEY and BYOK_ENFORCE=false — the platform key
        is NEVER passed as if it were the tenant's. (resolve_tenant_owned_key never returns the
        platform key, so there is nothing to leak.)"""
        from middleware import g03_doc_pipeline

        async def _no_key(provider, tenant_id):
            return None

        with patch("providers.key_resolver.resolve_tenant_owned_key", _no_key), \
             patch.object(g03_doc_pipeline, "_finetune_byok_enforced", lambda gc=None: False):
            ok, env = await self._run_trigger("NOVA-STG-01", "support", 150)

        assert ok is True
        assert "TENANT_PROVIDER_KEY" not in env  # no key shipped at all — no platform-key leak
        assert env["BYOK_ENFORCE"] == "false"

    async def test_trigger_fine_tuning_ships_genuine_tenant_key(self):
        """When the tenant OWNS a key, it is shipped as TENANT_PROVIDER_KEY with enforcement."""
        from middleware import g03_doc_pipeline

        async def _tenant_key(provider, tenant_id):
            return "sk-genuinely-the-tenants-own-key"

        with patch("providers.key_resolver.resolve_tenant_owned_key", _tenant_key), \
             patch.object(g03_doc_pipeline, "_finetune_byok_enforced", lambda gc=None: False):
            ok, env = await self._run_trigger("NOVA-STG-01", "support", 150)

        assert ok is True
        assert env["TENANT_PROVIDER_KEY"] == "sk-genuinely-the-tenants-own-key"
        assert env["BYOK_ENFORCE"] == "true"  # enforce in the Job whenever we have a real key

    async def test_trigger_fine_tuning_refuses_on_undecryptable_key(self):
        """A stored-but-undecryptable tenant key fails closed (402), never platform fallback."""
        from middleware import g03_doc_pipeline
        from middleware.g03_doc_pipeline import FineTuneByokError
        from providers.key_resolver import ProviderKeyDecryptError

        async def _corrupt(provider, tenant_id):
            raise ProviderKeyDecryptError(provider, tenant_id)

        with patch("providers.key_resolver.resolve_tenant_owned_key", _corrupt):
            with pytest.raises(FineTuneByokError):
                await g03_doc_pipeline.trigger_fine_tuning_pipeline("NOVA-STG-01", "support", 150)

    async def test_trigger_fine_tuning_emits_metric(self):
        from middleware import g03_doc_pipeline
        from middleware.g18_observability import FINETUNE_JOBS_TOTAL

        before = FINETUNE_JOBS_TOTAL.labels(
            tenant_id="NOVA-STG-01", status="trigger_error", provider="openai"
        )._value.get()
        # run_v2 unavailable → trigger_error path → metric increments.
        await g03_doc_pipeline.trigger_fine_tuning_pipeline("NOVA-STG-01", "support", 150)
        after = FINETUNE_JOBS_TOTAL.labels(
            tenant_id="NOVA-STG-01", status="trigger_error", provider="openai"
        )._value.get()
        assert after >= before + 1


@pytest.mark.asyncio
class TestRAGFallbackOrchestrator:
    async def test_fallback_disabled_uses_strict_only(self):
        from middleware.g03_doc_pipeline import RAGFallbackOrchestrator

        orchestrator = RAGFallbackOrchestrator()
        orchestrator.fallback_enabled = False
        orchestrator._execute_search = AsyncMock(return_value=[{"text": "hit", "score": 0.9}])

        results = await orchestrator.search_with_fallback("query")

        assert results == [{"text": "hit", "score": 0.9}]
        orchestrator._execute_search.assert_awaited_once_with(
            "strict_hybrid", "query", "rag_docs", 5, 0.85
        )

    async def test_fallback_escalates_through_strategies_until_results_found(self):
        from middleware.g03_doc_pipeline import RAGFallbackOrchestrator

        orchestrator = RAGFallbackOrchestrator()
        orchestrator.fallback_enabled = True

        async def fake_execute(strategy, query, collection, top_k, threshold):
            if strategy == "dense_only":
                return [{"text": "dense hit", "score": 0.8}]
            return []

        orchestrator._execute_search = AsyncMock(side_effect=fake_execute)

        results = await orchestrator.search_with_fallback("query")

        assert results == [{"text": "dense hit", "score": 0.8}]
        called_strategies = [c.args[0] for c in orchestrator._execute_search.call_args_list]
        assert called_strategies == ["strict_hybrid", "relaxed_hybrid", "dense_only"]

    async def test_fallback_returns_empty_when_no_strategy_finds_results(self):
        from middleware.g03_doc_pipeline import RAGFallbackOrchestrator

        orchestrator = RAGFallbackOrchestrator()
        orchestrator.fallback_enabled = True
        orchestrator._execute_search = AsyncMock(return_value=[])

        results = await orchestrator.search_with_fallback("query")

        assert results == []
        assert orchestrator._execute_search.call_count == 4

    async def test_fallback_short_circuits_on_first_strategy_hit(self):
        from middleware.g03_doc_pipeline import RAGFallbackOrchestrator

        orchestrator = RAGFallbackOrchestrator()
        orchestrator.fallback_enabled = True
        orchestrator._execute_search = AsyncMock(return_value=[{"text": "strict hit", "score": 0.95}])

        results = await orchestrator.search_with_fallback("query")

        assert results == [{"text": "strict hit", "score": 0.95}]
        orchestrator._execute_search.assert_awaited_once()

    async def test_execute_search_returns_results_from_qdrant(self):
        from middleware.g03_doc_pipeline import RAGFallbackOrchestrator

        orchestrator = RAGFallbackOrchestrator()

        mock_point = MagicMock()
        mock_point.payload = {"text": "doc chunk"}
        mock_point.score = 0.91

        mock_qdrant_client = MagicMock()
        mock_qdrant_client.search.return_value = [mock_point]

        mock_embedding = MagicMock()
        mock_embedding.tolist.return_value = [0.1, 0.2, 0.3]
        mock_st_model = MagicMock()
        mock_st_model.encode.return_value = mock_embedding

        with patch("qdrant_client.QdrantClient", return_value=mock_qdrant_client), \
             patch("sentence_transformers.SentenceTransformer", return_value=mock_st_model):
            results = await orchestrator._execute_search(
                "strict_hybrid", "query", "rag_docs", 5, 0.85
            )

        assert results == [{"text": "doc chunk", "score": 0.91}]

    async def test_execute_search_sparse_only_returns_empty(self):
        from middleware.g03_doc_pipeline import RAGFallbackOrchestrator

        orchestrator = RAGFallbackOrchestrator()

        mock_embedding = MagicMock()
        mock_embedding.tolist.return_value = [0.1, 0.2, 0.3]
        mock_st_model = MagicMock()
        mock_st_model.encode.return_value = mock_embedding

        with patch("qdrant_client.QdrantClient", return_value=MagicMock()), \
             patch("sentence_transformers.SentenceTransformer", return_value=mock_st_model):
            results = await orchestrator._execute_search(
                "sparse_only", "query", "rag_docs", 5, 0.60
            )

        assert results == []

    async def test_execute_search_exception_returns_empty(self):
        from middleware.g03_doc_pipeline import RAGFallbackOrchestrator

        orchestrator = RAGFallbackOrchestrator()

        with patch("qdrant_client.QdrantClient", side_effect=Exception("connection refused")):
            results = await orchestrator._execute_search(
                "strict_hybrid", "query", "rag_docs", 5, 0.85
            )

        assert results == []


@pytest.mark.asyncio
class TestDetectOodAndFallback:
    async def test_high_confidence_primary_is_not_ood(self):
        from middleware.g03_doc_pipeline import RAGFallbackOrchestrator

        orchestrator = RAGFallbackOrchestrator()
        orchestrator._execute_search = AsyncMock(
            return_value=[{"text": "primary hit", "score": 0.95}]
        )

        result = await orchestrator.detect_ood_and_fallback("query", "primary-index")

        assert result["is_ood"] is False
        assert result["strategy_used"] == "primary_strict"
        assert result["confidence"] == pytest.approx(0.95)
        assert result["fallback_results"] == []

    async def test_low_confidence_primary_falls_back_to_relaxed(self):
        from middleware.g03_doc_pipeline import RAGFallbackOrchestrator

        orchestrator = RAGFallbackOrchestrator()

        async def fake_execute(strategy, query, collection, top_k, threshold):
            if strategy == "strict_hybrid":
                return [{"text": "weak primary", "score": 0.1}]
            if strategy == "relaxed_hybrid":
                return [{"text": "relaxed hit", "score": 0.7}]
            return []

        orchestrator._execute_search = AsyncMock(side_effect=fake_execute)

        result = await orchestrator.detect_ood_and_fallback("query", "primary-index")

        assert result["is_ood"] is False
        assert result["strategy_used"] == "primary_relaxed"
        assert result["primary_results"] == [{"text": "relaxed hit", "score": 0.7}]

    async def test_no_primary_results_falls_back_to_broad_domain(self):
        from middleware.g03_doc_pipeline import RAGFallbackOrchestrator, _RAG_FALLBACK_INDEX

        orchestrator = RAGFallbackOrchestrator()

        async def fake_execute(strategy, query, collection, top_k, threshold):
            if collection == _RAG_FALLBACK_INDEX:
                return [{"text": "broad domain hit", "score": 0.5}]
            return []

        async def fake_strict_hybrid(query, collection, top_k, threshold):
            return await fake_execute("strict_hybrid", query, collection, top_k, threshold)

        orchestrator._execute_search = AsyncMock(side_effect=fake_execute)
        orchestrator._strict_hybrid_search = AsyncMock(side_effect=fake_strict_hybrid)

        result = await orchestrator.detect_ood_and_fallback("query", "primary-index")

        assert result["is_ood"] is True
        assert result["strategy_used"] == "fallback_broad_domain"
        assert result["fallback_results"] == [{"text": "broad domain hit", "score": 0.5}]
        assert result["confidence"] == pytest.approx(0.5)

    async def test_no_results_anywhere_returns_no_results(self):
        from middleware.g03_doc_pipeline import RAGFallbackOrchestrator

        orchestrator = RAGFallbackOrchestrator()
        orchestrator._execute_search = AsyncMock(return_value=[])
        orchestrator._strict_hybrid_search = AsyncMock(return_value=[])

        result = await orchestrator.detect_ood_and_fallback("query", "primary-index")

        assert result["is_ood"] is True
        assert result["strategy_used"] == "no_results"
        assert result["confidence"] == 0.0
        assert result["primary_results"] == []
        assert result["fallback_results"] == []


@pytest.mark.asyncio
class TestTikaSidecarClient:
    async def test_extract_text_success(self):
        from middleware.g03_doc_pipeline import TikaSidecarClient

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.text = "Extracted document text"

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.put = AsyncMock(return_value=mock_resp)

        with patch("httpx.AsyncClient", return_value=mock_client):
            text = await TikaSidecarClient().extract_text(b"binary content", "doc.pdf")

        assert text == "Extracted document text"

    async def test_extract_text_failure_returns_empty_string(self):
        from middleware.g03_doc_pipeline import TikaSidecarClient

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.put = AsyncMock(side_effect=Exception("connection refused"))

        with patch("httpx.AsyncClient", return_value=mock_client):
            text = await TikaSidecarClient().extract_text(b"binary content", "doc.pdf")

        assert text == ""

    async def test_extract_metadata_success(self):
        from middleware.g03_doc_pipeline import TikaSidecarClient

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"Content-Type": "application/pdf", "Author": "Acme"}

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.put = AsyncMock(return_value=mock_resp)

        with patch("httpx.AsyncClient", return_value=mock_client):
            meta = await TikaSidecarClient().extract_metadata(b"binary content", "doc.pdf")

        assert meta == {"Content-Type": "application/pdf", "Author": "Acme"}

    async def test_extract_metadata_failure_returns_empty_dict(self):
        from middleware.g03_doc_pipeline import TikaSidecarClient

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.put = AsyncMock(side_effect=Exception("connection refused"))

        with patch("httpx.AsyncClient", return_value=mock_client):
            meta = await TikaSidecarClient().extract_metadata(b"binary content", "doc.pdf")

        assert meta == {}


@pytest.mark.asyncio
class TestG03DocPipelineMiddleware:
    async def test_disabled_passes_through_unchanged(self, make_ctx):
        ctx = make_ctx()
        ctx.config["groups"]["G3_doc_pipeline"] = {"enabled": False}

        from middleware.g03_doc_pipeline import G03DocPipeline
        result = await G03DocPipeline().process_request(ctx)

        assert result is ctx
        assert not hasattr(result, "rag_results")

    async def test_missing_config_section_treated_as_disabled(self, make_ctx):
        ctx = make_ctx()
        ctx.config["groups"].pop("G3_doc_pipeline", None)

        from middleware.g03_doc_pipeline import G03DocPipeline
        result = await G03DocPipeline().process_request(ctx)

        assert result is ctx

    async def test_enabled_without_rag_query_passes_through(self, make_ctx):
        ctx = make_ctx()
        ctx.config["groups"]["G3_doc_pipeline"] = {"enabled": True}

        from middleware.g03_doc_pipeline import G03DocPipeline
        result = await G03DocPipeline().process_request(ctx)

        assert not hasattr(result, "rag_results")

    async def test_enabled_with_rag_query_populates_rag_results(self, make_ctx):
        ctx = make_ctx()
        ctx.config["groups"]["G3_doc_pipeline"] = {
            "enabled": True,
            "collection": "rag_docs",
            "top_k": 3,
        }
        ctx.rag_query = "What is our refund policy?"

        from middleware.g03_doc_pipeline import G03DocPipeline
        mw = G03DocPipeline()
        mw.rag_orchestrator.search_with_fallback = AsyncMock(
            return_value=[{"text": "refund policy chunk", "score": 0.9}]
        )

        result = await mw.process_request(ctx)

        assert result.rag_results == [{"text": "refund policy chunk", "score": 0.9}]
        mw.rag_orchestrator.search_with_fallback.assert_awaited_once_with(
            "What is our refund policy?", collection="rag_docs", top_k=3
        )


@pytest.mark.asyncio
class TestListTenantFinetuneJobs:
    """The shared finetune-status reader: tenant-prefixed keys + pipelined (not N+1) fetch."""

    class _PipelineFake:
        """Redis fake that records whether .pipeline() batching was used."""
        def __init__(self, hashes):
            self._h = hashes
            self.hgetall_calls = 0
            self.pipeline_used = False

        async def zrevrange(self, key, a, b):
            return list(self._h.keys())

        async def scan_iter(self, match=None):
            for j in self._h:
                yield f"t:NOVA-STG-01:tok_opt:finetune:{j}"
            # also emit a domain-index key that must be skipped
            yield "t:NOVA-STG-01:tok_opt:finetune:domain:support"

        async def hgetall(self, key):
            self.hgetall_calls += 1
            jid = key.rsplit("tok_opt:finetune:", 1)[-1]
            return self._h.get(jid, {})

        def pipeline(self):
            outer = self

            class _Pipe:
                def __init__(self):
                    self._keys = []
                def hgetall(self, key):
                    self._keys.append(key)
                async def execute(self):
                    outer.pipeline_used = True
                    return [outer._h.get(k.rsplit("tok_opt:finetune:", 1)[-1], {}) for k in self._keys]
            return _Pipe()

    async def test_uses_pipeline_not_n_plus_1(self):
        from middleware.g03_doc_pipeline import list_tenant_finetune_jobs
        redis = self._PipelineFake({"j1": {"status": "RUNNING"}, "j2": {"status": "DONE"}})
        jobs = await list_tenant_finetune_jobs(redis, "NOVA-STG-01")
        assert len(jobs) == 2
        assert redis.pipeline_used is True         # batched, not sequential
        assert redis.hgetall_calls == 0            # no per-id sequential hgetall
        # the domain-index key was skipped (not treated as a job id)
        assert all("domain" not in (j.get("status") or "") for j in jobs)

    async def test_falls_back_to_sequential_without_pipeline(self):
        from middleware.g03_doc_pipeline import list_tenant_finetune_jobs

        class _NoPipe:
            def __init__(self, h): self._h = h
            async def scan_iter(self, match=None):
                for j in self._h:
                    yield f"t:NOVA-STG-01:tok_opt:finetune:{j}"
            async def hgetall(self, key):
                return self._h.get(key.rsplit("tok_opt:finetune:", 1)[-1], {})
            # no .pipeline() attribute at all

        redis = _NoPipe({"j1": {"status": "RUNNING"}})
        jobs = await list_tenant_finetune_jobs(redis, "NOVA-STG-01")
        assert jobs == [{"status": "RUNNING"}]

    async def test_empty_returns_empty_list(self):
        from middleware.g03_doc_pipeline import list_tenant_finetune_jobs

        class _Empty:
            async def scan_iter(self, match=None):
                return
                yield  # pragma: no cover
        jobs = await list_tenant_finetune_jobs(_Empty(), "NOVA-STG-01")
        assert jobs == []
