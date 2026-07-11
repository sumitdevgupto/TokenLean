"""Google Gemini ``generateContent`` ↔ OpenAI translation.

Covers system instruction, text + inline-image parts, generationConfig sampling params,
function declarations, usage, and finishReason mapping, plus SSE streaming
(``streamGenerateContent?alt=sse``). The model is taken from the URL path
(``…/models/{model}:generateContent``), not the body. ``functionCall``/``functionResponse``
round-trip structurally in both directions, non-streaming and streaming: inbound
``functionCall`` parts become the assistant's own OpenAI ``tool_calls`` (with a
synthesised id — Gemini parts carry none) and ``functionResponse`` parts become
``role:"tool"`` messages correlated to the matching prior call by function name;
outbound ``tool_calls`` become ``functionCall`` parts.

Malformed tool history (by design): an orphaned ``functionResponse`` — no matching
pending call of that name — degrades to text rather than emit a dangling ``role:"tool"``;
an unanswered *non-trailing* ``functionCall`` is passed through as-is (never fabricated),
so malformed tool history may 400 at the provider — the same outcome it returns for that
input. Correlation limit (D1, inherent): because Gemini parts carry no id, a
``functionResponse`` is matched to its ``functionCall`` FIFO per function name — the only
key the id-less protocol provides. Two *parallel* calls to the **same** function answered
out of order therefore bind to the wrong call (no error, silently mis-correlated). Calls
to distinct functions, or same-name calls answered in order, correlate correctly.
"""
from __future__ import annotations

import json
from collections import deque
from typing import Any, Dict, Iterable, List

from protocols.base import (
    IngressProtocol, StreamTranslator, sse_line, finalize_fanout, safe_json_dumps,
)

# OpenAI finish_reason → Gemini finishReason.
_FINISH_REASON = {
    "stop": "STOP", "length": "MAX_TOKENS", "content_filter": "SAFETY",
    "tool_calls": "STOP", "function_call": "STOP",
}
_GOOGLE_STATUS = {
    400: "INVALID_ARGUMENT", 401: "UNAUTHENTICATED", 403: "PERMISSION_DENIED",
    404: "NOT_FOUND", 429: "RESOURCE_EXHAUSTED", 500: "INTERNAL",
    502: "UNAVAILABLE", 503: "UNAVAILABLE",
}
_ROLE_IN = {"user": "user", "model": "assistant", "system": "system"}


def _parts_to_openai(parts: Any) -> Any:
    """Gemini text/inline-image parts → OpenAI content (string when all-text, else
    multimodal parts).

    Only text + inlineData contribute; any other part (incl. ``functionCall`` /
    ``functionResponse``) is ignored here. In the message path those tool parts are
    extracted upstream by ``_parts_to_openai_messages`` so they never reach this
    function; the ``systemInstruction`` path calls this directly, where a tool part
    would be nonsensical and is simply dropped (system instructions are text)."""
    if not isinstance(parts, list):
        return ""
    out: List[Dict[str, Any]] = []
    has_image = False
    for p in parts:
        if not isinstance(p, dict):
            continue
        if "text" in p:
            out.append({"type": "text", "text": p.get("text", "")})
        elif "inlineData" in p or "inline_data" in p:
            inline = p.get("inlineData") or p.get("inline_data") or {}
            mime = inline.get("mimeType") or inline.get("mime_type") or "image/png"
            data = inline.get("data", "")
            out.append({"type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{data}"}})
            has_image = True
    if not has_image:
        return "".join(p["text"] for p in out if p.get("type") == "text")
    return out


class _GeminiToolState:
    """Cross-message state for the inbound fan-out (``parse_request``'s message loop).

    Gemini ``functionCall``/``functionResponse`` parts carry no id, unlike Anthropic's
    ``tool_use``/``tool_result``. Synthesises a deterministic id (``call_<n>``) per
    functionCall and correlates each functionResponse to the matching prior call —
    FIFO per function name (the only correlation key Gemini provides). An unmatched
    functionResponse degrades to text instead of emitting an orphaned OpenAI
    ``role:"tool"`` message — litellm/providers reject a ``tool_call_id`` with no
    matching assistant ``tool_calls[].id`` (400)."""

    __slots__ = ("counter", "pending")

    def __init__(self) -> None:
        self.counter = 0
        self.pending: Dict[str, deque] = {}


