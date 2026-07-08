"""
G17 · Loop Control & Token Budget Propagation
Stage: Across the Loop
Saving: 10–30% iterative tokens + downstream verbosity control
Technique:
  1. Enforce max_iterations via turn count tracking in Redis.
  2. Inject token_budget_remaining into every inter-agent message.
  3. Trigger compact-output instruction when budget is low.
  4. Confidence-based stop condition.
  5. Wall-clock timeout-based stop.
  6. Structured inter-agent state schema for token budget.
"""
import logging
import os
import time
from typing import Dict, Optional
from pydantic import BaseModel

from middleware import RequestContext
from savings.calculator import count_messages_tokens

logger = logging.getLogger(__name__)
GROUP = "G17"

# WS21: all three loop-state keys are tenant-prefixed at use (ctx.redis_prefix +
# these suffixes). workflow_id is CLIENT-supplied — without the tenant prefix two
# tenants sending the same workflow_id would share/drain one budget and trip each
# other's loop stops (cross-tenant DoS). Same pattern as G18's turn_baseline key.
_BUDGET_PREFIX = "tok_opt:budget:"
_TURN_PREFIX = "tok_opt:turns:"
_START_TIME_PREFIX = "tok_opt:start_time:"


def _scoped(ctx, prefix: str, workflow_id: str) -> str:
    return f"{getattr(ctx, 'redis_prefix', '')}{prefix}{workflow_id}"


class InterAgentState(BaseModel):
    """Structured inter-agent state schema for token budget and loop control."""
    token_budget_remaining: int
    workflow_turn: int
    max_iterations: int
    confidence_score: Optional[float] = None
    wall_clock_elapsed_seconds: Optional[float] = None
    stop_reason: Optional[str] = None

    def to_header_value(self) -> str:
        """Export state as base64-encoded JSON for HTTP header."""
        import base64
        import json

        data = self.model_dump()
        json_bytes = json.dumps(data, separators=(",", ":")).encode("utf-8")
        return base64.b64encode(json_bytes).decode("utf-8")

    @classmethod
    def from_header_value(cls, header_value: str) -> "InterAgentState":
        """Parse state from HTTP header value."""
        import base64
        import json

        json_bytes = base64.b64decode(header_value)
        data = json.loads(json_bytes.decode("utf-8"))
        return cls(**data)


def _get_redis():
    from cache.redis_pool import get_redis as _pool_get_redis
    return _pool_get_redis()


