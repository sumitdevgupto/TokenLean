"""
Application-quality metrics — the Context-Quality + Output-Reliability signal surface.

Deliberately SEPARATE from `g18_observability.py`: G18 tracks *operational / gateway*
health (tokens, cost, latency, cache hits, failover) and the *savings* value-metric.
This module tracks *application-reasoning quality* — did retrieval provide relevant,
fresh grounding, and is the output structurally/verifiably sound. Keeping the two
apart honours the gateway-vs-quality split (a quality signal must never be confused
with an ops signal on the same dashboard).

Every metric is PII-free: labels are `tenant_id` (+ a small enum where noted) only —
never content. Emit helpers wrap each metric in try/except so a metrics hiccup can
never break the request path (same discipline as G18).

Sources (some wired now, some by later tasks):
  * Context Quality  — retrieval hit-rate / chunks-returned / freshness (G07);
                       grounding coverage (heuristic below).
  * Output Reliability — schema-validation failures (G11, Task 4), tool-eligibility
                       denials (G32, Task 6), output-verify score (G33, Task 7).
"""
import logging
import re
from typing import List, Optional

from prometheus_client import Counter, Gauge, Histogram

logger = logging.getLogger(__name__)

# ── Context Quality ───────────────────────────────────────────────────────────
RETRIEVAL_REQUESTS_TOTAL = Counter(
    "token_opt_retrieval_requests_total",
    "RAG retrieval attempts, labelled by result. hit_rate = hit / (hit + miss).",
    ["tenant_id", "result"],   # result ∈ {hit, miss}
)
RETRIEVAL_CHUNKS_RETURNED = Histogram(
    "token_opt_retrieval_chunks_returned",
    "Number of chunks injected into the prompt per RAG retrieval.",
    ["tenant_id"],
    buckets=(0, 1, 2, 3, 5, 8, 13, 21),
)
CONTEXT_MAX_AGE_SECONDS = Gauge(
    "token_opt_context_max_age_seconds",
    "Age (seconds) of the OLDEST chunk injected on the last RAG request per tenant "
    "(freshness). Only set when at least one injected chunk carried a timestamp.",
    ["tenant_id"],
    # Worst (oldest) freshness any worker saw — a staleness signal must not be
    # diluted by whichever worker happens to answer the scrape.
    multiprocess_mode="max",
)
GROUNDING_COVERAGE = Histogram(
    "token_opt_grounding_coverage",
    "Fraction (0-1) of answer sentences with lexical overlap in the retrieved context "
    "(cheap heuristic; the authoritative signal is the sampled G33 judge).",
    ["tenant_id"],
    buckets=(0.0, 0.25, 0.5, 0.75, 0.9, 1.0),
)

# ── Output Reliability ────────────────────────────────────────────────────────
OUTPUT_SCHEMA_FAILURES_TOTAL = Counter(
    "token_opt_output_schema_failures_total",
    "Model responses that failed JSON / json_schema validation (G11, Task 4). "
    "`mode` is the policy that applied (flag/repair/block).",
    ["tenant_id", "mode"],
)
TOOL_ELIGIBILITY_DENIED_TOTAL = Counter(
    "token_opt_tool_eligibility_denied_total",
    "Tool calls the model requested that were denied by the eligibility gate "
    "(G32), one increment per denied call. `mode` is the policy that applied: "
    "flag = recorded but the call was left in the response; block = the call was "
    "stripped before any auto-executing group could dispatch it.",
    ["tenant_id", "mode"],
)
TOOL_ELIGIBILITY_FAILURES_TOTAL = Counter(
    "token_opt_tool_eligibility_failures_total",
    "Times the G32 eligibility gate could not be applied to a response it was "
    "supposed to gate. `path` names where: short_circuit = the cache-hit / bypass "
    "hoist in main.py, which degrades to serving the response ungated so a broken "
    "gate cannot turn a cache hit into an error. A non-zero rate means responses "
    "are being served WITHOUT tool-call enforcement — alert on it.",
    ["tenant_id", "path"],
)
TOOL_DISPATCH_BLOCKED_TOTAL = Counter(
    "token_opt_tool_dispatch_blocked_total",
    "Server-side tool executions the proxy refused to perform (G15/G28 auto-exec sites). "
    "Distinct from token_opt_tool_eligibility_denied_total, which counts what the "
    "RESPONSE gate denied: this counts what an EXECUTION site declined to run. `reason`: "
    "not_injected = the proxy never advertised that tool (an injected, hallucinated or "
    "colliding name); policy_denied = the tenant's tool policy rejects it; "
    "evaluation_error = the check itself failed and the site failed closed. The refused "
    "call is still returned to the caller, unexecuted.",
    ["tenant_id", "reason"],
)
OUTPUT_VERIFY_SCORE = Histogram(
    "token_opt_output_verify_score",
    "Faithfulness/accuracy score (1-5) from the sampled inline judge (G33, Task 7).",
    ["tenant_id"],
    buckets=(1, 2, 3, 4, 5),
)


# ── Emit helpers (PII-free; never raise) ──────────────────────────────────────

def _safe(fn):
    try:
        fn()
    except Exception as exc:  # metrics must never break the request path
        logger.debug("quality_metrics emit failed: %s", exc)


