"""Unit tests for doc-pipeline tenant scoping (data-safety).

Covers the three write-path guarantees added for per-tenant isolation:
  1. collision-free, deterministic, tenant-namespaced point ids (no more 32-bit md5)
  2. tenant_id stamped into every point payload
  3. an invalid QDRANT_COLLECTION is refused (sys.exit) before any Qdrant write
"""
import importlib.util
import os
import sys
import types
from pathlib import Path

import pytest

_PIPELINE_PATH = (
    Path(__file__).resolve().parents[3] / "src" / "doc-pipeline" / "pipeline.py"
)


def _load_pipeline(monkeypatch, tenant_id="NOVA-STG-01", collection="rag_nova-stg-01"):
    """Load src/doc-pipeline/pipeline.py fresh with the given tenant env."""
    monkeypatch.setenv("TENANT_ID", tenant_id)
    monkeypatch.setenv("QDRANT_COLLECTION", collection)
    spec = importlib.util.spec_from_file_location("_docpipeline_under_test", _PIPELINE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeQdrant:
    """Minimal QdrantClient stand-in capturing the upserted points."""

    def __init__(self, *a, **k):
        self.upserted = None

    def get_collections(self):
        return types.SimpleNamespace(collections=[])

    def get_collection(self, name):  # pragma: no cover - not hit when collection is new
        raise AssertionError("should not be called for a fresh collection")

    def create_collection(self, **k):
        return None

    def upsert(self, collection_name, points):
        self.collection = collection_name
        self.upserted = points


def _install_fake_qdrant(monkeypatch, fake):
    """Make `from qdrant_client import QdrantClient` (and models) resolve to fakes."""
    qmod = types.ModuleType("qdrant_client")
    qmod.QdrantClient = lambda *a, **k: fake
    models = types.ModuleType("qdrant_client.models")
    # PointStruct just records its kwargs so the test can inspect id/payload.
    models.PointStruct = lambda **kw: kw
    models.Distance = types.SimpleNamespace(COSINE="Cosine")
    models.VectorParams = lambda **kw: kw
    models.SparseVectorParams = lambda **kw: kw
    models.SparseIndexParams = lambda **kw: kw
    qmod.models = models
    monkeypatch.setitem(sys.modules, "qdrant_client", qmod)
    monkeypatch.setitem(sys.modules, "qdrant_client.models", models)


def _run_upsert(mod, fake, monkeypatch, tenant_source="gs://b/doc.pdf", n=2):
    _install_fake_qdrant(monkeypatch, fake)
    monkeypatch.setattr(mod, "_to_sparse_vector", lambda x: x, raising=False)
    chunks = [f"chunk {i}" for i in range(n)]
    dense = [[0.1, 0.2] for _ in range(n)]
    sparse = [object() for _ in range(n)]
    mod.upsert_to_qdrant(chunks, dense, sparse, tenant_source)


def test_point_ids_are_collision_free_across_tenants(monkeypatch):
    mod_a = _load_pipeline(monkeypatch, "AAAA-STG-01", "rag_aaaa-stg-01")
    fake_a = _FakeQdrant()
    _run_upsert(mod_a, fake_a, monkeypatch)
    ids_a = [p["id"] for p in fake_a.upserted]

    mod_b = _load_pipeline(monkeypatch, "BBBB-STG-01", "rag_bbbb-stg-01")
    fake_b = _FakeQdrant()
    _run_upsert(mod_b, fake_b, monkeypatch)  # SAME source + indices, different tenant
    ids_b = [p["id"] for p in fake_b.upserted]

    # Same source/index but different tenant → disjoint id sets (no silent overwrite).
    assert set(ids_a).isdisjoint(ids_b)


def test_point_ids_are_deterministic_idempotent(monkeypatch):
    mod = _load_pipeline(monkeypatch, "AAAA-STG-01", "rag_aaaa-stg-01")
    f1 = _FakeQdrant(); _run_upsert(mod, f1, monkeypatch)
    f2 = _FakeQdrant(); _run_upsert(mod, f2, monkeypatch)
    assert [p["id"] for p in f1.upserted] == [p["id"] for p in f2.upserted]


def test_payload_carries_tenant_id(monkeypatch):
    mod = _load_pipeline(monkeypatch, "NOVA-STG-01", "rag_nova-stg-01")
    fake = _FakeQdrant()
    _run_upsert(mod, fake, monkeypatch)
    for p in fake.upserted:
        assert p["payload"]["tenant_id"] == "NOVA-STG-01"
    assert fake.collection == "rag_nova-stg-01"


def test_invalid_collection_is_refused(monkeypatch):
    # Uppercase / illegal collection must be rejected BEFORE any Qdrant call.
    mod = _load_pipeline(monkeypatch, "x", "Rag_BAD;DROP")
    fake = _FakeQdrant()
    with pytest.raises(SystemExit):
        _run_upsert(mod, fake, monkeypatch)
    assert fake.upserted is None
