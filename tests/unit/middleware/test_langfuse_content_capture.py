"""What a Langfuse trace may carry of a request's text.

Every tenant's traces go to one Langfuse project, so a trace's input and output are readable
by anyone with project access. Content capture is therefore off unless the operator (or the
tenant's override) turns it on, and even then a request whose PII was found but left in the
text (G29 or G31 in `flag` mode, or a block) is traced without its content: only masked text
may be stored.
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

from unittest.mock import MagicMock

import pytest

from middleware.langfuse_tracing import finish_trace

_ANSWER = {"choices": [{"message": {"role": "assistant", "content": "Your SSN is noted."}}],
           "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}
_REDACTED = "[redacted: capture_trace_content=false]"


def _traced(make_ctx, capture=None, tenant_capture=None, **flags):
    ctx = make_ctx([{"role": "user", "content": "My SSN is 123-45-6789"}])
    g18 = ctx.config["groups"]["G18_observability"]
    if capture is not None:
        g18["capture_trace_content"] = capture
    if tenant_capture is not None:
        ctx.config.setdefault("tenants", {})[ctx.tenant_id] = {
            "groups": {"G18_observability": {"capture_trace_content": tenant_capture}}}
    for name, value in flags.items():
        setattr(ctx, name, value)
    trace = MagicMock()
    ctx.langfuse_trace = trace
    finish_trace(ctx, _ANSWER)
    generation = [c for c in trace.method_calls if c[0] == "generation"]
    assert generation, "finish_trace wrote no generation"
    return generation[0].kwargs


def test_content_is_not_captured_unless_asked_for(make_ctx):
    kwargs = _traced(make_ctx)
    assert kwargs["input"] == _REDACTED and kwargs["output"] == _REDACTED


def test_content_is_captured_when_the_operator_asks(make_ctx):
    kwargs = _traced(make_ctx, capture=True)
    assert kwargs["input"][0]["content"] == "My SSN is 123-45-6789"
    assert kwargs["output"] == "Your SSN is noted."


def test_a_tenant_override_decides_for_that_tenant(make_ctx):
    assert _traced(make_ctx, capture=True, tenant_capture=False)["input"] == _REDACTED
    assert _traced(make_ctx, capture=False, tenant_capture=True)["input"] != _REDACTED


@pytest.mark.parametrize("flags", [
    {"pii_action": "flag"},                      # G29 found PII and left it in place
    {"pii_action": "block"},
    {"context_trust_pii_action": "flag"},        # G31: PII in retrieved context
    {"context_trust_pii_action": "block"},
    {"pii_action": "mask", "context_trust_pii_action": "flag"},
], ids=lambda f: "+".join(f"{k}={v}" for k, v in f.items()))
def test_unmasked_pii_is_never_captured(make_ctx, flags):
    kwargs = _traced(make_ctx, capture=True, **flags)
    assert kwargs["input"] == _REDACTED and kwargs["output"] == _REDACTED


def test_masked_text_is_captured(make_ctx):
    # Mask mode replaced the PII before the call; the trace stores what the model saw.
    kwargs = _traced(make_ctx, capture=True, pii_action="mask", context_trust_pii_action="mask")
    assert kwargs["input"] != _REDACTED


def _span(make_ctx, capture=None, **flags):
    from middleware.langfuse_tracing import add_span
    ctx = make_ctx([{"role": "user", "content": "find my SSN 123-45-6789"}])
    if capture is not None:
        ctx.config["groups"]["G18_observability"]["capture_trace_content"] = capture
    for name, value in flags.items():
        setattr(ctx, name, value)
    trace = MagicMock()
    ctx.langfuse_trace = trace
    add_span(ctx, "G07-retrieval",
             span_input={"rag_query": "find my SSN 123-45-6789", "tokens_before": 40,
                         "chunks": ["text"], "filters": {"k": "v"}},
             output={"chunks_retrieved": 3, "cache_hit": False, "ratio": 0.5, "summary": "x"})
    return [c for c in trace.method_calls if c[0] == "span"][0].kwargs


@pytest.mark.parametrize("setup", [{}, {"capture": True, "pii_action": "flag"}],
                         ids=["capture-off", "pii-flagged"])
def test_a_span_keeps_its_measurements_and_drops_its_text(make_ctx, setup):
    kwargs = _span(make_ctx, **setup)
    assert kwargs["input"] == {"tokens_before": 40}
    assert kwargs["output"] == {"chunks_retrieved": 3, "cache_hit": False, "ratio": 0.5}


def test_a_span_keeps_everything_when_content_is_captured(make_ctx):
    kwargs = _span(make_ctx, capture=True)
    assert kwargs["input"]["rag_query"] == "find my SSN 123-45-6789"
    assert kwargs["output"]["summary"] == "x"
