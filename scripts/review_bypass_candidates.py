#!/usr/bin/env python3
"""
Phase 4b — Bypass Candidate Review CLI

Interactive CLI for reviewing bypass candidates from pattern analysis.
Human approves/rejects each candidate, producing an adaptive_bypass_rules.yaml
config file consumed by G24 adaptive bypass middleware.

Usage:
    python scripts/review_bypass_candidates.py --input analysis/pattern_report.json
    python scripts/review_bypass_candidates.py --input analysis/pattern_report.json --output config/adaptive_bypass_rules.yaml
    python scripts/review_bypass_candidates.py --input analysis/pattern_report.json --auto-approve 0.8
"""
import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import yaml

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def load_report(path: Path) -> Dict[str, Any]:
    """Load the pattern report JSON."""
    with open(path, "r") as f:
        return json.load(f)


def display_candidate(idx: int, total: int, candidate: Dict[str, Any]) -> None:
    """Display a single bypass candidate for review."""
    print(f"\n{'─' * 60}")
    print(f"  Candidate {idx + 1}/{total}: {candidate['group']}")
    print(f"{'─' * 60}")
    print(f"  Reason:         {candidate['reason']}")
    print(f"  Confidence:     {candidate['confidence']:.3f}")
    print(f"  Action:         {candidate['recommended_action']}")
    print(f"  Negative %:     {candidate['negative_pct']:.1f}%")
    print(f"  Observations:   {candidate['total_observations']}")
    print(f"  Avg increase:   +{candidate['avg_token_increase']:.0f} tokens")
    print(f"  Datasets:       {', '.join(candidate['datasets'])}")
    print(f"  Models:         {', '.join(candidate['models'])}")

    pattern = candidate.get("pattern", {})
    if "token_range" in pattern:
        tr = pattern["token_range"]
        print(f"  Token range:    {tr['min']}–{tr['max']} (avg {tr['avg']})")
    print()


def prompt_decision() -> str:
    """Prompt user for approve/reject/modify decision."""
    while True:
        choice = input("  Decision [a]pprove / [r]eject / [m]odify / [s]kip / [q]uit: ").strip().lower()
        if choice in ("a", "approve"):
            return "approve"
        elif choice in ("r", "reject"):
            return "reject"
        elif choice in ("m", "modify"):
            return "modify"
        elif choice in ("s", "skip"):
            return "skip"
        elif choice in ("q", "quit"):
            return "quit"
        print("  Invalid choice. Enter a/r/m/s/q.")


def prompt_modification(candidate: Dict[str, Any]) -> Dict[str, Any]:
    """Allow user to modify thresholds for a candidate."""
    print("  Modify parameters (press Enter to keep default):")

    # Allow adjusting the min token threshold
    pattern = candidate.get("pattern", {})
    token_range = pattern.get("token_range", {})
    default_min = token_range.get("min", 0)

    min_tokens_input = input(f"    Min tokens to trigger bypass [{default_min}]: ").strip()
    min_tokens = int(min_tokens_input) if min_tokens_input else default_min

    # Allow restricting to specific datasets
    all_datasets = candidate.get("datasets", [])
    datasets_input = input(f"    Restrict to datasets (comma-separated) [{','.join(all_datasets)}]: ").strip()
    datasets = [d.strip() for d in datasets_input.split(",")] if datasets_input else all_datasets

    # Allow restricting to specific models
    all_models = candidate.get("models", [])
    models_input = input(f"    Restrict to models (comma-separated) [{','.join(all_models)}]: ").strip()
    models = [m.strip() for m in models_input.split(",")] if models_input else all_models

    return {
        "min_tokens": min_tokens,
        "datasets": datasets,
        "models": models,
    }


def prompt_rejection_reason() -> str:
    """Ask user for rejection reason."""
    reason = input("  Rejection reason (optional): ").strip()
    return reason or "No reason provided"


def build_bypass_rule(candidate: Dict[str, Any], modifications: Dict[str, Any] = None) -> Dict[str, Any]:
    """Build a bypass rule from a candidate and optional modifications."""
    pattern = candidate.get("pattern", {})
    token_range = pattern.get("token_range", {})

    rule = {
        "group": candidate["group"],
        "enabled": True,
        "reason": candidate["reason"],
        "confidence": candidate["confidence"],
        "conditions": {
            "min_prompt_tokens": token_range.get("min", 0),
            "datasets": candidate.get("datasets", []),
            "models": candidate.get("models", []),
        },
        "approved_at": datetime.now(timezone.utc).isoformat(),
        "approved_by": "human_review",
    }

    if modifications:
        rule["conditions"]["min_prompt_tokens"] = modifications.get("min_tokens", rule["conditions"]["min_prompt_tokens"])
        rule["conditions"]["datasets"] = modifications.get("datasets", rule["conditions"]["datasets"])
        rule["conditions"]["models"] = modifications.get("models", rule["conditions"]["models"])

    return rule


