"""
Apache Tika Sidecar Service

Provides HTTP wrapper around Apache Tika for document text extraction.
Alternative to Unstructured for simpler use cases.

Supports:
- PDF, Word, Excel, PowerPoint, HTML, TXT, and many more formats
- Metadata extraction
- Language detection
- OCR for scanned documents (if Tesseract available)
"""
import io
import logging
import os
from typing import Any, Dict, Optional

from fastapi import FastAPI, File, HTTPException, UploadFile, status
from fastapi.responses import JSONResponse, PlainTextResponse
import httpx

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Tika Document Extraction Sidecar",
    description="Apache Tika wrapper for document text and metadata extraction",
    version="1.0.0",
)

# Tika server configuration (can run embedded or connect to external)
_TIKA_MODE = os.getenv("TIKA_MODE", "embedded")  # "embedded" or "external"
_TIKA_SERVER_URL = os.getenv("TIKA_SERVER_URL", "http://localhost:9998")
_TIKA_JAR_PATH = os.getenv("TIKA_JAR_PATH", "/tika/tika-server-standard-2.9.1.jar")

# Embedded Tika process (if running in embedded mode)
_tika_process: Optional[Any] = None


@app.on_event("startup")
async def startup():
    """Start embedded Tika server if in embedded mode."""
    if _TIKA_MODE == "embedded":
        import subprocess
        import time
        
        global _tika_process
        
        logger.info("Starting embedded Tika server...")
        
        _tika_process = subprocess.Popen(
            ["java", "-jar", _TIKA_JAR_PATH, "-p", "9998"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        
        # Wait for Tika to be ready
        await wait_for_tika()
        logger.info("Tika server ready")


@app.on_event("shutdown")
async def shutdown():
    """Stop embedded Tika server."""
    if _tika_process:
        logger.info("Stopping Tika server...")
        _tika_process.terminate()
        try:
            _tika_process.wait(timeout=10)
        except Exception:
            _tika_process.kill()


async def wait_for_tika(timeout: float = 60.0) -> bool:
    """Wait for Tika server to be ready."""
    import asyncio
    
    start = asyncio.get_event_loop().time()
    
    async with httpx.AsyncClient() as client:
        while (asyncio.get_event_loop().time() - start) < timeout:
            try:
                resp = await client.get(f"{_TIKA_SERVER_URL}/tika", timeout=2.0)
                if resp.status_code == 200:
                    return True
            except Exception:
                pass
            
            await asyncio.sleep(0.5)
    
    return False


@app.get("/health")
async def health():
    """Health check endpoint."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{_TIKA_SERVER_URL}/tika", timeout=5.0)
            if resp.status_code == 200:
                return {"status": "healthy", "tika": "ready"}
    except Exception as exc:
        logger.warning("Health check failed: %s", exc)
    
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"status": "unhealthy", "tika": "not ready"}
    )


@app.put("/tika")
async def extract_text(
    file: UploadFile = File(...),
    accept: str = "text/plain",
):
    """
    Extract text from uploaded document.
    
    Supports PDF, DOC, DOCX, XLS, XLSX, PPT, PPTX, HTML, TXT, and more.
    
    Headers:
        Accept: text/plain (default) or application/json
    """
    try:
        content = await file.read()
        
        async with httpx.AsyncClient() as client:
            resp = await client.put(
                f"{_TIKA_SERVER_URL}/tika",
                content=content,
                headers={
                    "Content-Type": file.content_type or "application/octet-stream",
                    "Accept": accept,
                    "X-Filename": file.filename or "unknown",
                },
                timeout=60.0,
            )
            
            resp.raise_for_status()
            
            if accept == "application/json":
                return JSONResponse(content=resp.json())
            else:
                return PlainTextResponse(content=resp.text)
                
    except httpx.HTTPStatusError as exc:
        logger.error("Tika extraction failed: HTTP %d", exc.response.status_code)
        raise HTTPException(
            status_code=exc.response.status_code,
            detail=f"Extraction failed: {exc.response.text}"
        )
    except Exception as exc:
        logger.error("Tika extraction error: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Extraction error: {str(exc)}"
        )


@app.put("/tika/metadata")
async def extract_metadata(file: UploadFile = File(...)):
    """Extract metadata from document."""
    try:
        content = await file.read()
        
        async with httpx.AsyncClient() as client:
            resp = await client.put(
                f"{_TIKA_SERVER_URL}/meta",
                content=content,
                headers={
                    "Content-Type": file.content_type or "application/octet-stream",
                    "Accept": "application/json",
                },
                timeout=60.0,
            )
            
            resp.raise_for_status()
            return JSONResponse(content=resp.json())
            
    except Exception as exc:
        logger.error("Metadata extraction error: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Metadata extraction error: {str(exc)}"
        )


@app.put("/tika/language")
async def detect_language(file: UploadFile = File(...)):
    """Detect document language."""
    try:
        content = await file.read()
        
        async with httpx.AsyncClient() as client:
            resp = await client.put(
                f"{_TIKA_SERVER_URL}/language/string",
                content=content,
                headers={
                    "Content-Type": file.content_type or "application/octet-stream",
                },
                timeout=30.0,
            )
            
            resp.raise_for_status()
            return JSONResponse(content={"language": resp.text.strip()})
            
    except Exception as exc:
        logger.error("Language detection error: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Language detection error: {str(exc)}"
        )


@app.get("/formats")
async def supported_formats():
    """List supported document formats."""
    return {
        "formats": [
            "application/pdf",
            "application/msword",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.ms-excel",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/vnd.ms-powerpoint",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "text/html",
            "text/plain",
            "text/markdown",
            "application/rtf",
            "application/epub+zip",
            "image/png",  # With OCR
            "image/jpeg",  # With OCR
        ],
        "features": [
            "text_extraction",
            "metadata_extraction",
            "language_detection",
            "ocr_scanned_documents",
        ]
    }


if __name__ == "__main__":
    import uvicorn
    
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", "9998")),
        log_level="info",
    )
