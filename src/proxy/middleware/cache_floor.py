"""
Provider prefix-cache floor — the shared budget the shrinking groups honour.

Providers only cache a prompt prefix once it is large enough, and they decline in
SILENCE: no cache read, no cache write, no error, no signal of any kind. A prompt
compressed under that floor therefore sends fewer tokens and can still cost more,
because a large discount on a repeated prefix is forfeited (backlog #41, measured on
DS8 2026-09-04: 63% fewer tokens sent, 2.5x the input cost).

The naive fix — "stop compressing when it would cross the floor" — is itself a defect.
Whether holding tokens back is cheaper depends on the provider's own cache rates and on
how often the prefix actually repeats, and on the SAME workload the two can point in
opposite directions:

    span P=2044, floor F=1024, N=30 reuses, costs in full-rate input tokens

    read x0.10 / write x1.25   compress 22,080   preserve 8,483   floor-to-F  4,250
    read x0.50 / write x1.00   compress 19,140   preserve 31,682  floor-to-F 15,872
                                                 ^ preserving COSTS 1.66x here

So this module decides with arithmetic rather than a threshold, using the same
per-provider multipliers that already feed G18's cached-token cost credit.

Three arms, in units of input tokens billed at the full rate (the variable suffix is
billed identically in every arm and cancels out — only the cacheable span matters):

    A  compress freely, span ends under the floor, nothing is cached   N * P'
    B  preserve the span uncompressed                                  w*P + (N-1)*r*P
    C  compress the span DOWN TO the floor, not below                  w*F + (N-1)*r*F

C dominates B whenever F < P, which is always. At N=1 every floor loses, because the
first request pays a cache WRITE (w >= 1.0) on a bigger span — which is why N is
measured rather than assumed, and why this ships off.

Nothing here knows a provider's name. The floor, the multipliers and the definition of
"the span the provider measures its floor over" all come from the adapter (Gate 3).
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional, Sequence, Set

from savings.calculator import count_messages_tokens, count_tools_tokens

logger = logging.getLogger(__name__)

# Config lives on G01's block: the knob predates this module (it shipped 2026-09-04 as a
# whole-prompt, all-or-nothing guard) and renaming it would silently restore the old
# behaviour for anyone who had turned it on.
CONFIG_KEY = "G1_compression"

DEFAULT_MARGIN = 0.05
DEFAULT_ASSUMED_REUSE = 1
DEFAULT_REUSE_WINDOW_SECONDS = 300

# ctx.cache_floor_action values.
ACTION_NONE = "none"
ACTION_FLOORED = "floored"
ACTION_PRESERVED = "preserved"


@dataclass(frozen=True)
class CacheFloor:
    """The reservation, computed once per request before any group shrinks anything.

    ``active`` False is the inert state and means every consumer behaves exactly as it
    did before this module existed. It is the answer whenever anything is unknown —
    the knob is off, the provider publishes no floor, the span was never big enough to
    be cached, or the reuse count could not be established. Failing safe here is not a
    nicety: the alternative is holding tokens back for a discount that never arrives.
    """

    active: bool = False
    floor: int = 0
    span_tokens: int = 0
    read_mult: float = 1.0
    write_mult: float = 1.0
    reuse: int = 1
    margin: float = DEFAULT_MARGIN
    # Roles the provider's cacheable span covers. Roles alone are not enough: G07 and G10
    # inject retrieved documents and memories as `system`/`tool` messages AFTER the
    # reservation is taken, and those are per-query volatile content that was never part
    # of the prefix being protected. Counting them inflates the span and makes G19 refuse
    # to prune a retrieved chunk in order to defend a cache entry that cannot be hit
    # again anyway.
    span_roles: frozenset = field(default_factory=frozenset)
    # Content fingerprints of the messages the reservation was actually taken over. None
    # means "no snapshot" (role-only matching), which is the legacy behaviour and what a
    # hand-built fixture gets; `reserve()` always populates it.
    span_fingerprints: Optional[frozenset] = None
    # True when the span is the whole prompt (a provider that measures its floor over the
    # entire serialized prefix), in which case every RESERVED message is in span whatever
    # its role.
    span_is_whole_prompt: bool = False

    def covers(self, message: Dict[str, Any]) -> bool:
        """Is this message inside the span the provider measures against its floor?

        Snapshot ∩ roles. A message that was not present when the reservation was taken
        is outside it by construction, whatever its role — see ``span_fingerprints``.
        """
        if not self.active:
            return False
        if (self.span_fingerprints is not None
                and message_fingerprint(message) not in self.span_fingerprints):
            return False
        if self.span_is_whole_prompt:
            return True
        return message.get("role") in self.span_roles


_INERT = CacheFloor()


def message_fingerprint(message: Dict[str, Any]) -> str:
    """Stable identity for ONE message, used to tell a reserved message from one injected
    after the reservation was taken.

    Role plus content, so a message rewritten by a shrinking group does NOT match — which
    is why the shrinking consumers decide membership on the BEFORE list and count tokens
    on the AFTER list (:func:`span_tokens_paired`) rather than re-testing the rewrite.
    """
    content = message.get("content")
    h = hashlib.sha256()
    h.update(str(message.get("role", "")).encode("utf-8", "replace"))
    h.update(b"\x00")
    h.update((content if isinstance(content, str) else str(content)).encode("utf-8", "replace"))
    return h.hexdigest()


def get(ctx: Any) -> CacheFloor:
    """The reservation for this request, or the inert default when none was made."""
    floor = getattr(ctx, "cache_floor", None)
    return floor if isinstance(floor, CacheFloor) else _INERT


# ─── arithmetic ──────────────────────────────────────────────────────────────────

def compress_is_cheaper(
    span_before: int,
    span_after: int,
    floor: int,
    read_mult: float,
    write_mult: float,
    reuse: int,
) -> bool:
    """True when compressing the span freely (arm A) is the cheapest of the three arms.

    Pure function, no ctx, no config — so the whole economic decision is one testable
    expression rather than a rule of thumb spread across three middleware files.

    Arm A costs ``N * span_after``; the best cacheable alternative costs
    ``w*F + (N-1)*r*F`` (arm C, which is never worse than arm B since F <= span_before).
    Compressing wins when A is at least as cheap, and also whenever the compression did
    not actually cross the floor — there is nothing to protect then.
    """
    if floor <= 0 or reuse < 1:
        return True
    if span_before < floor:
        # The span was never cacheable. Compressing it forfeits nothing.
        return True
    if span_after >= floor:
        # Still cacheable after compression — the best possible outcome, take it.
        return True
    cacheable_cost = write_mult * floor + (reuse - 1) * read_mult * floor
    compressed_cost = reuse * span_after
    return compressed_cost <= cacheable_cost


def snapshot_is_stale(ctx: Any) -> bool:
    """True once :func:`allows_shrink` has found the reservation unmeasurable.

    Consumers need this for their RESTORE path, not just their decision: membership is
    tested with :meth:`CacheFloor.covers`, and a stale snapshot answers False to every
    message — so a group that refuses the shrink would then restore nothing and take it
    anyway. Stale means "we cannot tell which messages are in the span", and the only
    safe reading of that is "all of them".
    """
    return bool(getattr(ctx, "cache_floor_stale_snapshot", False))


def allows_shrink(ctx: Any, span_before: int, span_after: int, group: str = "") -> bool:
    """Consumer entry point: may this group take the span from ``span_before`` to
    ``span_after``? Inert (always True) unless a reservation is active.

    Carries the structural backstop for a STALE snapshot. A reservation is only ever
    taken when the span was already at or above the floor, so a live span that measures
    ZERO cannot be the truth — it means every message the snapshot named has been
    rewritten by some group that did not call :func:`resnapshot`. The arithmetic below
    would read that as "the span was never cacheable, nothing to protect" and permit any
    shrink: fail-OPEN, and silently, which is exactly how the G01→G19 handoff defect
    behaved. Refusing instead is fail-SAFE — it can cost tokens, never a forfeited
    discount, and the warning names the group so the missing `resnapshot` is findable
    rather than inferred from a cost that quietly fails to fall.

    Belt and braces on purpose: :func:`resnapshot` at every rewrite site is the fix, this
    is the backstop for the site nobody remembered to add.
    """
    fl = get(ctx)
    if not fl.active:
        return True
    if span_before <= 0 < fl.floor <= fl.span_tokens:
        if not snapshot_is_stale(ctx):
            ctx.cache_floor_stale_snapshot = True
            logger.warning(
                "[%s] cache-floor: the reserved span (%dt, floor %dt) now measures 0 by "
                "the time %s asked — a group rewrote the messages without calling "
                "cache_floor.resnapshot, so the span cannot be measured. Refusing the "
                "shrink rather than compressing a prefix that may already be at its "
                "floor.",
                getattr(ctx, "request_id", "-"), fl.span_tokens, fl.floor,
                group or "a later group",
            )
        return False
    return compress_is_cheaper(
        span_before, span_after, fl.floor, fl.read_mult, fl.write_mult, fl.reuse
    )


def target_rate(ctx: Any, shrinkable_tokens: int, fixed_tokens: int = 0) -> Optional[float]:
    """The keep-fraction that lands the span just ABOVE the floor.

    The compression sidecar takes ``ratio`` as a keep-fraction, so aiming at the floor is
    a single call at a milder rate rather than a search — this is what makes "compress
    down to the floor, not below" (arm C) affordable. ``margin`` buys headroom against
    token-count estimate error, since undershooting loses the whole discount.

    Only part of a span is usually shrinkable: a compressor rewrites the messages it
    chose as candidates and leaves the rest — and tool definitions — untouched.
    ``fixed_tokens`` is that untouched remainder, so the rate is derived from the subset
    it will actually be applied to. Deriving it from the whole span instead lands the
    result well ABOVE the target (safe, but it gives back tokens for nothing).
    """
    fl = get(ctx)
    if not fl.active or shrinkable_tokens <= 0 or fl.floor <= 0:
        return None
    target_total = fl.floor * (1.0 + fl.margin)
    target_shrinkable = target_total - max(0, fixed_tokens)
    if target_shrinkable <= 0:
        # The untouched remainder alone already clears the floor; there is no rate that
        # both compresses and stays above it, so the caller keeps the span whole (arm B).
        return None
    rate = target_shrinkable / float(shrinkable_tokens)
    if rate >= 1.0:
        return None  # already at or under the floor; nothing to aim at
    return round(rate, 4)


# ─── span measurement ────────────────────────────────────────────────────────────

def _adapter_span(adapter: Any, messages: Sequence[Dict], params: Dict, config: Dict) -> List[Dict]:
    """``config`` is the FULL config, not a group block: an adapter has to be able to see
    whether the operator actually enabled the marker its span depends on. Same contract as
    ``cache_read_cost_multiplier(config)`` next to it."""
    try:
        span = adapter.cacheable_span_messages(list(messages), params or {}, config or {})
    except Exception:  # an adapter without the method must never break a request
        return list(messages)
    return list(span) if isinstance(span, list) else list(messages)


def _tools_tokens(ctx: Any) -> int:
    """Tokens in this request's tool definitions.

    Tools are inside the cacheable span under BOTH provider shapes — first in the block on
    a marker-based provider, and part of the prompt on a whole-prompt one — so they count
    either way. Omitting them would let a tool-description trim slip the span under the
    floor unseen, which is precisely one of the ways this fails silently.
    """
    tools = (getattr(ctx, "params", None) or {}).get("tools")
    if not tools:
        return 0
    try:
        return count_tools_tokens(tools, getattr(ctx, "model", "") or "")
    except Exception:
        return 0


def span_tokens(ctx: Any, messages: Sequence[Dict]) -> int:
    """Tokens in the cacheable span of ``messages`` for this request's reservation.

    Tool definitions count: on a provider whose cacheable block starts with tools they
    are the first thing inside it, and G08 trims their descriptions.
    """
    fl = get(ctx)
    if not fl.active:
        return 0
    in_span = [m for m in messages if fl.covers(m)]
    total = count_messages_tokens(in_span, getattr(ctx, "model", "") or "")
    return total + _tools_tokens(ctx)


def span_tokens_paired(ctx: Any, before: Sequence[Dict], after: Sequence[Dict]) -> int:
    """Span size of ``after``, with membership decided on the index-aligned ``before``.

    A shrinking group rewrites a message's content, so the rewrite no longer matches the
    reservation's snapshot (:func:`message_fingerprint`). Re-testing the rewrite would
    drop it from the span and make the guard believe the span fell further than it did —
    holding tokens back that were never at risk. Deciding membership on the original and
    counting the replacement at the same index is the honest measurement.
    """
    fl = get(ctx)
    if not fl.active:
        return 0
    in_span = [after[i] for i, msg in enumerate(before)
               if i < len(after) and fl.covers(msg)]
    total = count_messages_tokens(in_span, getattr(ctx, "model", "") or "")
    return total + _tools_tokens(ctx)


def resnapshot(ctx: Any, before: Sequence[Dict], after: Sequence[Dict]) -> None:
    """Re-take the span snapshot after a group has ASSIGNED its rewritten list to
    ``ctx.messages``. Every group that rewrites in-span content must call this.

    The reservation is shared: G01, G08 and G19 all shrink the same span, and the whole
    point of a shared budget is that the second group cannot undo what the first one
    protected. But :meth:`CacheFloor.covers` identifies a span member by CONTENT, so the
    instant a group replaces ``ctx.messages`` the snapshot describes messages that no
    longer exist. The next group's :func:`span_tokens` then reads the span as empty, and
    :func:`compress_is_cheaper` treats an empty span as "never cacheable, nothing to
    protect" and permits ANY shrink.

    That is fail-OPEN, and it lands precisely where the guard has just succeeded: G01
    compresses the span down ONTO the floor and records ``floored``, and G19 then prunes
    the very same message straight through the floor while measuring zero — the discount
    is forfeited on a request whose own observable claims it was saved. (Arm B is
    unaffected: it restores the originals, so the snapshot still matches.)

    Index-paired with the list membership was decided on, exactly like
    :func:`span_tokens_paired`.
    """
    fl = get(ctx)
    if not fl.active:
        return
    ctx.cache_floor = replace(fl, span_fingerprints=frozenset(
        message_fingerprint(after[i])
        for i, msg in enumerate(before)
        if i < len(after) and fl.covers(msg)
    ))


def span_tokens_for(adapter: Any, ctx: Any, messages: Sequence[Dict], config: Dict) -> int:
    """Span size for an adapter directly, with no reservation in play. ``config`` is the
    FULL config (see :func:`_adapter_span`).

    G21 needs this: the guard ships OFF, so on a default deployment there is no
    reservation, and yet that is exactly when an operator most needs to be told the
    markers being injected cannot pay out. Returns 0 when the adapter reports no cacheable
    span at all — there is nothing under a floor then, only nothing.
    """
    params = getattr(ctx, "params", None) or {}
    msgs = list(messages)
    in_span = _adapter_span(adapter, msgs, params, config or {})
    if msgs and not in_span:
        return 0
    return count_messages_tokens(in_span, getattr(ctx, "model", "") or "") + _tools_tokens(ctx)


def _fingerprint(messages: Sequence[Dict], params: Dict) -> str:
    """Stable identity for a cacheable span, so its reuse can be counted.

    Content only — never a request id or a timestamp, or every request would look like a
    first sighting and the reuse count would never rise above 1.
    """
    h = hashlib.sha256()
    for m in messages:
        content = m.get("content")
        h.update(str(m.get("role", "")).encode("utf-8", "replace"))
        h.update(b"\x00")
        h.update((content if isinstance(content, str) else str(content)).encode("utf-8", "replace"))
        h.update(b"\x01")
    tools = (params or {}).get("tools")
    if tools:
        h.update(str(tools).encode("utf-8", "replace"))
    return h.hexdigest()


# ─── reuse count ─────────────────────────────────────────────────────────────────

async def _observed_reuse(ctx: Any, fingerprint: str, window_seconds: int) -> Optional[int]:
    """How many times this exact span has already been sent inside the cache window.

    Redis, not a module dict: with several uvicorn workers per container and several
    containers, a per-process counter would under-count by exactly the scale-out factor
    and would quietly stop the guard from ever firing where it is needed most.

    Returns the count BEFORE this request's own increment, so the first sighting reads 0
    and correctly declines to hold tokens back for a reuse that has not happened. None
    means "unknown" — Redis missing, unreachable or erroring — which the caller treats as
    inert. That is the fail-SAFE direction: unknown reuse must never buy a cache write.
    """
    try:
        from cache.redis_pool import get_redis
        redis = get_redis()
    except Exception:
        return None
    if redis is None:
        return None
    key = f"{getattr(ctx, 'redis_prefix', '') or ''}cachefloor:{fingerprint}"
    ttl = max(1, int(window_seconds))
    try:
        # FIXED window, not a sliding one. `INCR` + `EXPIRE` on every sighting renews the
        # TTL each time, so a prefix seen once a minute forever accumulates a LIFETIME
        # count that no window ever expires — and N is exactly what decides whether tokens
        # are held back. `SET key 0 NX EX ttl` starts the window on the first sighting and
        # is a no-op afterwards, so the key really does die `ttl` seconds later and the
        # count restarts. It also matches what the config documents.
        await redis.set(key, 0, nx=True, ex=ttl)
        seen = await redis.incr(key)
    except Exception as exc:
        logger.debug("[%s] cache-floor: reuse counter unavailable (%s)",
                     getattr(ctx, "request_id", "-"), exc)
        return None
    try:
        return max(0, int(seen) - 1)
    except (TypeError, ValueError):
        return None


# ─── reservation ─────────────────────────────────────────────────────────────────

async def reserve(ctx: Any) -> None:
    """Compute this request's floor reservation, once, before anything shrinks the prompt.

    Runs before Stage 3 rather than inside G21 because G21 is the LAST request-side stage:
    by the time it executes every compressor has already run, so it can align what it is
    handed but cannot protect it.
    """
    ctx.cache_floor = _INERT
    if getattr(ctx, "cache_floor_action", None) is None:
        ctx.cache_floor_action = ACTION_NONE

    if getattr(ctx, "bypassed", False) or getattr(ctx, "cache_hit", False):
        # Regime 10: the provider is never called, so there is no prefix cache to protect
        # and — importantly — no reuse to count. Counting here would inflate N with
        # requests the provider never saw.
        return

    try:
        from middleware import resolve_group_config
        cfg = resolve_group_config(ctx, CONFIG_KEY) or {}
    except Exception:
        cfg = ((getattr(ctx, "config", None) or {}).get("groups", {}) or {}).get(CONFIG_KEY, {}) or {}

    if not cfg.get("preserve_cacheable_prefix", False):
        return

    adapter = getattr(ctx, "provider_adapter", None)
    if adapter is None:
        return

    config = getattr(ctx, "config", None) or {}
    model = getattr(ctx, "routed_model", None) or getattr(ctx, "model", "") or ""
    try:
        floor = int(adapter.min_cacheable_prompt_tokens(config, model) or 0)
    except Exception:
        floor = 0
    if floor <= 0:
        return  # provider publishes no minimum — nothing to protect

    messages = list(getattr(ctx, "messages", None) or [])
    params = getattr(ctx, "params", None) or {}
    span_msgs = _adapter_span(adapter, messages, params, config)
    if messages and not span_msgs:
        # The adapter reports no cacheable span at all — typically because the operator
        # never enabled the marker the span depends on. There is no discount to protect,
        # so holding tokens back would buy nothing.
        return
    whole_prompt = len(span_msgs) == len(messages)
    roles = frozenset(str(m.get("role", "")) for m in span_msgs)
    fingerprints = frozenset(message_fingerprint(m) for m in span_msgs)

    probe = CacheFloor(active=True, floor=floor, span_roles=roles,
                       span_fingerprints=fingerprints,
                       span_is_whole_prompt=whole_prompt)
    ctx.cache_floor = probe
    tokens = span_tokens(ctx, messages)
    if tokens < floor:
        # Never cacheable in the first place: compressing it forfeits nothing, so stay out
        # of the way entirely rather than reporting a guard that cannot help.
        ctx.cache_floor = _INERT
        return

    window = int(cfg.get("prefix_reuse_window_seconds", DEFAULT_REUSE_WINDOW_SECONDS) or
                 DEFAULT_REUSE_WINDOW_SECONDS)
    observed = await _observed_reuse(ctx, _fingerprint(span_msgs, params), window)
    if observed is None:
        ctx.cache_floor = _INERT
        return
    try:
        assumed = int(cfg.get("assumed_prefix_reuse", DEFAULT_ASSUMED_REUSE) or DEFAULT_ASSUMED_REUSE)
    except (TypeError, ValueError):
        assumed = DEFAULT_ASSUMED_REUSE
    # `observed` counts sightings already made; +1 for this one. The operator constant is
    # a floor on that, never a ceiling, so declaring reuse a deployment does not have can
    # only be corrected upward by evidence.
    reuse = max(1, assumed, observed + 1)

    try:
        read_mult = float(adapter.cache_read_cost_multiplier(config))
        write_mult = float(adapter.cache_write_cost_multiplier(config))
    except Exception:
        ctx.cache_floor = _INERT
        return

    try:
        margin = float(cfg.get("cacheable_prefix_margin", DEFAULT_MARGIN))
    except (TypeError, ValueError):
        margin = DEFAULT_MARGIN
    if margin < 0:
        margin = DEFAULT_MARGIN

    ctx.cache_floor = CacheFloor(
        active=True, floor=floor, span_tokens=tokens, read_mult=read_mult,
        write_mult=write_mult, reuse=reuse, margin=margin, span_roles=roles,
        span_fingerprints=fingerprints, span_is_whole_prompt=whole_prompt,
    )
    logger.debug(
        "[%s] cache-floor reserved: span=%dt floor=%dt reuse=%d read=%.2f write=%.2f",
        getattr(ctx, "request_id", "-"), tokens, floor, reuse, read_mult, write_mult,
    )


def record_action(ctx: Any, action: str) -> None:
    """Record what the floor actually did, so a measurement can assert the mechanism
    FIRED rather than infer it from a token delta (Gate 8.6)."""
    if action == ACTION_NONE and getattr(ctx, "cache_floor_action", ACTION_NONE) != ACTION_NONE:
        return  # never downgrade a real outcome
    ctx.cache_floor_action = action
