"""Ingress-protocol adapter base + the OpenAI identity adapter.

An adapter converts between a client wire protocol and the proxy's internal OpenAI
shape. Four surfaces, each pure:

  * ``parse_request(body, headers, path_model)`` → ``(messages, model, params)`` (OpenAI)
  * ``serialise_response(openai_response)``       → the caller's non-stream body
  * ``serialise_error(status, message, code)``    → ``(body, http_status)``
  * ``stream_translator()``                       → a :class:`StreamTranslator`

The OpenAI adapter is the identity: it returns the request/response essentially
unchanged, so the existing ``/v1/chat/completions`` path keeps its exact behaviour.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional, Tuple


# The default / primary ingress protocol — the wire format the pipeline speaks
# internally. This is the ONE place the name is written; every other module derives it
# from here or from an adapter's own ``.name``, so there is no scattered "openai"
# literal to drift (config-driven / no-hardcoded-provider-names rule).
DEFAULT_PROTOCOL_NAME = "openai"


def sse_line(obj: Any) -> str:
    """Serialise one object as a single SSE ``data:`` frame (compact JSON)."""
    return f"data: {json.dumps(obj, separators=(',', ':'))}\n\n"


class StreamTranslator:
    """Converts the proxy's OpenAI stream chunks into a protocol's SSE frames.

    ``_stream_response`` drives it: ``start()`` before any chunk, ``chunk(c)`` per
    OpenAI delta, then ``finish()`` on success or ``error(msg)`` if the stream failed.
    Each returns an iterable of ready-to-write SSE strings. Stateful (some protocols
    need block indices / a started flag), so a fresh instance is used per request.
    """

    def start(self) -> Iterable[str]:
        return ()

    def chunk(self, openai_chunk: Dict[str, Any]) -> Iterable[str]:
        raise NotImplementedError

    def error(self, message: str) -> Iterable[str]:
        return ()

    def finish(self) -> Iterable[str]:
        return ()


class IngressProtocol:
    """Base adapter. Subclasses override the four translation surfaces."""

    name: str = DEFAULT_PROTOCOL_NAME
    stream_media_type: str = "text/event-stream"
    # Native-SDK credential channels beyond ``Authorization: Bearer`` (#4). Each adapter
    # declares only the channels ITS SDK actually uses, so ``?key=`` / ``x-api-key`` are
    # never accepted on protocols that don't need them. ``credential_headers`` are checked
    # in order; ``credential_query_param`` (if set) permits ``?<name>=<key>`` — which lands
    # in URL/access logs, so it is opt-in per protocol, never a global auth channel.
    credential_headers: Tuple[str, ...] = ()
    credential_query_param: str = ""

    def parse_request(
        self, body: Dict[str, Any], headers: Optional[Dict[str, str]] = None,
        path_model: str = "",
    ) -> Tuple[List[Dict[str, Any]], str, Dict[str, Any]]:
        raise NotImplementedError

    def serialise_response(self, openai_response: Dict[str, Any]) -> Dict[str, Any]:
        return openai_response

    def serialise_error(self, status: int, message: str, code: str = "") -> Tuple[Dict[str, Any], int]:
        return {"error": {"message": message, "type": "error", "code": code or None}}, status

    def stream_translator(self) -> StreamTranslator:
        raise NotImplementedError


# ── OpenAI identity adapter ────────────────────────────────────────────────────
class _OpenAIStream(StreamTranslator):
    def chunk(self, openai_chunk: Dict[str, Any]) -> Iterable[str]:
        yield sse_line(openai_chunk)

    def error(self, message: str) -> Iterable[str]:
        yield sse_line({"error": message})

    def finish(self) -> Iterable[str]:
        yield "data: [DONE]\n\n"


class OpenAIProtocol(IngressProtocol):
    """Identity adapter — the wire format already IS the internal format."""

    name = DEFAULT_PROTOCOL_NAME

    def parse_request(self, body, headers=None, path_model=""):
        messages = body.get("messages", [])
        model = body.get("model") or ""
        params = {k: v for k, v in body.items() if k not in ("messages", "model")}
        return messages, model, params

    def serialise_response(self, openai_response):
        return openai_response

    def serialise_error(self, status, message, code=""):
        # OpenAI error envelope (matches the existing hand-rolled error bodies).
        return {"error": {"message": message, "type": "invalid_request_error",
                          "code": code or None}}, status

    def stream_translator(self):
        return _OpenAIStream()
