"""Ingress-protocol adapter base + the OpenAI identity adapter.

An adapter converts between a client wire protocol and the proxy's internal OpenAI
shape. Four surfaces, each pure:

  * ``parse_request(body, headers, path_model)`` → ``(messages, model, params)`` (OpenAI)
  * ``serialise_response(openai_response)``       → the caller's non-stream body
  * ``serialise_error(status, message, code)``    → ``(body, http_status)``
  * ``stream_translator()``                       → a :class:`StreamTranslator`

The OpenAI adapter is the identity: it returns the request/response essentially
unchanged, except that request fields outside the documented parameter set are
dropped (see :func:`filter_client_params`).
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)


# The default / primary ingress protocol — the wire format the pipeline speaks
# internally. This is the ONE place the name is written; every other module derives it
# from here or from an adapter's own ``.name``, so there is no scattered "openai"
# literal to drift (config-driven / no-hardcoded-provider-names rule).
DEFAULT_PROTOCOL_NAME = "openai"


# ── OpenAI ingress parameter allowlist ─────────────────────────────────────────
# The OpenAI adapter used to copy EVERY body field into params. That let a client set
# the proxy's own `_`-prefixed internal flags, and pass fields that litellm reads as
# arguments of the upstream CALL (where it goes, which headers and body it carries)
# rather than as model parameters. Only the fields below are admitted; the Anthropic
# and Gemini adapters already build params from fixed fields.
OPENAI_CHAT_PARAMS = frozenset({
    # Chat Completions API
    "audio", "frequency_penalty", "function_call", "functions", "logit_bias", "logprobs",
    "max_completion_tokens", "max_tokens", "modalities", "n", "parallel_tool_calls",
    "prediction", "presence_penalty", "prompt_cache_key", "prompt_cache_retention",
    "reasoning_effort", "response_format", "safety_identifier", "seed", "service_tier",
    "stop", "store", "stream", "stream_options", "temperature", "tool_choice", "tools",
    "top_logprobs", "top_p", "user", "verbosity", "web_search_options",
    # Provider extensions the adapters understand in OpenAI shape
    "thinking", "thinking_config", "top_k",
})
# TokenLean's unprefixed client parameters, each read by a middleware group. Every
# `x_`-prefixed field is also a TokenLean client parameter and is admitted by prefix
# (providers.outgoing_params_for strips `x_` fields before the upstream call).
TOKENLEAN_CLIENT_PARAMS = frozenset({
    "batch_topic", "complexity", "complexity_tier", "dataset_id", "json_schema",
    "rag_collection", "rag_query", "session_id", "template_id", "token_opt_state",
    "user_id", "workflow_id",
})
CLIENT_PARAM_PREFIX = "x_"
# TokenLean's request headers, keyed as the param each becomes (X-Template-ID ->
# x_template_id). Only these are copied into ctx.params, which G13 persists to the Redis
# batch stream: every other X-* header — an infrastructure one such as X-Cloud-Trace-Context,
# a caller's own routing hint, a future internal header — reaches only the G06 routing rules,
# through ctx.routing_headers (routing_header_params), which nothing persists.
REQUEST_HEADER_PARAMS = frozenset({
    "x_complexity", "x_complexity_tier", "x_compress_user", "x_confidence_score", "x_dataset",
    "x_echo_prompt", "x_feature", "x_jit_retrieval", "x_json_output", "x_no_cache",
    "x_prefix_profile", "x_rag_collection", "x_rag_top_k", "x_route_to", "x_session_id",
    "x_step_inputs_hash", "x_step_name", "x_team", "x_template_id", "x_template_version",
    "x_workflow_id",
})
# X-* headers that are never params, not even for the routing rules: credentials (the
# native-SDK proxy key) and headers the proxy reads from the raw request itself.
_NON_PARAM_HEADERS = frozenset({"x-user-id", "x-scenario-tag", "x-api-key", "x-goog-api-key"})
# The proxy's internal flags. Never admitted from a client, not even via extra_allowed.
INTERNAL_PARAM_PREFIX = "_"
# Set by the ingress when the caller's own request marked its prompt for provider caching.
# G18 reads it: sent straight to the provider, that request would have been cached too, so
# the discount is not the proxy's saving.
CALLER_CACHE_MARKERS = INTERNAL_PARAM_PREFIX + "caller_cache_markers"
_CACHE_MARKER = "cache_control"

# Each distinct dropped name warns once per process; the cap stops a client that sends
# random field names from growing this set (or the log) without bound.
_WARNED_DROPPED: set = set()
_MAX_WARNED_DROPPED = 256
_MAX_LOGGED_NAME = 64


class UnsupportedRequestField(ValueError):
    """A request field this proxy cannot carry to the provider as the client meant it. The
    route answers 400 with this message, so it names the field and never echoes content."""


def filter_client_params(
    body: Dict[str, Any], extra_allowed: Iterable[str] = (),
) -> Tuple[Dict[str, Any], List[str]]:
    """Split an OpenAI request body (minus ``messages``/``model``) into the params the
    proxy admits and the names of the fields it dropped.

    Admitted: :data:`OPENAI_CHAT_PARAMS`, :data:`TOKENLEAN_CLIENT_PARAMS`, any ``x_``
    field, and the operator's ``extra_allowed`` names. A ``_`` field is always dropped.
    """
    allowed = OPENAI_CHAT_PARAMS | TOKENLEAN_CLIENT_PARAMS | frozenset(
        name for name in extra_allowed if isinstance(name, str))
    kept: Dict[str, Any] = {}
    dropped: List[str] = []
    for key, value in body.items():
        if key in ("messages", "model"):
            continue
        if (isinstance(key, str) and not key.startswith(INTERNAL_PARAM_PREFIX)
                and (key in allowed or key.startswith(CLIENT_PARAM_PREFIX))):
            kept[key] = value
        else:
            dropped.append(str(key))
    return kept, dropped


def routing_header_params(headers: Any) -> Dict[str, Any]:
    """Every X-* request header as an ``x_*`` key (X-Team -> x_team): the routing hints a
    G06 rule may match. Credentials and identity headers are left out."""
    if not hasattr(headers, "items"):
        return {}
    out: Dict[str, Any] = {}
    for name, value in headers.items():
        lower = str(name).lower()
        if lower.startswith("x-") and lower not in _NON_PARAM_HEADERS:
            out[lower.replace("-", "_")] = value
    return out


def header_params(headers: Any) -> Dict[str, Any]:
    """The request headers that become ctx.params: TokenLean's own (:data:`REQUEST_HEADER_PARAMS`)."""
    return {key: value for key, value in routing_header_params(headers).items()
            if key in REQUEST_HEADER_PARAMS}


