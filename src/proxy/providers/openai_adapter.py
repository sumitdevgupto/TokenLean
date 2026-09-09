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

    # Model families this adapter treats as reasoning-capable. A module constant, not an
    # inline literal, because a config's `model_prefixes` must cover every family named
    # here or provider detection cannot see the model at all: `o4` was recognised here
    # and missing from the prefix list, so an o4-mini request was silently served by
    # gpt-4o-mini with the reasoning params stripped (2026-09-08). Pinned by
    # tests/unit/test_provider_model_prefix_coverage.py against the shipped template.
    REASONING_MODEL_FAMILIES = ("o1", "o3", "o4")

    def supports_reasoning(self, model: str, config: Optional[Dict] = None) -> bool:
        """o-series only. An explicit ``reasoning_models`` list on the provider entry
        NARROWS this further (e.g. to pin a specific o-series model); it never widens it,
        because a non-o-series OpenAI model rejects ``reasoning_effort`` outright."""
        if not any(f in model for f in self.REASONING_MODEL_FAMILIES):
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

    # Output-budget keys, most specific first. The o-series takes `max_completion_tokens`;
    # the older key is honoured too so a caller that sends it is not left unprotected.
    _OUTPUT_BUDGET_KEYS = ("max_completion_tokens", "max_tokens")
    # Mirrors config.yaml.template's groups.G12_reasoning.reasoning_headroom.
    # allowance_tokens (Gate 2: the code default equals the template default). `off` is
    # the smallest allowance and NOT zero — this family reasons intrinsically, so an
    # `off` request still needs room; zero would re-create the empty-answer defect for
    # exactly the callers who asked for the cheapest possible request.
    _DEFAULT_ALLOWANCES = {REASONING_OFF: 1024, "low": 1024, "medium": 4096, "high": 16384}

    def reasoning_headroom_needed(
        self, model: str, config: Optional[Dict] = None,
        effort: Optional[str] = None,
    ) -> Optional[int]:
        """``answer_floor_tokens + allowance_tokens[effort]`` — the smallest output
        budget under which this model can think and still answer.

        The single derivation of that number. ``reserve_reasoning_headroom`` below raises
        a caller's budget to it; ``g06_routing`` asks for it before routing INTO a
        reasoning model. Both used to compute it independently — the middleware from
        config alone, this class from config merged over ``_DEFAULT_ALLOWANCES`` — so a
        deployment carrying no ``reasoning_headroom`` block had two different answers for
        one policy, and G06 refused routes the reservation would have handled.

        None when the model does not reason here, when the operator disabled the
        reservation, or when the configured numbers cannot be read.
        """
        if not self.supports_reasoning(model, config):
            return None
        hcfg = (
            (config or {}).get("groups", {})
            .get("G12_reasoning", {})
            .get("reasoning_headroom", {})
        ) or {}
        if not hcfg.get("enabled", True):
            return None
        # An absent effort means the model applies its own default, which is the middle
        # tier on this family — assume that rather than the cheapest, or the reservation
        # under-provisions exactly the unlabelled requests. An explicit `off` is honoured
        # as the SMALLEST tier (it cannot be zero: this family always reasons).
        tier = str(effort or hcfg.get("assumed_effort", "medium")).lower()
        allowances = dict(self._DEFAULT_ALLOWANCES)
        allowances.update(hcfg.get("allowance_tokens") or {})
        try:
            # An unrecognised tier falls back to the middle allowance, never to zero:
            # under-provisioning is the failure this whole path exists to prevent.
            allowance = int(allowances.get(tier, allowances.get("medium", 4096)))
            floor = int(hcfg.get("answer_floor_tokens", 512))
        except (TypeError, ValueError, KeyError):
            return None
        return allowance + floor

    def reserve_reasoning_headroom(
        self,
        params: Dict,
        model: str,
        config: Dict,
        requested_effort: Optional[str] = None,
    ) -> Optional[Dict]:
        """Grow the caller's output budget so a reasoning model can think AND answer.

        On the o-series the hidden reasoning tokens are billed INSIDE the output budget.
        A caller who sizes that budget for the answer alone gets the whole allowance
        spent on thinking and an EMPTY reply, billed in full. Raise the budget to
        ``answer_floor + allowance(effort)``, never above ``max_output_tokens``.

        No-op when: the model does not reason; the caller set no budget at all (the
        provider default already covers this); the budget is already sufficient; or the
        operator disabled the reservation. Returns the disclosure dict, else None.
        """
        if not self.supports_reasoning(model, config):
            return None
        hcfg = (
            (config or {}).get("groups", {})
            .get("G12_reasoning", {})
            .get("reasoning_headroom", {})
        ) or {}
        if not hcfg.get("enabled", True):
            return None

        key = next((k for k in self._OUTPUT_BUDGET_KEYS if params.get(k) is not None), None)
        if key is None:
            # No caller budget → the provider applies its own, which is not the
            # too-small-budget failure this guards. Adding one would CAP a request
            # that was never capped.
            return None
        try:
            budget = int(params[key])
        except (TypeError, ValueError):
            return None
        if budget <= 0:
            return None

        # An absent `reasoning_effort` means the model uses its own default, which is
        # the middle tier on this family — assume that rather than the cheapest, or the
        # reservation would under-provision exactly the unlabelled requests. But a
        # caller who explicitly asked for `off` asked for as little reasoning as this
        # family can do (it cannot be disabled — see can_disable_reasoning), so they get
        # the SMALLEST allowance, not the default one; provisioning them at `medium`
        # would inflate the budget of the one request that asked for the opposite.
        effort = str(
            requested_effort
            or params.get("reasoning_effort")
            or hcfg.get("assumed_effort", "medium")
        ).lower()
        # One derivation, shared with G06's routing floor — see reasoning_headroom_needed.
        needed = self.reasoning_headroom_needed(model, config, effort)
        if needed is None:
            return None
        if budget >= needed:
            return None

        by_model = hcfg.get("max_output_tokens_by_model") or {}
        cap_raw = next(
            (v for m, v in by_model.items() if isinstance(m, str) and m.lower() in model.lower()),
            hcfg.get("max_output_tokens", 32768),
        )
        try:
            cap = int(cap_raw)
        except (TypeError, ValueError):
            cap = 32768
        new_budget = min(needed, cap)
        if new_budget <= budget:
            # The operator's cap is below what this effort needs. Leave the caller's
            # budget alone and say nothing false — the empty-completion detector on the
            # response path is what reports the outcome if it still happens.
            return None

        params[key] = new_budget
        return {
            "param": key,
            "from": budget,
            "to": new_budget,
            "effort": effort,
            "reason": "reasoning_headroom",
        }

    def default_reasoning_effort(self, model: str, config: Optional[Dict] = None) -> str:
        """``medium`` on the o-series — the effort the model applies when the request
        omits ``reasoning_effort`` — and ``off`` on every other OpenAI model, which
        cannot reason at all.

        This is the one family where "medium is the provider default" was always true,
        so nothing changes here; the method exists so the claim is asked of the ADAPTER
        rather than assumed of every provider (the pre-2026-09-08 bug was assuming it of
        Anthropic, where thinking is opt-in and `medium` is an INCREASE).
        """
        override = self._configured_default_reasoning_effort(config)
        if override is not None:
            return override
        return "medium" if self.supports_reasoning(model, config) else REASONING_OFF

    def can_disable_reasoning(self, model: str) -> bool:
        """False for the o-series: those models reason intrinsically.

        Omitting ``reasoning_effort`` selects the model's own default, it does not turn
        reasoning off. Saying True here would let the proxy report a reasoning saving it
        never made. On a non-reasoning OpenAI model the question is moot, and True is the
        honest answer — there is nothing to disable.
        """
        return not self.supports_reasoning(model)
