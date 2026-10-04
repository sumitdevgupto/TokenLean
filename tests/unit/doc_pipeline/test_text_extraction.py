"""Ingestion reads a document with Unstructured, then Tika, and refuses what neither can read.

Unstructured is tried first for every file, so PDF and Word extraction is what it was. When
it fails or finds no text, Tika (when TIKA_SIDECAR_URL is set) reads it: Excel, PowerPoint
and the other formats the job image has no Unstructured parser for. A file neither can read
used to be decoded byte for byte as UTF-8 and stored as RAG context. Now only a text file
that really is UTF-8 is decoded; anything else is refused, and the job stores nothing.
"""
import importlib.util
import sys
import types
from pathlib import Path

import pytest

_PIPELINE = Path(__file__).resolve().parents[3] / "src" / "doc-pipeline" / "pipeline.py"
_XLSX = b"PK\x03\x04\x14\x00\x06\x00\x08\x00\x00\x00!\x00\xb5U\x8f\xa3"   # a zip, as xlsx is


def _load(monkeypatch, tika_url=None, **env):
    monkeypatch.delenv("TIKA_SIDECAR_URL", raising=False)
    if tika_url is not None:
        monkeypatch.setenv("TIKA_SIDECAR_URL", tika_url)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    spec = importlib.util.spec_from_file_location("_docpipeline_extraction", _PIPELINE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _unstructured(monkeypatch, elements=None, error=None):
    """Install a stand-in ``unstructured.partition.auto``; returns the files it was given."""
    seen = []

    def partition(filename):
        seen.append(filename)
        if error is not None:
            raise error
        return list(elements or [])

    auto = types.ModuleType("unstructured.partition.auto")
    auto.partition = partition
    monkeypatch.setitem(sys.modules, "unstructured", types.ModuleType("unstructured"))
    monkeypatch.setitem(sys.modules, "unstructured.partition",
                        types.ModuleType("unstructured.partition"))
    monkeypatch.setitem(sys.modules, "unstructured.partition.auto", auto)
    return seen


def _tika(monkeypatch, mod, text):
    calls = []

    def extract(content, filename):
        calls.append(filename)
        return text

    monkeypatch.setattr(mod, "extract_text_with_tika", extract)
    return calls


def test_unstructured_text_is_used_and_tika_is_not_asked(monkeypatch):
    mod = _load(monkeypatch, tika_url="https://tika-svc-x.a.run.app")
    _unstructured(monkeypatch, elements=["Quarterly report", "Revenue grew."])
    tika = _tika(monkeypatch, mod, "from tika")
    assert mod.extract_text(b"%PDF-1.7 ...", "report.pdf") == "Quarterly report\n\nRevenue grew."
    assert tika == []


def test_tika_reads_what_unstructured_cannot(monkeypatch):
    mod = _load(monkeypatch, tika_url="https://tika-svc-x.a.run.app")
    _unstructured(monkeypatch, error=ValueError("no parser for xlsx"))
    tika = _tika(monkeypatch, mod, "Region,Revenue\nEMEA,10")
    assert mod.extract_text(_XLSX, "sales.xlsx") == "Region,Revenue\nEMEA,10"
    assert tika == ["sales.xlsx"]


def test_tika_is_asked_when_unstructured_finds_no_text(monkeypatch):
    mod = _load(monkeypatch, tika_url="https://tika-svc-x.a.run.app")
    _unstructured(monkeypatch, elements=["  ", ""])
    tika = _tika(monkeypatch, mod, "slide text")
    assert mod.extract_text(b"PK\x03\x04", "deck.pptx") == "slide text"
    assert tika == ["deck.pptx"]


def test_without_a_tika_url_tika_is_not_asked(monkeypatch):
    mod = _load(monkeypatch)
    _unstructured(monkeypatch, error=ValueError("no parser for xlsx"))
    tika = _tika(monkeypatch, mod, "from tika")
    with pytest.raises(mod.UnreadableDocumentError):
        mod.extract_text(_XLSX, "sales.xlsx")
    assert tika == []


def test_a_file_neither_can_read_is_refused(monkeypatch):
    mod = _load(monkeypatch, tika_url="https://tika-svc-x.a.run.app")
    _unstructured(monkeypatch, error=ValueError("no parser"))
    _tika(monkeypatch, mod, "")
    with pytest.raises(mod.UnreadableDocumentError, match="sales.xlsx"):
        mod.extract_text(_XLSX, "sales.xlsx")


@pytest.mark.parametrize("name", ["notes.txt", "README.md", "rows.csv", "data.json"])
def test_a_utf8_text_file_is_read_as_text_when_both_fail(monkeypatch, name):
    mod = _load(monkeypatch, tika_url="https://tika-svc-x.a.run.app")
    _unstructured(monkeypatch, error=ValueError("no parser"))
    _tika(monkeypatch, mod, "")
    assert mod.extract_text("Plain text, café.".encode("utf-8"), name) == "Plain text, café."


@pytest.mark.parametrize("content, name", [
    (b"\xff\xfe\x00b\x00a\x00d", "notes.txt"),          # a text name, but not UTF-8
    (b"caf\xe9 au lait", "notes.txt"),                    # Latin-1, no NUL: still not UTF-8
    (b"plain ascii\x00with a NUL", "notes.txt"),          # NUL bytes: not text
    (b"%PDF-1.4 ascii only body", "scan.pdf"),            # decodes, but not a text format
])
def test_bytes_are_never_stored_as_text(monkeypatch, content, name):
    mod = _load(monkeypatch, tika_url="https://tika-svc-x.a.run.app")
    _unstructured(monkeypatch, error=ValueError("no parser"))
    _tika(monkeypatch, mod, "")
    with pytest.raises(mod.UnreadableDocumentError):
        mod.extract_text(content, name)


def test_run_refuses_an_unreadable_file_and_stores_nothing(monkeypatch):
    mod = _load(monkeypatch, GCS_BUCKET="b", GCS_OBJECT="sales.xlsx", TENANT_ID="t1",
                QDRANT_COLLECTION="rag_t1")
    _unstructured(monkeypatch, error=ValueError("no parser"))
    stored = []
    monkeypatch.setattr(mod, "download_from_gcs", lambda b, o: _XLSX)
    monkeypatch.setattr(mod, "upsert_to_qdrant", lambda *a, **k: stored.append(a))
    with pytest.raises(SystemExit) as stop:
        mod.run()
    assert stop.value.code == 1
    assert stored == []
