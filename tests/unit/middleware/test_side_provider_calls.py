"""Every paid provider call a request makes besides the served one is recorded as a side call,
so G18 adds it to cost_actual (and the spend cap that accrues it).

The G06 judge (plan time and cascade), G09's schema extraction, the G10/G26 summariser and
G11's repair re-ask were never recorded: cost_actual priced only the answer, in the direction
that flatters the savings figure.
"""
import sys
import types

import pytest

USAGE = {"prompt_tokens": 321, "completion_tokens": 12}


def _completion(content):
    return {"choices": [{"message": {"content": content}, "finish_reason": "stop"}],
            "usage": dict(USAGE)}


def _side_calls(ctx):
    return [(c["model"], c["prompt_tokens"], c["completion_tokens"])
            for c in ctx.provider_calls if c.get("side")]


@pytest.mark.asyncio
async def test_the_plan_time_judge_is_a_side_call_on_the_served_tenants_key(make_ctx, monkeypatch):
    import middleware.g06_routing as g06

    tenants = []

    async def _key(model, tenant_id="default"):
        tenants.append(tenant_id)
        return None

    async def _acompletion(**kwargs):
        return _completion('{"tier": "simple", "confidence": 0.9}')

    monkeypatch.setattr(g06, "_resolve_provider_key", _key)
    monkeypatch.setattr(g06.litellm, "acompletion", _acompletion)
    ctx = make_ctx([{"role": "user", "content": "what is 2+2"}], model="gpt-4o")
    ctx.tenant_id = "acme"
    ctx.params["_auth_tenant_id"] = "admin-tenant"     # an admin key acting for acme
    await g06._classify_llm_judge(ctx.messages, ctx.params, {"judge_model": "gpt-4o-mini"},
                                  ctx=ctx)
    assert _side_calls(ctx) == [("gpt-4o-mini", 321, 12)]
    assert tenants == ["acme"]


@pytest.mark.asyncio
async def test_the_cascade_confidence_judge_is_a_side_call(make_ctx, monkeypatch):
    import middleware.g06_routing as g06

    async def _key(model, tenant_id="default"):
        return None

    async def _acompletion(**kwargs):
        return _completion('{"confidence": 0.8}')

    monkeypatch.setattr(g06, "_resolve_provider_key", _key)
    monkeypatch.setattr(g06.litellm, "acompletion", _acompletion)
    ctx = make_ctx([{"role": "user", "content": "hi"}], model="gpt-4o")
    score = await g06._evaluate_response_confidence(
        ctx.messages, _completion("an answer"), "gpt-4o-mini", ctx.tenant_id, ctx=ctx)
    assert score == 0.8
    assert _side_calls(ctx) == [("gpt-4o-mini", 321, 12)]


@pytest.mark.asyncio
async def test_every_schema_extraction_attempt_is_a_side_call(make_ctx, monkeypatch):
    """Instructor may retry; each attempt is a paid call through our wrapper."""
    import litellm
    from middleware import g09_context_schema as g09

    async def _acompletion(*args, **kwargs):
        return _completion('{"a": "1", "b": "2"}')

    class _Client:
        def __init__(self, fn):
            self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=self._create))
            self._fn = fn

        async def _create(self, response_model=None, **kwargs):
            await self._fn(**kwargs)                  # a failed first attempt
            await self._fn(**kwargs)                  # the retry that parsed
            return types.SimpleNamespace(model_dump=lambda: {"a": "1", "b": "2"})

    monkeypatch.setitem(sys.modules, "instructor",
                        types.SimpleNamespace(from_litellm=lambda fn: _Client(fn)))
    monkeypatch.setattr(litellm, "acompletion", _acompletion)
    ctx = make_ctx([{"role": "user", "content": "hi"}], model="gpt-4o")
    out = await g09._compact_with_schema("text", {"a": "x", "b": "y"}, "gpt-4o-mini", "", ctx=ctx)
    assert out == "a=1|b=2"
    assert _side_calls(ctx) == [("gpt-4o-mini", 321, 12)] * 2


@pytest.mark.asyncio
async def test_the_summariser_is_a_side_call(make_ctx, monkeypatch):
    import litellm
    from middleware import history_utils

    class _Resp(dict):
        @property
        def choices(self):
            return [types.SimpleNamespace(message=types.SimpleNamespace(content="a summary"))]

    async def _acompletion(**kwargs):
        return _Resp(_completion("a summary"))

    async def _key(*args, **kwargs):
        return "sk-test"

    monkeypatch.setattr(litellm, "acompletion", _acompletion)
    monkeypatch.setattr("providers.key_resolver.resolve_provider_key", _key)
    ctx = make_ctx([{"role": "user", "content": "hi"}], model="gpt-4o")
    turns = [{"role": "user", "content": "first"}, {"role": "assistant", "content": "reply"}]
    assert await history_utils.summarise_turns(turns, "gpt-4o-mini", ctx) == "a summary"
    assert _side_calls(ctx) == [("gpt-4o-mini", 321, 12)]


@pytest.mark.asyncio
async def test_the_repair_re_ask_is_a_side_call(make_ctx, monkeypatch):
    import litellm
    from middleware import g11_output_format as g11

    async def _acompletion(**kwargs):
        return _completion('{"ok": true}')

    async def _key(*args, **kwargs):
        return "sk-test"

    monkeypatch.setattr(litellm, "acompletion", _acompletion)
    monkeypatch.setattr("providers.key_resolver.resolve_provider_key", _key)
    ctx = make_ctx([{"role": "user", "content": "give json"}], model="gpt-4o")
    ctx.routed_model = "gpt-4o"
    assert await g11._reask(ctx, "not json", None, 50) == '{"ok": true}'
    assert _side_calls(ctx) == [("gpt-4o", 321, 12)]
