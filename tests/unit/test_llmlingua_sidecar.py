"""Unit tests for the LLMLingua-2 sidecar's digit-preservation contract (G01 5e).

The sidecar must pass `force_reserve_digit` (and date/id separators in
`force_tokens`) into `compress_prompt`, so a value like an incident date
`2023-10-18` is not silently corrupted by the compressor. The model itself is
mocked — these tests assert the wiring, not LLMLingua's behaviour.
"""
import asyncio
import importlib.util
import os

import pytest

# The sidecar app imports fastapi/pydantic/uvicorn; skip cleanly if absent.
pytest.importorskip("fastapi")
pytest.importorskip("pydantic")
pytest.importorskip("uvicorn")

_SIDECAR_APP = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "src", "llmlingua-sidecar", "app.py")
)


def _load_sidecar():
    # Load by file path under a unique module name so it can't collide with the
    # proxy's own `app` module.
    spec = importlib.util.spec_from_file_location("llmlingua_sidecar_app", _SIDECAR_APP)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _RecordingCompressor:
    def __init__(self):
        self.calls = []

    def compress_prompt(self, text, rate, force_tokens, force_reserve_digit):
        self.calls.append({
            "rate": rate,
            "force_tokens": force_tokens,
            "force_reserve_digit": force_reserve_digit,
        })
        # Echo a "compressed" string that keeps the date intact.
        return {"compressed_prompt": "Incident 2023-10-18 logs reviewed"}


def test_compress_passes_force_reserve_digit_on_by_default(monkeypatch):
    sidecar = _load_sidecar()
    rec = _RecordingCompressor()
    monkeypatch.setattr(sidecar, "_get_compressor", lambda: rec)

    req = sidecar.CompressRequest(
        text="Investigate the incident that occurred on 2023-10-18 across all systems.",
        ratio=0.5,
    )
    resp = asyncio.run(sidecar.compress(req))

    assert rec.calls[0]["force_reserve_digit"] is True          # default on
    assert "-" in rec.calls[0]["force_tokens"]                  # date separators preserved
    assert "2023-10-18" in resp.compressed


def test_compress_respects_explicit_force_reserve_digit_false(monkeypatch):
    sidecar = _load_sidecar()
    rec = _RecordingCompressor()
    monkeypatch.setattr(sidecar, "_get_compressor", lambda: rec)

    req = sidecar.CompressRequest(text="x" * 100, ratio=0.5, force_reserve_digit=False)
    asyncio.run(sidecar.compress(req))

    assert rec.calls[0]["force_reserve_digit"] is False


def test_compress_allows_custom_force_tokens(monkeypatch):
    sidecar = _load_sidecar()
    rec = _RecordingCompressor()
    monkeypatch.setattr(sidecar, "_get_compressor", lambda: rec)

    req = sidecar.CompressRequest(text="y" * 100, ratio=0.5, force_tokens=["\n", "%"])
    asyncio.run(sidecar.compress(req))

    assert rec.calls[0]["force_tokens"] == ["\n", "%"]


# ─── Request bounds ───────────────────────────────────────────────────────────
# One CPU instance serves every caller, so an oversized or malformed request is refused
# before the model runs; the proxy then skips compression for that message.

def _refusal(sidecar, req):
    with pytest.raises(sidecar.HTTPException) as exc:
        asyncio.run(sidecar.compress(req))
    return exc.value.status_code


def test_an_oversized_text_is_refused_before_the_model_runs(monkeypatch):
    sidecar = _load_sidecar()
    rec = _RecordingCompressor()
    monkeypatch.setattr(sidecar, "_get_compressor", lambda: rec)
    too_long = sidecar.CompressRequest(text="z" * (sidecar._MAX_TEXT_CHARS + 1), ratio=0.5)
    assert _refusal(sidecar, too_long) == 413 and rec.calls == []
    asyncio.run(sidecar.compress(
        sidecar.CompressRequest(text="z" * sidecar._MAX_TEXT_CHARS, ratio=0.5)))
    assert len(rec.calls) == 1                      # the limit itself is allowed


@pytest.mark.parametrize("ratio", [0, -0.5, 1.01])
def test_a_ratio_outside_0_to_1_is_refused(monkeypatch, ratio):
    sidecar = _load_sidecar()
    rec = _RecordingCompressor()
    monkeypatch.setattr(sidecar, "_get_compressor", lambda: rec)
    assert _refusal(sidecar, sidecar.CompressRequest(text="y" * 100, ratio=ratio)) == 422
    assert rec.calls == []


