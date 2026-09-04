"""
GenericLiteLLMAdapter — config-driven adapter for the long tail of providers.

Powers the "config-only" extension path: any LiteLLM-supported provider (mode A,
``litellm_prefix``) or any OpenAI-compatible endpoint LiteLLM doesn't list (mode B,
``openai_compatible`` + ``api_base``) works with a ``providers:`` entry and no code.

Unlike the first-class adapters it is NOT registered in the no-arg registry — it is
constructed with ``(name, provider_cfg)`` by ``_adapter_for_entry`` for providers[]
entries that set ``adapter: generic`` / ``openai_compatible`` or name a provider with no
dedicated adapter class. Routing/auth come from the base ``build_call`` (modes A/B);
structured-output and reasoning use OpenAI-shaped defaults that LiteLLM normalises.
"""
from typing import Dict, Optional

from providers import ProviderAdapter, REASONING_OFF


class GenericLiteLLMAdapter(ProviderAdapter):
    #: First-class thin subclasses set this so they can be registered and built no-arg
    #: (the registry instantiates ``cls()``); the config-only path passes ``name`` explicitly.
    PROVIDER_NAME = "generic"
    #: First-class subclasses set their LiteLLM provider namespace (e.g. "mistral");
    #: merged into the call config so the model routes as "<prefix>/<model>".
    LITELLM_PREFIX: Optional[str] = None

    def __init__(self, name: Optional[str] = None, provider_cfg: Optional[Dict] = None):
        self._name = name or self.PROVIDER_NAME
        self._cfg = provider_cfg or {}

    @property
    def name(self) -> str:
        return self._name

    def build_call(self, model: str, provider_cfg: Dict, api_key: Optional[str]) -> tuple:
        """Inject the subclass LITELLM_PREFIX (if config didn't set routing) then delegate."""
        cfg = dict(provider_cfg or {})
        if self.LITELLM_PREFIX and not cfg.get("litellm_prefix") and not cfg.get("openai_compatible"):
            cfg["litellm_prefix"] = self.LITELLM_PREFIX
        return super().build_call(model, cfg, api_key)

    # build_call is inherited — the base implementation already handles the bare model,
    # ``litellm_prefix`` (mode A) and ``openai_compatible`` + ``api_base`` (mode B) from
    # the provider_cfg it is passed, so the generic adapter needs no override.

    def map_structured_output(self, format_type: str, schema: Optional[Dict] = None) -> Dict:
        """OpenAI-shaped structured output; LiteLLM translates for compatible providers."""
        if format_type == "json_object":
            return {"response_format": {"type": "json_object"}}
        if format_type == "json_schema" and schema:
            return {"response_format": {"type": "json_schema", "json_schema": schema}}
        return {}

    def map_reasoning_effort(self, tier: str, config: Dict) -> Dict:
        """Emit ``reasoning_effort`` only when the config provides a value for this provider.

        Avoids sending a reasoning param to providers that reject it — opt in by adding
        ``effort_map[<tier>].<name>`` and ``supports_reasoning: true`` on the provider entry.
        """
        if tier == REASONING_OFF:
            return {}
        tier_cfg = (
            config.get("groups", {})
            .get("G12_reasoning", {})
            .get("effort_map", {})
            .get(tier, {})
        )
        value = tier_cfg.get(self._name)
        if value is None:
            return {}
        return {"reasoning_effort": value}

    def can_disable_reasoning(self, model: str) -> bool:
        """Conservative default OFF for an unknown OpenAI-compatible endpoint.

        Whether omitting ``reasoning_effort`` actually stops the model reasoning is not
        knowable for an arbitrary provider, and this flag only governs what we REPORT.
        Defaulting False makes the proxy under-claim rather than credit itself with a
        reasoning saving it cannot demonstrate; a provider that genuinely honours it
        opts in with ``can_disable_reasoning: true`` on its config entry.
        """
        return bool(self._cfg.get("can_disable_reasoning", False))

    def supports_reasoning(self, model: str, config: Optional[Dict] = None) -> bool:
        """Conservative default off — opt in per provider via ``supports_reasoning: true``.

        Reads this adapter's OWN construction config, so the optional ``config`` argument
        (present for interface parity with the base signature) is not needed here.
        """
        if not bool(self._cfg.get("supports_reasoning", False)):
            return False
        models = self._cfg.get("reasoning_models")
        if isinstance(models, list) and models:
            low = (model or "").lower()
            return any(isinstance(m, str) and m.lower() in low for m in models)
        return True

    def cache_read_cost_multiplier(self, config: Dict) -> float:
        pcfg = (
            config.get("groups", {})
            .get("G21_cache_alignment", {})
            .get("providers", {})
            .get(self._name, {})
        )
        return float(pcfg.get("cache_read_multiplier", 1.0))

    def cache_write_cost_multiplier(self, config: Dict) -> float:
        """Config-driven per-provider write rate; 1.0 = billed at the normal input rate.

        Every config-only provider gets an honest, operator-settable write rate with no
        per-provider code, the same way ``cache_read_multiplier`` already works.
        """
        pcfg = (
            config.get("groups", {})
            .get("G21_cache_alignment", {})
            .get("providers", {})
            .get(self._name, {})
        )
        return float(pcfg.get("cache_write_multiplier", 1.0))

    def supports_native_batch(self) -> bool:
        return bool(self._cfg.get("native_batch", False))

    def requires_api_key(self) -> bool:
        return bool(self._cfg.get("requires_api_key", True))

    def unsupported_params(self) -> set:
        return set(self._cfg.get("unsupported_params", []) or [])
