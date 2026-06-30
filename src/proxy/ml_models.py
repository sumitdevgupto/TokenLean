"""
Shared singleton loader for local embedding/reranking models.

SentenceTransformer, fastembed (TextEmbedding/SparseTextEmbedding) and
CrossEncoder model loads take ~1-2s and hold the model weights in memory.
Re-instantiating them on every request (as G5/G7/G10 previously did) adds
that cold-start latency to every cache/RAG/memory lookup. These getters
lazily load each distinct model once per process and reuse it thereafter.

Inference calls (encode/embed/predict) on these models are read-only and
thread-safe, so a single shared instance can serve concurrent requests.
"""
import threading
from typing import Any, Dict, Tuple

_lock = threading.Lock()
_instances: Dict[Tuple[str, str], Any] = {}


def _get_or_create(cache_key: Tuple[str, str], factory) -> Any:
    instance = _instances.get(cache_key)
    if instance is not None:
        return instance
    with _lock:
        instance = _instances.get(cache_key)
        if instance is None:
            instance = factory()
            _instances[cache_key] = instance
        return instance


def get_sentence_transformer(model_name: str = "all-MiniLM-L6-v2") -> Any:
    """Return a shared SentenceTransformer instance for the given model name."""
    def _load():
        from sentence_transformers import SentenceTransformer
        return SentenceTransformer(model_name)
    return _get_or_create(("sentence_transformer", model_name), _load)


def get_text_embedding(model_name: str) -> Any:
    """Return a shared fastembed TextEmbedding instance for the given model name."""
    def _load():
        from fastembed import TextEmbedding
        return TextEmbedding(model_name)
    return _get_or_create(("text_embedding", model_name), _load)


def get_sparse_text_embedding(model_name: str) -> Any:
    """Return a shared fastembed SparseTextEmbedding instance for the given model name."""
    def _load():
        from fastembed import SparseTextEmbedding
        return SparseTextEmbedding(model_name)
    return _get_or_create(("sparse_text_embedding", model_name), _load)


def get_cross_encoder(model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2") -> Any:
    """Return a shared CrossEncoder instance for the given model name."""
    def _load():
        from sentence_transformers import CrossEncoder
        return CrossEncoder(model_name)
    return _get_or_create(("cross_encoder", model_name), _load)


def _reset_for_tests() -> None:
    """Clear cached instances. Test-only helper."""
    with _lock:
        _instances.clear()