def _parts_to_openai_messages(role: str, parts: Any, state: "_GeminiToolState") -> List[Dict[str, Any]]:
    """Gemini message (role + parts) → one or more OpenAI messages.

    Mirrors the Anthropic fan-out: ``functionCall`` parts become the assistant's own
    ``tool_calls[]`` and ``functionResponse`` parts become ``role:"tool"`` messages
    matched to the prior call. Plain text/image parts — the common case — still
    collapse to exactly one message, byte-identical to before."""
    if not isinstance(parts, list):
        return [{"role": role, "content": _parts_to_openai(parts)}]

    residual: List[Dict[str, Any]] = []  # text / inlineData / degraded-tool parts
    tool_calls: List[Dict[str, Any]] = []
    tool_msgs: List[Dict[str, Any]] = []
    new_calls: List[tuple] = []  # (name, id) for this message — registered AFTER the loop

    for p in parts:
        if not isinstance(p, dict):
            continue
        if "functionCall" in p or "function_call" in p:
            fc = p.get("functionCall") or p.get("function_call") or {}
            name = fc.get("name", "")
            if role == "assistant" and name:
                call_id = f"call_{state.counter}"
                state.counter += 1
                tool_calls.append({"id": call_id, "type": "function", "function": {
                    "name": name, "arguments": safe_json_dumps(fc.get("args") or {})}})
                new_calls.append((name, call_id))
            else:
                # Malformed (no name) or a functionCall outside a model turn — degrade
                # defensively rather than drop silently.
                residual.append({"type": "text",
                                 "text": f"[functionCall {name}]: {safe_json_dumps(fc.get('args', {}) or {})}"})
        elif "functionResponse" in p or "function_response" in p:
            fr = p.get("functionResponse") or p.get("function_response") or {}
            name = fr.get("name", "")
            # Match only calls REGISTERED by a prior message (a response can't answer a
            # call declared in the same turn — that would emit role:"tool" before its
            # assistant tool_calls). Unmatched → degrade to text, never a dangling call.
            # D1 (inherent, see module docstring): FIFO-by-name is the only key Gemini's
            # id-less parts allow — same-name parallel calls answered out of order mis-bind.
            pending = state.pending.get(name)
            if name and pending:
                call_id = pending.popleft()
                tool_msgs.append({"role": "tool", "tool_call_id": call_id,
                                  "content": safe_json_dumps(fr.get("response") or {})})
            else:
                residual.append({"type": "text",
                                 "text": f"[functionResponse {name}]: {safe_json_dumps(fr.get('response', {}) or {})}"})
        else:
            residual.append(p)  # text / inlineData — let _parts_to_openai collapse it

    for name, call_id in new_calls:
        state.pending.setdefault(name, deque()).append(call_id)
    return finalize_fanout(role, _parts_to_openai(residual), tool_calls, tool_msgs)