def run_interactive_review(candidates: List[Dict[str, Any]], auto_approve_threshold: float = None) -> tuple:
    """Run interactive review of all candidates. Returns (approved, rejected) lists."""
    approved_rules = []
    rejected = []

    for idx, candidate in enumerate(candidates):
        display_candidate(idx, len(candidates), candidate)

        # Auto-approve if above threshold
        if auto_approve_threshold and candidate["confidence"] >= auto_approve_threshold:
            print(f"  ✅ AUTO-APPROVED (confidence {candidate['confidence']:.3f} >= {auto_approve_threshold})")
            approved_rules.append(build_bypass_rule(candidate))
            continue

        decision = prompt_decision()

        if decision == "quit":
            print("  Review session terminated.")
            break
        elif decision == "approve":
            approved_rules.append(build_bypass_rule(candidate))
            print("  ✅ Approved")
        elif decision == "modify":
            mods = prompt_modification(candidate)
            approved_rules.append(build_bypass_rule(candidate, mods))
            print("  ✅ Approved (modified)")
        elif decision == "reject":
            reason = prompt_rejection_reason()
            rejected.append({
                "group": candidate["group"],
                "reason": reason,
                "confidence": candidate["confidence"],
                "rejected_at": datetime.now(timezone.utc).isoformat(),
            })
            print(f"  ❌ Rejected: {reason}")
        elif decision == "skip":
            print("  ⏭️  Skipped")

    return approved_rules, rejected


def write_bypass_rules(rules: List[Dict[str, Any]], rejected: List[Dict[str, Any]], output_path: Path) -> None:
    """Write approved rules to YAML config file."""
    config = {
        "# Generated by review_bypass_candidates.py": None,
        "adaptive_bypass": {
            "enabled": True,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "rules": rules,
        },
    }

    # Remove the comment key hack and build proper YAML
    yaml_content = f"# Generated by review_bypass_candidates.py\n"
    yaml_content += f"# Date: {datetime.now(timezone.utc).isoformat()}\n"
    yaml_content += f"# Approved rules: {len(rules)}, Rejected: {len(rejected)}\n\n"
    yaml_content += yaml.dump(
        {"adaptive_bypass": {"enabled": True, "rules": rules}},
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
    )

    if rejected:
        yaml_content += "\n# Rejected candidates (for reference)\n"
        yaml_content += yaml.dump(
            {"_rejected": rejected},
            default_flow_style=False,
            sort_keys=False,
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        f.write(yaml_content)


def main():
    parser = argparse.ArgumentParser(
        description="Review bypass candidates and generate adaptive bypass rules"
    )
    parser.add_argument(
        "--input",
        required=True,
        help="Path to pattern_report.json from analyse_savings_patterns.py",
    )
    parser.add_argument(
        "--output",
        default="config/adaptive_bypass_rules.yaml",
        help="Output path for bypass rules YAML (default: config/adaptive_bypass_rules.yaml)",
    )
    parser.add_argument(
        "--auto-approve",
        type=float,
        default=None,
        help="Auto-approve candidates with confidence >= this threshold (e.g. 0.8)",
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Non-interactive mode — only auto-approve candidates above threshold",
    )
    args = parser.parse_args()

    # Resolve paths
    project_root = Path(__file__).resolve().parent.parent
    input_path = Path(args.input)
    if not input_path.is_absolute():
        input_path = project_root / input_path
    output_path = Path(args.output)
    if not output_path.is_absolute():
        output_path = project_root / output_path

    if not input_path.exists():
        logger.error("Input file not found: %s", input_path)
        sys.exit(1)

    # Load report
    report = load_report(input_path)
    candidates = report.get("bypass_candidates", [])

    if not candidates:
        print("\n✅ No bypass candidates in report — nothing to review.")
        print(f"   Source: {input_path}\n")
        sys.exit(0)

    print(f"\n{'═' * 60}")
    print(f"  BYPASS CANDIDATE REVIEW")
    print(f"  Source: {input_path.name}")
    print(f"  Candidates: {len(candidates)}")
    if args.auto_approve:
        print(f"  Auto-approve threshold: {args.auto_approve}")
    print(f"{'═' * 60}")

    if args.non_interactive:
        if not args.auto_approve:
            logger.error("--non-interactive requires --auto-approve threshold")
            sys.exit(1)
        # Auto-approve above threshold, reject rest
        approved = []
        rejected = []
        for c in candidates:
            if c["confidence"] >= args.auto_approve:
                approved.append(build_bypass_rule(c))
                print(f"  ✅ AUTO: {c['group']} (confidence {c['confidence']:.3f})")
            else:
                rejected.append({
                    "group": c["group"],
                    "reason": f"Below auto-approve threshold ({c['confidence']:.3f} < {args.auto_approve})",
                    "confidence": c["confidence"],
                    "rejected_at": datetime.now(timezone.utc).isoformat(),
                })
                print(f"  ❌ SKIP: {c['group']} (confidence {c['confidence']:.3f})")
    else:
        approved, rejected = run_interactive_review(candidates, args.auto_approve)

    # Write results
    if approved:
        write_bypass_rules(approved, rejected, output_path)
        print(f"\n{'═' * 60}")
        print(f"  REVIEW COMPLETE")
        print(f"  Approved: {len(approved)} rules")
        print(f"  Rejected: {len(rejected)}")
        print(f"  Output:   {output_path}")
        print(f"{'═' * 60}\n")
    else:
        print(f"\n  No rules approved — no output file generated.\n")


if __name__ == "__main__":
    main()