def _log_dropped(dropped: List[str]) -> None:
    """Log dropped field NAMES — never values, which may carry a credential or URL."""
    if not dropped:
        return
    names = [name[:_MAX_LOGGED_NAME] for name in dropped]
    logger.debug("OpenAI ingress dropped body fields %r", names)
    for name in names:
        if name in _WARNED_DROPPED or len(_WARNED_DROPPED) >= _MAX_WARNED_DROPPED:
            continue
        _WARNED_DROPPED.add(name)
        logger.warning(
            "OpenAI ingress dropped unsupported body field %r (value not logged). If a "
            "client relies on it, add the name to ingress.extra_allowed_params.", name)


def cache_marker(item: Any) -> Optional[Dict[str, Any]]:
    """The prompt-cache marker ``item`` carries (its ``cache_control`` object), else None.
    A null or non-object value marks nothing: the provider would not cache on it."""
    value = item.get(_CACHE_MARKER) if isinstance(item, dict) else None
    return value if isinstance(value, dict) else None


def _any_marked(items: Any, *, nested: bool = False) -> bool:
    """Whether any dict in the list ``items`` carries a cache marker. ``nested`` also
    looks one level into each item's own ``content`` blocks (an Anthropic
    ``tool_result`` holds blocks of its own)."""
    if not isinstance(items, list):
        return False
    for item in items:
        if not isinstance(item, dict):
            continue
        if cache_marker(item) is not None or (nested and _any_marked(item.get("content"))):
            return True
    return False


