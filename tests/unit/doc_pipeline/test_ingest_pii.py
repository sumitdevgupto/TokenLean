"""Task 2 — PII/PHI redaction at RAG ingest (src/doc-pipeline/pipeline.py).

Proves the vector store never holds raw personal data: the ingest scan runs BEFORE
chunk/embed/store, is off by default, and (in mask mode) the upserted chunk payload
carries placeholders, not the original PII. Uses the I2-style importlib + fake-Qdrant
harness (no live Qdrant / embedder / GCS)."""
import importlib.util
import sys
import types
from pathlib import Path

import pytest

_PIPELINE_PATH = Path(__file__).resolve().parents[3] / "src" / "doc-pipeline" / "pipeline.py"

EMAIL = "alice@example.com"
SSN = "123-45-6789"
DEA = "AB1234563"        # checksum-valid DEA


def _load_pipeline(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    spec = importlib.util.spec_from_file_location("_docpipeline_ingest_pii", _PIPELINE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── redact_ingest_pii direct behaviour ───────────────────────────────────────

class TestRedactIngestPii:
    def test_off_by_default_is_passthrough(self, monkeypatch):
        mod = _load_pipeline(monkeypatch)   # no INGEST_PII_MODE → off
        text = f"contact {EMAIL} ssn {SSN}"
        assert mod.redact_ingest_pii(text) == text

    def test_flag_mode_detects_but_does_not_mutate(self, monkeypatch):
        mod = _load_pipeline(monkeypatch, INGEST_PII_MODE="flag")
        text = f"contact {EMAIL}"
        assert mod.redact_ingest_pii(text) == text   # flag never mutates

    def test_mask_mode_removes_raw_pii(self, monkeypatch):
        mod = _load_pipeline(monkeypatch, INGEST_PII_MODE="mask")
        out = mod.redact_ingest_pii(f"contact {EMAIL} ssn {SSN}")
        assert EMAIL not in out and SSN not in out
        assert "[EMAIL]" in out and "[US_SSN]" in out

    def test_phi_masked_only_when_enabled(self, monkeypatch):
        mod = _load_pipeline(monkeypatch, INGEST_PII_MODE="mask")
        # PHI off → DEA stays raw (PII default set only)
        assert DEA in mod.redact_ingest_pii(f"prescriber DEA {DEA}")
        # PHI on → DEA masked
        mod2 = _load_pipeline(monkeypatch, INGEST_PII_MODE="mask", INGEST_PII_PHI="true")
        assert DEA not in mod2.redact_ingest_pii(f"prescriber DEA {DEA}")

    def test_no_pii_is_unchanged(self, monkeypatch):
        mod = _load_pipeline(monkeypatch, INGEST_PII_MODE="mask")
        text = "the quarterly report covers three regions"
        assert mod.redact_ingest_pii(text) == text


# ── end-to-end run(): redaction happens BEFORE embed/store ───────────────────

class _FakeQdrant:
    def __init__(self, *a, **k):
        self.upserted = None

    def get_collections(self):
        return types.SimpleNamespace(collections=[])

    def create_collection(self, **k):
        return None

    def upsert(self, collection_name, points):
        self.upserted = points


def _install_fake_qdrant(monkeypatch, fake):
    qmod = types.ModuleType("qdrant_client")
    qmod.QdrantClient = lambda *a, **k: fake
    models = types.ModuleType("qdrant_client.models")
    models.PointStruct = lambda **kw: kw
    models.Distance = types.SimpleNamespace(COSINE="Cosine")
    models.VectorParams = lambda **kw: kw
    models.SparseVectorParams = lambda **kw: kw
    models.SparseIndexParams = lambda **kw: kw
    qmod.models = models
    monkeypatch.setitem(sys.modules, "qdrant_client", qmod)
    monkeypatch.setitem(sys.modules, "qdrant_client.models", models)


def test_run_masks_pii_before_store(monkeypatch):
    mod = _load_pipeline(
        monkeypatch,
        GCS_BUCKET="b", GCS_OBJECT="doc.txt",
        TENANT_ID="NOVA-STG-01", QDRANT_COLLECTION="rag_nova-stg-01",
        INGEST_PII_MODE="mask", INGEST_PII_PHI="true",
    )
    raw = (f"Patient contact {EMAIL}, SSN {SSN}, prescriber DEA {DEA}. "
           "This document section is deliberately long enough to clear the "
           "minimum-length guard in the ingestion pipeline so it is processed.")
    monkeypatch.setattr(mod, "download_from_gcs", lambda b, o: b"bytes")
    monkeypatch.setattr(mod, "extract_text", lambda content, name: raw)
    monkeypatch.setattr(mod, "embed_chunks_dense", lambda chunks: [[0.1, 0.2] for _ in chunks])
    monkeypatch.setattr(mod, "embed_chunks_sparse", lambda chunks: [object() for _ in chunks])
    monkeypatch.setattr(mod, "_to_sparse_vector", lambda x: x, raising=False)
    fake = _FakeQdrant()
    _install_fake_qdrant(monkeypatch, fake)

    mod.run()

    assert fake.upserted, "nothing was upserted"
    stored = " ".join(p["payload"]["text"] for p in fake.upserted)
    # The stored (embedded + persisted) chunk text must carry NO raw PII/PHI.
    assert EMAIL not in stored
    assert SSN not in stored
    assert DEA not in stored
    assert "[EMAIL]" in stored   # placeholders present → redaction actually ran
