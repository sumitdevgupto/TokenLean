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
import os
import threading
from typing import Any, Dict, Tuple

_lock = threading.Lock()
_instances: Dict[Tuple[str, str], Any] = {}

# When HF_HUB_OFFLINE=1 (the commercial image sets it; models are pre-baked into
# FASTEMBED_CACHE_PATH), force fastembed's local_files_only so it does NOT make an
# HF-CDN "is the cache up to date?" metadata call. That call HANGS on an
# egress-restricted host (Cloud Run --vpc-egress=private-ranges-only) until timeout,
# even though the model is present locally. Empty (online default) otherwise.
def _fastembed_offline_kwargs() -> Dict[str, Any]:
    return {"local_files_only": True} if os.getenv("HF_HUB_OFFLINE", "") in ("1", "true", "True") else {}

# Whether the installed qdrant-client accepts the `check_compatibility` kwarg. It was added
# in qdrant-client 1.14; on older clients (e.g. 1.12.x) passing it raises
# "unexpected keyword argument 'check_compatibility'", which silently breaks EVERY Qdrant
# path (G07 retrieval, G03 ingest, G10 memory, docs-chat). Detected once via the ctor
# signature so call sites stay version-agnostic. Suppresses the client's version-skew warning
# where supported, and is simply omitted where not.
_qdrant_supports_check_compat: Any = None
_qdrant_compat_lock = threading.Lock()


def qdrant_client_kwargs(**extra: Any) -> Dict[str, Any]:
    """Return kwargs for QdrantClient/AsyncQdrantClient, adding `check_compatibility=False`
    only when the installed client supports it. Pass through `url`, `api_key`, etc. via
    ``extra``. Use everywhere a Qdrant client is constructed so a client-version bump/downgrade
    can't break connectivity."""
    global _qdrant_supports_check_compat
    if _qdrant_supports_check_compat is None:
        with _qdrant_compat_lock:
            if _qdrant_supports_check_compat is None:
                try:
                    import inspect
                    from qdrant_client import QdrantClient
                    _qdrant_supports_check_compat = (
                        "check_compatibility" in inspect.signature(QdrantClient.__init__).parameters
                    )
                except Exception:
                    _qdrant_supports_check_compat = False
    kwargs = {k: v for k, v in extra.items() if v is not None or k == "api_key"}
    if _qdrant_supports_check_compat:
        kwargs["check_compatibility"] = False
    return kwargs


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
        return TextEmbedding(model_name, **_fastembed_offline_kwargs())
    return _get_or_create(("text_embedding", model_name), _load)


def get_sparse_text_embedding(model_name: str) -> Any:
    """Return a shared fastembed SparseTextEmbedding instance for the given model name."""
    def _load():
        from fastembed import SparseTextEmbedding
        return SparseTextEmbedding(model_name, **_fastembed_offline_kwargs())
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
