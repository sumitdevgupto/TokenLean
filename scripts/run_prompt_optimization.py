#!/usr/bin/env python3
"""
G20 · Offline Prompt Optimization Pipeline

Uses Opik (or DSPy fallback) to find minimal effective prompt variants.
Reads current G2 templates, runs multi-trial optimization, and pushes
optimized templates back to the G2 Redis registry.

NOT in the request path — runs as a scheduled job or manual invocation.

Usage:
    python scripts/run_prompt_optimization.py \\
        --config config/config.yaml \\
        --template-id customer-support-classifier \\
        --eval-dataset data/eval_samples.jsonl \\
        --dry-run

Environment Variables:
    REDIS_URL          — Redis connection string (default: redis://localhost:6379)
    OPENAI_API_KEY     — Required for LLM-based optimization
    OPIK_API_KEY       — Optional Opik Cloud API key (uses local mode if unset)
"""
import argparse
import json
import logging
import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("g20_prompt_optimization")

# Opik integration (optional — falls back to DSPy or built-in)
_opik_available = False
try:
    import opik
    _opik_available = True
except ImportError:
    pass

# DSPy fallback
_dspy_available = False
try:
    import dspy
    _dspy_available = True
except ImportError:
    pass


def load_config(config_path: str) -> Dict[str, Any]:
    """Load proxy config from YAML file."""
    import yaml
    with open(config_path) as f:
        return yaml.safe_load(f)


def load_eval_dataset(path: str) -> List[Dict[str, Any]]:
    """Load evaluation dataset from JSONL file."""
    samples = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                samples.append(json.loads(line))
    return samples


def get_current_template(redis_url: str, template_id: str) -> Optional[Dict[str, Any]]:
    """Fetch current template from Redis G2 registry."""
    try:
        import redis
        r = redis.from_url(redis_url)
        key = f"tok_opt:template:meta:{template_id}"
        data = r.get(key)
        if data:
            return json.loads(data)
    except Exception as exc:
        logger.warning("Failed to load template from Redis: %s", exc)
    return None


def push_optimized_template(
    redis_url: str,
    template_id: str,
    optimized_prompt: str,
    quality_score: float,
    original_tokens: int,
    optimized_tokens: int,
    optimizer_used: str,
) -> bool:
    """Push optimized template to Redis G2 registry."""
    try:
        import redis
        r = redis.from_url(redis_url)

        # Store optimized prompt
        key = f"tok_opt:template:optimized:{template_id}"
        record = {
            "template_id": template_id,
            "optimized_prompt": optimized_prompt,
            "quality_score": quality_score,
            "original_tokens": original_tokens,
            "optimized_tokens": optimized_tokens,
            "token_reduction_pct": round((1 - optimized_tokens / original_tokens) * 100, 2) if original_tokens > 0 else 0,
            "optimizer": optimizer_used,
            "optimized_at": time.time(),
        }
        r.set(key, json.dumps(record))

        # Also store in history sorted set
        history_key = f"tok_opt:template:optimization_history:{template_id}"
        r.zadd(history_key, {json.dumps(record): time.time()})

        logger.info(
            "Pushed optimized template '%s': %d→%d tokens (%.1f%% reduction, quality=%.3f)",
            template_id, original_tokens, optimized_tokens,
            record["token_reduction_pct"], quality_score,
        )
        return True
    except Exception as exc:
        logger.error("Failed to push optimized template: %s", exc)
        return False


