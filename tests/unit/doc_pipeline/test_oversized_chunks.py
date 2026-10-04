"""Ingestion cuts a chunk that is over MAX_CHUNK_TOKENS into pieces; it never summarises it.

The chunker cuts by characters, about four per token, so only token-dense text (some
non-Latin scripts, or a raised CHUNK_SIZE_TOKENS) runs over the limit. Such a chunk used to
go to an LLM summariser that could not run in the job image, whose libraries were never
installed. Had it run, it would have sent tenant text to a model on the platform's key and
stored a lossy summary instead of the text. It is now cut into pieces within the limit: no
model, no key, and nothing leaves the job.
"""
import importlib.util
import sys
import types
from pathlib import Path

import tiktoken

_PIPELINE = Path(__file__).resolve().parents[3] / "src" / "doc-pipeline" / "pipeline.py"
_ENC = tiktoken.get_encoding("cl100k_base")
_WORDS = [f"word{i}" for i in range(400)]


def _tokens(text):
    return len(_ENC.encode(text))


def _load(monkeypatch, **env):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    spec = importlib.util.spec_from_file_location("_docpipeline_oversized", _PIPELINE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_chunk_within_the_limit_is_unchanged(monkeypatch):
    mod = _load(monkeypatch)
    chunks = ["short one", "another short chunk"]
    assert mod.split_oversized_chunks(chunks, max_tokens=50) == chunks


def test_a_chunk_exactly_at_the_limit_is_unchanged(monkeypatch):
    mod = _load(monkeypatch)
    text = " ".join(_WORDS[:30])
    assert mod.split_oversized_chunks([text], max_tokens=_tokens(text)) == [text]


def test_an_oversized_chunk_becomes_pieces_within_the_limit(monkeypatch):
    mod = _load(monkeypatch)
    pieces = mod.split_oversized_chunks([" ".join(_WORDS)], max_tokens=60)
    assert len(pieces) > 1
    assert all(_tokens(p) <= 60 for p in pieces)
    assert all(p == p.strip() for p in pieces)          # the cut whitespace is not kept
    assert " ".join(pieces).split() == _WORDS          # nothing lost, nothing reordered


def test_cuts_fall_between_words(monkeypatch):
    mod = _load(monkeypatch)
    pieces = mod.split_oversized_chunks([" ".join(_WORDS)], max_tokens=60)
    assert all(set(p.split()) <= set(_WORDS) for p in pieces)


def test_a_line_break_is_preferred_to_a_space(monkeypatch):
    mod = _load(monkeypatch)
    lines = [" ".join(f"w{line}x{i}" for i in range(8)) for line in range(40)]
    pieces = mod.split_oversized_chunks(["\n".join(lines)], max_tokens=60)
    assert len(pieces) > 1
    assert all(p.split("\n")[-1] in lines for p in pieces)   # each piece ends a whole line


def test_text_without_spaces_is_still_cut(monkeypatch):
    mod = _load(monkeypatch)
    dense = "字" * 3000
    pieces = mod.split_oversized_chunks([dense], max_tokens=200)
    assert len(pieces) > 1 and all(_tokens(p) <= 200 for p in pieces)
    assert "".join(pieces) == dense


def test_neighbouring_chunks_keep_their_order(monkeypatch):
    mod = _load(monkeypatch)
    pieces = mod.split_oversized_chunks(["first", " ".join(_WORDS), "last"], max_tokens=40)
    assert pieces[0] == "first" and pieces[-1] == "last"
    assert " ".join(pieces[1:-1]).split() == _WORDS


def test_the_job_no_longer_calls_a_model_for_this():
    source = _PIPELINE.read_text(encoding="utf-8")
    assert "litellm" not in source
    assert "secretmanager" not in source
    assert "summarise_large_chunks" not in source


# ── run(): what is stored is within the limit ────────────────────────────────

class _FakeQdrant:
    def __init__(self):
        self.upserted = []

    def get_collections(self):
        return types.SimpleNamespace(collections=[])

    def create_collection(self, **kwargs):
        return None

    def upsert(self, collection_name, points):
        self.upserted.extend(points)


def _install(monkeypatch, fake):
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


def test_run_stores_token_dense_text_within_the_limit(monkeypatch):
    mod = _load(monkeypatch, GCS_BUCKET="b", GCS_OBJECT="doc.txt", TENANT_ID="t1",
                QDRANT_COLLECTION="rag_t1", CHUNK_SIZE_TOKENS="1200", MAX_CHUNK_TOKENS="1000")
    dense = "字" * 6000
    monkeypatch.setattr(mod, "download_from_gcs", lambda b, o: b"bytes")
    monkeypatch.setattr(mod, "extract_text", lambda content, name: dense)
    monkeypatch.setattr(mod, "embed_chunks_dense", lambda chunks: [[0.1, 0.2] for _ in chunks])
    monkeypatch.setattr(mod, "embed_chunks_sparse", lambda chunks: [object() for _ in chunks])
    monkeypatch.setattr(mod, "_to_sparse_vector", lambda x: x, raising=False)
    fake = _FakeQdrant()
    _install(monkeypatch, fake)

    mod.run()

    texts = [p["payload"]["text"] for p in fake.upserted]
    assert texts and max(_tokens(t) for t in texts) <= 1000
    assert max(_tokens(t) for t in mod.chunk_text(dense)) > 1000     # the cut was needed