class G17LoopControl:
    async def process_request(self, ctx: RequestContext) -> RequestContext:
        cfg = ctx.config.get("groups", {}).get("G17_loop", {})
        if not cfg.get("enabled", False):
            return ctx

        max_iterations: int = cfg.get("max_iterations", 10)
        starting_budget: int = cfg.get("starting_budget_tokens", 10000)
        compact_below: int = cfg.get("compact_output_below_tokens", 500)
        confidence_threshold: float = cfg.get("confidence_stop_threshold", 0.95)
        wall_clock_timeout_seconds: int = cfg.get("wall_clock_timeout_seconds", 300)

        workflow_id = ctx.params.get("workflow_id") or ctx.params.get("x_workflow_id")

        tokens_before = ctx.current_token_count
        stop_reason = None

        # Single-turn request without workflow_id: set default state
        if not workflow_id:
            state = InterAgentState(
                token_budget_remaining=starting_budget,
                workflow_turn=1,
                max_iterations=max_iterations,
            )
            ctx.params["_token_budget"] = state.model_dump()
            ctx.savings.add_step(
                GROUP,
                f"Budget propagation: {starting_budget}t remaining (turn 1/{max_iterations})",
                tokens_before,
                tokens_before,
            )
            return ctx

        redis = None
        try:
            redis = _get_redis()

            # Initialize start time on first turn
            start_time_key = _scoped(ctx, _START_TIME_PREFIX, workflow_id)
            start_time_raw = await redis.get(start_time_key)
            if start_time_raw is None:
                await redis.set(start_time_key, str(time.time()), ex=3600)
                start_time = time.time()
            else:
                start_time = float(start_time_raw)

            # Check and increment turn counter
            turn_key = _scoped(ctx, _TURN_PREFIX, workflow_id)
            turn_count = await redis.incr(turn_key)
            await redis.expire(turn_key, 3600)

            # Wall-clock timeout check
            elapsed = time.time() - start_time
            if elapsed > wall_clock_timeout_seconds:
                stop_reason = f"wall_clock_timeout ({elapsed:.1f}s > {wall_clock_timeout_seconds}s)"
                logger.warning(
                    "[%s] G17 wall-clock timeout: %.1fs > %ds for workflow '%s'",
                    ctx.request_id,
                    elapsed,
                    wall_clock_timeout_seconds,
                    workflow_id,
                )
                ctx.params["_token_opt_loop_limit_reached"] = True
                ctx.params.setdefault("_token_opt_warnings", []).append(stop_reason)

            # Max iterations check
            if turn_count > max_iterations:
                stop_reason = stop_reason or f"max_iterations ({turn_count}/{max_iterations})"
                logger.warning(
                    "[%s] G17 loop limit reached: %d > %d turns for workflow '%s'",
                    ctx.request_id,
                    turn_count,
                    max_iterations,
                    workflow_id,
                )
                ctx.params["_token_opt_loop_limit_reached"] = True
                ctx.params.setdefault("_token_opt_warnings", []).append(
                    f"Loop limit reached ({turn_count}/{max_iterations} turns)"
                )

            # Confidence-based stop check (if confidence score provided in params)
            confidence_score = ctx.params.get("x_confidence_score")
            if confidence_score is not None and isinstance(confidence_score, (int, float)):
                if confidence_score >= confidence_threshold:
                    stop_reason = stop_reason or f"confidence_threshold ({confidence_score:.2f} >= {confidence_threshold})"
                    logger.info(
                        "[%s] G17 confidence stop: %.2f >= %.2f for workflow '%s'",
                        ctx.request_id,
                        confidence_score,
                        confidence_threshold,
                        workflow_id,
                    )
                    ctx.params["_token_opt_loop_limit_reached"] = True
                    ctx.params.setdefault("_token_opt_warnings", []).append(stop_reason)

            # Track and propagate token budget
            budget_key = _scoped(ctx, _BUDGET_PREFIX, workflow_id)
            budget_raw = await redis.get(budget_key)
            if budget_raw is None:
                remaining = starting_budget
            else:
                try:
                    remaining = max(0, int(budget_raw) - tokens_before)
                except ValueError:
                    logger.warning("G17 corrupt budget value in Redis (%r), resetting to starting budget", budget_raw)
                    remaining = starting_budget

            await redis.set(budget_key, str(remaining), ex=3600)

            # Build structured inter-agent state
            state = InterAgentState(
                token_budget_remaining=remaining,
                workflow_turn=turn_count,
                max_iterations=max_iterations,
                confidence_score=confidence_score,
                wall_clock_elapsed_seconds=elapsed,
                stop_reason=stop_reason,
            )
            ctx.params["_token_budget"] = state.model_dump()

            # Inject compact-output instruction if budget is low
            if remaining < compact_below:
                ctx.messages = _inject_compact_instruction(ctx.messages, remaining)
                tokens_after = count_messages_tokens(ctx.messages, ctx.model)
                ctx.savings.add_step(
                    GROUP,
                    f"Budget propagation: {remaining}t remaining → compact mode injected",
                    tokens_before,
                    tokens_after,
                )
            else:
                ctx.savings.add_step(
                    GROUP,
                    f"Budget propagation: {remaining}t remaining (turn {turn_count}/{max_iterations})",
                    tokens_before,
                    tokens_before,
                )

        except Exception as exc:
            logger.warning("G17 loop control error: %s", exc)

        return ctx


def _inject_compact_instruction(messages: list, remaining: int) -> list:
    """Prepend a compact-output instruction when budget is low."""
    instruction = (
        f"[BUDGET] token_budget_remaining={remaining}. "
        "Respond ONLY with required JSON fields — no explanation, no preamble."
    )
    # Add to the last system message or prepend a new one
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "system":
            messages = list(messages)
            messages[i] = {
                **messages[i],
                "content": instruction + "\n" + messages[i].get("content", ""),
            }
            return messages
    return [{"role": "system", "content": instruction}] + list(messages)
