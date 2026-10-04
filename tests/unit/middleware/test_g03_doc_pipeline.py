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


# ── A tenant's own key never rides on the Job execution ──────────────────────────────
# The trigger passed the key as a plain env override, and an execution keeps its overrides:
# anyone who could view the Job's executions could read the key. Now the key goes into one
# Secret Manager secret, a version per run, and the Job is given only the version's name.
from datetime import datetime, timezone  # noqa: E402
from types import SimpleNamespace  # noqa: E402

_KEY = "sk-genuinely-the-tenants-own-key"
_KEY_SECRET = "projects/tl-test/secrets/finetune-tenant-key"


class FakeSecretManager:
    """Stands in for Secret Manager's async client: the versions of the key secret, each with
    its data, its creation time and its state."""

    def __init__(self, fail_add=False, fail_list=False, existing_ages=()):
        self.fail_add, self.fail_list = fail_add, fail_list
        self.versions = {}
        for age in existing_ages:
            self._add(_KEY_SECRET, b"a key from an earlier run", time.time() - age)

    def _add(self, parent, data, created):
        name = f"{parent}/versions/{len(self.versions) + 1}"
        self.versions[name] = {"data": data, "created": created, "state": "ENABLED"}
        return name

    async def add_secret_version(self, request):
        if self.fail_add:
            raise PermissionError("secretmanager.versions.add denied")
        return SimpleNamespace(name=self._add(request["parent"], request["payload"]["data"],
                                              time.time()))

    async def list_secret_versions(self, request):
        if self.fail_list:
            raise PermissionError("secretmanager.versions.list denied")
        assert request["filter"] == "state:ENABLED"
        found = [SimpleNamespace(name=name, create_time=datetime.fromtimestamp(v["created"], timezone.utc))
                 for name, v in self.versions.items()
                 if v["state"] == "ENABLED" and name.startswith(request["parent"] + "/")]

        async def pages():
            for version in found:
                yield version
        return pages()

    async def destroy_secret_version(self, request):
        self.versions[request["name"]]["state"] = "DESTROYED"


async def _trigger(secrets, key=_KEY, run_job_error=None):
    """Run the fine-tune trigger for a tenant whose own key is ``key`` (None: no key of its
    own), with Cloud Run and Secret Manager faked. Returns (ok, the RunJob request or None)."""
    import contextlib
    from middleware import g03_doc_pipeline

    mock_client = MagicMock()
    mock_client.run_job = AsyncMock(side_effect=run_job_error)
    mod = TestG03TriggerPipelines._fake_run_v2(mock_client)

    async def _tenant_key(provider, tenant_id):
        return key

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.dict(sys.modules, {"google.cloud.run_v2": mod}))
        import google.cloud as _gc
        stack.enter_context(patch.object(_gc, "run_v2", mod, create=True))
        stack.enter_context(patch("google.cloud.secretmanager.SecretManagerServiceAsyncClient",
                                  lambda *a, **k: secrets))
        stack.enter_context(patch("providers.key_resolver.resolve_tenant_owned_key", _tenant_key))
        stack.enter_context(patch.object(g03_doc_pipeline, "_finetune_byok_enforced",
                                         lambda gc=None: False))
        stack.enter_context(patch.object(g03_doc_pipeline, "_GCP_PROJECT", "tl-test"))
        ok = await g03_doc_pipeline.trigger_fine_tuning_pipeline("NOVA-STG-01", "support", 150)
    call = mock_client.run_job.call_args
    return ok, (call.kwargs["request"] if call else None)


def _env(request):
    return {e["name"]: e["value"] for e in request["overrides"]["container_overrides"][0]["env"]}


@pytest.mark.asyncio
class TestTheTenantKeyNeverRidesOnTheExecution:

    async def test_the_job_is_given_only_the_name_of_a_version_holding_the_key(self):
        secrets = FakeSecretManager()
        ok, request = await _trigger(secrets)
        assert ok is True
        holding = [name for name, v in secrets.versions.items() if v["data"] == _KEY.encode()]
        assert len(holding) == 1 and holding[0].startswith(_KEY_SECRET + "/versions/")
        env = _env(request)
        assert env["TENANT_PROVIDER_KEY_VERSION"] == holding[0]
        assert "TENANT_PROVIDER_KEY" not in env
        assert _KEY not in repr(request)
        assert env["BYOK_ENFORCE"] == "true"   # enforced in the Job whenever the key is the tenant's
        assert secrets.versions[holding[0]]["state"] == "ENABLED"   # the Job destroys it

    async def test_a_job_that_cannot_start_leaves_no_key_behind(self):
        secrets = FakeSecretManager()
        ok, _ = await _trigger(secrets, run_job_error=RuntimeError("run.jobs.run denied"))
        assert ok is False
        assert [v["state"] for v in secrets.versions.values()] == ["DESTROYED"]

    async def test_no_job_starts_when_the_key_cannot_be_stored(self):
        from middleware.g18_observability import FINETUNE_JOBS_TOTAL
        errors = FINETUNE_JOBS_TOTAL.labels(tenant_id="NOVA-STG-01", status="trigger_error",
                                            provider="openai")
        before = errors._value.get()
        ok, request = await _trigger(FakeSecretManager(fail_add=True))
        assert ok is False and request is None
        assert errors._value.get() == before + 1

    async def test_versions_left_from_a_day_ago_are_destroyed(self):
        secrets = FakeSecretManager(existing_ages=(2 * 86400, 3600))
        ok, _ = await _trigger(secrets)
        assert ok is True
        # two days old; an hour old (a run still going); this run's
        assert [v["state"] for v in secrets.versions.values()] == ["DESTROYED", "ENABLED", "ENABLED"]

    async def test_a_failed_clean_up_does_not_stop_the_run(self):
        secrets = FakeSecretManager(fail_list=True)
        ok, request = await _trigger(secrets)
        assert ok is True and "TENANT_PROVIDER_KEY_VERSION" in _env(request)

    async def test_without_a_key_of_its_own_secret_manager_is_untouched(self):
        secrets = FakeSecretManager()
        ok, request = await _trigger(secrets, key=None)
        assert ok is True
        assert secrets.versions == {}
        assert "TENANT_PROVIDER_KEY_VERSION" not in _env(request)