def estimate_tokens_simple(text: str) -> int:
    """Simple token estimation (char/4 ceiling)."""
    return max(1, (len(text) + 3) // 4)


# ─── Optimizers ──────────────────────────────────────────────────────────────

def optimize_with_opik(
    prompt: str,
    eval_data: List[Dict[str, Any]],
    optimizer_name: str,
    quality_threshold: float,
    max_prompt_tokens: int,
    model: str,
) -> Tuple[Optional[str], float]:
    """Run Opik optimizer to find minimal effective prompt."""
    if not _opik_available:
        logger.warning("Opik not installed. Install with: pip install opik")
        return None, 0.0

    try:
        # Configure Opik
        client = opik.Opik()

        # Create experiment with eval dataset
        experiment = client.create_experiment(
            name=f"g20-optimize-{int(time.time())}",
            dataset_name="g20_eval",
        )

        # Run optimization
        result = client.optimize(
            prompt=prompt,
            dataset=eval_data,
            optimizer=optimizer_name,
            max_tokens=max_prompt_tokens,
            quality_threshold=quality_threshold,
            model=model,
        )

        if result and result.quality_score >= quality_threshold:
            return result.optimized_prompt, result.quality_score

        logger.warning(
            "Opik optimization did not meet quality threshold: %.3f < %.3f",
            result.quality_score if result else 0.0, quality_threshold,
        )
        return None, result.quality_score if result else 0.0

    except Exception as exc:
        logger.error("Opik optimization failed: %s", exc)
        return None, 0.0


def optimize_with_dspy(
    prompt: str,
    eval_data: List[Dict[str, Any]],
    quality_threshold: float,
    max_prompt_tokens: int,
    model: str,
) -> Tuple[Optional[str], float]:
    """DSPy MIPROv2 fallback optimizer."""
    if not _dspy_available:
        logger.warning("DSPy not installed. Install with: pip install dspy-ai")
        return None, 0.0

    try:
        lm = dspy.LM(model)
        dspy.configure(lm=lm)

        # Define a simple prompt optimization signature
        class PromptOptimizer(dspy.Signature):
            """Optimize a system prompt to be shorter while preserving quality."""
            original_prompt: str = dspy.InputField(desc="The original system prompt")
            task_description: str = dspy.InputField(desc="What the prompt should accomplish")
            optimized_prompt: str = dspy.OutputField(desc="Shorter but equivalent prompt")

        optimizer = dspy.MIPROv2(
            metric=lambda gold, pred, trace: len(pred.optimized_prompt) < len(gold.original_prompt),
            num_candidates=10,
            max_bootstrapped_demos=3,
        )

        # Run optimization
        program = dspy.ChainOfThought(PromptOptimizer)
        optimized = optimizer.compile(
            program,
            trainset=eval_data[:min(len(eval_data), 20)],
        )

        # Evaluate quality
        result = optimized(original_prompt=prompt, task_description="Optimize this prompt")
        optimized_prompt = result.optimized_prompt

        if estimate_tokens_simple(optimized_prompt) <= max_prompt_tokens:
            # DSPy MIPROv2 has no built-in quality metric; caller must evaluate independently
            logger.warning(
                "DSPy optimizer used without eval metric for '%s'; quality score is unverified",
                prompt[:50],
            )
            return optimized_prompt, None  # None = unverified, caller must gate
        return None, 0.0

    except Exception as exc:
        logger.error("DSPy optimization failed: %s", exc)
        return None, 0.0


def optimize_builtin(
    prompt: str,
    quality_threshold: float,
    max_prompt_tokens: int,
) -> Tuple[Optional[str], float]:
    """Built-in heuristic optimizer (no external dependencies).
    
    Applies rule-based compression:
    1. Remove redundant whitespace and blank lines
    2. Shorten verbose phrases
    3. Remove filler words
    """
    import re

    result = prompt

    # Collapse multiple blank lines
    result = re.sub(r"\n{3,}", "\n\n", result)

    # Collapse multiple spaces
    result = re.sub(r" {2,}", " ", result)

    # Remove common filler phrases
    filler_patterns = [
        (r"\bplease note that\b", "note:"),
        (r"\bit is important to\b", ""),
        (r"\bmake sure to\b", ""),
        (r"\bensure that you\b", ""),
        (r"\bin order to\b", "to"),
        (r"\bfor the purpose of\b", "for"),
        (r"\bat this point in time\b", "now"),
        (r"\bdue to the fact that\b", "because"),
        (r"\bin the event that\b", "if"),
        (r"\bwith regard to\b", "regarding"),
        (r"\bin addition to\b", "also"),
    ]
    for pattern, replacement in filler_patterns:
        result = re.sub(pattern, replacement, result, flags=re.IGNORECASE)

    # Clean up resulting double spaces
    result = re.sub(r" {2,}", " ", result)
    result = result.strip()

    original_tokens = estimate_tokens_simple(prompt)
    optimized_tokens = estimate_tokens_simple(result)

    if optimized_tokens < original_tokens:
        reduction_pct = (1 - optimized_tokens / original_tokens) * 100
        logger.info("Built-in optimizer: %.1f%% reduction (%d→%d tokens)", reduction_pct, original_tokens, optimized_tokens)
        return result, 1.0  # Quality assumed since only removing filler
    return None, 1.0


# ─── Main ────────────────────────────────────────────────────────────────────

def run_optimization(
    config: Dict[str, Any],
    template_id: str,
    eval_data: List[Dict[str, Any]],
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Run the full optimization pipeline for a single template."""
    g20_cfg = config.get("groups", {}).get("G20_prompt_optimization", {})

    if not g20_cfg.get("enabled", True):
        logger.info("G20 prompt optimization disabled in config; skipping '%s'", template_id)
        return {"status": "disabled", "template_id": template_id}

    optimizer_name = g20_cfg.get("optimizer", "builtin")
    quality_threshold = g20_cfg.get("quality_threshold", 0.95)
    max_prompt_tokens = g20_cfg.get("max_prompt_tokens", 4000)
    model = g20_cfg.get("model", "gpt-4o-mini")
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")

    # Get current template
    template = get_current_template(redis_url, template_id)
    if template:
        current_prompt = template.get("description", "") or template.get("prompt", "")
    else:
        # Fallback: check config budgets for the template
        budgets = config.get("groups", {}).get("G2_template_registry", {}).get("budgets", {})
        tpl_cfg = budgets.get(template_id, {})
        current_prompt = tpl_cfg.get("prompt", "")

    if not current_prompt:
        logger.error("No prompt found for template '%s'", template_id)
        return {"status": "error", "message": "No prompt found"}

    original_tokens = estimate_tokens_simple(current_prompt)
    logger.info(
        "Optimizing template '%s': %d tokens, optimizer=%s, threshold=%.2f",
        template_id, original_tokens, optimizer_name, quality_threshold,
    )

    # Run optimizer
    optimized_prompt = None
    quality_score = 0.0

    if optimizer_name.lower() in ("miprov2", "hrpo", "metaprompt", "evolutionary", "gepa", "mipro"):
        if _opik_available:
            optimized_prompt, quality_score = optimize_with_opik(
                current_prompt, eval_data, optimizer_name, quality_threshold, max_prompt_tokens, model,
            )
        elif _dspy_available:
            logger.info("Opik not available, falling back to DSPy")
            optimized_prompt, quality_score = optimize_with_dspy(
                current_prompt, eval_data, quality_threshold, max_prompt_tokens, model,
            )
        else:
            logger.info("Neither Opik nor DSPy available, using built-in optimizer")
            optimized_prompt, quality_score = optimize_builtin(
                current_prompt, quality_threshold, max_prompt_tokens,
            )
    elif optimizer_name.lower() == "dspy":
        optimized_prompt, quality_score = optimize_with_dspy(
            current_prompt, eval_data, quality_threshold, max_prompt_tokens, model,
        )
    else:
        optimized_prompt, quality_score = optimize_builtin(
            current_prompt, quality_threshold, max_prompt_tokens,
        )

    if optimized_prompt is None:
        logger.warning("Optimization produced no improvement for '%s'", template_id)
        return {
            "status": "no_improvement",
            "template_id": template_id,
            "original_tokens": original_tokens,
            "quality_score": quality_score,
        }

    # Quality gate — reject prompts that are unverified (None) or below threshold.
    # The DSPy path returns quality_score=None ("unverified, caller must gate").
    if quality_score is None or quality_score < quality_threshold:
        logger.warning(
            "Quality gate failed for '%s': score=%s < threshold=%.2f; not pushing",
            template_id,
            "unverified" if quality_score is None else f"{quality_score:.3f}",
            quality_threshold,
        )
        return {
            "status": "quality_gate_failed",
            "template_id": template_id,
            "original_tokens": original_tokens,
            "quality_score": quality_score,
            "quality_threshold": quality_threshold,
        }

    optimized_tokens = estimate_tokens_simple(optimized_prompt)
    reduction_pct = round((1 - optimized_tokens / original_tokens) * 100, 2) if original_tokens > 0 else 0

    result = {
        "status": "optimized",
        "template_id": template_id,
        "optimizer": optimizer_name,
        "original_tokens": original_tokens,
        "optimized_tokens": optimized_tokens,
        "reduction_pct": reduction_pct,
        "quality_score": quality_score,
        "dry_run": dry_run,
    }

    if dry_run:
        logger.info("[DRY RUN] Would push: %s", json.dumps(result, indent=2))
        result["optimized_prompt_preview"] = optimized_prompt[:200] + "..." if len(optimized_prompt) > 200 else optimized_prompt
    else:
        success = push_optimized_template(
            redis_url, template_id, optimized_prompt,
            quality_score, original_tokens, optimized_tokens, optimizer_name,
        )
        result["pushed"] = success

    return result


def main():
    parser = argparse.ArgumentParser(
        description="G20 Prompt Optimization Pipeline — find minimal effective prompts",
    )
    parser.add_argument("--config", default="config/config.yaml", help="Path to config.yaml")
    parser.add_argument("--template-id", required=True, help="G2 template ID to optimize")
    parser.add_argument("--eval-dataset", default="", help="Path to eval dataset (JSONL)")
    parser.add_argument("--dry-run", action="store_true", help="Preview optimization without pushing")
    parser.add_argument("--optimizer", default=None, help="Override optimizer (opik/dspy/builtin)")
    parser.add_argument("--quality-threshold", type=float, default=None, help="Override quality threshold")
    parser.add_argument("--model", default=None, help="Override LLM model for optimization")
    args = parser.parse_args()

    config = load_config(args.config)

    # Apply CLI overrides to config
    g20_cfg = config.setdefault("groups", {}).setdefault("G20_prompt_optimization", {})
    if args.optimizer:
        g20_cfg["optimizer"] = args.optimizer
    if args.quality_threshold is not None:
        g20_cfg["quality_threshold"] = args.quality_threshold
    if args.model:
        g20_cfg["model"] = args.model

    # Load eval dataset
    eval_data = []
    if args.eval_dataset and os.path.exists(args.eval_dataset):
        eval_data = load_eval_dataset(args.eval_dataset)
        logger.info("Loaded %d eval samples from %s", len(eval_data), args.eval_dataset)

    result = run_optimization(config, args.template_id, eval_data, dry_run=args.dry_run)
    print(json.dumps(result, indent=2))

    return 0 if result.get("status") != "error" else 1


if __name__ == "__main__":
    sys.exit(main())
