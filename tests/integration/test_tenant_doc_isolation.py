"""End-to-end tenant document isolation — the core data-safety guarantee.

Drives the write path (doc-pipeline upsert) for two tenants into their own collections,
then asserts:
  * each collection contains ONLY its owner's points (tenant_id payload)
  * the read-side collection resolver (G07 _resolve_collection) sends tenant A's query to
    rag_A and tenant B's to rag_B — so A can never retrieve B's chunks and vice-versa.

Uses an in-memory fake Qdrant that enforces per-collection separation, so the isolation
LOGIC is verified in CI without external infra. (A live-Qdrant variant is documented in the
plan's Verification section for the manual/GCP proof.)
"""
import importlib.util
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".." / "src" / "proxy"))

_PIPELINE_PATH = Path(__file__).resolve().parents[2] / "src" / "doc-pipeline" / "pipeline.py"


class _InMemoryQdrant:
    """Per-collection point store — the isolation boundary under test."""

    _store: dict = {}

    def __init__(self, *a, **k):
        pass

    def get_collections(self):
        cols = [types.SimpleNamespace(name=n) for n in self._store]
        return types.SimpleNamespace(collections=cols)

    def get_collection(self, name):
        return types.SimpleNamespace(
            config=types.SimpleNamespace(params=types.SimpleNamespace(vectors={"dense": 1}))
        )

    def create_collection(self, collection_name, **k):
        self._store.setdefault(collection_name, [])

    def upsert(self, collection_name, points):
        self._store.setdefault(collection_name, []).extend(points)

    def scroll(self, collection_name):
        return self._store.get(collection_name, [])


def _load_pipeline(monkeypatch, tenant, collection):
    monkeypatch.setenv("TENANT_ID", tenant)
    monkeypatch.setenv("QDRANT_COLLECTION", collection)
    spec = importlib.util.spec_from_file_location(f"_pipe_{tenant}", _PIPELINE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _install_fake_qdrant(monkeypatch):
    qmod = types.ModuleType("qdrant_client")
    qmod.QdrantClient = _InMemoryQdrant
    models = types.ModuleType("qdrant_client.models")
    models.PointStruct = lambda **kw: kw
    models.Distance = types.SimpleNamespace(COSINE="Cosine")
    models.VectorParams = lambda **kw: kw
    models.SparseVectorParams = lambda **kw: kw
    models.SparseIndexParams = lambda **kw: kw
    qmod.models = models
    monkeypatch.setitem(sys.modules, "qdrant_client", qmod)
    monkeypatch.setitem(sys.modules, "qdrant_client.models", models)


def _ingest(mod, monkeypatch, source):
    monkeypatch.setattr(mod, "_to_sparse_vector", lambda x: x, raising=False)
    chunks = ["shared secret content", "second chunk"]
    dense = [[0.1, 0.2], [0.3, 0.4]]
    sparse = [object(), object()]
    mod.upsert_to_qdrant(chunks, dense, sparse, source)


def test_ingested_docs_are_isolated_per_tenant(monkeypatch):
    _InMemoryQdrant._store = {}
    _install_fake_qdrant(monkeypatch)

    from tenancy.context import TenantContext

    col_a = TenantContext.for_tenant("AAAA-STG-01").qdrant_collection
    col_b = TenantContext.for_tenant("BBBB-STG-01").qdrant_collection

    mod_a = _load_pipeline(monkeypatch, "AAAA-STG-01", col_a)
    _ingest(mod_a, monkeypatch, "gs://token-opt-docs-aaaa-stg-01/doc.pdf")
    mod_b = _load_pipeline(monkeypatch, "BBBB-STG-01", col_b)
    _ingest(mod_b, monkeypatch, "gs://token-opt-docs-bbbb-stg-01/doc.pdf")

    store = _InMemoryQdrant._store
    # Each collection holds ONLY its owner's points.
    assert {p["payload"]["tenant_id"] for p in store[col_a]} == {"AAAA-STG-01"}
    assert {p["payload"]["tenant_id"] for p in store[col_b]} == {"BBBB-STG-01"}
    # No point id appears in both collections (collision-free across tenants).
    ids_a = {p["id"] for p in store[col_a]}
    ids_b = {p["id"] for p in store[col_b]}
    assert ids_a.isdisjoint(ids_b)


def test_read_resolver_pins_each_tenant_to_own_collection(monkeypatch):
    from middleware.g07_retrieval import _resolve_collection
    from tenancy.context import TenantContext

    def _ctx(tenant):
        c = types.SimpleNamespace()
        c.qdrant_collection = TenantContext.for_tenant(tenant).qdrant_collection
        c.params = {"x_rag_collection": TenantContext.for_tenant("BBBB-STG-01").qdrant_collection}
        c.is_admin_key = False
        c.tenant_id = tenant
        c.request_id = "r"
        return c

    # Tenant A, even trying to point at B's collection via header, is pinned to rag_A.
    resolved = _resolve_collection(_ctx("AAAA-STG-01"), {})
    assert resolved == TenantContext.for_tenant("AAAA-STG-01").qdrant_collection
    assert resolved != TenantContext.for_tenant("BBBB-STG-01").qdrant_collection