class _FakeQdrant:
    """qdrant_client.AsyncQdrantClient: finds one chunk at or below `hit_at`."""
    instances = []
    collections = {"rag_docs"}
    hit_at = 0.0
    fail = False

    def __init__(self, **kwargs):
        self.queries, self.closed = [], False
        _FakeQdrant.instances.append(self)

    async def collection_exists(self, name):
        return name in _FakeQdrant.collections

    async def query_points(self, collection_name, query, using, limit, score_threshold,
                           with_payload):
        if _FakeQdrant.fail:
            raise ConnectionError("qdrant down")
        self.queries.append((using, limit, round(score_threshold, 2)))
        point = type("P", (), {"payload": {"text": "chunk"}, "score": 0.8})()
        return type("R", (), {"points": [point] if score_threshold <= _FakeQdrant.hit_at else []})()

    async def close(self):
        self.closed = True


class _FakeModel:
    def __init__(self):
        self.threads = []

    def encode(self, text):
        import threading
        self.threads.append(threading.get_ident())
        return type("V", (), {"tolist": lambda self: [0.1, 0.2]})()


@pytest.fixture
def qdrant(monkeypatch):
    import qdrant_client
    import ml_models
    model = _FakeModel()
    _FakeQdrant.instances, _FakeQdrant.collections = [], {"rag_docs"}
    _FakeQdrant.hit_at, _FakeQdrant.fail = 0.0, False
    monkeypatch.setattr(qdrant_client, "AsyncQdrantClient", _FakeQdrant)
    monkeypatch.setattr(ml_models, "get_sentence_transformer", lambda name: model)
    return model


@pytest.mark.asyncio
class TestRAGFallbackOrchestrator:
    """One fallback search = one async Qdrant client (always closed) and one embedding,
    computed off the event loop, shared by every strategy."""

    def _orch(self, enabled=True):
        from middleware.g03_doc_pipeline import RAGFallbackOrchestrator
        orchestrator = RAGFallbackOrchestrator()
        orchestrator.fallback_enabled = enabled
        return orchestrator

    async def test_fallback_disabled_uses_strict_only(self, qdrant):
        assert await self._orch(enabled=False).search_with_fallback("query") == []
        assert _FakeQdrant.instances[0].queries == [("dense", 5, 0.85)]

    async def test_escalates_until_a_strategy_finds_results(self, qdrant):
        _FakeQdrant.hit_at = 0.70                                  # relaxed_hybrid's threshold
        results = await self._orch().search_with_fallback("query")
        assert results == [{"text": "chunk", "score": 0.8}]
        assert _FakeQdrant.instances[0].queries == [("dense", 5, 0.85), ("dense", 10, 0.7)]

    async def test_nothing_found_tries_each_searchable_strategy_once(self, qdrant):
        assert await self._orch().search_with_fallback("query") == []
        # sparse_only needs a sparse query vector this path does not build: no search.
        assert [q[2] for q in _FakeQdrant.instances[0].queries] == [0.85, 0.7, 0.75]

    async def test_the_query_is_embedded_once_and_off_the_event_loop(self, qdrant):
        import threading
        await self._orch().search_with_fallback("query")
        assert len(qdrant.threads) == 1
        assert qdrant.threads[0] != threading.get_ident()

    async def test_the_client_is_closed_even_when_a_search_fails(self, qdrant):
        _FakeQdrant.fail = True
        assert await self._orch().search_with_fallback("query") == []
        assert len(_FakeQdrant.instances) == 1 and _FakeQdrant.instances[0].closed

    async def test_a_missing_collection_is_not_searched(self, qdrant):
        assert await self._orch().search_with_fallback("query", collection="rag_nobody") == []
        client = _FakeQdrant.instances[0]
        assert client.queries == [] and client.closed and qdrant.threads == []

    async def test_the_g3_rag_fallback_settings_are_read(self, qdrant):
        cfg = {"rag_fallback": {"strategies": ["dense_only"], "similarity_threshold": 0.9,
                                "top_k": 3}}
        await self._orch().search_with_fallback("query", cfg=cfg)
        assert _FakeQdrant.instances[0].queries == [("dense", 3, 0.8)]

    async def test_rag_fallback_enabled_false_uses_strict_only(self, qdrant):
        await self._orch().search_with_fallback("query", cfg={"rag_fallback": {"enabled": False}})
        assert _FakeQdrant.instances[0].queries == [("dense", 5, 0.85)]


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
