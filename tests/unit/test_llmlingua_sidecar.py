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
