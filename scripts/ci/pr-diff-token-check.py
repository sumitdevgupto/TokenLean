#!/usr/bin/env python3
"""
G02 CI Integration — PR-diff token count check.

Validates that template changes in a PR don't exceed budget limits.
Designed to run in Cloud Build or GitHub Actions on pull requests.

Usage:
    python pr-diff-token-check.py --base-ref main --head-ref feature-branch
"""
import argparse
import json
import logging
import os
import subprocess
import sys
from typing import Dict, List, Tuple

import yaml

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def get_changed_templates(base_ref: str, head_ref: str) -> List[str]:
    """Get list of changed template files in the PR."""
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", f"{base_ref}...{head_ref}"],
            capture_output=True,
            text=True,
            check=True,
        )
        files = result.stdout.strip().split("\n")
        # Filter for template files
        templates = [f for f in files if "template" in f.lower() and f.endswith((".yaml", ".yml", ".json"))]
        return templates
    except subprocess.CalledProcessError as exc:
        logger.error("Failed to get changed files: %s", exc)
        return []


def count_tokens_in_template(file_path: str) -> Tuple[int, int, int]:
    """
    Count tokens in a template file.
    Returns (system_tokens, user_tokens, total_tokens).
    """
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
        
        # Try to parse as YAML/JSON template
        try:
            data = yaml.safe_load(content)
        except yaml.YAMLError:
            data = None
        
        if data and isinstance(data, dict):
            # Extract template fields
            system_prompt = data.get("system_prompt", "")
            user_template = data.get("user_template", data.get("prompt", ""))
            
            # Count tokens (rough estimate: 1 token ≈ 4 chars)
            system_tokens = len(system_prompt) // 4
            user_tokens = len(user_template) // 4
        else:
            # Raw content - count everything
            total = len(content) // 4
            system_tokens = total
            user_tokens = 0
        
        total_tokens = system_tokens + user_tokens
        return system_tokens, user_tokens, total_tokens
        
    except Exception as exc:
        logger.error("Failed to count tokens in %s: %s", file_path, exc)
        return 0, 0, 0


def get_template_budget(file_path: str, config_path: str = "config/config.yaml.template") -> Dict:
    """Get budget limits for a template from config."""
    defaults = {
        "system_prompt_max": 500,
        "total_input_max": 2000,
        "output_max": 500,
    }
    
    try:
        with open(config_path, "r") as f:
            config = yaml.safe_load(f)
        
        g02_config = config.get("groups", {}).get("G2_template_registry", {})
        budgets = g02_config.get("budgets", {})
        
        # Try to match template file to budget config
        template_name = os.path.basename(file_path).replace(".yaml", "").replace(".yml", "")
        if template_name in budgets:
            return {**defaults, **budgets[template_name]}
        
        return defaults
    except Exception as exc:
        logger.debug("Could not load budget config: %s", exc)
        return defaults


def check_template_budget(file_path: str, base_ref: str, head_ref: str) -> Dict:
    """Check if template changes stay within budget."""
    # Get token counts for both versions
    current_tokens = count_tokens_in_template(file_path)
    
    # Try to get previous version
    try:
        result = subprocess.run(
            ["git", "show", f"{base_ref}:{file_path}"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            # Write to temp file and count
            import tempfile
            with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
                f.write(result.stdout)
                temp_path = f.name
            
            previous_tokens = count_tokens_in_template(temp_path)
            os.unlink(temp_path)
        else:
            previous_tokens = (0, 0, 0)
    except Exception as exc:
        logger.debug("Could not get previous version: %s", exc)
        previous_tokens = (0, 0, 0)
    
    # Get budget
    budget = get_template_budget(file_path)
    
    # Check compliance
    system_tokens, user_tokens, total_tokens = current_tokens
    prev_system, prev_user, prev_total = previous_tokens
    
    system_within_budget = system_tokens <= budget["system_prompt_max"]
    total_within_budget = total_tokens <= budget["total_input_max"]
    
    # Check for regression (increase > 10%)
    token_change = total_tokens - prev_total
    token_change_pct = (token_change / prev_total * 100) if prev_total > 0 else 0
    
    return {
        "file": file_path,
        "previous_tokens": prev_total,
        "current_tokens": total_tokens,
        "token_change": token_change,
        "token_change_pct": round(token_change_pct, 2),
        "system_tokens": system_tokens,
        "system_budget": budget["system_prompt_max"],
        "system_within_budget": system_within_budget,
        "total_within_budget": total_within_budget,
        "regression": token_change_pct > 10 and prev_total > 0,
    }


def main():
    parser = argparse.ArgumentParser(description="Check template token budgets in PR diff")
    parser.add_argument("--base-ref", default=os.getenv("BASE_REF", "main"), help="Base git ref")
    parser.add_argument("--head-ref", default=os.getenv("HEAD_REF", "HEAD"), help="Head git ref")
    parser.add_argument("--config", default="config/config.yaml.template", help="Config file path")
    parser.add_argument("--output-json", help="Write report to JSON file")
    parser.add_argument("--fail-on-budget-exceeded", action="store_true", help="Exit with error if budget exceeded")
    parser.add_argument("--fail-on-regression", action="store_true", help="Exit with error if >10% token increase")
    
    args = parser.parse_args()
    
    # Get changed templates
    changed = get_changed_templates(args.base_ref, args.head_ref)
    
    if not changed:
        logger.info("No template files changed in this PR")
        print("✅ No template changes to validate")
        return 0
    
    logger.info("Found %d changed template files", len(changed))
    
    # Check each template
    results = []
    budget_violations = 0
    regressions = 0
    
    for template_file in changed:
        if not os.path.exists(template_file):
            logger.warning("File not found (deleted?): %s", template_file)
            continue
        
        result = check_template_budget(template_file, args.base_ref, args.head_ref)
        results.append(result)
        
        if not result["total_within_budget"]:
            budget_violations += 1
            logger.error(
                "❌ %s: Budget exceeded! %d tokens > %d limit",
                result["file"],
                result["current_tokens"],
                result["system_budget"]
            )
        
        if result["regression"]:
            regressions += 1
            logger.warning(
                "⚠️  %s: Token regression! +%d tokens (+%.1f%%)",
                result["file"],
                result["token_change"],
                result["token_change_pct"]
            )
        
        if result["total_within_budget"] and not result["regression"]:
            logger.info(
                "✅ %s: Within budget (%d tokens, %s%d from previous)",
                result["file"],
                result["current_tokens"],
                "+" if result["token_change"] >= 0 else "",
                result["token_change"]
            )
    
    # Summary
    print(f"\n{'='*60}")
    print(f"PR Diff Token Check Summary")
    print(f"{'='*60}")
    print(f"Templates checked: {len(results)}")
    print(f"Budget violations: {budget_violations}")
    print(f"Token regressions (>10%): {regressions}")
    
    # Write JSON report
    if args.output_json:
        report = {
            "base_ref": args.base_ref,
            "head_ref": args.head_ref,
            "templates_checked": len(results),
            "budget_violations": budget_violations,
            "regressions": regressions,
            "results": results,
        }
        with open(args.output_json, "w") as f:
            json.dump(report, f, indent=2)
        print(f"\nReport written to: {args.output_json}")
    
    # Exit with error if requested
    if args.fail_on_budget_exceeded and budget_violations > 0:
        print(f"\n❌ {budget_violations} template(s) exceeded budget limits")
        return 1
    
    if args.fail_on_regression and regressions > 0:
        print(f"\n⚠️  {regressions} template(s) had token regressions >10%")
        return 1
    
    print("\n✅ All checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
