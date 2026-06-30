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
