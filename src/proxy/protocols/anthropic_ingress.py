"""Anthropic Messages API (``/v1/messages``) ↔ OpenAI translation.

Covers the common surface fully: system prompt, text + image content, core sampling
params, tools, usage, and stop-reason mapping, plus streaming (message_start →
content_block_delta → message_stop). Tool *result* round-tripping is best-effort
(rendered as text) — full agentic tool replay across protocols is a follow-up.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional, Tuple

from protocols.base import IngressProtocol, StreamTranslator

# OpenAI finish_reason → Anthropic stop_reason.
_STOP_REASON = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "content_filter": "end_turn",
    "function_call": "tool_use",
}
_ERROR_TYPE = {
    400: "invalid_request_error", 401: "authentication_error", 403: "permission_error",
    404: "not_found_error", 413: "request_too_large", 429: "rate_limit_error",
    500: "api_error", 502: "api_error", 503: "overloaded_error",
}


def _content_to_openai(content: Any) -> Any:
    """Anthropic message content (string or block list) → OpenAI content.

    All-text block lists collapse to a plain string; a list containing images is kept
    as OpenAI multimodal parts; tool_use / tool_result blocks degrade to text."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: List[Dict[str, Any]] = []
    has_image = False
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            parts.append({"type": "text", "text": block.get("text", "")})
        elif btype == "image":
            src = block.get("source") or {}
            if src.get("type") == "base64":
                url = f"data:{src.get('media_type', 'image/png')};base64,{src.get('data', '')}"
                parts.append({"type": "image_url", "image_url": {"url": url}})
                has_image = True
            elif src.get("type") == "url":
                parts.append({"type": "image_url", "image_url": {"url": src.get("url", "")}})
                has_image = True
        elif btype == "tool_result":  # best-effort: render as text
            parts.append({"type": "text",
                          "text": f"[tool_result {block.get('tool_use_id', '')}]: "
                                  f"{_flatten_text(block.get('content'))}"})
        elif btype == "tool_use":
            parts.append({"type": "text",
                          "text": f"[tool_use {block.get('name', '')}]: "
                                  f"{json.dumps(block.get('input', {}))}"})
    if not has_image:
        return "".join(p["text"] for p in parts if p.get("type") == "text")
    return parts


def _flatten_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content
                       if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _tool_to_openai(tool: Dict[str, Any]) -> Dict[str, Any]:
    return {"type": "function", "function": {
        "name": tool.get("name", ""),
        "description": tool.get("description", ""),
        "parameters": tool.get("input_schema", {}) or {},
    }}


