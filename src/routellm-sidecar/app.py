"""
RouteLLM HTTP Sidecar — G06 routing service.

POST /route
  Request:  { "messages": [...], "router": "bert", "threshold": 0.4066,
              "strong_model": "gpt-4-1106-preview", "weak_model": "gpt-4o-mini" }
  Response: { "routed_model": "gpt-4o-mini", "confidence": 0.85, "reason": "below_threshold" }

The answer is always one of the two model names in the request (G06 maps it by exact name).
The sidecar only DECIDES: `Controller.route()` scores the last turn and makes no completion
call, so nothing is billed and no conversation is sent to a model. (The `mf` and
`sw_ranking` routers do embed that turn with OpenAI.)

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

# route() answers with one name of the Controller's model pair. These labels only say which
# side won; the response carries the names the caller sent.
_STRONG, _WEAK = "strong", "weak"
_controllers: Dict[str, Any] = {}

# RouteLLM's threshold per router for ~50% strong-model calls (`routellm.calibrate_threshold
# --strong-model-pct 0.5` on its published Chatbot Arena scores); G06 uses the same table. bert
# is the default router: its checkpoint is Apache-2.0, while mf's has no licence.
_THRESHOLDS = {"bert": 0.4066, "mf": 0.11593, "sw_ranking": 0.21647, "causal_llm": 0.0962}
_DEFAULT_ROUTER = "bert"


def _get_controller(router: str):
    """The RouteLLM Controller for one router, built on first use. Building a router loads
    its checkpoint (causal_llm's is an 8B model), so only routers actually asked for load."""
    if router not in _controllers:
        from routellm.controller import Controller

        if router in ("mf", "sw_ranking") and not os.getenv("OPENAI_API_KEY"):
            logger.warning("OPENAI_API_KEY not set - router %r embeds with OpenAI", router)
        _controllers[router] = Controller(routers=[router], strong_model=_STRONG,
                                          weak_model=_WEAK)
        logger.info("RouteLLM Controller loaded for router %r", router)
    return _controllers[router]


def _last_turn_text(messages: List[Dict[str, Any]]) -> str:
    """What RouteLLM routes on: the last turn's text (text parts of a list content)."""
    content = messages[-1].get("content", "")
    if isinstance(content, list):
        content = " ".join(part.get("text", "") for part in content
                           if isinstance(part, dict) and part.get("type") == "text")
    return content if isinstance(content, str) else str(content)


class RouteRequest(BaseModel):
    messages: List[Dict[str, Any]]
    router: str = _DEFAULT_ROUTER
    threshold: Optional[float] = None   # None: the router's own calibration (_THRESHOLDS)
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
    """Route a request to the strong or the weak model using RouteLLM."""
    if not req.messages:
        raise HTTPException(status_code=400, detail="Messages cannot be empty")

    strong_model = req.strong_model or os.getenv("ROUTELLM_STRONG_MODEL", "gpt-4-1106-preview")
    weak_model = req.weak_model or os.getenv("ROUTELLM_WEAK_MODEL", "gpt-4o-mini")
    threshold = (req.threshold if req.threshold is not None
                 else _THRESHOLDS.get(req.router, _THRESHOLDS[_DEFAULT_ROUTER]))
    try:
        controller = _get_controller(req.router)
        # Synchronous (it may embed the prompt) — off the event loop.
        side = await asyncio.to_thread(
            controller.route, _last_turn_text(req.messages), req.router, threshold)
    except Exception as exc:
        logger.error("Routing failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Routing error: {str(exc)}") from exc

    # RouteLLM doesn't expose confidence directly, so it is inferred from the decision.
    if side == _WEAK:
        routed_model, confidence, reason = weak_model, 1.0 - threshold, "below_threshold"
    elif side == _STRONG:
        routed_model, confidence, reason = strong_model, threshold, "above_threshold"
    else:
        raise HTTPException(status_code=500, detail=f"Router returned an unknown model {side!r}")
    return RouteResponse(
        routed_model=routed_model,
        confidence=confidence,
        reason=reason,
        router_used=req.router,
    )


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8081"))
    uvicorn.run("app:app", host="0.0.0.0", port=port, log_level="info")
