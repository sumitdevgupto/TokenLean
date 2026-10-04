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
import socket
import time
import uuid
from collections import Counter
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

from middleware import RequestContext, resolve_group_config
from savings.calculator import count_messages_tokens, estimate_tokens

logger = logging.getLogger(__name__)
GROUP = "G13"

_BATCH_STREAM_PREFIX = os.getenv("BATCH_STREAM_PREFIX", "tok_opt:batch")
# Every stream a request was queued on, per topic (a set): the consumer reads it to find the
# tenants' streams. Written before the entry, so no entry sits on a stream nobody reads.
_BATCH_STREAMS_KEY = "tok_opt:batch_streams"


def _stream_key(prefix: str, topic: str) -> str:
    """A tenant's stream for a topic: its Redis prefix, then the topic. The default tenant's
    prefix is empty, so its stream is the key all tenants shared until 2026-10-02, and what
    was queued there still drains."""
    return f"{prefix}{_BATCH_STREAM_PREFIX}:{topic}"

# Topics this process's batch consumer reads (start_batch_consumer). A request is deferred
# only onto one of these: on a topic nobody reads it would get a 202, be billed, and never
# be answered.
_CONSUMED_TOPICS: set = set()
# Queued requests at which a topic stops taking more (G13_batch.max_backlog): further
# requests are answered now, so a flood cannot grow the stream without bound. The consumer
# deletes what it has processed, so the stream holds only the backlog.
_DEFAULT_MAX_BACKLOG = 10000

# A consumer idle this long with nothing pending belongs to a stopped process: a live one
# reads every flush interval. Each process has its own consumer name, so every restart adds
# one to the group, and the sweep deletes these.
_STOPPED_CONSUMER_IDLE_MS = 24 * 3600 * 1000

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
        if batch_topic and _tool_bearing(ctx):
            # A batched request is answered out-of-band by the flush worker and its result
            # is handed to the caller by /v1/batch/results/{id} — neither path runs the
            # response pipeline, so G32 never sees the tool calls the model asks for. That
            # would make batching a silent bypass of a trust & safety gate whose whole
            # guarantee is that it runs before anything can auto-execute a tool.
            #
            # Refusing to batch is the honest resolution: batching exists for fire-and-
            # forget bulk prompts, and a request that can make the proxy ACT is not that.
            # The request still runs — just synchronously, through the full gated pipeline.
            logger.info(
                "[%s] G13 not batching a tool-bearing request (topic=%s) — batch results "
                "skip the response pipeline, which would bypass the G32 eligibility gate",
                ctx.request_id, batch_topic,
            )
            batch_topic = None
        if batch_topic:
            # The same reasoning for the response-side trust & safety steps G29 and G30
            # take: a batched result would reach the caller without them.
            skipped = _response_safety_needed(ctx)
            if skipped:
                logger.info("[%s] G13 not batching (topic=%s): %s, which batch results skip",
                            ctx.request_id, batch_topic, skipped)
                batch_topic = None
        if batch_topic and batch_topic not in _CONSUMED_TOPICS:
            logger.info("[%s] G13 not batching: no consumer reads topic %r (list it in "
                        "G13_batch.batch_topics); answering the request now",
                        ctx.request_id, batch_topic)
            batch_topic = None
        max_backlog = cfg.get("max_backlog", _DEFAULT_MAX_BACKLOG)
        # H1: /v1/batch/results/{id} serves a result only to the tenant recorded as its
        # owner, so the owner goes on record BEFORE the request is queued (a result can never
        # be stored without one) and a request whose owner cannot be recorded is answered
        # now: its result would be served to nobody.
        if batch_topic and await _record_batch_owner(ctx.request_id,
                                                     getattr(ctx, "tenant_id", "default")):
            if await _accumulate(ctx, batch_topic, max_backlog=max_backlog):
                ctx.batch_deferred = True  # response will be delivered async
            else:
                await _forget_batch_owner(ctx.request_id)

        return ctx


