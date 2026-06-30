"""Groq adapter — OpenAI-compatible via LiteLLM (``groq/`` namespace), fast inference."""
from providers import register_adapter
from providers.generic_adapter import GenericLiteLLMAdapter


@register_adapter("groq")
class GroqAdapter(GenericLiteLLMAdapter):
    PROVIDER_NAME = "groq"
    LITELLM_PREFIX = "groq"
