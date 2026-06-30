"""Mistral adapter — OpenAI-compatible via LiteLLM (``mistral/`` namespace)."""
from providers import register_adapter
from providers.generic_adapter import GenericLiteLLMAdapter


@register_adapter("mistral")
class MistralAdapter(GenericLiteLLMAdapter):
    PROVIDER_NAME = "mistral"
    LITELLM_PREFIX = "mistral"