class AnthropicProtocol(IngressProtocol):
    name = "anthropic"
    # Anthropic SDK authenticates with the x-api-key header (proxy key rides here; the
    # tenant's BYOK provider key is resolved server-side).
    credential_headers = ("x-api-key",)

    def parse_request(self, body, headers=None, path_model=""):
        messages: List[Dict[str, Any]] = []
        system = body.get("system")
        sys_text = system if isinstance(system, str) else _flatten_text(system)
        if sys_text:
            messages.append({"role": "system", "content": sys_text})
        for m in body.get("messages", []) or []:
            if not isinstance(m, dict):
                continue
            messages.append({"role": m.get("role", "user"),
                             "content": _content_to_openai(m.get("content"))})

        model = body.get("model") or path_model or ""
        params: Dict[str, Any] = {}
        if body.get("max_tokens") is not None:
            params["max_tokens"] = body["max_tokens"]
        for k in ("temperature", "top_p", "top_k", "stream", "metadata"):
            if k in body:
                params[k] = body[k]
        if body.get("stop_sequences"):
            params["stop"] = body["stop_sequences"]
        if body.get("tools"):
            params["tools"] = [_tool_to_openai(t) for t in body["tools"] if isinstance(t, dict)]
        if body.get("tool_choice"):
            params["tool_choice"] = _map_tool_choice(body["tool_choice"])
        return messages, model, params

    def serialise_response(self, resp):
        choice = (resp.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        blocks: List[Dict[str, Any]] = []
        text = msg.get("content")
        if isinstance(text, str) and text:
            blocks.append({"type": "text", "text": text})
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            args = fn.get("arguments")
            try:
                inp = json.loads(args) if isinstance(args, str) else (args or {})
            except Exception:
                inp = {}
            blocks.append({"type": "tool_use", "id": tc.get("id", ""),
                           "name": fn.get("name", ""), "input": inp})
        if not blocks:
            blocks.append({"type": "text", "text": ""})
        usage = resp.get("usage") or {}
        return {
            "id": resp.get("id", "") or "msg_0",
            "type": "message",
            "role": "assistant",
            "model": resp.get("model", ""),
            "content": blocks,
            "stop_reason": _STOP_REASON.get(choice.get("finish_reason") or "stop", "end_turn"),
            "stop_sequence": None,
            "usage": {
                "input_tokens": usage.get("prompt_tokens", 0) or 0,
                "output_tokens": usage.get("completion_tokens", 0) or 0,
            },
        }

    def serialise_error(self, status, message, code=""):
        return {"type": "error",
                "error": {"type": _ERROR_TYPE.get(status, "api_error"), "message": message}}, status

    def stream_translator(self):
        return _AnthropicStream()


def _map_tool_choice(tc: Any) -> Any:
    if isinstance(tc, dict):
        t = tc.get("type")
        if t == "auto":
            return "auto"
        if t == "any":
            return "required"
        if t == "tool" and tc.get("name"):
            return {"type": "function", "function": {"name": tc["name"]}}
    return "auto"


def _event(etype: str, data: Dict[str, Any]) -> str:
    """One Anthropic SSE event: an ``event:`` line + a ``data:`` line."""
    return f"event: {etype}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


class _AnthropicStream(StreamTranslator):
    """OpenAI stream chunks → Anthropic message-stream events (text + tool_use blocks).

    Text deltas stream live as ``content_block_delta`` on the index-0 text block.
    Streamed ``tool_calls`` deltas (id / name / argument fragments) are accumulated and
    emitted as complete ``tool_use`` blocks in ``finish()`` — so a streamed agentic reply
    that ends ``stop_reason: tool_use`` actually carries the tool name/id/input the SDK
    needs to run the call (previously the tool_calls were dropped)."""

    def __init__(self) -> None:
        self._started = False
        self._model = ""
        self._msg_id = "msg_0"
        self._stop_reason = "end_turn"
        self._output_tokens = 0
        self._input_tokens = 0
        # index → {"id","name","args"} accumulated across tool_call deltas.
        self._tool_calls: Dict[int, Dict[str, str]] = {}

    def start(self) -> Iterable[str]:
        # Deferred until the first chunk so we can carry the real model/id/usage.
        return ()

    def _emit_start(self) -> Iterable[str]:
        self._started = True
        yield _event("message_start", {"type": "message_start", "message": {
            "id": self._msg_id, "type": "message", "role": "assistant",
            "model": self._model, "content": [], "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": self._input_tokens, "output_tokens": 0},
        }})
        yield _event("content_block_start", {"type": "content_block_start", "index": 0,
                     "content_block": {"type": "text", "text": ""}})

    def chunk(self, openai_chunk: Dict[str, Any]) -> Iterable[str]:
        self._model = openai_chunk.get("model") or self._model
        self._msg_id = openai_chunk.get("id") or self._msg_id
        usage = openai_chunk.get("usage") or {}
        if usage:
            self._input_tokens = usage.get("prompt_tokens", self._input_tokens) or self._input_tokens
            self._output_tokens = usage.get("completion_tokens", self._output_tokens) or self._output_tokens
        choice = (openai_chunk.get("choices") or [{}])[0]
        if choice.get("finish_reason"):
            self._stop_reason = _STOP_REASON.get(choice["finish_reason"], "end_turn")
        delta = choice.get("delta") or {}
        text = delta.get("content")
        if not self._started:
            yield from self._emit_start()
        if isinstance(text, str) and text:
            yield _event("content_block_delta", {"type": "content_block_delta", "index": 0,
                         "delta": {"type": "text_delta", "text": text}})
        # Accumulate streamed tool-call fragments; emitted as tool_use blocks in finish().
        for tc in delta.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            idx = tc.get("index", 0)
            slot = self._tool_calls.setdefault(idx, {"id": "", "name": "", "args": ""})
            if tc.get("id"):
                slot["id"] = tc["id"]
            fn = tc.get("function") or {}
            if fn.get("name"):
                slot["name"] = fn["name"]
            if isinstance(fn.get("arguments"), str):
                slot["args"] += fn["arguments"]

    def error(self, message: str) -> Iterable[str]:
        yield _event("error", {"type": "error",
                     "error": {"type": "api_error", "message": message}})

    def finish(self) -> Iterable[str]:
        if not self._started:
            yield from self._emit_start()
        yield _event("content_block_stop", {"type": "content_block_stop", "index": 0})
        # Emit accumulated tool calls as tool_use blocks (index 1+, after the text block).
        block_index = 1
        for idx in sorted(self._tool_calls):
            slot = self._tool_calls[idx]
            args = slot["args"]
            yield _event("content_block_start", {"type": "content_block_start",
                         "index": block_index, "content_block": {
                             "type": "tool_use", "id": slot["id"],
                             "name": slot["name"], "input": {}}})
            yield _event("content_block_delta", {"type": "content_block_delta",
                         "index": block_index,
                         "delta": {"type": "input_json_delta",
                                   "partial_json": args if args.strip() else "{}"}})
            yield _event("content_block_stop", {"type": "content_block_stop",
                         "index": block_index})
            block_index += 1
        yield _event("message_delta", {"type": "message_delta",
                     "delta": {"stop_reason": self._stop_reason, "stop_sequence": None},
                     "usage": {"output_tokens": self._output_tokens}})
        yield _event("message_stop", {"type": "message_stop"})
