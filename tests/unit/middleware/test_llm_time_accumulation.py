"""Middleware provider calls are booked as LLM time, not proxy overhead.

G10 summarisation and G09 schema compaction make real provider calls inside the
request pipeline. Each must add its wall-time to ``ctx.llm_elapsed_ms`` (with +=,
preserving any time already accumulated) so the SLA split — proxy_overhead =
elapsed − llm_elapsed_ms — does not misattribute LLM latency to the proxy.
(G06's accumulation is covered by test_cascade_shortcircuit.py::_timed_llm.)
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


@pytest.mark.asyncio
async def test_g10_summarise_accumulates_llm_time(make_ctx, monkeypatch):
    import litellm
    from middleware import g10_memory

    ctx = make_ctx([{"role": "user", "content": "hello"}])
    ctx.llm_elapsed_ms = 3.0  # pretend earlier middleware already spent 3ms of LLM time

    fake_adapter = MagicMock()
    fake_adapter.name = "openai"
    fake_adapter.requires_api_key.return_value = True
    fake_adapter.build_call.return_value = ("gpt-4o-mini", {"api_key": "sk"})

    async def _slow_acompletion(**kwargs):
        await asyncio.sleep(0.02)  # ~20ms — reliably > timer resolution
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="a compact summary"))]
        )

    monkeypatch.setattr("providers.get_adapter", lambda *a, **k: fake_adapter)
    monkeypatch.setattr("providers.get_provider_entry", lambda *a, **k: {})
    monkeypatch.setattr("auth.api_key_manager.get_llm_provider_key", lambda *a, **k: "sk")
    monkeypatch.setattr(litellm, "acompletion", _slow_acompletion)

    result = await g10_memory._summarise(
        [{"role": "user", "content": "turn 1"}, {"role": "assistant", "content": "turn 2"}],
        "gpt-4o-mini",
        ctx,
    )

    assert result == "a compact summary"
    # += semantics: prior 3ms preserved AND this call's ~20ms added on top.
    assert ctx.llm_elapsed_ms > 3.0


@pytest.mark.asyncio
async def test_g10_summarise_records_time_even_on_error(make_ctx, monkeypatch):
    import litellm
    from middleware import g10_memory

    ctx = make_ctx([{"role": "user", "content": "hello"}])

    fake_adapter = MagicMock()
    fake_adapter.name = "openai"
    fake_adapter.requires_api_key.return_value = True
    fake_adapter.build_call.return_value = ("gpt-4o-mini", {"api_key": "sk"})

    async def _boom(**kwargs):
        await asyncio.sleep(0.01)
        raise RuntimeError("provider down")

    monkeypatch.setattr("providers.get_adapter", lambda *a, **k: fake_adapter)
    monkeypatch.setattr("providers.get_provider_entry", lambda *a, **k: {})
    monkeypatch.setattr("auth.api_key_manager.get_llm_provider_key", lambda *a, **k: "sk")
    monkeypatch.setattr(litellm, "acompletion", _boom)

    # _summarise swallows the error and returns a placeholder, but the finally
    # block must still have booked the (failed) call's wall-time as LLM time.
    result = await g10_memory._summarise(
        [{"role": "user", "content": "t1"}], "gpt-4o-mini", ctx
    )
    assert result == "[summary unavailable]"
    assert ctx.llm_elapsed_ms > 0.0


@pytest.mark.asyncio
async def test_g09_instructor_accumulates_llm_time(make_ctx, monkeypatch):
    from middleware.g09_context_schema import G09ContextSchema

    prose = (
        "Customer Alice Smith called about order #A99 which was shipped. "
        "She requested a status update and mentioned she needs it delivered soon. "
        "The customer told us the order was placed two weeks ago and is now urgent."
    )
    ctx = make_ctx([
        {"role": "system", "content": "You are a helpful support agent."},  # primary — never rewritten
        {"role": "system", "content": prose},                               # secondary prose → instructor path
        {"role": "user", "content": "Summarise."},
    ])
    groups = ctx.config.setdefault("groups", {})
    groups["G9_context_schema"] = {
        "enabled": True,
        "use_instructor": True,
        "schema_fields": {"customer_name": "Name", "order_id": "Order"},
        "instructor_model": "gpt-4o-mini",
        "instructor_timeout_ms": 2000,
        "instructor_fallback_to_heuristic": True,
    }
    ctx.llm_elapsed_ms = 1.0

    async def _slow_compact(*a, **k):
        await asyncio.sleep(0.02)
        return "customer_name=Alice Smith; order_id=A99"

    monkeypatch.setattr("auth.api_key_manager.get_llm_provider_key", lambda *a, **k: "sk-test")
    monkeypatch.setattr("config_loader.get_provider_model_prefixes", lambda: {"gpt": "openai"})
    monkeypatch.setattr("middleware.g09_context_schema._compact_with_schema", _slow_compact)

    ctx = await G09ContextSchema().process_request(ctx)

    # Provider call time booked (+= over the pre-existing 1ms).
    assert ctx.llm_elapsed_ms > 1.0
