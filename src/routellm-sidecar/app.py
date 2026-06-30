"""
RouteLLM HTTP Sidecar — G06 routing service.

POST /route
  Request:  { "messages": [...], "router": "mf", "threshold": 0.11593, 
              "strong_model": "gpt-4-1106-preview", "weak_model": "gpt-4o-mini" }
  Response: { "routed_model": "gpt-4o-mini", "confidence": 0.85, "reason": "below_threshold" }

POST /health
  Response: { "status": "ok" }
"""
import asyncio
import logging
import os
from typing import List, Dict, Any, Optional

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

app = FastAPI(title="RouteLLM Sidecar", version="1.0.0")

_controller = None


def _get_controller():
    """Lazy-load RouteLLM Controller with configured models."""
    global _controller
    if _controller is None:
        try:
            from routellm.controller import Controller

            # Get model configuration from environment
            strong_model = os.getenv("ROUTELLM_STRONG_MODEL", "gpt-4-1106-preview")
            weak_model = os.getenv("ROUTELLM_WEAK_MODEL", "gpt-4o-mini")
            
            # RouteLLM requires OPENAI_API_KEY for embeddings (mf, sw_ranking routers)
            openai_key = os.getenv("OPENAI_API_KEY")
            if not openai_key:
                logger.warning("OPENAI_API_KEY not set - some routers may fail")

            _controller = Controller(
                routers=["mf", "sw_ranking", "causal_llm"],
                strong_model=strong_model,
                weak_model=weak_model,
            )
            logger.info(
                "RouteLLM Controller loaded: strong=%s, weak=%s",
                strong_model,
                weak_model,
            )
        except Exception as exc:
            logger.error("Failed to load RouteLLM Controller: %s", exc)
            raise
    return _controller


class RouteRequest(BaseModel):
    messages: List[Dict[str, Any]]
    router: str = "mf"
    threshold: float = 0.11593
    strong_model: Optional[str] = None
    weak_model: Optional[str] = None


class RouteResponse(BaseModel):
    routed_model: str
    confidence: float
    reason: str
    router_used: str


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/route", response_model=RouteResponse)
async def route(req: RouteRequest):
    """
    Route a request to the appropriate model using RouteLLM.
    
    The model field in RouteLLM uses format: router-[ROUTER_NAME]-[THRESHOLD]
    e.g., router-mf-0.11593
    """
    if not req.messages:
        raise HTTPException(status_code=400, detail="Messages cannot be empty")

    try:
        controller = _get_controller()
        
        # Override models if provided in request
        strong_model = req.strong_model or os.getenv("ROUTELLM_STRONG_MODEL", "gpt-4-1106-preview")
        weak_model = req.weak_model or os.getenv("ROUTELLM_WEAK_MODEL", "gpt-4o-mini")
        
        # Construct model string for RouteLLM
        model_string = f"router-{req.router}-{req.threshold}"
        
        # RouteLLM's chat.completions.create is synchronous — run in thread pool
        # to avoid blocking the async event loop.
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: controller.chat.completions.create(
                model=model_string,
                messages=req.messages,
                max_tokens=1,
            ),
        )
        
        # Extract the actual model that was used
        routed_model = response.model
        
        # Estimate confidence based on threshold
        # RouteLLM doesn't expose confidence directly, so we infer from routing decision
        # If routed to weak model, confidence is high for that decision
        if routed_model == weak_model:
            confidence = 1.0 - req.threshold
            reason = "below_threshold"
        else:
            confidence = req.threshold
            reason = "above_threshold"
            
        return RouteResponse(
            routed_model=routed_model,
            confidence=confidence,
            reason=reason,
            router_used=req.router,
        )
        
    except Exception as exc:
        logger.error("Routing failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Routing error: {str(exc)}")


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8081"))
    uvicorn.run("app:app", host="0.0.0.0", port=port, log_level="info")
