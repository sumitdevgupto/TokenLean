"""Cohere adapter — routed via LiteLLM (``cohere/`` namespace).

Cohere's native API differs from OpenAI, but LiteLLM normalises requests/responses, so
the generic OpenAI-shaped structured-output / usage defaults work. Override here only if a
future Cohere-specific param mapping is needed.
"""
from providers import register_adapter
from providers.generic_adapter import GenericLiteLLMAdapter


@register_adapter("cohere")
class CohereAdapter(GenericLiteLLMAdapter):
    PROVIDER_NAME = "cohere"
    LITELLM_PREFIX = "cohere"