def _response_safety_needed(ctx: RequestContext) -> Optional[str]:
    """Why this request's RESPONSE needs a trust & safety step that batching would skip,
    or None. Batch results bypass the response pipeline, as for the G32 case above.

    G29 in ``mask`` mode masks PII the model writes and restores the caller's own masked
    values; G30 with ``scan_response`` checks (and may withhold) the model's output. Both
    are served synchronously. G29's default ``flag`` mode only detects and records, and a
    batched output is not scanned for it: the same caveat as streaming.
    """
    from middleware import coerce_mode, resolve_group_config
    from middleware.g29_pii_redaction import _VALID_MODES as _G29_MODES
    g29 = resolve_group_config(ctx, "G29_pii_redaction")
    if g29.get("enabled", True) and coerce_mode(g29.get("mode"), _G29_MODES, "flag") == "mask":
        return "G29 masks the response"
    g30 = resolve_group_config(ctx, "G30_guardrails")
    if g30.get("enabled", True) and g30.get("scan_response", False):
        return "G30 scans the response"
    return None


def _tool_bearing(ctx: RequestContext) -> bool:
    """True when this request could come back with tool calls.

    Checked against the request rather than the response because the decision has to be
    made BEFORE the request is deferred. `tools` is the OpenAI field; `functions` is its
    deprecated predecessor, still accepted by providers, so both count.
    """
    params = getattr(ctx, "params", None) or {}
    return bool(params.get("tools") or params.get("functions"))


def _resolve_toon_cfg(ctx: RequestContext) -> Dict[str, Any]:
    """G13_batch config with the per-tenant override merged in (tenant wins).

    Delegates to the shared ``middleware.resolve_group_config`` so every group resolves
    the tenant overlay identically. Two things change here: the merge is now DEEP (this
    was the only group shallow-merging, so a tenant overriding one key of a nested block
    silently dropped its siblings), and every level is type-guarded — ``config.yaml`` is
    operator-edited, and an unguarded ``.get()`` chain over a mis-indented ``tenants:``
    block raised ``AttributeError`` here, 500-ing every request for that tenant.
    """
    return resolve_group_config(ctx, "G13_batch")


async def _apply_toon(
    ctx: RequestContext, tokens_before: int
) -> Tuple[bool, RequestContext]:
    """
    Detect arrays of uniform objects in user messages and compress them to TOON.

    Gating is config-driven:
      • ``toon_auto_detect`` false (default): only runs when a system message carries the
        TOON marker, a line written in the notation itself (``schema:name|age``). Any
        prompt that mentioned "schema" and held a "|", as a markdown table does, used to
        switch it on.
      • ``toon_auto_detect`` true: runs on every request, relying on the per-block
        eligibility + net-savings gates so non-tabular / nested / would-inflate data
        is left untouched as JSON (the "JSON fallback").
    """
    cfg = _resolve_toon_cfg(ctx)

    if not cfg.get("toon_auto_detect", False):
        if not any(_has_toon_marker(msg.get("content"))
                   for msg in ctx.messages if msg.get("role") == "system"):
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


_TOON_MARKER_RE = re.compile(r"^[ \t]*schema:[^\n|]*\|", re.MULTILINE)


