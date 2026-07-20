"""
G13 · Batch Processing & Compact Notation
Stage: Inside the LLM
Saving: 20–60% overhead per item; up to 98% on repetitive structured data
Technique:
  1. Accumulate similar requests by topic tag in a Cloud Tasks queue.
  2. Flush batch when size or time threshold reached — single shared system prompt.
  3. TOON compact schema for repetitive structured data in messages.
"""
import asyncio
import json
import logging
import os
import re
import time
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

from middleware import RequestContext
from savings.calculator import count_messages_tokens, estimate_tokens

logger = logging.getLogger(__name__)
GROUP = "G13"

_BATCH_STREAM_PREFIX = os.getenv("BATCH_STREAM_PREFIX", "tok_opt:batch")

# Kafka configuration (optional - falls back to Redis)
_KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "")
_KAFKA_BATCH_TOPIC = os.getenv("KAFKA_BATCH_TOPIC", "token-opt-batches")


def _get_redis():
    from cache.redis_pool import get_redis as _pool_get_redis
    return _pool_get_redis()


class G13Batch:
    async def process_request(self, ctx: RequestContext) -> RequestContext:
        cfg = ctx.config.get("groups", {}).get("G13_batch", {})
        if not cfg.get("enabled", False):
            return ctx

        tokens_before = ctx.current_token_count

        # 1. TOON compact notation for messages with repetitive structured data
        changed, ctx = await _apply_toon(ctx, tokens_before)

        # 2. Batch accumulation via Redis Streams (only for explicitly tagged requests)
        batch_topic = ctx.params.get("batch_topic")
        if batch_topic:
            await _accumulate(ctx, batch_topic)
            ctx.batch_deferred = True  # response will be delivered async
            # H1: record the owning tenant so /v1/batch/results/{id} can refuse a
            # cross-tenant poll even though the request_id is an unguessable UUID.
            await _record_batch_owner(ctx.request_id, getattr(ctx, "tenant_id", "default"))

        return ctx


def _resolve_toon_cfg(ctx: RequestContext) -> Dict[str, Any]:
    """G13_batch config with the per-tenant override merged in (tenant wins)."""
    base = ctx.config.get("groups", {}).get("G13_batch", {})
    tenant_cfg = (
        ctx.config.get("tenants", {})
        .get(ctx.tenant_id, {})
        .get("groups", {})
        .get("G13_batch", {})
    )
    return {**base, **tenant_cfg}


async def _apply_toon(
    ctx: RequestContext, tokens_before: int
) -> Tuple[bool, RequestContext]:
    """
    Detect arrays of uniform objects in user messages and compress them to TOON.

    Gating is config-driven and the defaults reproduce the legacy behaviour:
      • ``toon_auto_detect`` false (default): only runs when a system message already
        carries a TOON schema marker (``schema`` + ``|``).
      • ``toon_auto_detect`` true: runs on every request, relying on the per-block
        eligibility + net-savings gates so non-tabular / nested / would-inflate data
        is left untouched as JSON (the "JSON fallback").
    """
    cfg = _resolve_toon_cfg(ctx)

    if not cfg.get("toon_auto_detect", False):
        toon_detected = any(
            "schema" in str(msg.get("content", "")).lower()
            and "|" in str(msg.get("content", ""))
            for msg in ctx.messages
            if msg.get("role") == "system"
        )
        if not toon_detected:
            return False, ctx

    new_messages = []
    changed = False
    for msg in ctx.messages:
        if msg.get("role") != "user":
            new_messages.append(msg)
            continue
        content = msg.get("content", "")
        if not isinstance(content, str):
            new_messages.append(msg)
            continue
        compacted = _compact_json_to_toon(content, cfg, ctx.model)
        if compacted != content:
            new_messages.append({**msg, "content": compacted})
            changed = True
        else:
            new_messages.append(msg)

    if changed:
        ctx.messages = new_messages
        tokens_after = count_messages_tokens(ctx.messages, ctx.model)
        ctx.savings.add_step(
            GROUP,
            "TOON compact notation applied to structured data",
            tokens_before,
            tokens_after,
        )
        logger.debug(
            "[%s] G13 TOON: %d → %d tokens",
            ctx.request_id,
            tokens_before,
            tokens_after,
        )
    return changed, ctx


_DEFAULT_TOON_MAX_BLOCK_CHARS = 20000
_array_pattern_cache: Dict[int, "re.Pattern"] = {}


