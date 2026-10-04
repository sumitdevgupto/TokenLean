"""The doc-pipeline job's Tika call carries an identity token for a Cloud Run tika-svc.

On GCP tika-svc refuses callers without one. The job image does not ship the
proxy's ml_models, so pipeline.py has its own small helper; these tests pin it.
"""
import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_PIPELINE_PATH = Path(__file__).resolve().parents[3] / "src" / "doc-pipeline" / "pipeline.py"


def _load(monkeypatch):
    monkeypatch.setenv("TENANT_ID", "NOVA-STG-01")
    monkeypatch.setenv("QDRANT_COLLECTION", "rag_nova-stg-01")
    spec = importlib.util.spec_from_file_location("_docpipeline_tika_auth", _PIPELINE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Metadata:
    """urlopen stand-in for the metadata server; records each request."""

    def __init__(self):
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        resp = MagicMock()
        resp.__enter__.return_value = resp
        resp.read.return_value = b"tok\n"
        return resp


def test_a_cloud_run_tika_url_gets_a_token_for_its_origin(monkeypatch):
    mod = _load(monkeypatch)
    metadata = _Metadata()
    with patch("urllib.request.urlopen", metadata):
        headers = mod._cloud_run_auth_headers("https://tika-svc-abc123-el.a.run.app/tika")
    assert headers == {"Authorization": "Bearer tok"}
    (req,) = metadata.requests
    assert req.full_url.endswith("/identity?audience=https://tika-svc-abc123-el.a.run.app")
    assert req.get_header("Metadata-flavor") == "Google"


@pytest.mark.parametrize("url", [
    "http://tika-svc:9998",                    # local compose
    "https://tika.example.com",                # not Cloud Run: never sent the identity
    "https://evil.example/tika.run.app",       # .run.app only in the path
    "",
])
def test_any_other_url_gets_nothing_and_asks_no_one(monkeypatch, url):
    mod = _load(monkeypatch)
    metadata = _Metadata()
    with patch("urllib.request.urlopen", metadata):
        assert mod._cloud_run_auth_headers(url) == {}
    assert metadata.requests == []


def test_the_tika_call_carries_the_token(monkeypatch):
    mod = _load(monkeypatch)
    monkeypatch.setenv("TIKA_SIDECAR_URL", "https://tika-svc-abc123-el.a.run.app")
    client = MagicMock()
    client.__enter__.return_value = client
    client.put.return_value.text = "extracted"
    with patch("urllib.request.urlopen", _Metadata()), patch("httpx.Client", return_value=client):
        assert mod.extract_text_with_tika(b"%PDF-1.7", "a.pdf") == "extracted"
    headers = client.put.call_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer tok" and headers["X-Filename"] == "a.pdf"