def _has_toon_marker(content: Any) -> bool:
    """Whether a system message's text (or one of its text parts) carries the TOON marker."""
    if isinstance(content, str):
        return bool(_TOON_MARKER_RE.search(content))
    if isinstance(content, list):
        return any(isinstance(part, dict) and part.get("type") == "text"
                   and isinstance(part.get("text"), str) and _TOON_MARKER_RE.search(part["text"])
                   for part in content)
    return False


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
    """Render a JSON value as a TOON cell. A key the row lacks is the empty cell, so null is
    written ``null`` and an empty string ``""``; a string that would read as either (or that
    starts with a quote) and a nested value are written as JSON."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        if value in ("", "null") or value.startswith('"'):
            return json.dumps(value, ensure_ascii=False)
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def _toon_safe(text: str) -> bool:
    """Whether ``text`` can be a cell or column name: the delimiter or a line break in one
    would shift the columns or split the row."""
    return not any(ch in text for ch in ("|", "\n", "\r"))


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

    # Header = union of keys (first-seen order) so near-uniform rows stay lossless; a key
    # a row lacks is its empty cell.
    keys: List[str] = []
    for item in data:
        for k in item.keys():
            if k not in keys:
                keys.append(k)
    cells = [[_toon_cell(item[k]) if k in item else "" for k in keys] for item in data]
    if not all(_toon_safe(k) for k in keys) or not all(
            _toon_safe(cell) for row in cells for cell in row):
        return None

    header = "|".join(keys)
    rows = "\n".join("|".join(row) for row in cells)
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


def _default_consumer_name() -> str:
    """This process's own consumer name. Instances sharing one name are a single consumer
    to Redis, so nothing could tell which of them held an unacknowledged entry. The random
    part keeps two containers apart even where hostname and pid repeat."""
    return f"proxy-{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"


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
    consumer = batch_cfg.get("consumer_name") or _default_consumer_name()
    max_batch = batch_cfg.get("max_batch_size", 50)
    flush_ms = batch_cfg.get("flush_interval_ms", 500)
    stale_ms = int(batch_cfg.get("max_pending_ack_ms", 30000))

    redis = _get_redis()
    grouped: set = set()

    async def _ensure_group(stream: str) -> None:
        try:
            await redis.xgroup_create(stream, group, id="0", mkstream=True)
            logger.info("G13 created consumer group for stream '%s'", stream)
        except Exception as exc:
            logger.debug("G13 consumer group on '%s' not created (it exists?): %s", stream, exc)
        grouped.add(stream)

    async def _streams() -> List[str]:
        """Every stream to read: each topic's default-tenant stream, the tenant streams
        registered for it, and any already read (a failed registry read drops none)."""
        found = set(grouped) | {_stream_key("", topic) for topic in topics}
        for topic in topics:
            try:
                found.update(await redis.smembers(f"{_BATCH_STREAMS_KEY}:{topic}") or ())
            except Exception as exc:
                logger.debug("G13 could not read the streams registered for '%s': %s", topic, exc)
        for stream in sorted(found - grouped):
            await _ensure_group(stream)
        return sorted(found)

    for topic in topics:                 # the default tenant's streams, before any request
        await _ensure_group(_stream_key("", topic))

    _CONSUMED_TOPICS.update(topics)
    logger.info("G13 batch consumer started for topics: %s", topics)

    pel_check_interval = stale_ms / 1000
    last_pel_check = time.time()

    while True:
        try:
            streams = await _streams()
            entries = await redis.xreadgroup(
                group, consumer, {stream: ">" for stream in streams},
                count=max_batch, block=flush_ms
            )

            if entries:
                # Group entries by stream: each is one tenant's queue for one topic
                stream_batches: Dict[str, List[Tuple[str, Dict]]] = {}
                for stream_name, messages in entries:
                    for msg_id, fields in messages:
                        payload = json.loads(fields.get("payload", "{}"))
                        stream_batches.setdefault(stream_name, []).append((msg_id, payload))

                for stream, items in stream_batches.items():
                    topic = stream.rsplit(":", 1)[-1]
                    try:
                        await _flush_held(redis, stream, group, consumer, topic, items, cfg,
                                          stale_ms)
                    except Exception as exc:
                        logger.error("G13 flush failed for stream '%s': %s", stream, exc)
                        # Left pending: the sweep retries them once nobody holds them

            # Periodically retry what a stopped consumer left pending
            if time.time() - last_pel_check >= pel_check_interval:
                last_pel_check = time.time()
                for stream in streams:
                    await _reclaim_stale_pel(redis, stream.rsplit(":", 1)[-1], group, consumer,
                                             batch_cfg, cfg, stream=stream)

        except Exception as exc:
            logger.error("G13 consumer loop error: %s", exc)
            await asyncio.sleep(1)


async def _accumulate(ctx: RequestContext, topic: str,
                      max_backlog: int = _DEFAULT_MAX_BACKLOG) -> bool:
    """Push request payload to the Redis Stream for this topic. True only when it was
    queued: on a failed write, or a stream already holding ``max_backlog`` requests, the
    caller answers the request now instead of promising a result nothing will produce."""
    from middleware.g00_rate_limit import counter_prefix
    redis = _get_redis()
    stream = _stream_key(getattr(ctx, "redis_prefix", "") or "", topic)
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
        # The tenant's spend counter, which the consumer adds this request's cost to when its
        # answer arrives (main bills it, and counts it against the quota and trial, now).
        # Empty when an admin key sent it as the tenant: that spends nothing of the tenant's.
        "spend_prefix": "" if getattr(ctx, "impersonator_tenant_id", None) else counter_prefix(ctx),
    })
    try:
        if max_backlog and await redis.xlen(stream) >= max_backlog:
            logger.warning("[%s] G13 stream '%s' already holds %d queued requests; "
                           "answering this one now", ctx.request_id, stream, max_backlog)
            return False
        await redis.sadd(f"{_BATCH_STREAMS_KEY}:{topic}", stream)
        await redis.xadd(stream, {"payload": payload})
        logger.debug("[%s] G13 pushed to stream '%s'", ctx.request_id, stream)
        return True
    except Exception as exc:
        logger.warning("[%s] G13 could not queue the request (%s); answering it now",
                       ctx.request_id, exc)
        return False


async def _forget(redis, stream: str, msg_ids) -> None:
    """Delete stream entries once acknowledged, so the stream holds only the backlog (the
    length the ``max_backlog`` check reads). Best effort: a failure only delays that."""
    try:
        await redis.xdel(stream, *msg_ids)
    except Exception as exc:
        logger.debug("G13 could not delete processed entries from '%s': %s", stream, exc)


async def _flush_held(redis, stream: str, group: str, consumer: str, topic: str,
                      entries: List[Tuple[str, Dict]], cfg: Dict[str, Any],
                      stale_ms: int) -> None:
    """Flush ``entries`` (stream id, payload) while holding them, then acknowledge them.

    A flush makes up to ``max_batch_size`` provider calls in turn, so it can outlast
    ``max_pending_ack_ms``, the idle time after which any consumer's sweep takes an entry
    over. So this consumer re-claims its entries every third of that time while it flushes:
    an entry goes stale only once nobody is working on it. An error leaves the entries
    pending, for the sweep to retry.
    """
    msg_ids = [msg_id for msg_id, _ in entries]
    holder = asyncio.create_task(
        _hold(redis, stream, group, consumer, msg_ids, stale_ms / 3000))
    try:
        await _flush_batch(topic, [payload for _, payload in entries], cfg)
    finally:
        holder.cancel()
        await asyncio.gather(holder, return_exceptions=True)
    if msg_ids:
        await redis.xack(stream, group, *msg_ids)
        await _forget(redis, stream, msg_ids)


async def _hold(redis, stream: str, group: str, consumer: str, msg_ids: List[str],
                interval_s: float) -> None:
    """Reset the idle time of ``msg_ids`` every ``interval_s`` until cancelled. JUSTID
    leaves their delivery count alone: holding an entry is not delivering it again."""
    while True:
        await asyncio.sleep(interval_s)
        try:
            await redis.xclaim(stream, group, consumer, 0, msg_ids, justid=True)
        except Exception as exc:
            logger.debug("G13 could not hold entries on '%s': %s", stream, exc)


_RESULT_TTL = int(os.getenv("BATCH_RESULT_TTL_SECONDS", "3600"))
_RESULT_KEY_PREFIX = "tok_opt:batch_result"
_OWNER_KEY_PREFIX = "tok_opt:batch_owner"
# Recorded when the request is deferred, the owner must outlast its whole wait as well as its
# result: a provider batch has a 24h completion window, and a backlog adds to that. A result
# stored later still extends it (_keep_owner_for_result).
_OWNER_TTL = 3 * 86400 + _RESULT_TTL


# Write a result unless the key already holds a completed answer: one step on the server,
# so no completed write can land between the check and the write.
_UNLESS_COMPLETED = """
local stored = redis.call('GET', KEYS[1])
if stored then
  local ok, doc = pcall(cjson.decode, stored)
  if ok and type(doc) == 'table' and doc['status'] == 'completed' then return 0 end
