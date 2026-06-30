"""Azure OpenAI adapter — OpenAI param-compatible, tenant-specific endpoint.

Inherits OpenAI's structured-output / reasoning / cache-policy behaviour, but routes to a
``azure/<deployment>`` model with api_base + api_version. The model string is treated as the
Azure deployment name.
"""
from typing import Dict, Optional

from providers import register_adapter
from providers.openai_adapter import OpenAIAdapter


@register_adapter("azure")
class AzureOpenAIAdapter(OpenAIAdapter):
    @property
    def name(self) -> str:
        return "azure"

    def supports_native_batch(self) -> bool:
        # OpenAI's direct-SDK batch lane does not apply to Azure; use the per-item loop.
        return False

    def build_call(self, model: str, provider_cfg: Dict, api_key: Optional[str]) -> tuple:
        cfg = provider_cfg or {}
        model_str = model if model.startswith("azure/") else f"azure/{model}"
        kwargs: Dict = {"custom_llm_provider": "azure"}
        if api_key:
            kwargs["api_key"] = api_key
        if cfg.get("api_base"):
            kwargs["api_base"] = cfg["api_base"]
        if cfg.get("api_version"):
            kwargs["api_version"] = cfg["api_version"]
        return model_str, kwargs
