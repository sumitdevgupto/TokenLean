from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, List, Dict, Any


@dataclass
class StepSaving:
    group: str
    description: str
    tokens_before: int
    tokens_after: int

    @property
    def absolute_saving(self) -> int:
        return max(0, self.tokens_before - self.tokens_after)


@dataclass
class SavingsRecord:
    request_id: str
    user_id: str
    timestamp: datetime
    model_requested: str
    routed_model: str
    baseline_tokens: int
    step_savings: List[StepSaving] = field(default_factory=list)
    cache_hit: bool = False
    cache_level: Optional[str] = None  # "L1" | "L2"
    bypassed: bool = False
    final_tokens_sent: int = 0
    response_tokens: int = 0
    # B1 — three-number savings model:
    #   x = baseline_tokens         (what the user sent; estimated)
    #   y = proxy_optimised_tokens  (our estimator on the optimised request, tools-symmetric with x)
    #   z = provider_prompt_tokens  (what the provider actually billed; None until G18 sees usage)
    proxy_optimised_tokens: int = 0
    provider_prompt_tokens: Optional[int] = None
    cost_baseline_usd: float = 0.0
    cost_actual_usd: float = 0.0
    effective_token_et: Optional[float] = None
    # G6 routing-specific fields
    # "heuristic" | "routellm" | "cascade" | "llm_judge" | "user_override" |
    # "cascade_execution" | "<classifier>_fallback" | "rules:<rule_id>" (tenant routing
    # rule hit; may carry "+cost_floor"/"+tier_unreachable_fallback" suffixes)
    routing_mode: Optional[str] = None
    routellm_router_used: Optional[str] = None  # e.g., "mf"
    routellm_threshold: Optional[float] = None
    routellm_confidence: Optional[float] = None

    def add_step(
        self,
        group: str,
        description: str,
        tokens_before: int,
        tokens_after: int,
    ) -> None:
        self.step_savings.append(
            StepSaving(
                group=group,
                description=description,
                tokens_before=tokens_before,
                tokens_after=tokens_after,
            )
        )

    @property
    def total_absolute_saving(self) -> int:
        return max(0, self.baseline_tokens - self.final_tokens_sent)

    @property
    def total_pct_saving(self) -> float:
        if self.baseline_tokens == 0:
            return 0.0
        return round((self.total_absolute_saving / self.baseline_tokens) * 100, 2)

    # ── B1: two distinct savings ─────────────────────────────────────────────
    # Proxy savings (estimated): what the optimisation layers removed, measured by
    # our own estimator on both sides (x → y). Apples-to-apples, tools-symmetric.
    @property
    def proxy_tokens_saved(self) -> int:
        return max(0, self.baseline_tokens - self.proxy_optimised_tokens)

    @property
    def proxy_pct_saving(self) -> float:
        if self.baseline_tokens == 0:
            return 0.0
        return round((self.proxy_tokens_saved / self.baseline_tokens) * 100, 2)

    # Actual savings (provider truth): x → z, what the user would have been billed
    # vs what they were actually billed. final_tokens_sent == z after G18, so this
    # equals total_*; exposed under explicit names for the value-metric surfaces.
    @property
    def actual_tokens_saved(self) -> int:
        return self.total_absolute_saving

    @property
    def actual_pct_saving(self) -> float:
        return self.total_pct_saving

    @property
    def cost_saving_usd(self) -> float:
        return round(self.cost_baseline_usd - self.cost_actual_usd, 6)

    def to_langfuse_metadata(self) -> Dict[str, Any]:
        steps_data: Dict[str, Any] = {}
        for s in self.step_savings:
            pct = (
                round((s.absolute_saving / self.baseline_tokens) * 100, 2)
                if self.baseline_tokens > 0
                else 0.0
            )
            steps_data[s.group] = {
                "description": s.description,
                "tokens_before": s.tokens_before,
                "tokens_after": s.tokens_after,
                "abs_saving": s.absolute_saving,
                "pct_saving_vs_baseline": pct,
            }
        metadata = {
            "request_id": self.request_id,
            "user_id": self.user_id,
            "timestamp": self.timestamp.isoformat(),
            "model_requested": self.model_requested,
            "routed_model": self.routed_model,
            "baseline_tokens": self.baseline_tokens,
            "final_tokens_sent": self.final_tokens_sent,
            "response_tokens": self.response_tokens,
            "total_abs_saving": self.total_absolute_saving,
            "total_pct_saving": self.total_pct_saving,
            # B1 — explicit three-number model + two savings (value metric, never billed)
            "tokens_user_sent": self.baseline_tokens,            # x
            "tokens_after_proxy": self.proxy_optimised_tokens,   # y
            "tokens_provider_billed": self.provider_prompt_tokens,  # z (may be None)
            "proxy_savings_abs": self.proxy_tokens_saved,
            "proxy_savings_pct": self.proxy_pct_saving,
            "actual_savings_abs": self.actual_tokens_saved,
            "actual_savings_pct": self.actual_pct_saving,
            "cache_hit": self.cache_hit,
            "cache_level": self.cache_level,
            "bypassed": self.bypassed,
            "cost_baseline_usd": round(self.cost_baseline_usd, 6),
            "cost_actual_usd": round(self.cost_actual_usd, 6),
            "cost_saving_usd": self.cost_saving_usd,
            "step_savings": steps_data,
        }
        if self.effective_token_et is not None:
            metadata["effective_token_et"] = self.effective_token_et
        # Add G6 routing-specific metadata if available
        if self.routing_mode:
            metadata["routing_mode"] = self.routing_mode
        if self.routellm_router_used:
            metadata["routellm_router_used"] = self.routellm_router_used
        if self.routellm_threshold is not None:
            metadata["routellm_threshold"] = self.routellm_threshold
        if self.routellm_confidence is not None:
            metadata["routellm_confidence"] = self.routellm_confidence
        return metadata
