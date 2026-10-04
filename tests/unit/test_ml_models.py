"""Unit tests for the shared ML model singleton loader (src/proxy/ml_models.py)."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "proxy")))

import threading
import types
from unittest.mock import MagicMock, patch

import pytest

import ml_models


@pytest.fixture(autouse=True)
def _reset_ml_models_cache():
    ml_models._reset_for_tests()
    yield
    ml_models._reset_for_tests()


class TestSentenceTransformerSingleton:
    def test_same_model_name_returns_same_instance(self):
        mock_cls = MagicMock(side_effect=lambda name: MagicMock(name=f"st-{name}"))
        with patch("sentence_transformers.SentenceTransformer", mock_cls):
            a = ml_models.get_sentence_transformer("all-MiniLM-L6-v2")
            b = ml_models.get_sentence_transformer("all-MiniLM-L6-v2")
        assert a is b
        mock_cls.assert_called_once_with("all-MiniLM-L6-v2")

    def test_different_model_names_return_different_instances(self):
        mock_cls = MagicMock(side_effect=lambda name: MagicMock())
        with patch("sentence_transformers.SentenceTransformer", mock_cls):
            a = ml_models.get_sentence_transformer("model-a")
            b = ml_models.get_sentence_transformer("model-b")
        assert a is not b
        assert mock_cls.call_count == 2

    def test_concurrent_calls_load_model_only_once(self):
        """Many concurrent requests asking for the same model must trigger a
        single underlying load — the whole point of the singleton cache."""
        mock_cls = MagicMock(side_effect=lambda name: MagicMock())
        results = []

        def worker():
            results.append(ml_models.get_sentence_transformer("all-MiniLM-L6-v2"))

        with patch("sentence_transformers.SentenceTransformer", mock_cls):
            threads = [threading.Thread(target=worker) for _ in range(16)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        assert mock_cls.call_count == 1
        assert all(r is results[0] for r in results)


class TestFastembedSingletons:
    def _fake_fastembed_module(self):
        module = types.ModuleType("fastembed")
        module.TextEmbedding = MagicMock(side_effect=lambda name: MagicMock())
        module.SparseTextEmbedding = MagicMock(side_effect=lambda name: MagicMock())
        return module

    def test_text_embedding_singleton_per_model_name(self):
        fake = self._fake_fastembed_module()
        with patch.dict(sys.modules, {"fastembed": fake}):
            a = ml_models.get_text_embedding("all-MiniLM-L6-v2")
            b = ml_models.get_text_embedding("all-MiniLM-L6-v2")
            c = ml_models.get_text_embedding("other-model")
        assert a is b
        assert a is not c
        assert fake.TextEmbedding.call_count == 2

    def test_sparse_text_embedding_singleton(self):
        fake = self._fake_fastembed_module()
        with patch.dict(sys.modules, {"fastembed": fake}):
            a = ml_models.get_sparse_text_embedding("Qdrant/bm25")
            b = ml_models.get_sparse_text_embedding("Qdrant/bm25")
        assert a is b
        assert fake.SparseTextEmbedding.call_count == 1


class TestCrossEncoderSingleton:
    def test_cross_encoder_singleton(self):
        mock_cls = MagicMock(side_effect=lambda name: MagicMock())
        with patch("sentence_transformers.CrossEncoder", mock_cls):
            a = ml_models.get_cross_encoder("cross-encoder/ms-marco-MiniLM-L-6-v2")
            b = ml_models.get_cross_encoder("cross-encoder/ms-marco-MiniLM-L-6-v2")
        assert a is b
        mock_cls.assert_called_once_with("cross-encoder/ms-marco-MiniLM-L-6-v2")


class TestSingletonsAreIndependentAcrossKinds:
    def test_sentence_transformer_and_text_embedding_do_not_collide(self):
        """Same model name string used for different loader kinds must not
        share a cache slot (cache key includes the kind)."""
        st_mock = MagicMock(side_effect=lambda name: MagicMock(kind="st"))
        fake_fastembed = types.ModuleType("fastembed")
        fake_fastembed.TextEmbedding = MagicMock(side_effect=lambda name: MagicMock(kind="te"))

        with patch("sentence_transformers.SentenceTransformer", st_mock), \
                patch.dict(sys.modules, {"fastembed": fake_fastembed}):
            st = ml_models.get_sentence_transformer("all-MiniLM-L6-v2")
            te = ml_models.get_text_embedding("all-MiniLM-L6-v2")

        assert st is not te
        assert st.kind == "st"
        assert te.kind == "te"


# ─── Cloud Run IAM for the G01 / G03 sidecars ────────────────────────────────
# The sidecars refuse callers without an identity token. The token goes only to a Cloud
# Run URL, for the service origin, and only on GCP.

class TestCloudRunAuthHeaders:

    @staticmethod
    def _patch(monkeypatch, on_gcp=True):
        minted = []
        monkeypatch.setattr(ml_models, "_on_gcp", lambda: on_gcp)
        monkeypatch.setattr(ml_models, "_gcp_identity_token",
                            lambda audience: minted.append(audience) or "tok")
        return minted

    def test_a_cloud_run_url_gets_a_token_for_its_origin(self, monkeypatch):
        minted = self._patch(monkeypatch)
        headers = ml_models.cloud_run_auth_headers(
            "https://llmlingua-svc-abc123-el.a.run.app/compress?x=1")
        assert headers == {"Authorization": "Bearer tok"}
        assert minted == ["https://llmlingua-svc-abc123-el.a.run.app"]   # no path: Cloud Run checks the origin

    @pytest.mark.parametrize("url", [
        "http://llmlingua-svc:8080/compress",               # local compose
        "http://llmlingua-svc-abc123-el.a.run.app/compress",  # plain http
        "https://sidecar.example.com/compress",             # not Cloud Run: never sent the identity
        "https://evil.example/llmlingua.run.app",           # .run.app only in the path
        "https://llmlingua.run.app.evil.example/compress",  # .run.app only as a label
        "https://evilrun.app/compress",                     # run.app, but not a subdomain of it
        "",
    ])
    def test_any_other_url_gets_nothing(self, monkeypatch, url):
        minted = self._patch(monkeypatch)
        assert ml_models.cloud_run_auth_headers(url) == {}
        assert minted == []

    def test_off_gcp_nothing_is_attached(self, monkeypatch):
        minted = self._patch(monkeypatch, on_gcp=False)
        assert ml_models.cloud_run_auth_headers("https://tika-svc-abc123-el.a.run.app") == {}
        assert minted == []