def test_a_ratio_of_1_is_allowed(monkeypatch):
    sidecar = _load_sidecar()
    rec = _RecordingCompressor()
    monkeypatch.setattr(sidecar, "_get_compressor", lambda: rec)
    asyncio.run(sidecar.compress(sidecar.CompressRequest(text="y" * 100, ratio=1.0)))
    assert rec.calls[0]["rate"] == 1.0


def test_too_many_force_tokens_are_refused(monkeypatch):
    sidecar = _load_sidecar()
    rec = _RecordingCompressor()
    monkeypatch.setattr(sidecar, "_get_compressor", lambda: rec)
    many = [str(i) for i in range(sidecar._MAX_FORCE_TOKENS + 1)]
    too_many = sidecar.CompressRequest(text="y" * 100, ratio=0.5, force_tokens=many)
    assert _refusal(sidecar, too_many) == 422 and rec.calls == []
    asyncio.run(sidecar.compress(
        sidecar.CompressRequest(text="y" * 100, ratio=0.5, force_tokens=many[:-1])))
    assert len(rec.calls) == 1


# ── The model never runs on the event loop ────────────────────────────────────
# One worker serves every caller: inference (seconds on a long prompt) and the first
# request's model load (tens of seconds) ran inside the async handler, so /health and every
# other tenant's request queued behind it until the proxy gave up.

def _on_the_loop() -> bool:
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


class _ThreadNoting(_RecordingCompressor):
    def compress_prompt(self, text, rate, force_tokens, force_reserve_digit):
        self.on_loop = _on_the_loop()
        return super().compress_prompt(text, rate, force_tokens, force_reserve_digit)


def test_inference_runs_off_the_event_loop(monkeypatch):
    sidecar = _load_sidecar()
    rec = _ThreadNoting()
    loaded_on_loop = []
    monkeypatch.setattr(sidecar, "_get_compressor", lambda: loaded_on_loop.append(_on_the_loop()) or rec)
    asyncio.run(sidecar.compress(sidecar.CompressRequest(text="y" * 100, ratio=0.5)))
    assert rec.on_loop is False and loaded_on_loop == [False]


def test_health_answers_while_a_compression_runs(monkeypatch):
    import threading
    sidecar = _load_sidecar()
    started, release = threading.Event(), threading.Event()

    class _Slow:
        def compress_prompt(self, text, **_kw):
            started.set()
            release.wait(5)
            return {"compressed_prompt": "done"}

    monkeypatch.setattr(sidecar, "_get_compressor", lambda: _Slow())

    async def scenario():
        work = asyncio.create_task(sidecar.compress(sidecar.CompressRequest(text="y" * 100)))
        assert await asyncio.to_thread(started.wait, 5)
        health = await asyncio.wait_for(sidecar.health(), 1)
        still_running = not work.done()
        release.set()
        return health, still_running, await work

    health, still_running, result = asyncio.run(scenario())
    assert health == {"status": "ok"} and still_running and result.compressed == "done"


def test_the_model_loads_once_before_serving_off_the_loop(monkeypatch):
    import sys
    import threading
    import time
    import types
    built = []

    class _PromptCompressor:
        def __init__(self, **_kw):
            built.append(_on_the_loop())
            time.sleep(0.05)

    monkeypatch.setitem(sys.modules, "llmlingua", types.SimpleNamespace(PromptCompressor=_PromptCompressor))
    sidecar = _load_sidecar()

    async def start():
        async with sidecar.app.router.lifespan_context(sidecar.app):
            pass

    asyncio.run(start())
    assert built == [False]
    # Requests racing the first load still build it once.
    sidecar._compressor = None
    built.clear()
    threads = [threading.Thread(target=sidecar._get_compressor) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(built) == 1


def test_a_model_that_fails_to_load_at_start_does_not_stop_the_service(monkeypatch):
    sidecar = _load_sidecar()

    def _broken():
        raise RuntimeError("no model")

    monkeypatch.setattr(sidecar, "_get_compressor", _broken)

    async def start():
        async with sidecar.app.router.lifespan_context(sidecar.app):
            return await sidecar.health()

    assert asyncio.run(start()) == {"status": "ok"}