end
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
return 1
"""


async def _store_batch_result(request_id: str, result: Dict) -> None:
    """Store a batched request's result for its poller. A completed answer always wins: any
    other status never replaces one, so a late failure (a retry that gave up, a stale
    sweep) cannot hide an answer that was produced and priced."""
    key = f"{_RESULT_KEY_PREFIX}:{request_id}"
    redis = _get_redis()
    try:
        if result.get("status") == "completed":
            await redis.set(key, json.dumps(result), ex=_RESULT_TTL)
        else:
            await redis.eval(_UNLESS_COMPLETED, 1, key, json.dumps(result), _RESULT_TTL)
    except Exception as exc:
        logger.warning("G13 result store failed for %s: %s", request_id, exc)
        return
    await _keep_owner_for_result(redis, request_id)


async def _keep_owner_for_result(redis, request_id: str) -> None:
    """Keep the owner record at least as long as the result just stored: its poller is
    refused once the owner is gone. Never shortened, and never recreated (the owner is not
    known here)."""
    owner_key = f"{_OWNER_KEY_PREFIX}:{request_id}"
    try:
        if 0 <= await redis.ttl(owner_key) < _RESULT_TTL:
            await redis.expire(owner_key, _RESULT_TTL)
    except Exception as exc:
        logger.warning("G13 could not extend the owner record of %s: %s", request_id, exc)


async def _completed(request_id: str) -> bool:
    """Whether a completed answer is stored for ``request_id``."""
    try:
        stored = await _get_redis().get(f"{_RESULT_KEY_PREFIX}:{request_id}")
        return bool(stored) and json.loads(stored).get("status") == "completed"
    except Exception:
        return False


async def _record_batch_owner(request_id: str, tenant_id: str) -> bool:
    """Record which tenant owns a deferred batch request (H1 isolation), for longer than
    the request can wait. False when it could not be written."""
    try:
        redis = _get_redis()
        await redis.set(f"{_OWNER_KEY_PREFIX}:{request_id}", tenant_id, ex=_OWNER_TTL)
        return True
    except Exception as exc:
        logger.warning("[%s] G13 could not record the request's owner (%s); answering it now",
                       request_id, exc)
        return False


async def _forget_batch_owner(request_id: str) -> None:
    """Take back the owner record of a request that was not queued after all, so its id
    polls as not found rather than pending forever. Best effort: the record expires anyway."""
    try:
        await _get_redis().delete(f"{_OWNER_KEY_PREFIX}:{request_id}")
    except Exception as exc:
        logger.debug("G13 could not remove the owner record of %s: %s", request_id, exc)


async def get_batch_result_owner(request_id: str) -> Optional[str]:
    """Return the tenant that owns a deferred batch request, or None when none is on record.
    A failed read raises: the caller must not mistake it for "no owner"."""
    return await _get_redis().get(f"{_OWNER_KEY_PREFIX}:{request_id}")


async def _deliveries(redis, stream: str, group: str, msg_id: str) -> int:
    """How many times ``msg_id`` has been delivered, the claim that just took it included;
    0 when it is no longer pending."""
    pending = await redis.xpending_range(stream, group, min=msg_id, max=msg_id, count=1)
    return int(pending[0]["times_delivered"]) if pending else 0


async def _reclaim_stale_pel(
    redis, topic: str, group: str, consumer: str, batch_cfg: Dict,
    cfg: Optional[Dict[str, Any]] = None, stream: Optional[str] = None,
) -> None:
    """Retry the entries a stopped consumer left pending.

    A consumer holds what it is flushing (``_flush_held``), so an entry idle for
    ``max_pending_ack_ms`` is one nobody is working on. It is claimed and flushed again, up
    to ``max_attempts`` deliveries in all, then marked failed so its poller gets an answer.
    One that already has a completed answer, or whose content is gone, is only
    acknowledged: nothing is left to do for it. ``stream`` is the tenant stream to sweep
    (default: the topic's default-tenant stream).
    """
    stream = stream or _stream_key("", topic)
    stale_ms = int(batch_cfg.get("max_pending_ack_ms", 30000))
    max_attempts = int(batch_cfg.get("max_attempts", 3))
    try:
        claimed = await redis.xautoclaim(
            stream, group, consumer,
            min_idle_time=stale_ms,
            start_id="0-0",
            count=10,
        )
        # claimed[1] contains the reclaimed messages
        reclaimed_messages = claimed[1] if isinstance(claimed, (list, tuple)) and len(claimed) > 1 else []
        retry: List[Tuple[str, Dict]] = []
        settled: List[str] = []
        for msg_id, fields in reclaimed_messages:
            if not fields or "payload" not in fields:
                settled.append(msg_id)
                continue
            payload = json.loads(fields["payload"])
            request_id = payload.get("request_id", "unknown")
            if await _completed(request_id):
                settled.append(msg_id)
                continue
            delivered = await _deliveries(redis, stream, group, msg_id)
            if not delivered:
                continue          # acknowledged by its own consumer since the claim
            if delivered > max_attempts:
                await _store_batch_result(request_id, {
                    "status": "failed",
                    "request_id": request_id,
                    "error": f"Batch item not processed after {max_attempts} attempts",
                })
                settled.append(msg_id)
            else:
                retry.append((msg_id, payload))
        if reclaimed_messages:
            logger.warning(
                "G13 sweep: %d entries on stream '%s' left by a stopped consumer, %d retried",
                len(reclaimed_messages), stream, len(retry),
            )
        if settled:
            await redis.xack(stream, group, *settled)
            await _forget(redis, stream, settled)
        if retry:
            await _flush_held(redis, stream, group, consumer, topic, retry, cfg or {}, stale_ms)
        await _drop_stopped_consumers(redis, stream, group, consumer)
    except Exception as exc:
        logger.warning("G13 PEL reclaim failed for topic '%s': %s", topic, exc)


async def _drop_stopped_consumers(redis, stream: str, group: str, consumer: str,
                                  idle_ms: int = _STOPPED_CONSUMER_IDLE_MS) -> None:
    """Delete the consumers of stopped processes: idle ``idle_ms`` with nothing pending.
    One with pending entries still has work for the sweep to take over, and deleting it
    would drop that work from the group, so it stays until then."""
    try:
        for info in await redis.xinfo_consumers(stream, group):
            name = info.get("name")
            if (name != consumer and int(info.get("pending", 0)) == 0
                    and int(info.get("idle", 0)) > idle_ms):
                await redis.xgroup_delconsumer(stream, group, name)
    except Exception as exc:
        logger.debug("G13 could not tidy the consumers of '%s': %s", stream, exc)


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
    # Whose spend counter each answer's cost goes to, and the model it is priced at.
    spend = {it.get("request_id"): {"prefix": it["spend_prefix"], "model": it.get("model")}
             for it in items if it.get("spend_prefix")}
    try:
        await redis.hset(
            _BATCH_JOBS_KEY,
            job_id,
            json.dumps({
                "provider": provider,
                "request_ids": request_ids,
                "baseline_tokens": baseline_tokens,
                "spend": spend,
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
        spend_map = meta.get("spend", {}) or {}
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
                    owed = spend_map.get(rid) or {}
                    await _add_answer_cost(owed.get("prefix"), rid, owed.get("model") or "",
                                           resp, adapter, cfg, native=True)
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
        from config_loader import get_provider_model_prefixes, get_providers
        from providers import (
            build_litellm_call, get_adapter, outgoing_messages_for, outgoing_params_for,
        )
        from providers.resilience import request_timeout_for

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
                # Tracker 23.44: route through the SAME provider param-hygiene +
                # reasoning-headroom-reservation seam every other call site uses
                # (main.py primary + failover, G06 cascade tiers) — see
                # providers.outgoing_params_for / reserve_reasoning_headroom. Without
                # this, a batched request to a reasoning model reached the provider
                # with a budget sized for the answer alone and could come back empty,
                # billed in full, with nothing able to detect it afterwards: the
                # result-serve endpoint (`/v1/batch/results`) has no RequestContext at
                # poll time. This background flush loop has no RequestContext either
                # (items are plain dicts read off a Redis Stream), so a minimal
                # duck-typed stand-in supplies exactly the two attributes the seam
                # reads (`params`, `tenant_id`) plus the `output_budget_raised` slot it
                # writes for disclosure (best-effort; the seam itself wraps that write
                # in try/except, so a plain object is sufficient) — it carries no
                # reservation logic of its own, all of which stays in the seam/adapter.
                _routed_adapter = get_adapter(model, get_providers())
                _batch_ctx = SimpleNamespace(
                    params=params, tenant_id=tenant_id, output_budget_raised=None
                )
                outgoing_params = outgoing_params_for(
                    _batch_ctx, _routed_adapter, model, cfg, request_id
                )
                response = await litellm.acompletion(
                    model=_call_model,
                    messages=outgoing_messages_for(_routed_adapter, messages),
                    **_call_kwargs,
                    **outgoing_params,
                    timeout=request_timeout_for(cfg, model),
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
                await _add_answer_cost(item.get("spend_prefix"), request_id, model,
                                       response_dict, _routed_adapter, cfg)
            except Exception as exc:
                await _store_batch_result(request_id, {"status": "failed", "error": str(exc)})
                logger.error("G13 batch item %s failed: %s", request_id, exc)
                _note_batch_provider_outcome(provider, exc)
    except Exception as exc:
        logger.error("G13 batch flush failed: %s", exc)


def _answer_cost(request_id: str, model: str, response: Dict, adapter: Any,
                 cfg: Dict[str, Any], *, native: bool) -> float:
    """What a batched request's answer cost, priced as G18 prices a served call
    (``price_billed_call``): from the usage the provider reported, so nothing when it
    reported none, at the configured native-batch discount on the native lane, and nothing
    while G18 prices no calls. The flush has no RequestContext, so a stand-in carries what
    pricing reads."""
    from datetime import datetime, timezone
    from middleware.g18_observability import price_billed_call
    from savings.models import SavingsRecord
    call = SimpleNamespace(
        request_id=request_id, config=cfg, routed_model=model, provider_adapter=adapter,
        params={"_native_batch": True} if native else {},
        savings=SavingsRecord(
            request_id=request_id, user_id="", timestamp=datetime.now(timezone.utc),
            model_requested=model, routed_model=model, baseline_tokens=0),
    )
    price_billed_call(call, response)
    return call.savings.cost_actual_usd


async def _add_answer_cost(spend_prefix: Optional[str], request_id: str, model: str,
                           response: Dict, adapter: Any, cfg: Dict[str, Any],
                           *, native: bool = False) -> None:
    """Add a batched request's answer cost to its tenant's spend counter, which the spend
    cap reads. The request was billed, and counted against the quota and trial, when it was
    queued; its cost is known only now. No ``spend_prefix``: an admin key sent it as the
    tenant, or it was queued without one. Never raises: the answer is stored either way."""
    if not spend_prefix:
        return
    try:
        from middleware.g00_rate_limit import add_to_spend
        await add_to_spend(spend_prefix, _answer_cost(request_id, model, response, adapter,
                                                      cfg, native=native))
    except Exception as exc:
        logger.warning("G13 batch item %s: its cost was not added to the spend counter: %s",
                       request_id, exc)


def _note_batch_provider_outcome(provider: str, exc) -> None:
    """Feed a batch item's provider outcome into the circuit breaker (observation
    only — the per-item loop keeps its own failure handling and is never gated).
    Review K7: without this the breaker was blind to batch traffic."""
    try:
        from config_loader import get_config
        from providers.resilience import note_provider_outcome
        note_provider_outcome(provider, exc, get_config() or {})
    except Exception as err:  # never let observability break the flush
        logger.debug("batch provider outcome not recorded: %r", err)