def carries_cache_markers(body: Any) -> bool:
    """True when a request body carries the caller's own prompt-cache markers
    (``cache_control`` objects): on the request itself (Anthropic's automatic caching), a
    message, a content block, a block inside a tool result, an Anthropic ``system`` block
    or a tool.

    Read from the body as it arrived, because translation can drop a marker, and what
    counts is what the caller's own request asked the provider for. Only those places
    count, so a tool parameter that happens to be named ``cache_control`` is not a marker.
    Never raises.
    """
    if not isinstance(body, dict):
        return False
    if (cache_marker(body) is not None or _any_marked(body.get("system"))
            or _any_marked(body.get("tools"))):
        return True
    messages = body.get("messages")
    if not isinstance(messages, list):
        return False
    return _any_marked(messages) or any(
        isinstance(m, dict) and _any_marked(m.get("content"), nested=True) for m in messages)


def sse_line(obj: Any) -> str:
    """Serialise one object as a single SSE ``data:`` frame (compact JSON)."""
    return f"data: {json.dumps(obj, separators=(',', ':'))}\n\n"


def safe_json_dumps(obj: Any) -> str:
    """Compact JSON string for a tool-call ``arguments`` / result payload, tolerant of
    non-serialisable input (falls back to ``{}``). Shared by the ingress adapters so the
    two protocols serialise the same tool payload identically."""
    try:
        return json.dumps(obj if obj is not None else {}, separators=(",", ":"))
    except Exception:
        return "{}"


def finalize_fanout(
    role: str, collapsed: Any, tool_calls: List[Dict[str, Any]],
    tool_msgs: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Assemble one inbound message's fanned-out pieces into ordered OpenAI messages.

    Shared by the Anthropic and Gemini adapters (they differ only in how they RECOGNISE
    tool blocks/parts; the assembly is identical). ``tool_msgs`` (answers to the PRIOR
    assistant turn) come first, then this turn's content/assistant message:
      * assistant + tool_calls → ``content`` is the text/multimodal payload, or ``None``
        when there is no text (OpenAI convention for a tool-call-only assistant turn);
      * otherwise a single ``{role, content}`` message — emitted when there is residual
        content OR there were no tool results at all, so a plain-text turn still yields
        exactly one message (and a tool-result-only turn doesn't append an empty one).
    """
    out: List[Dict[str, Any]] = list(tool_msgs)
    if role == "assistant" and tool_calls:
        out.append({"role": "assistant", "content": collapsed or None, "tool_calls": tool_calls})
    elif collapsed or not tool_msgs:
        out.append({"role": role, "content": collapsed})
    return out


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
    """Identity adapter — the wire format already IS the internal format. Only
    documented request fields become params (:func:`filter_client_params`)."""

    name = DEFAULT_PROTOCOL_NAME

    def parse_request(self, body, headers=None, path_model="", extra_allowed=()):
        messages = body.get("messages", [])
        model = body.get("model") or ""
        params, dropped = filter_client_params(body, extra_allowed)
        _log_dropped(dropped)
        if carries_cache_markers(body):
            params[CALLER_CACHE_MARKERS] = True
        return messages, model, params

    def serialise_response(self, openai_response):
        return openai_response

    def serialise_error(self, status, message, code=""):
        # OpenAI error envelope (matches the existing hand-rolled error bodies).
        return {"error": {"message": message, "type": "invalid_request_error",
                          "code": code or None}}, status

    def stream_translator(self):
        return _OpenAIStream()
