"""DeepSeek adapter — OpenAI-compatible via LiteLLM (``deepseek/`` namespace).

Reasoning is selected via the model (``deepseek-reasoner``), not a request param, so the
default supports_reasoning=False is correct (no ``reasoning_effort`` is injected).
"""
from providers import register_adapter
from providers.generic_adapter import GenericLiteLLMAdapter


@register_adapter("deepseek")
class DeepSeekAdapter(GenericLiteLLMAdapter):
    PROVIDER_NAME = "deepseek"
    LITELLM_PREFIX = "deepseek"
