"""Native multi-protocol ingress (#4).

The optimisation pipeline is OpenAI-shaped internally. These adapters translate an
inbound request from another provider's wire protocol into that OpenAI shape on the way
IN, and re-serialise the OpenAI response back to the caller's protocol on the way OUT —
so a Claude-SDK, Anthropic-SDK, or Gemini-SDK client gets the same one-line base-URL
swap the OpenAI clients already enjoy, with every G-group optimisation applied.

Each adapter is a pure translator (no I/O), so the request-parse / response-serialise /
streaming / error paths are all unit-testable in isolation. ``get_protocol`` resolves the
adapter for a route; ``OpenAIProtocol`` is the identity adapter, so the existing
``/v1/chat/completions`` path is byte-for-byte unchanged.
"""
from protocols.base import (
    IngressProtocol, OpenAIProtocol, StreamTranslator, sse_line, DEFAULT_PROTOCOL_NAME,
)
from protocols.anthropic_ingress import AnthropicProtocol
from protocols.gemini_ingress import GeminiProtocol

# Singleton adapters. The default is the OpenAI identity adapter; the routes import these
# instances directly, so no module re-types a protocol-name string literal.
OPENAI = OpenAIProtocol()
ANTHROPIC = AnthropicProtocol()
GEMINI = GeminiProtocol()
DEFAULT_PROTOCOL = OPENAI

# Registry keyed by each adapter's OWN name — the name lives once, on the adapter class.
_BY_NAME = {p.name: p for p in (OPENAI, ANTHROPIC, GEMINI)}


def get_protocol(name: str) -> IngressProtocol:
    """Return the ingress adapter for a protocol name (defaults to the identity adapter)."""
    return _BY_NAME.get((name or DEFAULT_PROTOCOL_NAME).lower(), DEFAULT_PROTOCOL)


__all__ = [
    "IngressProtocol",
    "OpenAIProtocol",
    "AnthropicProtocol",
    "GeminiProtocol",
    "StreamTranslator",
    "sse_line",
    "get_protocol",
    "OPENAI",
    "ANTHROPIC",
    "GEMINI",
    "DEFAULT_PROTOCOL",
    "DEFAULT_PROTOCOL_NAME",
]
