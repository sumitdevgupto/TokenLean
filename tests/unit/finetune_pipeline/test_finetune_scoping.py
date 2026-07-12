"""Unit tests for finetune-pipeline tenant scoping + BYOK guard (data-safety).

Covers the isolation guarantees added so a tenant's documents can never enter another
tenant's training corpus:
  1. reads the tenant's real collection (rag_<tenant>), not the phantom docs-<domain>
  2. scrolls with a tenant_id filter (defense-in-depth)
  3. exports under finetune-training/<tenant>/<domain>/
  4. tenant-prefixes Redis job keys
  5. BYOK fail-closed: refuses (exit 2) under strict-BYOK with no tenant key
"""
import importlib.util
import sys
import types
from pathlib import Path

import pytest

_PIPELINE_PATH = (
    Path(__file__).resolve().parents[3] / "src" / "finetune-pipeline" / "pipeline.py"
)


def _load(monkeypatch, env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    spec = importlib.util.spec_from_file_location("_ftpipe_under_test", _PIPELINE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_redis_prefix_matches_tenant_context():
    # Load with default env; the helpers are pure.
    spec = importlib.util.spec_from_file_location("_ftpipe_pure", _PIPELINE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod._redis_prefix("NOVA-STG-01") == "t:NOVA-STG-01:"
    assert mod._redis_prefix("default") == ""
    assert mod._sanitise_tenant_id("") == "default"


def test_builder_reads_tenant_collection_with_filter(monkeypatch):
    mod = _load(monkeypatch, {"TENANT_ID": "NOVA-STG-01", "QDRANT_COLLECTION": "rag_NOVA-STG-01"})

    captured = {}

    class _FakeClient:
        def scroll(self, collection_name, scroll_filter=None, limit=100, offset=None, with_payload=True):
            captured["collection"] = collection_name
            captured["filter"] = scroll_filter
            return ([], None)

    qmod = types.ModuleType("qdrant_client")
    qmod.QdrantClient = lambda *a, **k: _FakeClient()
    models = types.ModuleType("qdrant_client.models")
    models.Filter = lambda must: {"must": must}
    models.FieldCondition = lambda key, match: {"key": key, "match": match}
    models.MatchValue = lambda value: {"value": value}
    monkeypatch.setitem(sys.modules, "qdrant_client", qmod)
    monkeypatch.setitem(sys.modules, "qdrant_client.models", models)

    builder = mod.TrainingDataBuilder("http://q", "support", "NOVA-STG-01", "rag_NOVA-STG-01")
    builder.fetch_documents(min_chunks=1)

    assert captured["collection"] == "rag_NOVA-STG-01"  # real collection, not docs-support
    assert captured["filter"] == {"must": [{"key": "tenant_id", "match": {"value": "NOVA-STG-01"}}]}


def test_default_tenant_no_filter(monkeypatch):
    mod = _load(monkeypatch, {"TENANT_ID": "default", "QDRANT_COLLECTION": "rag_docs"})
    captured = {}

    class _FakeClient:
        def scroll(self, collection_name, scroll_filter=None, **k):
            captured["collection"] = collection_name
            captured["filter"] = scroll_filter
            return ([], None)

    qmod = types.ModuleType("qdrant_client")
    qmod.QdrantClient = lambda *a, **k: _FakeClient()
    monkeypatch.setitem(sys.modules, "qdrant_client", qmod)

    builder = mod.TrainingDataBuilder("http://q", "support", "default", "rag_docs")
    builder.fetch_documents(min_chunks=1)
    assert captured["collection"] == "rag_docs"
    assert captured["filter"] is None  # default tenant → collection-only scoping


def test_gcs_export_path_is_tenant_domain_nested(monkeypatch):
    mod = _load(monkeypatch, {"TENANT_ID": "NOVA-STG-01"})
    captured = {}

    class _FakeBlob:
        def upload_from_filename(self, p):
            pass

    class _FakeBucket:
        def blob(self, dest):
            captured["dest"] = dest
            return _FakeBlob()

    class _FakeClient:
        def bucket(self, name):
            return _FakeBucket()

    storage_mod = types.ModuleType("google.cloud.storage")
    storage_mod.Client = lambda *a, **k: _FakeClient()
    import contextlib
    from unittest.mock import patch
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.dict(sys.modules, {"google.cloud.storage": storage_mod}))
        try:
            import google.cloud as _gc
            stack.enter_context(patch.object(_gc, "storage", storage_mod, create=True))
        except Exception:
            pass
        tuner = mod.VertexAIFineTuner("proj", "us-central1", "bkt")
        uri = tuner.upload_training_data("/tmp/x.jsonl", "support", "NOVA-STG-01")
    assert captured["dest"].startswith("finetune-training/NOVA-STG-01/support/")
    assert uri.startswith("gs://bkt/finetune-training/NOVA-STG-01/support/")


def test_track_job_keys_are_tenant_prefixed(monkeypatch):
    mod = _load(monkeypatch, {"TENANT_ID": "NOVA-STG-01", "DOMAIN": "support"})
    keys = {"hset": None, "zadd": None}

    class _FakeRedis:
        def hset(self, key, mapping=None):
            keys["hset"] = key
        def zadd(self, key, mapping):
            keys["zadd"] = key

    redis_mod = types.ModuleType("redis")
    redis_mod.from_url = lambda *a, **k: _FakeRedis()
    monkeypatch.setitem(sys.modules, "redis", redis_mod)

    pipe = mod.FineTunePipeline()
    pipe._track_job("job-123", "RUNNING", {})
    assert keys["hset"] == "t:NOVA-STG-01:tok_opt:finetune:job-123"
    assert keys["zadd"] == "t:NOVA-STG-01:tok_opt:finetune:domain:support"


def test_byok_guard_refuses_without_tenant_key(monkeypatch):
    mod = _load(monkeypatch, {
        "TENANT_ID": "NOVA-STG-01", "BYOK_ENFORCE": "true", "TENANT_PROVIDER_KEY": "",
    })
    pipe = mod.FineTunePipeline()
    with pytest.raises(SystemExit) as exc:
        pipe._resolve_training_key()
    assert exc.value.code == 2  # fail-closed, never the platform key


def test_byok_uses_tenant_key_when_present(monkeypatch):
    mod = _load(monkeypatch, {
        "TENANT_ID": "NOVA-STG-01", "BYOK_ENFORCE": "true", "TENANT_PROVIDER_KEY": "sk-tenant",
    })
    pipe = mod.FineTunePipeline()
    assert pipe._resolve_training_key() == "sk-tenant"


def test_default_tenant_uses_platform_key(monkeypatch):
    mod = _load(monkeypatch, {
        "TENANT_ID": "default", "BYOK_ENFORCE": "false",
        "TENANT_PROVIDER_KEY": "", "OPENAI_API_KEY": "sk-platform",
    })
    pipe = mod.FineTunePipeline()
    assert pipe._resolve_training_key() == "sk-platform"  # single-tenant backward-compat