class GeminiProtocol(IngressProtocol):
    name = "gemini"
    # Gemini SDK authenticates with x-goog-api-key or ?key=<proxy-key> (the latter appears
    # in URL logs, so it is scoped to the Gemini routes only, never accepted elsewhere).
    credential_headers = ("x-goog-api-key",)
    credential_query_param = "key"

    def parse_request(self, body, headers=None, path_model=""):
        messages: List[Dict[str, Any]] = []
        sys_inst = body.get("systemInstruction") or body.get("system_instruction")
        if isinstance(sys_inst, dict):
            sys_text = _parts_to_openai(sys_inst.get("parts"))
            if isinstance(sys_text, str) and sys_text:
                messages.append({"role": "system", "content": sys_text})
        state = _GeminiToolState()
        for c in body.get("contents", []) or []:
            if not isinstance(c, dict):
                continue
            role = _ROLE_IN.get(c.get("role", "user"), "user")
            messages.extend(_parts_to_openai_messages(role, c.get("parts"), state))

        model = path_model or body.get("model") or ""
        if model.startswith("models/"):
            model = model.split("/", 1)[1]
        params: Dict[str, Any] = {}
        gen = body.get("generationConfig") or body.get("generation_config") or {}
        if isinstance(gen, dict):
            if gen.get("maxOutputTokens") is not None:
                params["max_tokens"] = gen["maxOutputTokens"]
            elif gen.get("max_output_tokens") is not None:
                params["max_tokens"] = gen["max_output_tokens"]
            if "temperature" in gen:
                params["temperature"] = gen["temperature"]
            if gen.get("topP") is not None:
                params["top_p"] = gen["topP"]
            if gen.get("stopSequences"):
                params["stop"] = gen["stopSequences"]
        tools = body.get("tools")
        fns = _gemini_tools_to_openai(tools)
        if fns:
            params["tools"] = fns
        return messages, model, params

    def serialise_response(self, resp):
        choice = (resp.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        parts: List[Dict[str, Any]] = []
        text = msg.get("content")
        if isinstance(text, str) and text:
            parts.append({"text": text})
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            args = fn.get("arguments")
            try:
                inp = json.loads(args) if isinstance(args, str) else (args or {})
            except Exception:
                inp = {}
            parts.append({"functionCall": {"name": fn.get("name", ""), "args": inp}})
        if not parts:
            parts.append({"text": ""})
        usage = resp.get("usage") or {}
        prompt = usage.get("prompt_tokens", 0) or 0
        completion = usage.get("completion_tokens", 0) or 0
        return {
            "candidates": [{
                "content": {"role": "model", "parts": parts},
                "finishReason": _FINISH_REASON.get(choice.get("finish_reason") or "stop", "STOP"),
                "index": 0,
                "safetyRatings": [],
            }],
            "usageMetadata": {
                "promptTokenCount": prompt,
                "candidatesTokenCount": completion,
                "totalTokenCount": prompt + completion,
            },
            "modelVersion": resp.get("model", ""),
        }

    def serialise_error(self, status, message, code=""):
        return {"error": {"code": status, "message": message,
                          "status": _GOOGLE_STATUS.get(status, "UNKNOWN")}}, status

    def stream_translator(self):
        return _GeminiStream()


def _gemini_tools_to_openai(tools: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        for fd in t.get("functionDeclarations") or t.get("function_declarations") or []:
            if isinstance(fd, dict):
                out.append({"type": "function", "function": {
                    "name": fd.get("name", ""),
                    "description": fd.get("description", ""),
                    "parameters": fd.get("parameters", {}) or {},
                }})
    return out


class _GeminiStream(StreamTranslator):
    """OpenAI stream chunks → Gemini SSE candidate frames (text + functionCall parts).

    Text deltas stream live as incremental candidate frames. Streamed ``tool_calls``
    deltas (name / argument fragments, keyed by index) are accumulated — Gemini has no
    incremental functionCall-args frame — and flushed as ``functionCall`` parts in the
    terminal frame (on ``finish_reason`` or in ``finish()``), mirroring the Anthropic
    stream's tool-call accumulation."""

    def __init__(self) -> None:
        self._model = ""
        self._prompt_tokens = 0
        self._completion_tokens = 0
        self._final_emitted = False
        # index → {"name","args"} accumulated across tool_call deltas.
        self._tool_calls: Dict[int, Dict[str, str]] = {}

    def _tool_call_parts(self) -> List[Dict[str, Any]]:
        parts: List[Dict[str, Any]] = []
        for idx in sorted(self._tool_calls):
            slot = self._tool_calls[idx]
            if not slot["name"]:
                continue
            try:
                args = json.loads(slot["args"]) if slot["args"].strip() else {}
            except Exception:
                args = {}
            parts.append({"functionCall": {"name": slot["name"], "args": args}})
        return parts

    def chunk(self, openai_chunk: Dict[str, Any]) -> Iterable[str]:
        self._model = openai_chunk.get("model") or self._model
        usage = openai_chunk.get("usage") or {}
        if usage:
            self._prompt_tokens = usage.get("prompt_tokens", self._prompt_tokens) or self._prompt_tokens
            self._completion_tokens = usage.get("completion_tokens", self._completion_tokens) or self._completion_tokens
        choice = (openai_chunk.get("choices") or [{}])[0]
        delta = choice.get("delta") or {}
        text = delta.get("content")
        finish = choice.get("finish_reason")
        tool_deltas = delta.get("tool_calls") or []
        for tc in tool_deltas:
            if not isinstance(tc, dict):
                continue
            idx = tc.get("index") or 0  # `or 0` (not default) so an explicit null can't key the dict
            slot = self._tool_calls.setdefault(idx, {"name": "", "args": ""})
            fn = tc.get("function") or {}
            if fn.get("name"):
                slot["name"] = fn["name"]
            if isinstance(fn.get("arguments"), str):
                slot["args"] += fn["arguments"]
        has_text = isinstance(text, str) and text
        if not has_text and not finish and not tool_deltas:
            return
        if finish:
            # Emit BOTH the finish chunk's own text (Gemini parts may legally hold text
            # alongside a functionCall) AND any accumulated tool calls — don't let the
            # tool parts shadow the narration.
            parts = ([{"text": text}] if has_text else []) + self._tool_call_parts()
            if not parts:
                parts = [{"text": ""}]
            frame: Dict[str, Any] = {"candidates": [{
                "content": {"role": "model", "parts": parts},
                "finishReason": _FINISH_REASON.get(finish, "STOP"),
                "index": 0,
            }], "usageMetadata": {
                "promptTokenCount": self._prompt_tokens,
                "candidatesTokenCount": self._completion_tokens,
                "totalTokenCount": self._prompt_tokens + self._completion_tokens,
            }}
            self._final_emitted = True
            yield sse_line(frame)
        elif has_text:
            yield sse_line({"candidates": [{
                "content": {"role": "model", "parts": [{"text": text}]},
                "index": 0,
            }]})
        # else: a tool_calls-only delta (no text, no finish) accumulates silently —
        # Gemini has no incremental functionCall-args frame to emit yet.

    def error(self, message: str) -> Iterable[str]:
        yield sse_line({"error": {"code": 502, "message": message, "status": "UNAVAILABLE"}})

    def finish(self) -> Iterable[str]:
        if self._final_emitted:
            return
        parts = self._tool_call_parts() or [{"text": ""}]
        yield sse_line({"candidates": [{
            "content": {"role": "model", "parts": parts},
            "finishReason": "STOP", "index": 0,
        }], "usageMetadata": {
            "promptTokenCount": self._prompt_tokens,
            "candidatesTokenCount": self._completion_tokens,
            "totalTokenCount": self._prompt_tokens + self._completion_tokens,
        }})
