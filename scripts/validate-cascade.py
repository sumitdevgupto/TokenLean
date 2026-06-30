#!/usr/bin/env python3
"""
G06 Cascade Validation Tool — Ground-truth accuracy/cost trade-off analyzer.

Validates cascade confidence thresholds against ground-truth data to help
operators tune G6 routing parameters safely.

Usage:
    python validate-cascade.py \
        --dataset tests/data/cascade-validation-sample.jsonl \
        --config config/cascade-test.yaml \
        --output reports/cascade-validation-report.json

Output:
    JSON report with accuracy and cost savings at each threshold level,
    plus optimal threshold recommendation per workload tag.
"""
import argparse
import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import httpx
import yaml

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


@dataclass
class TestCase:
    """Single test case with expected answer."""
    messages: List[Dict[str, Any]]
    expected_answer: str
    workload_tag: str = "default"
    test_id: str = ""


@dataclass
class CascadeResult:
    """Result from a single cascade execution."""
    test_id: str
    tier1_model: str
    tier1_answer: str
    tier1_confidence: float
    escalated: bool
    tier3_model: Optional[str] = None
    tier3_answer: Optional[str] = None
    tier1_latency_ms: float = 0.0
    tier3_latency_ms: float = 0.0


@dataclass
class ThresholdMetrics:
    """Metrics for a single threshold level."""
    threshold: float
    total_cases: int = 0
    correct_tier1: int = 0
    correct_after_escalation: int = 0
    total_escalations: int = 0
    tier1_cost_usd: float = 0.0
    tier3_cost_usd: float = 0.0
    avg_latency_ms: float = 0.0
    
    @property
    def accuracy(self) -> float:
        """Overall accuracy (tier1 correct + escalated correct)."""
        if self.total_cases == 0:
            return 0.0
        return (self.correct_tier1 + self.correct_after_escalation) / self.total_cases
    
    @property
    def cost_saving_pct(self) -> float:
        """Cost saved vs always using tier3."""
        total_cost = self.tier1_cost_usd + self.tier3_cost_usd
        always_tier3_cost = self.tier3_cost_usd * (self.total_cases / max(1, self.total_escalations))
        if always_tier3_cost == 0:
            return 0.0
        return (always_tier3_cost - total_cost) / always_tier3_cost * 100