def _array_of_objects_pattern(max_block_chars: int) -> "re.Pattern":
    """Lazily-bounded regex matching a single JSON array-of-objects block.

    Starts at ``[{`` and ends at the first ``}]`` so each array in a message is
    captured separately rather than merged across blocks.  The length bound keeps
    the scan cheap on very large payloads.
    """
    pat = _array_pattern_cache.get(max_block_chars)
    if pat is None:
        pat = re.compile(r"\[\s*\{[\s\S]{0,%d}?\}\s*\]" % int(max_block_chars))
        _array_pattern_cache[max_block_chars] = pat
    return pat


def _toon_cell(value: Any) -> str:
    """Render a scalar JSON value as a TOON cell."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _encode_block_to_toon(
    json_str: str,
    *,
    min_rows: int,
    uniform_threshold: float,
    allow_nested: bool,
    require_net_savings: bool,
    model: str = "",
) -> Optional[str]:
    """Encode one JSON array-of-objects block to TOON, or return None to skip it.

    Eligibility gates (all config-driven):
      • length ≥ ``min_rows``;
      • every item is an object;
      • fraction of rows sharing the modal key-set ≥ ``uniform_threshold``
        (1.0 = strictly uniform, the legacy behaviour);
      • scalar-only values unless ``allow_nested``;
      • when ``require_net_savings`` the TOON form must be strictly smaller, so the
        transform can never increase the token count.
    """
    try:
        data = json.loads(json_str)
    except Exception:
        return None
    if not isinstance(data, list) or len(data) < min_rows:
        return None
    if not all(isinstance(item, dict) for item in data):
        return None

    # Tabular-ness gate: fraction of rows sharing the modal key-set.
    keysets = [frozenset(item.keys()) for item in data]
    _modal_keyset, modal_count = Counter(keysets).most_common(1)[0]
    if modal_count / len(data) < uniform_threshold:
        return None

    # Scalar-only gate — nested objects/arrays would inflate or garble the row form.
    if not allow_nested:
        for item in data:
            if any(isinstance(v, (dict, list)) for v in item.values()):
                return None

    # Header = union of keys (first-seen order) so near-uniform rows stay lossless.
    keys: List[str] = []
    for item in data:
        for k in item.keys():
            if k not in keys:
                keys.append(k)

    header = "|".join(keys)
    rows = "\n".join(
        "|".join(_toon_cell(item.get(k, "")) for k in keys) for item in data
    )
    toon = f"schema:{header}\n{rows}"

    if require_net_savings and estimate_tokens(toon, model) >= estimate_tokens(json_str, model):
        return None
    return toon


def _compact_json_to_toon(
    content: str, cfg: Optional[Dict[str, Any]] = None, model: str = ""
) -> str:
    """
    Convert every eligible JSON array-of-objects block in ``content`` to TOON.

    Eligibility and net-savings are config-gated; ineligible or would-inflate blocks
    are left as JSON (the "JSON fallback").  Backwards-compatible: with no cfg the
    defaults reproduce the legacy strict-uniform, single-block behaviour — except the
    scan now covers every array in the message, not just the first.
    """
    cfg = cfg or {}
    min_rows = int(cfg.get("toon_min_rows", 2))
    uniform_threshold = float(cfg.get("toon_uniform_threshold", 1.0))
    allow_nested = bool(cfg.get("toon_allow_nested", False))
    require_net_savings = bool(cfg.get("toon_require_net_savings", True))
    max_block_chars = int(cfg.get("toon_max_block_chars", _DEFAULT_TOON_MAX_BLOCK_CHARS))

    pattern = _array_of_objects_pattern(max_block_chars)
    result = content
    for match in pattern.finditer(content):
        json_str = match.group(0)
        toon = _encode_block_to_toon(
            json_str,
            min_rows=min_rows,
            uniform_threshold=uniform_threshold,
            allow_nested=allow_nested,
            require_net_savings=require_net_savings,
            model=model,
        )
        if toon is not None and toon != json_str:
            result = result.replace(json_str, toon, 1)
    return result


async def start_batch_consumer(cfg: Dict[str, Any]) -> None:
    """Background coroutine: consume Redis Streams and flush batches."""
    batch_cfg = cfg.get("groups", {}).get("G13_batch", {})
    if not batch_cfg.get("enabled", False):
        return

    topics = batch_cfg.get("batch_topics", [])
    if not topics:
        logger.info("G13 no batch_topics configured — consumer not started")
        return

    group = batch_cfg.get("consumer_group", "proxy-batch-consumers")
    consumer = batch_cfg.get("consumer_name", f"proxy-{os.getpid()}")
    max_batch = batch_cfg.get("max_batch_size", 50)
    flush_ms = batch_cfg.get("flush_interval_ms", 500)

    redis = _get_redis()

    # Ensure consumer groups exist
    for topic in topics:
        stream = f"{_BATCH_STREAM_PREFIX}:{topic}"
        try:
            await redis.xgroup_create(stream, group, id="0", mkstream=True)
            logger.info("G13 created consumer group for stream '%s'", stream)
        except Exception:
            pass  # Group already exists

    logger.info("G13 batch consumer started for topics: %s", topics)

    pel_check_interval = batch_cfg.get("max_pending_ack_ms", 30000) / 1000
    last_pel_check = time.time()

    while True:
        try:
            streams = {f"{_BATCH_STREAM_PREFIX}:{t}": ">" for t in topics}
            entries = await redis.xreadgroup(
                group, consumer, streams,
                count=max_batch, block=flush_ms
            )

            if entries:
                # Group entries by topic
                topic_batches: Dict[str, List[Tuple[str, Dict]]] = {}
                for stream_name, messages in entries:
                    topic = stream_name.split(":")[-1]
                    topic_batches.setdefault(topic, [])
                    for msg_id, fields in messages:
                        payload = json.loads(fields.get("payload", "{}"))
                        topic_batches[topic].append((msg_id, payload))

                # Flush each topic batch
                for topic, items in topic_batches.items():
                    stream = f"{_BATCH_STREAM_PREFIX}:{topic}"
                    msg_ids = [item[0] for item in items]
                    payloads = [item[1] for item in items]
                    try:
                        await _flush_batch(topic, payloads, cfg)
                        if msg_ids:
                            await redis.xack(stream, group, *msg_ids)
                    except Exception as exc:
                        logger.error("G13 flush failed for topic '%s': %s", topic, exc)
                        # Items remain in PEL — will be reclaimed below

            # Periodically reclaim timed-out PEL entries
            if time.time() - last_pel_check >= pel_check_interval:
                last_pel_check = time.time()
                for topic in topics:
                    await _reclaim_stale_pel(redis, topic, group, consumer, batch_cfg)

        except Exception as exc:
            logger.error("G13 consumer loop error: %s", exc)
            await asyncio.sleep(1)


async def _accumulate(ctx: RequestContext, topic: str) -> None:
    """Push request payload to the Redis Stream for this topic."""
    redis = _get_redis()
    stream = f"{_BATCH_STREAM_PREFIX}:{topic}"
    payload = json.dumps({
        "request_id": ctx.request_id,
        "tenant_id": getattr(ctx, "tenant_id", "default"),  # BYOK: stamp so the background
        "messages": ctx.messages,                            # flush resolves the tenant's key
        "params": ctx.params,
        "model": ctx.model,
        "timestamp": time.time(),
        # So the /v1/batch/results poller can attach x-tokenlean-* savings headers on
        # completion (it has no RequestContext to read ctx.savings from at poll time).
        "baseline_tokens": getattr(ctx.savings, "baseline_tokens", 0),
    })
    try:
        await redis.xadd(stream, {"payload": payload})
        logger.debug("[%s] G13 pushed to stream '%s'", ctx.request_id, stream)
    except Exception as exc:
        logger.warning("G13 Redis XADD failed: %s", exc)


_RESULT_TTL = int(os.getenv("BATCH_RESULT_TTL_SECONDS", "3600"))
_RESULT_KEY_PREFIX = "tok_opt:batch_result"
_OWNER_KEY_PREFIX = "tok_opt:batch_owner"


async def _store_batch_result(request_id: str, result: Dict) -> None:
    key = f"{_RESULT_KEY_PREFIX}:{request_id}"
    redis = _get_redis()
    try:
        await redis.set(key, json.dumps(result), ex=_RESULT_TTL)
    except Exception as exc:
        logger.warning("G13 result store failed for %s: %s", request_id, exc)


async def _record_batch_owner(request_id: str, tenant_id: str) -> None:
    """Record which tenant owns a deferred batch request (H1 isolation)."""
    try:
        redis = _get_redis()
        await redis.set(f"{_OWNER_KEY_PREFIX}:{request_id}", tenant_id, ex=_RESULT_TTL)
    except Exception as exc:
        logger.debug("G13 owner record failed for %s: %s", request_id, exc)


async def get_batch_result_owner(request_id: str) -> Optional[str]:
    """Return the tenant that owns a deferred batch request, or None if unknown."""
    try:
        redis = _get_redis()
        return await redis.get(f"{_OWNER_KEY_PREFIX}:{request_id}")
    except Exception as exc:
        logger.debug("G13 owner lookup failed for %s: %s", request_id, exc)
        return None


async def _reclaim_stale_pel(
    redis, topic: str, group: str, consumer: str, batch_cfg: Dict
) -> None:
    """Claim messages stuck in PEL beyond max_pending_ack_ms and retry once."""
    stream = f"{_BATCH_STREAM_PREFIX}:{topic}"
    min_idle_ms = batch_cfg.get("max_pending_ack_ms", 30000)
    try:
        claimed = await redis.xautoclaim(
            stream, group, consumer,
            min_idle_time=min_idle_ms,
            start_id="0-0",
            count=10,
        )
        # claimed[1] contains the reclaimed messages
        reclaimed_messages = claimed[1] if isinstance(claimed, (list, tuple)) and len(claimed) > 1 else []
        if reclaimed_messages:
            logger.warning(
                "G13 PEL reclaim: %d stale messages on stream '%s'",
                len(reclaimed_messages), stream,
            )
            for msg_id, fields in reclaimed_messages:
                payload = json.loads(fields.get("payload", "{}"))
                request_id = payload.get("request_id", "unknown")
                # Mark as failed so poller gets a response
                await _store_batch_result(request_id, {
                    "status": "failed",
                    "request_id": request_id,
                    "error": "Batch item timed out in processing queue",
                })
                await redis.xack(stream, group, msg_id)
    except Exception as exc:
        logger.warning("G13 PEL reclaim failed for topic '%s': %s", topic, exc)


_BATCH_JOBS_KEY = "tok_opt:batch_jobs"

# Providers whose native batch submit failed this process → skip straight to the
# per-item loop instead of re-attempting every flush. Cleared on restart.
_NATIVE_BATCH_UNSUPPORTED: set = set()


async def _record_batch_job(job_id: str, provider: str, items: List[Dict]) -> None:
    """Persist an outstanding native-batch job so the background poller can finish it.

    Carries each item's baseline_tokens (keyed by request_id) alongside the job so
    ``poll_batch_jobs`` can pass it through to ``_store_batch_result`` on completion —
    the same attribution the per-item loop lane (``_flush_batch_loop``) stores, so
    ``/v1/batch/results`` can emit x-tokenlean-* headers for native-batch requests too."""
    redis = _get_redis()
    request_ids = [it.get("request_id") for it in items]
    baseline_tokens = {it.get("request_id"): it.get("baseline_tokens", 0) for it in items}
    try:
        await redis.hset(
            _BATCH_JOBS_KEY,
            job_id,
            json.dumps({
                "provider": provider,
                "request_ids": request_ids,
                "baseline_tokens": baseline_tokens,
                "created": time.time(),
            }),
        )
    except Exception as exc:
        logger.warning("G13 record batch job %s failed: %s", job_id, exc)


async def poll_batch_jobs(cfg: Dict[str, Any]) -> int:
    """Poll outstanding native-batch jobs once. On completion, store each result by
    request_id (so the existing /v1/batch/results/{id} poller serves it) and drop the
    job. Returns the number of jobs that finished (completed or failed) this pass.
    """
    from providers import get_adapter_by_name
    from auth.api_key_manager import get_llm_provider_key

    redis = _get_redis()
    try:
        jobs = await redis.hgetall(_BATCH_JOBS_KEY)
    except Exception as exc:
        logger.warning("G13 poll: hgetall failed: %s", exc)
        return 0

    finished = 0
    for raw_id, raw_meta in (jobs or {}).items():
        job_id = raw_id.decode() if isinstance(raw_id, bytes) else raw_id
        raw_meta = raw_meta.decode() if isinstance(raw_meta, bytes) else raw_meta
        try:
            meta = json.loads(raw_meta)
        except Exception:
            await redis.hdel(_BATCH_JOBS_KEY, job_id)
            continue

        provider = meta.get("provider", "")
        request_ids = meta.get("request_ids", [])
        baseline_map = meta.get("baseline_tokens", {}) or {}
        try:
            adapter = get_adapter_by_name(provider)
        except Exception:
            await redis.hdel(_BATCH_JOBS_KEY, job_id)
            continue
        api_key = get_llm_provider_key(provider)
        if not api_key:
            continue  # try again next pass

        try:
            status = await adapter.poll_batch(job_id, api_key)
        except Exception as exc:
            logger.warning("G13 poll_batch failed job=%s: %s", job_id, exc)
            continue

        if status == "pending":
            continue

        if status == "completed":
            try:
                results = await adapter.fetch_batch_results(job_id, api_key)
            except Exception as exc:
                logger.error("G13 fetch_batch_results failed job=%s: %s", job_id, exc)
                continue
            seen = set()
            for r in results:
                rid = r.get("request_id")
                if not rid:
                    continue
                seen.add(rid)
                if "response" in r:
                    resp = dict(r["response"])
                    resp["_batch_request_id"] = rid
                    await _store_batch_result(rid, {
                        "status": "completed",
                        "response": resp,
                        "baseline_tokens": baseline_map.get(rid, 0),
                    })
                else:
                    await _store_batch_result(
                        rid, {"status": "failed", "error": r.get("error", "batch item error")}
                    )
            for rid in request_ids:
                if rid not in seen:
                    await _store_batch_result(
                        rid, {"status": "failed", "error": "missing from batch results"}
                    )
        else:  # failed / expired / cancelled
            for rid in request_ids:
                await _store_batch_result(rid, {"status": "failed", "error": f"batch job {status}"})

        await redis.hdel(_BATCH_JOBS_KEY, job_id)
        finished += 1

    return finished


async def start_batch_poller(cfg: Dict[str, Any]) -> None:
    """Background coroutine: poll native-batch jobs until they finish.

    No-op unless G13 is enabled AND provider_native is on, so it adds nothing to
    deployments using the per-item loop.
    """
    batch_cfg = cfg.get("groups", {}).get("G13_batch", {})
    if not batch_cfg.get("enabled", False) or not batch_cfg.get("provider_native", False):
        return
    interval = batch_cfg.get("poll_interval_seconds", 30)
    logger.info("G13 native-batch poller started (interval=%ss)", interval)
    while True:
        try:
            await poll_batch_jobs(cfg)
        except Exception as exc:
            logger.error("G13 batch poller error: %s", exc)
        await asyncio.sleep(interval)


async def _flush_batch(
    topic: str, items: List[Dict], cfg: Dict[str, Any]
) -> None:
    """Dispatch a flushed batch to the provider-native lane (50% discount) when
    enabled, else the per-item sync loop. Native-incapable providers and submit
    failures fall back to the loop, so behaviour is unchanged when disabled."""
    batch_cfg = cfg.get("groups", {}).get("G13_batch", {})
    if batch_cfg.get("provider_native", False):
        await _flush_batch_native(topic, items, cfg)
    else:
        await _flush_batch_loop(topic, items, cfg)


async def _flush_batch_native(
    topic: str, items: List[Dict], cfg: Dict[str, Any]
) -> None:
    """Group items by provider and submit a provider-native batch job per group.

    Records each job for the background poller. Any provider without native batch
    support, a missing key, or a submit error falls back to the per-item loop.

    BYOK v1 LIMITATION: the native lane submits with the PLATFORM key (get_llm_provider_key),
    because a provider-native batch aggregates many requests into one job. Do NOT enable
    ``provider_native`` together with strict BYOK — tenant batches would bill the platform
    account. The default per-item flush lane (``_flush_batch_loop``) IS tenant-key aware.
    Per-(provider, tenant) native grouping is a documented v2 follow-up.
    """
    from providers import get_adapter
    from auth.api_key_manager import get_llm_provider_key

    providers_config = cfg.get("providers", [])
    batch_cfg = cfg.get("groups", {}).get("G13_batch", {})
    default_model = batch_cfg.get("default_model", "gpt-4o-mini")

    groups: Dict[str, List[Dict]] = {}
    adapters: Dict[str, Any] = {}
    for item in items:
        model = item.get("model", default_model)
        adapter = get_adapter(model, providers_config)
        groups.setdefault(adapter.name, []).append(item)
        adapters[adapter.name] = adapter

    for pname, group_items in groups.items():
        adapter = adapters[pname]
        if not adapter.supports_native_batch() or pname in _NATIVE_BATCH_UNSUPPORTED:
            await _flush_batch_loop(topic, group_items, cfg)
            continue
        api_key = get_llm_provider_key(pname)
        if not api_key:
            logger.warning("G13 native batch: key unavailable for %s — using loop", pname)
            await _flush_batch_loop(topic, group_items, cfg)
            continue
        try:
            job_id = await adapter.submit_batch(group_items, api_key, batch_cfg)
            await _record_batch_job(job_id, pname, group_items)
            logger.info(
                "G13 native batch submitted provider=%s job=%s items=%d", pname, job_id, len(group_items)
            )
        except Exception as exc:
            # Memoise so we don't re-attempt native every flush for an
            # unsupported provider / misconfig (cleared on restart).
            _NATIVE_BATCH_UNSUPPORTED.add(pname)
            logger.warning(
                "G13 native batch unavailable for %s (%s) — using loop; retry on restart", pname, exc
            )
            await _flush_batch_loop(topic, group_items, cfg)


async def _flush_batch_loop(
    topic: str, items: List[Dict], cfg: Dict[str, Any]
) -> None:
    logger.info("G13 flushing batch (loop) topic='%s' size=%d", topic, len(items))
    try:
        import litellm
        from auth.api_key_manager import get_llm_provider_key
        from config_loader import get_provider_model_prefixes, get_providers
        from providers import build_litellm_call

        provider_map = get_provider_model_prefixes()

        for item in items:
            request_id = item.get("request_id")
            messages = item.get("messages", [])
            params = item.get("params", {})
            model = item.get("model", cfg.get("default_model", "gpt-4o-mini"))
            baseline_tokens = item.get("baseline_tokens", 0)

            # Resolve provider
            model_lower = model.lower()
            provider = ""
            for fragment, p in provider_map.items():
                if fragment in model_lower:
                    provider = p
                    break
            if not provider:
                from config_loader import get_default_provider
                provider = get_default_provider()

            # BYOK: resolve the batched request's key for ITS tenant (stamped at accumulate).
            tenant_id = item.get("tenant_id", "default")
            try:
                from providers.key_resolver import resolve_provider_key, ProviderKeyError
                provider_key = await resolve_provider_key(provider, tenant_id, None)
            except ProviderKeyError as _pke:
                await _store_batch_result(
                    request_id, {"status": "failed", "error": _pke.public_message}
                )
                continue
            if not provider_key:
                logger.warning(
                    "G13 batch item %s: provider key unavailable for %s",
                    request_id, provider,
                )
                await _store_batch_result(
                    request_id,
                    {"status": "failed", "error": f"provider key unavailable for {provider}"},
                )
                continue

            try:
                _call_model, _call_kwargs = build_litellm_call(model, get_providers(), provider_key)
                response = await litellm.acompletion(
                    model=_call_model,
                    messages=messages,
                    **_call_kwargs,
                    **{
                        k: v for k, v in params.items()
                        if not k.startswith("_") and not k.startswith("x_") and k != "model"
                    },
                )
                response_dict = (
                    response.model_dump()
                    if hasattr(response, "model_dump")
                    else dict(response)
                )
                response_dict["_batch_request_id"] = request_id
                await _store_batch_result(request_id, {
                    "status": "completed",
                    "response": response_dict,
                    "baseline_tokens": baseline_tokens,
                })
                logger.info("G13 batch item %s processed successfully", request_id)
                _note_batch_provider_outcome(provider, None)
            except Exception as exc:
                await _store_batch_result(request_id, {"status": "failed", "error": str(exc)})
                logger.error("G13 batch item %s failed: %s", request_id, exc)
                _note_batch_provider_outcome(provider, exc)
    except Exception as exc:
        logger.error("G13 batch flush failed: %s", exc)


def _note_batch_provider_outcome(provider: str, exc) -> None:
    """Feed a batch item's provider outcome into the circuit breaker (observation
    only — the per-item loop keeps its own failure handling and is never gated).
    Review K7: without this the breaker was blind to batch traffic."""
    try:
        from config_loader import get_config
        from providers.resilience import note_provider_outcome
        note_provider_outcome(provider, exc, get_config() or {})
    except Exception:  # never let observability break the flush
        pass
