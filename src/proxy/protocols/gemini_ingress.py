"""Google Gemini ``generateContent`` ↔ OpenAI translation.

Covers system instruction, text + inline-image parts, generationConfig sampling params,
function declarations, usage, and finishReason mapping, plus SSE streaming
(``streamGenerateContent?alt=sse``). The model is taken from the URL path
(``…/models/{model}:generateContent``), not the body. Inbound ``functionCall`` /
``functionResponse`` parts round-trip best-effort (rendered as text, matching the
Anthropic adapter); full structural tool replay across protocols is a follow-up.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List

from protocols.base import IngressProtocol, StreamTranslator, sse_line

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
    """Gemini parts list → OpenAI content (string when all-text, else multimodal parts)."""
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
        elif "functionCall" in p or "function_call" in p:  # best-effort: render as text
            fc = p.get("functionCall") or p.get("function_call") or {}
            out.append({"type": "text",
                        "text": f"[functionCall {fc.get('name', '')}]: "
                                f"{json.dumps(fc.get('args', {}) or {})}"})
        elif "functionResponse" in p or "function_response" in p:
            fr = p.get("functionResponse") or p.get("function_response") or {}
            out.append({"type": "text",
                        "text": f"[functionResponse {fr.get('name', '')}]: "
                                f"{json.dumps(fr.get('response', {}) or {})}"})
    if not has_image:
        return "".join(p["text"] for p in out if p.get("type") == "text")
    return out


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
        for c in body.get("contents", []) or []:
            if not isinstance(c, dict):
                continue
            role = _ROLE_IN.get(c.get("role", "user"), "user")
            messages.append({"role": role, "content": _parts_to_openai(c.get("parts"))})

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
    """OpenAI stream chunks → Gemini SSE candidate frames (text only)."""

    def __init__(self) -> None:
        self._model = ""
        self._prompt_tokens = 0
        self._completion_tokens = 0
        self._final_emitted = False

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
        if not (isinstance(text, str) and text) and not finish:
            return
        frame: Dict[str, Any] = {"candidates": [{
            "content": {"role": "model", "parts": [{"text": text or ""}]},
            "index": 0,
        }]}
        if finish:
            frame["candidates"][0]["finishReason"] = _FINISH_REASON.get(finish, "STOP")
            frame["usageMetadata"] = {
                "promptTokenCount": self._prompt_tokens,
                "candidatesTokenCount": self._completion_tokens,
                "totalTokenCount": self._prompt_tokens + self._completion_tokens,
            }
            self._final_emitted = True
        yield sse_line(frame)

    def error(self, message: str) -> Iterable[str]:
        yield sse_line({"error": {"code": 502, "message": message, "status": "UNAVAILABLE"}})

    def finish(self) -> Iterable[str]:
        if self._final_emitted:
            return
        yield sse_line({"candidates": [{
            "content": {"role": "model", "parts": [{"text": ""}]},
            "finishReason": "STOP", "index": 0,
        }], "usageMetadata": {
            "promptTokenCount": self._prompt_tokens,
            "candidatesTokenCount": self._completion_tokens,
            "totalTokenCount": self._prompt_tokens + self._completion_tokens,
        }})