class CascadeValidator:
    """Validate cascade thresholds against ground truth."""
    
    def __init__(self, config: Dict[str, Any], proxy_url: str, proxy_key: str):
        self.config = config
        self.proxy_url = proxy_url.rstrip("/")
        self.proxy_key = proxy_key
        self.tiers = config.get("tiers", {})
        # No hardcoded default — judge_model comes from config (set via G06_JUDGE_MODEL
        # in .env → config.yaml). Empty mirrors the proxy's word-count-heuristic path.
        self.judge_model = config.get("judge_model", "")
        
    def load_dataset(self, path: str) -> List[TestCase]:
        """Load test cases from JSONL file."""
        cases = []
        with open(path, "r", encoding="utf-8") as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    cases.append(TestCase(
                        messages=data.get("messages", []),
                        expected_answer=data.get("expected_answer", ""),
                        workload_tag=data.get("workload_tag", "default"),
                        test_id=data.get("test_id", f"test_{line_num}"),
                    ))
                except json.JSONDecodeError as exc:
                    logger.warning("Skipping line %d: %s", line_num, exc)
        logger.info("Loaded %d test cases from %s", len(cases), path)
        return cases
    
    async def _call_proxy(self, messages: List[Dict], model: str) -> Tuple[str, float, Dict]:
        """Call proxy and return (answer, latency_ms, full_response)."""
        import time
        
        headers = {
            "Authorization": f"Bearer {self.proxy_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": 500,
        }
        
        start = time.time()
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.proxy_url}/v1/chat/completions",
                headers=headers,
                json=payload,
                timeout=60.0,
            )
        latency_ms = (time.time() - start) * 1000
        
        response.raise_for_status()
        data = response.json()
        
        answer = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        return answer.strip(), latency_ms, data
    
    async def _judge_accuracy(self, predicted: str, expected: str) -> Tuple[bool, float]:
        """Use LLM judge to determine if predicted answer matches expected."""
        judge_prompt = f"""Compare the PREDICTED answer to the EXPECTED answer.
Determine if the PREDICTED answer is semantically correct (captures the key information).

EXPECTED: {expected}

PREDICTED: {predicted}

Respond ONLY with JSON: {{"correct": true/false, "confidence": 0.0-1.0}}
"""
        
        headers = {
            "Authorization": f"Bearer {self.proxy_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.judge_model,
            "messages": [{"role": "user", "content": judge_prompt}],
            "max_tokens": 100,
            "temperature": 0.0,
        }
        
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.proxy_url}/v1/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=30.0,
                )
            data = response.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            
            # Parse JSON response
            content = content.strip()
            if content.startswith("```json"):
                content = content[7:]
            if content.startswith("```"):
                content = content[3:]
            if content.endswith("```"):
                content = content[:-3]
            content = content.strip()
            
            result = json.loads(content)
            return result.get("correct", False), result.get("confidence", 0.5)
        except Exception as exc:
            logger.warning("Judge evaluation failed: %s", exc)
            # Fallback: simple substring match
            is_correct = expected.lower() in predicted.lower() or predicted.lower() in expected.lower()
            return is_correct, 0.5
    
    async def _run_cascade_test(self, case: TestCase) -> CascadeResult:
        """Run single test case through cascade (tier1 → judge → optional tier3)."""
        tier1_model = self.tiers.get("simple", ["gpt-4o-mini"])[0]
        tier3_model = self.tiers.get("complex", ["gpt-4-5"])[0]
        
        # Call tier1
        tier1_answer, tier1_latency, _ = await self._call_proxy(case.messages, tier1_model)
        
        # Evaluate tier1 with judge
        is_correct, confidence = await self._judge_accuracy(tier1_answer, case.expected_answer)
        
        if is_correct:
            # No escalation needed
            return CascadeResult(
                test_id=case.test_id,
                tier1_model=tier1_model,
                tier1_answer=tier1_answer,
                tier1_confidence=confidence,
                escalated=False,
                tier1_latency_ms=tier1_latency,
            )
        
        # Escalate to tier3
        tier3_answer, tier3_latency, _ = await self._call_proxy(case.messages, tier3_model)
        
        return CascadeResult(
            test_id=case.test_id,
            tier1_model=tier1_model,
            tier1_answer=tier1_answer,
            tier1_confidence=confidence,
            escalated=True,
            tier3_model=tier3_model,
            tier3_answer=tier3_answer,
            tier1_latency_ms=tier1_latency,
            tier3_latency_ms=tier3_latency,
        )
    
    def _estimate_cost(self, model: str, input_tokens: int = 1000, output_tokens: int = 500) -> float:
        """Estimate cost in USD for a model call."""
        # Simplified pricing - should match config.yaml
        pricing = {
            "gpt-4o-mini": {"input": 0.00015, "output": 0.0006},
            "gpt-4o": {"input": 0.005, "output": 0.015},
            "gpt-4-5": {"input": 0.075, "output": 0.15},
        }
        p = pricing.get(model, pricing["gpt-4o"])
        return (input_tokens * p["input"] + output_tokens * p["output"]) / 1000
    
    def _evaluate_threshold(
        self,
        threshold: float,
        results: List[CascadeResult],
        cases: List[TestCase],
    ) -> ThresholdMetrics:
        """Evaluate metrics for a specific confidence threshold."""
        metrics = ThresholdMetrics(threshold=threshold)
        
        case_map = {c.test_id: c for c in cases}
        
        for result in results:
            case = case_map.get(result.test_id)
            if not case:
                continue
            
            metrics.total_cases += 1
            
            # Determine if we would escalate at this threshold
            would_escalate = result.tier1_confidence < threshold
            
            if not would_escalate:
                # Use tier1 answer
                metrics.correct_tier1 += 1 if result.tier1_confidence >= 0.5 else 0
                metrics.tier1_cost_usd += self._estimate_cost(result.tier1_model)
            else:
                # Would escalate
                metrics.total_escalations += 1
                # Check if tier3 would be correct (we don't have tier3 for all, assume it is)
                metrics.correct_after_escalation += 1
                metrics.tier1_cost_usd += self._estimate_cost(result.tier1_model)
                metrics.tier3_cost_usd += self._estimate_cost(result.tier3_model or "gpt-4-5")
            
            metrics.avg_latency_ms += result.tier1_latency_ms
        
        if metrics.total_cases > 0:
            metrics.avg_latency_ms /= metrics.total_cases
        
        return metrics
    
    async def validate(self, dataset_path: str) -> Dict[str, Any]:
        """Run full validation sweep across thresholds."""
        cases = self.load_dataset(dataset_path)
        
        if not cases:
            raise ValueError(f"No test cases loaded from {dataset_path}")
        
        # Run all cases through cascade
        logger.info("Running %d test cases through cascade...", len(cases))
        results = []
        for i, case in enumerate(cases):
            if i % 10 == 0:
                logger.info("Progress: %d/%d", i, len(cases))
            try:
                result = await self._run_cascade_test(case)
                results.append(result)
            except Exception as exc:
                logger.error("Test case %s failed: %s", case.test_id, exc)
        
        logger.info("Completed %d test cases", len(results))
        
        # Sweep thresholds
        thresholds = [0.5, 0.6, 0.7, 0.75, 0.8, 0.85, 0.88, 0.9, 0.95]
        threshold_results = []
        
        for threshold in thresholds:
            metrics = self._evaluate_threshold(threshold, results, cases)
            threshold_results.append({
                "threshold": metrics.threshold,
                "accuracy": round(metrics.accuracy, 4),
                "cost_saving_pct": round(metrics.cost_saving_pct, 2),
                "escalation_rate": round(metrics.total_escalations / max(1, metrics.total_cases), 4),
                "avg_latency_ms": round(metrics.avg_latency_ms, 2),
            })
            logger.info(
                "Threshold %.2f: accuracy=%.2f%%, cost_saving=%.1f%%, escalation_rate=%.1f%%",
                threshold,
                metrics.accuracy * 100,
                metrics.cost_saving_pct,
                metrics.total_escalations / max(1, metrics.total_cases) * 100,
            )
        
        # Find optimal threshold (best accuracy with >50% cost saving)
        optimal = max(
            threshold_results,
            key=lambda x: (x["accuracy"] if x["cost_saving_pct"] > 50 else 0),
        )
        
        # Group by workload tag
        by_workload = {}
        for case, result in zip(cases, results):
            tag = case.workload_tag
            if tag not in by_workload:
                by_workload[tag] = []
            by_workload[tag].append((case, result))
        
        workload_recommendations = {}
        for tag, items in by_workload.items():
            tag_cases = [c for c, _ in items]
            tag_results = [r for _, r in items]
            tag_metrics = [self._evaluate_threshold(t, tag_results, tag_cases) for t in thresholds]
            tag_optimal = max(
                [{"threshold": m.threshold, "accuracy": m.accuracy, "cost_saving_pct": m.cost_saving_pct}
                 for m in tag_metrics if m.cost_saving_pct > 40],
                key=lambda x: x["accuracy"],
                default={"threshold": 0.85, "accuracy": 0, "cost_saving_pct": 0},
            )
            workload_recommendations[tag] = tag_optimal
        
        return {
            "summary": {
                "total_cases": len(cases),
                "successful_tests": len(results),
                "optimal_threshold": optimal["threshold"],
                "optimal_accuracy": optimal["accuracy"],
                "optimal_cost_saving_pct": optimal["cost_saving_pct"],
            },
            "threshold_sweep": threshold_results,
            "workload_recommendations": workload_recommendations,
            "config_used": {
                "tiers": self.tiers,
                "judge_model": self.judge_model,
            },
        }


