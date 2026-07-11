"""Trust & Safety engines shared by the G29 (PII redaction) and G30 (guardrails)
middleware.

Both engines are pure, dependency-free (stdlib ``re`` only), and deterministic so
they can be unit-tested exhaustively and run inline on the request path without a
network hop. The PII detector can optionally delegate to Microsoft Presidio when it
is installed and enabled in config (``guardrails/pii.py``), but the regex tier is
always available and is the default.

Open-core: this whole package is OSS core — the detection/masking/scan primitives
ship in every tier and are never behind a paywall. Only the managed console, the
signed attestation, and the subscription red-team ruleset feed are commercial.
"""

import time
from typing import Any, Dict

from guardrails.pii import (
    PiiDetector,
    PiiMatch,
    RedactionResult,
    mask_matches,
    unmask_text,
    remask_with_vault,
)
from guardrails.injection import (
    InjectionScanner,
    InjectionVerdict,
    DEFAULT_INJECTION_RULES,
)


def content_filter_response(request_id: str, model: str, message: str) -> Dict[str, Any]:
    """OpenAI-shaped chat-completion refusal used by a G30 guardrail block or a
    G29 PII-policy block.

    Returned as HTTP 200 with ``finish_reason: "content_filter"`` (the shape OpenAI
    itself uses for a moderation refusal) so OpenAI-SDK clients handle it on their
    normal completion path rather than as an unexpected 4xx. main.py bills it once,
    exactly like a bypass — a policy refusal is a served proxy decision, and billing
    it closes the free-abuse vector. Zero token usage is reported (no model call)."""
    return {
        "id": f"chatcmpl-blocked-{request_id}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": message},
            "finish_reason": "content_filter",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


__all__ = [
    "PiiDetector",
    "PiiMatch",
    "RedactionResult",
    "mask_matches",
    "unmask_text",
    "remask_with_vault",
    "InjectionScanner",
    "InjectionVerdict",
    "DEFAULT_INJECTION_RULES",
    "content_filter_response",
]
