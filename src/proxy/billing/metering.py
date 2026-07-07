"""UsageMeter — async, fire-and-forget usage recording.

Persists a ``UsageEvent`` to Postgres AND posts it to the OpenMeter REST API.
Both operations are best-effort: errors are logged but never raise to callers.
"""
import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from billing.models import UsageEvent

logger = logging.getLogger(__name__)

_OPENMETER_URL = os.getenv("OPENMETER_URL", "")
_OPENMETER_API_KEY = os.getenv("OPENMETER_API_KEY", "")
_RECORD_TIMEOUT = 5.0  # seconds


class UsageMeter:
    """Records usage events to Postgres and OpenMeter."""

    def __init__(
        self,
        db_pool: Optional[Any] = None,
        http_session: Optional[Any] = None,
        openmeter_url: str = "",
        openmeter_api_key: str = "",
    ) -> None:
        self._db = db_pool
        self._http = http_session
        self._om_url = openmeter_url or _OPENMETER_URL
        self._om_key = openmeter_api_key or _OPENMETER_API_KEY

    def _build_event(self, ctx: Any, response: Dict) -> UsageEvent:
        savings = ctx.savings
        groups = [s.group for s in savings.step_savings]
        trace_id = ""
        if ctx.otel_span:
            from tracing.otel import get_trace_id
            trace_id = get_trace_id(ctx.otel_span)
        # C2: cache-hit / bypass rows skip G18, so cost_saving_usd is 0 even though the
        # whole LLM call was avoided. Show the avoided input cost (an estimate) so the
        # confidence story holds on cached traffic. (Token savings already reflect it.)
        cost_saved = savings.cost_saving_usd
        if (
            cost_saved == 0
            and (getattr(savings, "cache_hit", False) or getattr(savings, "bypassed", False))
            and savings.baseline_tokens
        ):
            from savings.calculator import estimate_cost
            cost_saved = estimate_cost(savings.baseline_tokens, 0, ctx.model)
        return UsageEvent(
            tenant_id=getattr(ctx, "tenant_id", "default"),
            request_id=ctx.request_id,
            timestamp=datetime.now(timezone.utc),
            baseline_tokens=savings.baseline_tokens,
            optimised_tokens=savings.final_tokens_sent,
            tokens_saved=savings.total_absolute_saving,
            cost_saved_usd=cost_saved,
            groups_applied=groups,
            pricing_tier=getattr(ctx, "pricing_tier", "free"),
            model=ctx.model,
            routed_model=ctx.routed_model,
            otel_trace_id=trace_id,
            proxy_optimised_tokens=getattr(savings, "proxy_optimised_tokens", 0),
            provider_prompt_tokens=getattr(savings, "provider_prompt_tokens", None),
            response_tokens=getattr(savings, "response_tokens", 0),
            # Requests Explorer filter columns — mirror the request-context flags so the
            # indexed usage_events table carries every filterable field. complexity_tier
            # comes from the X-Complexity-Tier header (params.x_complexity_tier), same
            # source as the Langfuse trace tag.
            user_id=getattr(ctx, "user_id", "") or "",
            cache_hit=bool(getattr(ctx, "cache_hit", False)),
            cache_level=getattr(ctx, "cache_level", "") or "",
            complexity_tier=(ctx.params.get("x_complexity_tier")
                             or ctx.params.get("complexity_tier") or ""),
            bypassed=bool(getattr(ctx, "bypassed", False)),
        )

    async def _persist_postgres(self, event: UsageEvent) -> None:
        if self._db is None:
            return
        sql = """
            INSERT INTO usage_events
                (tenant_id, request_id, timestamp, baseline_tokens, optimised_tokens,
                 tokens_saved, cost_saved_usd, groups_applied, pricing_tier,
                 model, routed_model, otel_trace_id,
                 proxy_optimised_tokens, provider_prompt_tokens, response_tokens,
                 user_id, cache_hit, cache_level, complexity_tier, bypassed)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,
                    $16,$17,$18,$19,$20)
            ON CONFLICT (request_id) DO NOTHING
        """
        try:
            async with self._db.acquire() as conn:
                await conn.execute(
                    sql,
                    event.tenant_id, event.request_id, event.timestamp,
                    event.baseline_tokens, event.optimised_tokens,
                    event.tokens_saved, event.cost_saved_usd,
                    event.groups_applied, event.pricing_tier,
                    event.model, event.routed_model, event.otel_trace_id,
                    event.proxy_optimised_tokens, event.provider_prompt_tokens,
                    event.response_tokens,
                    event.user_id, event.cache_hit, event.cache_level,
                    event.complexity_tier, event.bypassed,
                )
        except Exception as exc:
            logger.warning("UsageMeter: Postgres insert failed: %s", exc)

    async def _post_openmeter(self, event: UsageEvent) -> None:
        if not self._om_url or self._http is None:
            return
        url = f"{self._om_url}/api/v1/events"
        payload = {
            "specversion": "1.0",
            "type": "proxy.usage",
            "source": "token-optimisation-proxy",
            "id": event.request_id,
            "time": event.timestamp.isoformat(),
            "subject": event.tenant_id,
            "data": {
                "tokens_saved": event.tokens_saved,
                "cost_saved_usd": event.cost_saved_usd,
                "pricing_tier": event.pricing_tier,
                "groups_applied": event.groups_applied,
            },
        }
        headers = {"Content-Type": "application/cloudevents+json"}
        if self._om_key:
            headers["Authorization"] = f"Bearer {self._om_key}"
        try:
            async with self._http.post(url, json=payload, headers=headers, timeout=_RECORD_TIMEOUT) as resp:
                if resp.status not in (200, 201, 204):
                    body = await resp.text()
                    logger.warning("OpenMeter POST returned %d: %s", resp.status, body[:200])
        except asyncio.TimeoutError:
            logger.warning("UsageMeter: OpenMeter POST timed out after %ss", _RECORD_TIMEOUT)
        except Exception as exc:
            logger.warning("UsageMeter: OpenMeter POST failed: %s", exc)

    async def record(self, ctx: Any, response: Dict) -> None:
        """Fire-and-forget: build event and write to Postgres + OpenMeter."""
        try:
            event = self._build_event(ctx, response)
        except Exception as exc:
            logger.warning("UsageMeter: failed to build event: %s", exc)
            return
        await asyncio.gather(
            self._persist_postgres(event),
            self._post_openmeter(event),
            return_exceptions=True,
        )
