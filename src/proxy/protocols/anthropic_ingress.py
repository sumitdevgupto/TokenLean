"""Anthropic Messages API (``/v1/messages``) ↔ OpenAI translation.

Covers the common surface fully: system prompt, text + image content, core sampling
params, tools, usage, and stop-reason mapping, plus streaming (message_start →
content_block_delta → message_stop). ``tool_use``/``tool_result`` round-trip
structurally in both directions (non-streaming and streaming): inbound ``tool_use``
blocks become the assistant's own OpenAI ``tool_calls`` (ids preserved) and
``tool_result`` blocks become ``role:"tool"`` messages matched to the prior call;
outbound ``tool_calls`` become ``tool_use`` blocks.

Malformed tool history (by design): an orphaned ``tool_result`` — one whose
``tool_use_id`` matches no prior ``tool_use`` — degrades to text rather than emit a
dangling ``role:"tool"``. In the other direction we do **not** fabricate a result for
an unanswered ``tool_use``; it passes through structurally as-is, so a genuinely
malformed conversation (a *non-trailing* call the client never answered) may be
rejected by the provider with a 400 — the same outcome the provider returns for that
input, surfaced honestly rather than hidden by silently reshaping tool structure into
prose. A valid *trailing* "awaiting execution" call passes through unchanged.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List

from protocols.base import (
    IngressProtocol, StreamTranslator, finalize_fanout, safe_json_dumps,
)

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
    """Anthropic text/image content (string or block list) → OpenAI content.

    All-text block lists collapse to a plain string; a list containing images is kept
    as OpenAI multimodal parts. Tool blocks are extracted and fanned out separately by
    ``_message_to_openai`` before this is called — by the time a block list reaches
    here it never contains ``tool_use``/``tool_result`` entries."""
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


class _ToolState:
    """Cross-message state for the inbound fan-out (``parse_request``'s message loop).

    Tracks the ``tool_use`` ids seen so far, so a later ``tool_result`` block can be
    matched to its call. An unmatched ``tool_result`` degrades to text instead of
    emitting an orphaned OpenAI ``role:"tool"`` message — litellm/providers reject a
    ``tool_call_id`` with no matching assistant ``tool_calls[].id`` (400)."""

    __slots__ = ("seen_tool_ids",)

    def __init__(self) -> None:
        self.seen_tool_ids: set = set()


def _message_to_openai(role: str, content: Any, state: "_ToolState") -> List[Dict[str, Any]]:
    """Anthropic message (role + content) → one or more OpenAI messages.

    A single Anthropic turn can carry text, ``tool_use`` (assistant call), and
    ``tool_result`` (a prior call's answer) blocks together. OpenAI/litellm need these
    as separate, well-formed messages: results as ``role:"tool"`` matching a PRIOR
    assistant ``tool_calls[].id``, and calls as the assistant's own ``tool_calls[]`` —
    never a dangling ``role:"tool"`` with no matching call. Plain text/image content —
    the common case — still collapses to exactly one message, byte-identical to before.
    """
    if not isinstance(content, list):
        return [{"role": role, "content": _content_to_openai(content)}]

    residual: List[Dict[str, Any]] = []  # text / image / degraded-tool blocks
    tool_calls: List[Dict[str, Any]] = []
    tool_msgs: List[Dict[str, Any]] = []
    new_ids: List[str] = []  # this message's tool_use ids — committed to state AFTER the loop

    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "tool_use":
            name, tool_id = block.get("name", ""), block.get("id", "")
            if role == "assistant" and name and tool_id:
                tool_calls.append({"id": tool_id, "type": "function", "function": {
                    "name": name, "arguments": safe_json_dumps(block.get("input", {}))}})
                new_ids.append(tool_id)
            else:
                # Malformed (no name/id) or a tool_use outside an assistant turn — Anthropic
                # never sends the latter, but degrade defensively rather than drop silently.
                residual.append({"type": "text",
                                 "text": f"[tool_use {name}]: {safe_json_dumps(block.get('input', {}))}"})
        elif btype == "tool_result":
            tool_use_id = block.get("tool_use_id", "")
            result_text = _flatten_text(block.get("content"))
            # Match only ids from PRIOR messages (already emitted as an assistant tool_calls
            # message). A tool_result sharing a turn with its tool_use is malformed — matching
            # it would emit a role:"tool" BEFORE the assistant message that declares the id
            # (an ordering violation providers reject), so degrade it to text instead.
            if tool_use_id and tool_use_id in state.seen_tool_ids:
                tool_msgs.append({"role": "tool", "tool_call_id": tool_use_id, "content": result_text})
            else:
                residual.append({"type": "text", "text": f"[tool_result {tool_use_id}]: {result_text}"})
        else:
            residual.append(block)  # text / image — let _content_to_openai collapse it

    state.seen_tool_ids.update(new_ids)
    return finalize_fanout(role, _content_to_openai(residual), tool_calls, tool_msgs)


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
        state = _ToolState()
        for m in body.get("messages", []) or []:
            if not isinstance(m, dict):
                continue
            messages.extend(_message_to_openai(m.get("role", "user"), m.get("content"), state))

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
            idx = tc.get("index") or 0  # `or 0` (not default) so an explicit null can't key the dict
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
