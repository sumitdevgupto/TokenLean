import hashlib
import logging
from typing import Any, Dict, List, Optional

from providers import (
    ProviderAdapter,
    REASONING_OFF,
    REASONING_TIERS,
    register_adapter,
    build_batch_jsonl,
    parse_batch_jsonl_results,
    _file_content_text,
)

logger = logging.getLogger(__name__)


@register_adapter("openai")
class OpenAIAdapter(ProviderAdapter):
    @property
    def name(self) -> str:
        return "openai"

    def unsupported_params(self) -> set:
        """OpenAI rejects Anthropic's ``thinking`` param (it uses ``reasoning_effort``)."""
        return {"thinking"}

    def requires_json_keyword(self) -> bool:
        """OpenAI's json_object/json_schema mode 400s unless 'json' appears in the prompt."""
        return True

    def align_prefix(self, ctx, system_msgs, variable_msgs, cfg) -> bool:
        """G21: OpenAI auto-caches contiguous prefixes — reorder system messages first.

        Honours ``providers.openai.auto`` (default on). No-op when the prefix is already
        contiguous. (Moved out of G21 so the middleware carries no provider-name checks.)
        """
        provider_cfg = cfg.get("providers", {}).get("openai", {})
        if not provider_cfg.get("auto", True):
            return False
        if not system_msgs:
            return False
        messages = ctx.messages
        n = len(system_msgs)
        already_contiguous = len(messages) >= n and all(
            messages[i].get("role") == "system" for i in range(n)
        )
        if already_contiguous:
            return False
        ctx.messages = system_msgs + variable_msgs
        return True

    def cache_policy_params(
        self,
        model: str,
        tenant_id: str,
        cache_seed: str,
        cfg: Dict,
    ) -> Dict:
        """
        Emit a deterministic, tenant-scoped ``prompt_cache_key`` (and optional
        ``prompt_cache_retention``) so identical prefixes from the same tenant route
        to the same OpenAI cache shard — raising the cache hit rate at no output cost.
        Disable with ``providers.openai.prompt_cache_key: false``.
        """
        pcfg = cfg.get("providers", {}).get("openai", {})
        if not pcfg.get("prompt_cache_key", True):
            return {}
        key_len = int(pcfg.get("prompt_cache_key_len", 32))
        digest = hashlib.sha256(f"{tenant_id}|{cache_seed}".encode("utf-8")).hexdigest()
        out: Dict[str, Any] = {"prompt_cache_key": digest[:key_len]}
        retention = pcfg.get("prompt_cache_retention")
        if retention:
            out["prompt_cache_retention"] = retention
        return out

    def cache_write_cost_multiplier(self, config: Dict) -> float:
        """OpenAI does not surcharge cache writes — they bill at the normal input rate.

        Explicit 1.0 (not merely inherited) so the shipped template can carry the real
        published rate for every provider side by side, and a future OpenAI change is a
        config edit rather than a code change.
        """
        pcfg = (
            config.get("groups", {})
            .get("G21_cache_alignment", {})
            .get("providers", {})
            .get("openai", {})
        )
        return float(pcfg.get("cache_write_multiplier", 1.0))

    def cache_read_cost_multiplier(self, config: Dict) -> float:
        """OpenAI bills cached input tokens at ~50% (config-overridable)."""
        pcfg = (
            config.get("groups", {})
            .get("G21_cache_alignment", {})
            .get("providers", {})
            .get("openai", {})
        )
        return float(pcfg.get("cache_read_multiplier", 0.5))

    def supports_service_tier(self) -> bool:
        """OpenAI accepts ``service_tier`` (e.g. Flex — 50% off, latency-tolerant)."""
        return True

    # ── Native batch lane (OpenAI Batch API — 50% discount, direct SDK) ───────

    def _make_async_client(self, api_key: str):
        """Construct an AsyncOpenAI client (factory isolated so tests can patch it)."""
        import openai
        return openai.AsyncOpenAI(api_key=api_key)

    def supports_native_batch(self) -> bool:
        return True

    async def submit_batch(self, items: List[Dict], api_key: str, cfg: Dict) -> str:
        """Upload a JSONL batch and create an OpenAI Batch job; return its id."""
        client = self._make_async_client(api_key)
        payload = build_batch_jsonl(items).encode("utf-8")
        upload = await client.files.create(file=payload, purpose="batch")
        batch = await client.batches.create(
            input_file_id=upload.id,
            endpoint="/v1/chat/completions",
            completion_window=cfg.get("completion_window", "24h"),
        )
        return batch.id

    async def poll_batch(self, job_id: str, api_key: str) -> str:
        client = self._make_async_client(api_key)
        batch = await client.batches.retrieve(job_id)
        status = getattr(batch, "status", "") or ""
        if status == "completed":
            return "completed"
        if status in ("failed", "expired", "cancelled", "cancelling"):
            return "failed"
        return "pending"

    async def fetch_batch_results(self, job_id: str, api_key: str) -> List[Dict]:
        client = self._make_async_client(api_key)
        batch = await client.batches.retrieve(job_id)
        out_id = getattr(batch, "output_file_id", None)
        if not out_id:
            return []
        content = await client.files.content(out_id)
        return parse_batch_jsonl_results(_file_content_text(content))

    def map_structured_output(
        self,
        format_type: str,
        schema: Optional[Dict] = None,
    ) -> Dict:
        if format_type == "json_object":
            return {"response_format": {"type": "json_object"}}
        if format_type == "json_schema" and schema:
            return {"response_format": {"type": "json_schema", "json_schema": schema}}
        return {}

    def supports_reasoning(self, model: str, config: Optional[Dict] = None) -> bool:
        """o-series only. An explicit ``reasoning_models`` list on the provider entry
        NARROWS this further (e.g. to pin a specific o-series model); it never widens it,
        because a non-o-series OpenAI model rejects ``reasoning_effort`` outright."""
        if not any(f in model for f in ("o1", "o3", "o4")):
            return False
        models = self._provider_entry(config).get("reasoning_models")
        if isinstance(models, list) and models:
            low = (model or "").lower()
            return any(isinstance(m, str) and m.lower() in low for m in models)
        return True

    def map_reasoning_effort(self, tier: str, config: Dict) -> Dict:
        """Map an effort tier onto ``reasoning_effort``.

        ``off`` and any unrecognised tier emit NOTHING. Emitting the tier string verbatim
        (the pre-#42 behaviour) sent ``reasoning_effort: "off"`` — an invalid enum the
        o-series 400s on — and would have done the same for the portal's `minimal`.
        """
        if tier not in REASONING_TIERS or tier == REASONING_OFF:
            return {}
        tier_cfg = (
            config.get("groups", {})
            .get("G12_reasoning", {})
            .get("effort_map", {})
            .get(tier, {})
        )
        effort_value = tier_cfg.get("openai", tier)
        return {"reasoning_effort": effort_value}

    def can_disable_reasoning(self, model: str) -> bool:
        """False for the o-series: those models reason intrinsically.

        Omitting ``reasoning_effort`` selects the model's own default, it does not turn
        reasoning off. Saying True here would let the proxy report a reasoning saving it
        never made. On a non-reasoning OpenAI model the question is moot, and True is the
        honest answer — there is nothing to disable.
        """
        return not self.supports_reasoning(model)