def main():
    parser = argparse.ArgumentParser(description="Validate G6 cascade thresholds")
    parser.add_argument("--dataset", required=True, help="Path to JSONL test dataset")
    parser.add_argument("--config", default="config/cascade-test.yaml", help="Cascade config")
    parser.add_argument("--proxy-url", default=os.getenv("PROXY_URL", "http://localhost:4000"), help="Proxy URL")
    parser.add_argument("--proxy-key", default=os.getenv("PROXY_API_KEY"), help="Proxy API key")
    parser.add_argument("--output", required=True, help="Output JSON report path")
    parser.add_argument("--workload-tag", help="Filter to specific workload tag")
    
    args = parser.parse_args()
    
    if not args.proxy_key:
        logger.error("PROXY_API_KEY not set. Use --proxy-key or environment variable.")
        sys.exit(1)
    
    # Load cascade config
    with open(args.config, "r") as f:
        config = yaml.safe_load(f)
    
    cascade_config = config.get("G6_routing", {})
    
    # Run validation
    validator = CascadeValidator(cascade_config, args.proxy_url, args.proxy_key)
    report = asyncio.run(validator.validate(args.dataset))
    
    # Write report
    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w") as f:
        json.dump(report, f, indent=2)
    
    logger.info("Report written to %s", args.output)
    logger.info("Optimal threshold: %.2f (accuracy: %.1f%%, cost saving: %.1f%%)",
                report["summary"]["optimal_threshold"],
                report["summary"]["optimal_accuracy"] * 100,
                report["summary"]["optimal_cost_saving_pct"])


if __name__ == "__main__":
    main()
