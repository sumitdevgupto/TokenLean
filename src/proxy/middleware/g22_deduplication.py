"""G22 — Semantic deduplication for multi-turn conversations.

Embeds each user/assistant turn and collapses near-duplicate turns
(cosine similarity ≥ dedup_threshold) into a single placeholder
``[summarised: N similar turns]``.  This reduces multi-turn bloat
caused by repeated phrasings or reformulations of the same question.

Embedding is done inline using the same sentence-transformers model
used by G05 (BGE-small-en-v1.5) when available, falling back to a
character n-gram similarity heuristic when the ML library is absent.

Reference: G22 in token_optimization_playbook_v7.md
"""

import logging
import math
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_DEFAULT_THRESHOLD = 0.97
_SUMMARISED_ROLES = {"user", "assistant"}


def _cosine(a: List[float], b: List[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(x * x for x in b))
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)


def _ngram_vector(text: str, n: int = 3) -> Dict[str, int]:
    """Character n-gram bag-of-words fallback."""
    text = text.lower()
    grams: Dict[str, int] = {}
    for i in range(len(text) - n + 1):
        g = text[i : i + n]
        grams[g] = grams.get(g, 0) + 1
    return grams


def _ngram_cosine(a: str, b: str, n: int = 3) -> float:
    va, vb = _ngram_vector(a, n), _ngram_vector(b, n)
    keys = set(va) | set(vb)
    dot = sum(va.get(k, 0) * vb.get(k, 0) for k in keys)
    mag_a = math.sqrt(sum(v * v for v in va.values()))
    mag_b = math.sqrt(sum(v * v for v in vb.values()))
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)


def _embed(text: str, model: Any) -> Optional[List[float]]:
    try:
        vec = model.encode([text])[0]
        return vec.tolist()
    except Exception:
        return None


def _similarity(text_a: str, text_b: str, model: Optional[Any]) -> float:
    if model is not None:
        va = _embed(text_a, model)
        vb = _embed(text_b, model)
        if va is not None and vb is not None:
            return _cosine(va, vb)
    return _ngram_cosine(text_a, text_b)


def _get_model(cfg: Dict) -> Optional[Any]:
    model_name = cfg.get("embedding_model", "BAAI/bge-small-en-v1.5")
    try:
        # Shared loader: cached singleton + HF_HUB_OFFLINE guard (local_files_only so the
        # baked model loads without an HF-CDN metadata call that hangs under VPC egress).
        from ml_models import get_sentence_transformer
        return get_sentence_transformer(model_name)
    except Exception:
        return None


class G22Deduplication:
    """
    Semantic deduplication: collapses near-duplicate conversation turns.
    Reference: G22 in token_optimization_playbook_v7.md
    """

    def __init__(self, embedding_model: Optional[Any] = None):
        self._model = embedding_model
        self._model_loaded = embedding_model is not None

    def _cfg(self, ctx: Any) -> Dict[str, Any]:
        return ctx.config.get("groups", {}).get("g22_deduplication", {})

    def _get_embedding_model(self, cfg: Dict) -> Optional[Any]:
        if self._model_loaded:
            return self._model
        if cfg.get("use_embeddings", True):
            self._model = _get_model(cfg)
        # Mark as loaded regardless (None = use n-gram fallback) to avoid
        # re-entering on every subsequent request when use_embeddings=false.
        self._model_loaded = True
        return self._model

    async def process_request(self, ctx: Any) -> Any:
        cfg = self._cfg(ctx)
        if not cfg.get("enabled", False):
            return ctx

        tenant_id = getattr(ctx, "tenant_id", None)
        tenant_threshold = cfg.get("tenant_thresholds", {}).get(tenant_id) if tenant_id else None
        threshold = float(tenant_threshold if tenant_threshold is not None else cfg.get("dedup_threshold", _DEFAULT_THRESHOLD))
        messages: List[Dict] = ctx.messages
        if len(messages) <= 1:
            return ctx

        model = self._get_embedding_model(cfg)
        deduped: List[Dict] = []
        pending_group: List[Dict] = []
        def _content_str(m: Dict) -> str:
            c = m.get("content", "")
            return c if isinstance(c, str) else ""

        tokens_before = sum(len(_content_str(m).split()) for m in messages)

        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if not isinstance(content, str):
                # Multimodal list content — skip dedup for this message
                if pending_group:
                    deduped.extend(pending_group)
                    pending_group = []
                deduped.append(msg)
                continue
            if role not in _SUMMARISED_ROLES or not content:
                if pending_group:
                    deduped.extend(pending_group)
                    pending_group = []
                deduped.append(msg)
                continue

            if not pending_group:
                pending_group.append(msg)
                continue

            last = pending_group[-1]
            if last.get("role") != role:
                deduped.extend(pending_group)
                pending_group = [msg]
                continue

            sim = _similarity(last.get("content", ""), content, model)
            if sim >= threshold:
                pending_group.append(msg)
            else:
                if len(pending_group) > 1:
                    placeholder = dict(pending_group[0])
                    placeholder["content"] = f"[summarised: {len(pending_group)} similar turns]"
                    deduped.append(placeholder)
                else:
                    deduped.extend(pending_group)
                pending_group = [msg]

        if pending_group:
            if len(pending_group) > 1:
                placeholder = dict(pending_group[0])
                placeholder["content"] = f"[summarised: {len(pending_group)} similar turns]"
                deduped.append(placeholder)
            else:
                deduped.extend(pending_group)

        ctx.messages = deduped
        tokens_after = sum(len(_content_str(m).split()) for m in deduped)

        ctx.savings.add_step(
            group="G22",
            description=f"G22: collapsed {len(messages) - len(deduped)} duplicate turns",
            tokens_before=tokens_before,
            tokens_after=tokens_after,
        )
        logger.debug(
            "G22: %d → %d messages, tokens %d → %d",
            len(messages),
            len(deduped),
            tokens_before,
            tokens_after,
        )
        return ctx

    async def process_response(self, ctx: Any, response: Dict) -> Dict:
        return response