def record_retrieval(tenant_id: str, n_chunks: int, max_age_seconds: Optional[float] = None) -> None:
    """One RAG retrieval outcome: hit (≥1 chunk injected) or miss (0), the chunk count,
    and the oldest-chunk age when known."""
    tid = tenant_id or "default"
    _safe(lambda: RETRIEVAL_REQUESTS_TOTAL.labels(tenant_id=tid,
          result="hit" if n_chunks > 0 else "miss").inc())
    _safe(lambda: RETRIEVAL_CHUNKS_RETURNED.labels(tenant_id=tid).observe(n_chunks))
    if max_age_seconds is not None:
        _safe(lambda: CONTEXT_MAX_AGE_SECONDS.labels(tenant_id=tid).set(max_age_seconds))


def record_grounding(tenant_id: str, coverage: float) -> None:
    _safe(lambda: GROUNDING_COVERAGE.labels(tenant_id=tenant_id or "default").observe(coverage))


def record_schema_failure(tenant_id: str, mode: str) -> None:
    _safe(lambda: OUTPUT_SCHEMA_FAILURES_TOTAL.labels(tenant_id=tenant_id or "default", mode=mode).inc())


def record_tool_denied(tenant_id: str, mode: str = "block", n: int = 1) -> None:
    _safe(lambda: TOOL_ELIGIBILITY_DENIED_TOTAL.labels(
        tenant_id=tenant_id or "default", mode=mode or "block").inc(n))


def record_tool_gate_failure(tenant_id: str, path: str = "short_circuit") -> None:
    """The gate was supposed to run and did not. Distinct from `record_tool_denied`:
    that one means the gate worked, this one means it did not."""
    _safe(lambda: TOOL_ELIGIBILITY_FAILURES_TOTAL.labels(
        tenant_id=tenant_id or "default", path=path or "unknown").inc())


def record_dispatch_blocked(tenant_id: str, reason: str = "policy_denied") -> None:
    """An auto-execution site declined to run a tool. See TOOL_DISPATCH_BLOCKED_TOTAL —
    this is 'the proxy did not act', not 'the caller was denied a result'."""
    _safe(lambda: TOOL_DISPATCH_BLOCKED_TOTAL.labels(
        tenant_id=tenant_id or "default", reason=reason or "unknown").inc())


def record_verify_score(tenant_id: str, score: float) -> None:
    _safe(lambda: OUTPUT_VERIFY_SCORE.labels(tenant_id=tenant_id or "default").observe(score))


# ── grounding_coverage heuristic (pure — unit-testable) ───────────────────────
_WORD_RE = re.compile(r"[A-Za-z0-9]+")
# A small stopword set so short function words don't count as "grounding" overlap.
_STOPWORDS = frozenset({
    "the", "and", "for", "are", "was", "has", "had", "with", "that", "this", "from",
    "you", "your", "its", "not", "but", "all", "can", "will", "have", "were", "they",
    "their", "our", "out", "who", "what", "when", "where", "which", "into", "than",
    "then", "them", "these", "those", "over", "some", "such", "also", "any", "his",
    "her", "him", "she", "does", "did", "been", "being", "how", "why",
})


def _content_tokens(text: str) -> set:
    """Lowercased alphanumeric tokens ≥3 chars, minus common stopwords."""
    return {t for t in (m.group().lower() for m in _WORD_RE.finditer(text or ""))
            if len(t) >= 3 and t not in _STOPWORDS}


def _split_sentences(text: str) -> List[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+", (text or "").strip()) if s.strip()]


def grounding_coverage(answer: str, chunks: List[str], *, min_overlap: float = 0.5) -> float:
    """Fraction (0-1) of the answer's sentences whose content is supported by the
    retrieved context. A sentence is 'grounded' when at least `min_overlap` of its
    content tokens appear in the union of the chunks' content tokens.

    Cheap and approximate ON PURPOSE — it catches an answer that ignored the retrieved
    context entirely, not subtle faithfulness. Empty answer or empty context → 0.0.
    A sentence with no content tokens is ignored (not counted for or against)."""
    if not answer or not chunks:
        return 0.0
    context = set()
    for c in chunks:
        context |= _content_tokens(c)
    if not context:
        return 0.0
    scored = 0
    grounded = 0
    for sentence in _split_sentences(answer):
        toks = _content_tokens(sentence)
        if not toks:
            continue
        scored += 1
        if len(toks & context) / len(toks) >= min_overlap:
            grounded += 1
    return (grounded / scored) if scored else 0.0


def _first_answer_text(response: dict) -> Optional[str]:
    """The first choice's assistant text, or None (tool-call / multimodal / empty)."""
    try:
        msg = (response.get("choices") or [{}])[0].get("message") or {}
        content = msg.get("content")
        return content if isinstance(content, str) else None
    except Exception:
        return None


def emit_grounding(ctx, response: dict) -> None:
    """Compute + emit grounding coverage for a RAG answer, if this request retrieved
    context (G07 stashed `ctx.rag_chunk_texts`) AND produced a plain-text answer.

    Called once on the response path. A no-op for non-RAG requests, tool-call answers,
    or when nothing was retrieved. Never raises (metrics must not break a response)."""
    def _do():
        chunks = getattr(ctx, "rag_chunk_texts", None)
        if not chunks:
            return
        answer = _first_answer_text(response)
        if not answer:
            return
        cov = grounding_coverage(answer, chunks)
        record_grounding(getattr(ctx, "tenant_id", "default"), cov)
    _safe(_do)
