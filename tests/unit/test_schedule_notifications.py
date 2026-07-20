"""main._schedule_notifications — outbound webhook event attribution.

Regression coverage (2026-07-20 code review): the GUARDRAIL_BLOCK / PII_DETECTED payload
fields must be attributed to WHICHEVER guardrail(s) actually blocked/acted, never picked
by "whichever field happens to be non-empty" — G30/G29 running in a non-blocking `flag`
mode alongside G31 in a blocking mode must never mask the real cause/severity.
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "proxy")))

from types import SimpleNamespace

import pytest

import events
import main


def _ctx(**kw):
    defaults = dict(
        tenant_id="t1", request_id="r1",
        guardrail_action=None, guardrail_categories=[],
        context_trust_action=None, context_trust_categories=[],
        pii_action=None, pii_entities=[], pii_redactions=0,
        context_trust_pii_action=None, context_trust_pii_entities=[],
        context_trust_pii_redactions=0,
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


@pytest.fixture(autouse=True)
def _dispatcher(monkeypatch):
    captured = []

    async def _disp(tenant_id, event, payload):
        captured.append((tenant_id, event, payload))

    events.set_webhook_dispatcher(_disp)
    yield captured
    events.set_webhook_dispatcher(None)


def _run(ctx):
    # schedule_event uses asyncio.create_task, which needs a RUNNING loop — call
    # _schedule_notifications from inside the coroutine, then yield twice so the
    # scheduled task(s) actually execute before asyncio.run tears the loop down.
    import asyncio
    async def _drive():
        main._schedule_notifications(ctx)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    asyncio.run(_drive())


def test_noop_without_dispatcher(monkeypatch):
    events.set_webhook_dispatcher(None)
    ctx = _ctx(guardrail_action="block", guardrail_categories=["x"])
    main._schedule_notifications(ctx)  # must not raise, no dispatcher installed


def test_noop_when_nothing_flagged(_dispatcher):
    _run(_ctx())
    assert _dispatcher == []


class TestGuardrailBlockAttribution:
    def test_g30_block_only(self, _dispatcher):
        _run(_ctx(guardrail_action="block", guardrail_categories=["instruction_override"]))
        assert len(_dispatcher) == 1
        _, event, payload = _dispatcher[0]
        assert event == events.GUARDRAIL_BLOCK
        assert payload["categories"] == ["instruction_override"]
        assert payload["source"] == "user"

    def test_g31_block_only(self, _dispatcher):
        _run(_ctx(context_trust_action="block", context_trust_categories=["prompt_leak"]))
        _, event, payload = _dispatcher[0]
        assert payload["categories"] == ["prompt_leak"]
        assert payload["source"] == "retrieved"

    def test_g30_flag_g31_block_reports_g31_categories_not_g30(self, _dispatcher):
        """The exact bug: G30 flags (non-blocking, but populates categories) while G31
        blocks. The payload must report G31's categories/source, not G30's — a non-
        blocking flag must never mask which guardrail actually caused the refusal."""
        ctx = _ctx(
            guardrail_action="flag", guardrail_categories=["role_play_jailbreak"],
            context_trust_action="block", context_trust_categories=["prompt_leak"],
        )
        _run(ctx)
        assert len(_dispatcher) == 1
        _, event, payload = _dispatcher[0]
        assert payload["categories"] == ["prompt_leak"]          # G31's, not G30's
        assert "role_play_jailbreak" not in payload["categories"]
        assert payload["source"] == "retrieved"                  # not "user"

    def test_both_block_merges_categories_and_sources(self, _dispatcher):
        ctx = _ctx(
            guardrail_action="block", guardrail_categories=["a"],
            context_trust_action="block", context_trust_categories=["b"],
        )
        _run(ctx)
        _, event, payload = _dispatcher[0]
        assert payload["categories"] == ["a", "b"]
        assert payload["source"] == "user+retrieved"

    def test_no_event_when_neither_blocks(self, _dispatcher):
        # G30 flags but doesn't block, G31 untouched — no GUARDRAIL_BLOCK event at all.
        _run(_ctx(guardrail_action="flag", guardrail_categories=["x"]))
        assert _dispatcher == []


class TestPiiDetectedAttribution:
    def test_g29_flag_only(self, _dispatcher):
        _run(_ctx(pii_action="flag", pii_entities=["EMAIL"], pii_redactions=1))
        _, event, payload = _dispatcher[0]
        assert event == events.PII_DETECTED
        assert payload["action"] == "flag"
        assert payload["source"] == "user"

    def test_g31_block_only(self, _dispatcher):
        _run(_ctx(context_trust_pii_action="block", context_trust_pii_entities=["US_SSN"],
                  context_trust_pii_redactions=1))
        _, event, payload = _dispatcher[0]
        assert payload["action"] == "block"
        assert payload["source"] == "retrieved"

    def test_g29_flag_g31_block_reports_block_not_flag(self, _dispatcher):
        """The exact bug: G29 flags (non-blocking) while G31 blocks. The payload must
        report 'block' (the more severe, actually-occurred outcome), never 'flag' — a
        SIEM consumer must not mistake a real refusal for an informational flag."""
        ctx = _ctx(
            pii_action="flag", pii_entities=["EMAIL"], pii_redactions=1,
            context_trust_pii_action="block", context_trust_pii_entities=["US_SSN"],
            context_trust_pii_redactions=1,
        )
        _run(ctx)
        _, event, payload = _dispatcher[0]
        assert payload["action"] == "block"
        assert payload["source"] == "user+retrieved"
        # Entities/count still union both sources (this part was already correct).
        assert set(payload["entities"]) == {"EMAIL", "US_SSN"}
        assert payload["count"] == 2

    def test_severity_precedence_mask_beats_flag(self, _dispatcher):
        ctx = _ctx(
            pii_action="flag", pii_entities=["EMAIL"], pii_redactions=1,
            context_trust_pii_action="mask", context_trust_pii_entities=["PHONE"],
            context_trust_pii_redactions=1,
        )
        _run(ctx)
        _, event, payload = _dispatcher[0]
        assert payload["action"] == "mask"

    def test_no_event_when_neither_acted(self, _dispatcher):
        _run(_ctx())
        assert _dispatcher == []
