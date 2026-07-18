"""Unit tests for the G29 PII-redaction middleware."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

from datetime import datetime, timezone

import pytest

from middleware import RequestContext
from middleware.g29_pii_redaction import G29PiiRedaction
from savings.models import SavingsRecord

EMAIL = "alice@example.com"


def _ctx(messages, mode="flag", enabled=True, params=None, **extra):
    g29 = {"enabled": enabled, "mode": mode}
    g29.update(extra)
    return RequestContext(
        request_id="req-g29", user_id="u",
        original_messages=[dict(m) for m in messages],
        messages=[dict(m) for m in messages],
        model="gpt-4o-mini", routed_model="gpt-4o-mini", params=params or {},
        config={"groups": {"G29_pii_redaction": g29}},
        savings=SavingsRecord(request_id="req-g29", user_id="u",
                              timestamp=datetime.now(timezone.utc),
                              model_requested="gpt-4o-mini", routed_model="gpt-4o-mini",
                              baseline_tokens=10),
    )


# ── Modes ────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_off_mode_passthrough():
    ctx = _ctx([{"role": "user", "content": f"email {EMAIL}"}], mode="off")
    out = await G29PiiRedaction().process_request(ctx)
    assert out.pii_action is None
    assert out.pii_redactions == 0
    assert EMAIL in out.messages[0]["content"]


@pytest.mark.asyncio
async def test_flag_mode_detects_without_mutating():
    ctx = _ctx([{"role": "user", "content": f"email {EMAIL}"}], mode="flag")
    out = await G29PiiRedaction().process_request(ctx)
    assert out.pii_action == "flag"
    assert out.pii_entities == ["EMAIL"]
    assert out.pii_redactions == 1
    assert EMAIL in out.messages[0]["content"]          # unchanged
    assert out.original_messages[0]["content"].endswith(EMAIL)  # untouched


@pytest.mark.asyncio
async def test_mask_mode_replaces_in_place_and_builds_vault():
    ctx = _ctx([{"role": "user", "content": f"Contact {EMAIL} please"}], mode="mask")
    out = await G29PiiRedaction().process_request(ctx)
    assert EMAIL not in out.messages[0]["content"]
    assert "[PII:EMAIL:1]" in out.messages[0]["content"]
    assert out.pii_vault["[PII:EMAIL:1]"] == EMAIL
    assert out.pii_redactions == 1


@pytest.mark.asyncio
async def test_mask_counts_multiple_spans():
    ctx = _ctx([{"role": "user", "content": "a@x.com and b@y.com"}], mode="mask")
    out = await G29PiiRedaction().process_request(ctx)
    assert out.pii_redactions == 2
    assert "a@x.com" not in out.messages[0]["content"]
    assert "b@y.com" not in out.messages[0]["content"]


@pytest.mark.asyncio
async def test_mask_sets_no_cache_to_prevent_lossy_key_collision():
    ctx = _ctx([{"role": "user", "content": f"email {EMAIL}"}], mode="mask")
    out = await G29PiiRedaction().process_request(ctx)
    assert out.no_cache is True   # masked → lossy cache key → G05 must skip


@pytest.mark.asyncio
async def test_flag_mode_leaves_cache_enabled():
    ctx = _ctx([{"role": "user", "content": f"email {EMAIL}"}], mode="flag")
    out = await G29PiiRedaction().process_request(ctx)
    assert out.no_cache is False  # unmutated content → key stays unique → cache safe


@pytest.mark.asyncio
async def test_mask_without_pii_leaves_cache_enabled():
    ctx = _ctx([{"role": "user", "content": "nothing sensitive"}], mode="mask")
    out = await G29PiiRedaction().process_request(ctx)
    assert out.no_cache is False  # nothing masked → key not lossy


# ── F1: detector cache must return the config it was asked for (no torn read) ──
@pytest.mark.asyncio
async def test_get_detector_returns_requested_config_after_other_cached():
    mw = G29PiiRedaction()
    d_narrow = mw._get_detector({"entities": ["EMAIL"]})   # caches a narrow detector
    d_default = mw._get_detector({})                        # must NOT return the narrow one
    assert set(d_narrow.entities) == {"EMAIL"}
    assert "US_SSN" in d_default.entities
    d_narrow_again = mw._get_detector({"entities": ["EMAIL"]})
    assert set(d_narrow_again.entities) == {"EMAIL"}


# ── F2: tool-call / function-call arguments are scanned + masked ────────────────
@pytest.mark.asyncio
async def test_mask_redacts_tool_call_arguments():
    ctx = _ctx([{
        "role": "assistant", "content": None,
        "tool_calls": [{"id": "c1", "type": "function",
                        "function": {"name": "send_email",
                                     "arguments": '{"to": "alice@example.com"}'}}],
    }], mode="mask")
    out = await G29PiiRedaction().process_request(ctx)
    args = out.messages[0]["tool_calls"][0]["function"]["arguments"]
    assert "alice@example.com" not in args
    assert "PII:EMAIL" in args
    assert out.pii_redactions == 1
    assert out.no_cache is True  # masked → not cacheable


@pytest.mark.asyncio
async def test_mask_redacts_legacy_function_call_arguments():
    ctx = _ctx([{"role": "assistant", "content": None,
                 "function_call": {"name": "send", "arguments": '{"email": "bob@y.com"}'}}],
               mode="mask")
    out = await G29PiiRedaction().process_request(ctx)
    assert "bob@y.com" not in out.messages[0]["function_call"]["arguments"]


@pytest.mark.asyncio
async def test_flag_counts_tool_call_pii_without_mutation():
    ctx = _ctx([{"role": "assistant", "content": None,
                 "tool_calls": [{"function": {"arguments": '{"to": "c@z.com"}'}}]}], mode="flag")
    out = await G29PiiRedaction().process_request(ctx)
    assert out.pii_redactions == 1
    assert "c@z.com" in out.messages[0]["tool_calls"][0]["function"]["arguments"]  # unmutated


@pytest.mark.asyncio
async def test_block_mode_short_circuits():
    ctx = _ctx([{"role": "user", "content": f"my email is {EMAIL}"}], mode="block")
    out = await G29PiiRedaction().process_request(ctx)
    assert out.security_blocked is True
    assert out.pii_action == "block"
    assert out.security_block_response["choices"][0]["finish_reason"] == "content_filter"


# ── Response path ────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_response_restores_reversible_placeholders():
    mw = G29PiiRedaction()
    ctx = _ctx([{"role": "user", "content": f"Contact {EMAIL}"}], mode="mask")
    await mw.process_request(ctx)  # populates ctx.pii_vault
    response = {"choices": [{"message": {"role": "assistant",
                "content": "Your email [PII:EMAIL:1] is confirmed."}}]}
    out = await mw.process_response(ctx, response)
    assert out["choices"][0]["message"]["content"] == f"Your email {EMAIL} is confirmed."


@pytest.mark.asyncio
async def test_response_masks_model_generated_pii():
    mw = G29PiiRedaction()
    ctx = _ctx([{"role": "user", "content": "no pii here"}], mode="mask")
    await mw.process_request(ctx)  # no request PII → empty vault
    response = {"choices": [{"message": {"role": "assistant",
                "content": "The SSN is 123-45-6789."}}]}
    out = await mw.process_response(ctx, response)
    assert "123-45-6789" not in out["choices"][0]["message"]["content"]
    assert "[US_SSN]" in out["choices"][0]["message"]["content"]


@pytest.mark.asyncio
async def test_response_flag_mode_counts_without_mutation():
    mw = G29PiiRedaction()
    ctx = _ctx([{"role": "user", "content": "hi"}], mode="flag")
    await mw.process_request(ctx)
    response = {"choices": [{"message": {"role": "assistant",
                "content": "reach me at bob@z.com"}}]}
    out = await mw.process_response(ctx, response)
    assert "bob@z.com" in out["choices"][0]["message"]["content"]  # not mutated
    assert ctx.pii_redactions == 1


# ── Escape hatches (mask mode only) ──────────────────────────────────────────
@pytest.mark.asyncio
async def test_mask_scrubs_rag_query_snapshot():
    ctx = _ctx([{"role": "user", "content": f"Contact {EMAIL}"}], mode="mask",
               params={"rag_query": f"lookup {EMAIL}"})
    out = await G29PiiRedaction().process_request(ctx)
    assert EMAIL not in out.params["rag_query"]


@pytest.mark.asyncio
async def test_mask_scrubs_original_messages_copy():
    ctx = _ctx([{"role": "user", "content": f"Contact {EMAIL}"}], mode="mask")
    out = await G29PiiRedaction().process_request(ctx)
    assert EMAIL not in out.original_messages[0]["content"]


@pytest.mark.asyncio
async def test_flag_mode_leaves_original_and_rag_query_untouched():
    ctx = _ctx([{"role": "user", "content": f"Contact {EMAIL}"}], mode="flag",
               params={"rag_query": f"lookup {EMAIL}"})
    out = await G29PiiRedaction().process_request(ctx)
    assert EMAIL in out.original_messages[0]["content"]
    assert EMAIL in out.params["rag_query"]


# ── Config surface ───────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_entity_narrowing():
    ctx = _ctx([{"role": "user", "content": f"{EMAIL} call 415-555-2671"}],
               mode="mask", entities=["EMAIL"])
    out = await G29PiiRedaction().process_request(ctx)
    assert EMAIL not in out.messages[0]["content"]
    assert "415-555-2671" in out.messages[0]["content"]  # phone not scanned


@pytest.mark.asyncio
async def test_multimodal_parts_masked():
    ctx = _ctx([{"role": "user", "content": [
        {"type": "text", "text": f"email {EMAIL}"},
        {"type": "image_url", "image_url": {"url": "http://x/y.png"}},
    ]}], mode="mask")
    out = await G29PiiRedaction().process_request(ctx)
    assert EMAIL not in out.messages[0]["content"][0]["text"]


@pytest.mark.asyncio
async def test_disabled_passthrough():
    ctx = _ctx([{"role": "user", "content": f"email {EMAIL}"}], enabled=False, mode="mask")
    out = await G29PiiRedaction().process_request(ctx)
    assert out.pii_action is None
    assert EMAIL in out.messages[0]["content"]


@pytest.mark.asyncio
async def test_metric_emitted():
    from middleware.g18_observability import PII_REDACTIONS_TOTAL
    before = PII_REDACTIONS_TOTAL.labels(
        tenant_id="default", entity_type="EMAIL", action="flag")._value.get()
    ctx = _ctx([{"role": "user", "content": f"email {EMAIL}"}], mode="flag")
    await G29PiiRedaction().process_request(ctx)
    after = PII_REDACTIONS_TOTAL.labels(
        tenant_id="default", entity_type="EMAIL", action="flag")._value.get()
    assert after == before + 1


# ── PHI opt-in (Task 9) ──────────────────────────────────────────────────────
from middleware.g29_pii_redaction import G29PiiRedaction as _G29

DEA_NUM = "AB1234563"   # checksum-valid DEA


class TestG29PhiOptIn:
    def test_resolve_entities_default_is_none(self):
        assert _G29._resolve_entities({}) is None

    def test_resolve_entities_phi_flag_adds_phi_set(self):
        out = _G29._resolve_entities({"phi": True})
        assert "DEA" in out and "NPI" in out and "MRN" in out and "ICD10" in out
        assert "EMAIL" in out   # PII default retained

    def test_resolve_entities_phi_token_expands(self):
        out = _G29._resolve_entities({"entities": ["EMAIL", "phi"]})
        assert "EMAIL" in out and "DEA" in out
        assert "US_SSN" not in out   # explicit list wasn't the default

    @pytest.mark.asyncio
    async def test_default_config_ignores_phi(self):
        # An existing tenant (no phi config) must NOT start flagging a DEA number.
        ctx = _ctx([{"role": "user", "content": f"prescriber DEA {DEA_NUM}"}], mode="flag")
        out = await G29PiiRedaction().process_request(ctx)
        assert out.pii_action is None

    @pytest.mark.asyncio
    async def test_phi_flagged_when_enabled(self):
        ctx = _ctx([{"role": "user", "content": f"prescriber DEA {DEA_NUM}"}],
                   mode="flag", phi=True)
        out = await G29PiiRedaction().process_request(ctx)
        assert out.pii_action == "flag"
        assert "DEA" in out.pii_entities

    @pytest.mark.asyncio
    async def test_phi_masked_when_enabled(self):
        ctx = _ctx([{"role": "user", "content": f"prescriber DEA {DEA_NUM}"}],
                   mode="mask", phi=True)
        out = await G29PiiRedaction().process_request(ctx)
        assert DEA_NUM not in out.messages[0]["content"]
        assert "[PII:DEA:1]" in out.messages[0]["content"]

    @pytest.mark.asyncio
    async def test_phi_blocks_when_enabled(self):
        ctx = _ctx([{"role": "user", "content": f"prescriber DEA {DEA_NUM}"}],
                   mode="block", phi=True)
        out = await G29PiiRedaction().process_request(ctx)
        assert out.security_blocked is True
        assert out.security_block_response["choices"][0]["finish_reason"] == "content_filter"
