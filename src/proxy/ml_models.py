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
# FASTEMBED_CACHE_PATH), force local_files_only so the loader does NOT make an
# HF-CDN "is the cache up to date?" metadata call. That call HANGS on an
# egress-restricted host (Cloud Run --vpc-egress=private-ranges-only) until timeout,
# even though the model is present locally. Empty (online default) otherwise.
#
# This applies to BOTH fastembed AND sentence-transformers: SentenceTransformer(name)
# / CrossEncoder(name) resolve the model via huggingface_hub's snapshot_download, which
# in the installed version still issues a metadata HEAD to huggingface.co even under
# HF_HUB_OFFLINE=1 unless local_files_only=True is passed explicitly — the observed
# 250s G07/G05 stall → Cloud Run 504 on the private-egress commercial stack. Both
# sentence_transformers constructors accept local_files_only.
def _hf_offline() -> bool:
    return os.getenv("HF_HUB_OFFLINE", "") in ("1", "true", "True")


def _fastembed_offline_kwargs() -> Dict[str, Any]:
    return {"local_files_only": True} if _hf_offline() else {}


def _st_offline_kwargs() -> Dict[str, Any]:
    return {"local_files_only": True} if _hf_offline() else {}

# Whether the installed qdrant-client accepts the `check_compatibility` kwarg. It was added
# in qdrant-client 1.14; on older clients (e.g. 1.12.x) passing it raises
# "unexpected keyword argument 'check_compatibility'", which silently breaks EVERY Qdrant
# path (G07 retrieval, G03 ingest, G10 memory, docs-chat). Detected once via the ctor
# signature so call sites stay version-agnostic. Suppresses the client's version-skew warning
# where supported, and is simply omitted where not.
_qdrant_supports_check_compat: Any = None
_qdrant_compat_lock = threading.Lock()


# ─── GCP identity-token auth for Qdrant (Cloud Run IAM, service-to-service) ────
# On GCP the Qdrant service runs with ingress ALL + IAM required: every request must
# carry `Authorization: Bearer <Google-signed identity token>` for a run.invoker
# principal. qdrant-client sends `api_key` ONLY as the Qdrant-native `api-key` header
# (verified on 1.12.2) — the Bearer header needs the separate `auth_token_provider`
# param, so `qdrant_client_kwargs` attaches a cached metadata-server token provider
# automatically when running on GCP. Locally (no metadata server / QDRANT_LOCAL_NOAUTH=1)
# nothing is attached and behaviour is byte-identical to before.
_gcp_metadata_available: Any = None  # tri-state: None=unprobed, True/False cached
_id_token_cache: Dict[str, Tuple[str, float]] = {}  # audience -> (token, expires_at)
_id_token_lock = threading.Lock()


def _gcp_identity_token(audience: str) -> str:
    """Fetch (and cache ~50 min) a GCP identity token for `audience` from the metadata
    server. Returns "" off-GCP — callers must not attach a provider in that case."""
    import time
    import urllib.request

    now = time.time()
    tok, exp = _id_token_cache.get(audience, ("", 0.0))
    if tok and now < exp:
        return tok
    with _id_token_lock:
        tok, exp = _id_token_cache.get(audience, ("", 0.0))
        if tok and now < exp:
            return tok
        req = urllib.request.Request(
            "http://metadata.google.internal/computeMetadata/v1/instance/"
            f"service-accounts/default/identity?audience={audience}",
            headers={"Metadata-Flavor": "Google"},
        )
        token = urllib.request.urlopen(req, timeout=5).read().decode().strip()
        _id_token_cache[audience] = (token, now + 3000)  # refresh 10 min before 1 h expiry
        return token


def _on_gcp() -> bool:
    """Probe the metadata server once per process; cache the verdict."""
    global _gcp_metadata_available
    if _gcp_metadata_available is None:
        import socket
        import urllib.request
        try:
            req = urllib.request.Request(
                "http://metadata.google.internal/computeMetadata/v1/instance/id",
                headers={"Metadata-Flavor": "Google"},
            )
            urllib.request.urlopen(req, timeout=2).read()
            _gcp_metadata_available = True
        except (socket.gaierror, socket.timeout, OSError, Exception):
            _gcp_metadata_available = False
    return _gcp_metadata_available


def qdrant_client_kwargs(**extra: Any) -> Dict[str, Any]:
    """Return kwargs for QdrantClient/AsyncQdrantClient, adding `check_compatibility=False`
    only when the installed client supports it, and — on GCP — an `auth_token_provider`
    that supplies the Cloud Run IAM identity token (audience = the Qdrant URL). Pass
    through `url`, `api_key` (the Qdrant app-layer key, sent as the `api-key` header),
    etc. via ``extra``. Use everywhere a Qdrant client is constructed so a client-version
    bump or an auth-topology change can't break connectivity."""
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
    # Qdrant app-layer key: the GCP-managed Qdrant enforces QDRANT__SERVICE__API_KEY,
    # so EVERY caller (G03/G05/G07/G10/docs-chat) must send the `api-key` header —
    # default it from the env centrally so call sites that never passed one keep working.
    if not kwargs.get("api_key"):
        kwargs["api_key"] = os.getenv("QDRANT_API_KEY") or None
    # qdrant-client defaults port=6333 even for an https URL WITHOUT an explicit port —
    # on Cloud Run (which serves 443 only) every request then SYNs a closed port and
    # dies as ConnectTimeout('') (the empty G07 "Qdrant search failed:" errors). Pin 443
    # for portless https URLs. Proven root cause 2026-07-16: identical client call went
    # 5s-timeout → 0.13s OK with port=443. Local http://host:6333 URLs are untouched.
    _u = str(kwargs.get("url") or "")
    if _u.startswith("https://") and "port" not in kwargs:
        _netloc = _u.split("//", 1)[1].split("/", 1)[0]
        if ":" not in _netloc:
            kwargs["port"] = 443
    # Cloud Run IAM bearer: only for https targets (GCP Cloud Run URLs), only on GCP,
    # honouring the local no-auth escape hatch. The provider is called by qdrant-client
    # per request; the token itself is cached ~50 min so the metadata hit is rare.
    url = kwargs.get("url") or ""
    if (
        "auth_token_provider" not in kwargs
        and str(url).startswith("https://")
        and os.getenv("QDRANT_LOCAL_NOAUTH") != "1"
        and _on_gcp()
    ):
        audience = str(url)
        kwargs["auth_token_provider"] = lambda: _gcp_identity_token(audience)
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
        return SentenceTransformer(model_name, **_st_offline_kwargs())
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
        return CrossEncoder(model_name, **_st_offline_kwargs())
    return _get_or_create(("cross_encoder", model_name), _load)


def _reset_for_tests() -> None:
    """Clear cached instances. Test-only helper."""
    with _lock:
        _instances.clear()
