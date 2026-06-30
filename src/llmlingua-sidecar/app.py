"""
LLMLingua-2 HTTP Sidecar — G01 compression service.

POST /compress
  Request:  { "text": "...", "ratio": 0.5,
              "force_reserve_digit": true,        # optional; preserve digit tokens (dates/IDs/amounts)
              "force_tokens": ["\n", ".", "-"] }  # optional; tokens never dropped
  Response: { "compressed": "...", "original_tokens": N, "compressed_tokens": N }

POST /health
  Response: { "status": "ok" }
"""
import logging
import os
from typing import List, Optional

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

app = FastAPI(title="LLMLingua-2 Sidecar", version="1.0.0")

_compressor = None

# Tokens LLMLingua-2 must never drop. Sentence punctuation keeps structure; the
# date/time/id separators ("-", ":", "/") keep values like "2023-10-18",
# "12:34:56" or "req-1042" from being split mid-value. Overridable per request.
_DEFAULT_FORCE_TOKENS = ["\n", ".", "!", "?", ",", "-", ":", "/"]

# Preserve ALL digit tokens by default. LLMLingua-2 otherwise treats digits as
# low-information and can silently drop one — corrupting dates, IDs and amounts
# (e.g. an incident date "2023-10-18" becoming "2023-10-02" changes a tool's
# `since=` window). Per-request `force_reserve_digit` overrides this default.
_FORCE_RESERVE_DIGIT_DEFAULT = os.getenv(
    "LLMLINGUA_FORCE_RESERVE_DIGIT", "true"
).strip().lower() in ("1", "true", "yes")


def _get_compressor():
    global _compressor
    if _compressor is None:
        try:
            from llmlingua import PromptCompressor
            _compressor = PromptCompressor(
                model_name="microsoft/llmlingua-2-bert-base-multilingual-cased-meetingbank",
                use_llmlingua2=True,
                device_map="cpu",
            )
            logger.info("LLMLingua-2 model loaded")
        except Exception as exc:
            logger.error("Failed to load LLMLingua-2: %s", exc)
            raise
    return _compressor


class CompressRequest(BaseModel):
    text: str
    ratio: float = 0.5
    target_token: Optional[int] = None
    # None → use the sidecar default (env-driven, on). Set explicitly to override.
    force_reserve_digit: Optional[bool] = None
    # None → use _DEFAULT_FORCE_TOKENS.
    force_tokens: Optional[List[str]] = None


class CompressResponse(BaseModel):
    compressed: str
    original_length: int
    compressed_length: int


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/compress", response_model=CompressResponse)
async def compress(req: CompressRequest):
    if not req.text or len(req.text.strip()) < 50:
        return CompressResponse(
            compressed=req.text,
            original_length=len(req.text),
            compressed_length=len(req.text),
        )

    try:
        compressor = _get_compressor()
        reserve_digit = (
            req.force_reserve_digit
            if req.force_reserve_digit is not None
            else _FORCE_RESERVE_DIGIT_DEFAULT
        )
        result = compressor.compress_prompt(
            req.text,
            rate=req.ratio,
            force_tokens=req.force_tokens or _DEFAULT_FORCE_TOKENS,
            force_reserve_digit=reserve_digit,
        )
        compressed = result.get("compressed_prompt", req.text)
        return CompressResponse(
            compressed=compressed,
            original_length=len(req.text),
            compressed_length=len(compressed),
        )
    except Exception as exc:
        logger.error("Compression failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Compression error: {str(exc)}")


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run("app:app", host="0.0.0.0", port=port, log_level="info")
