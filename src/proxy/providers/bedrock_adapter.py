"""AWS Bedrock adapter — multi-model gateway via LiteLLM (``bedrock/`` namespace).

Bedrock uses AWS SigV4 auth (no single bearer key): credentials come from the environment
/ instance profile, so requires_api_key() is False (the proxy's pre-call key guard is
skipped). Reasoning/cache behaviour depends on the underlying model and is config-driven.
"""
from typing import Dict, Optional

from providers import register_adapter
from providers.generic_adapter import GenericLiteLLMAdapter


@register_adapter("bedrock")
class BedrockAdapter(GenericLiteLLMAdapter):
    PROVIDER_NAME = "bedrock"

    def requires_api_key(self) -> bool:
        return False

    def build_call(self, model: str, provider_cfg: Dict, api_key: Optional[str]) -> tuple:
        cfg = provider_cfg or {}
        model_str = model if model.startswith("bedrock/") else f"bedrock/{model}"
        kwargs: Dict = {"custom_llm_provider": "bedrock"}
        region = cfg.get("aws_region_name") or cfg.get("aws_region") or cfg.get("region")
        if region:
            kwargs["aws_region_name"] = region
        # api_key intentionally omitted — litellm reads AWS creds from the environment.
        return model_str, kwargs
