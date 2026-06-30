#!/usr/bin/env python3
"""
Phase 4a — Savings Pattern Analysis Script

Analyses per-step savings data from ROI run outputs (aggregate.json files) and/or
Prometheus metrics to identify G-groups that consistently show negative savings
(token increase) for specific request characteristics.

Generates a pattern_report.json with candidate bypass rules.

Usage:
    python scripts/analyse_savings_patterns.py --output analysis/pattern_report.json
    python scripts/analyse_savings_patterns.py --days 7 --prometheus http://localhost:9090
    python scripts/analyse_savings_patterns.py --run-dir pitch-test-plan/output/run-16-06-26
"""
import argparse
import glob
import json
import logging
import os
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


@dataclass
class StepObservation:
    """Single observation of a G-group step in a request."""
    group: str
    tokens_before: int
    tokens_after: int
    abs_saving: int
    pct_saving: float
    dataset_id: str
    request_id: str
    model: str
    baseline_tokens: int
    tenant_id: str = "default"
    config_profile: str = "all-on"


@dataclass
class GroupPattern:
    """Aggregated pattern for a single G-group."""
    group: str
    total_observations: int = 0
    negative_count: int = 0       # tokens_after > tokens_before
    zero_count: int = 0           # tokens_after == tokens_before (no effect)
    positive_count: int = 0       # tokens_after < tokens_before (savings)
    total_abs_saving: int = 0
    avg_pct_saving: float = 0.0
    negative_pct: float = 0.0     # % of requests with negative savings
    datasets_affected: List[str] = field(default_factory=list)
    models_affected: List[str] = field(default_factory=list)
    observations: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def is_bypass_candidate(self) -> bool:
        """Returns True if >50% of observations show negative savings."""
        if self.total_observations == 0:
            return False
        return self.negative_pct > 50.0

    @property
    def confidence_score(self) -> float:
        """Confidence in bypass recommendation (0-1)."""
        if self.total_observations < 3:
            return 0.0
        # Higher confidence with more observations and higher negative %
        obs_factor = min(1.0, self.total_observations / 20)
        neg_factor = self.negative_pct / 100.0
        return round(obs_factor * neg_factor, 3)


@dataclass
class BypassCandidate:
    """A candidate bypass rule generated from pattern analysis."""
    group: str
    reason: str
    confidence: float
    negative_pct: float
    total_observations: int
    avg_token_increase: float  # average tokens added (negative = tokens saved)
    datasets: List[str]
    models: List[str]
    pattern: Dict[str, Any]
    recommended_action: str


@dataclass
class PatternReport:
    """Complete pattern analysis report."""
    generated_at: str
    source_type: str  # "roi_output" or "prometheus"
    total_requests_analyzed: int
    total_groups_analyzed: int
    groups: Dict[str, Dict[str, Any]]
    bypass_candidates: List[Dict[str, Any]]
    summary: Dict[str, Any]


def load_aggregate_files(run_dir: str) -> List[Dict[str, Any]]:
    """Load all aggregate.json files from a run directory."""
    aggregates = []
    patterns = [
        os.path.join(run_dir, "**", "aggregate.json"),
        os.path.join(run_dir, "**", "*-aggregate.json"),
    ]
    for pattern in patterns:
        for filepath in glob.glob(pattern, recursive=True):
            try:
                with open(filepath, "r") as f:
                    data = json.load(f)
                data["_source_path"] = filepath
                aggregates.append(data)
            except (json.JSONDecodeError, IOError) as e:
                logger.warning("Skipping %s: %s", filepath, e)
    logger.info("Loaded %d aggregate files from %s", len(aggregates), run_dir)
    return aggregates


def extract_observations(aggregates: List[Dict[str, Any]]) -> List[StepObservation]:
    """Extract per-step observations from aggregate data."""
    observations = []

    for agg in aggregates:
        dataset_id = agg.get("dataset_id", "unknown")
        source_path = agg.get("_source_path", "")

        # Determine config profile from filename
        config_profile = "all-on"
        basename = os.path.basename(source_path)
        if basename.startswith("only-"):
            config_profile = basename.replace("-aggregate.json", "")
        elif "all-off" in basename:
            config_profile = "all-off"
        elif "all-on" in basename:
            config_profile = "all-on"

        # Process each config section (all_on, all_off, only_GXX, etc.)
        for section_key in ["all_on", "all_off"]:
            section = agg.get(section_key)
            if not section:
                continue
            _extract_section_observations(
                section, dataset_id, section_key.replace("_", "-"), observations
            )

        # Process individual group isolations (only-GXX sections)
        for key, section in agg.items():
            if key.startswith("only_") or (isinstance(section, dict) and "results" in section and key not in ("all_on", "all_off")):
                profile = key.replace("_", "-") if key.startswith("only_") else key
                if isinstance(section, dict) and "results" in section:
                    _extract_section_observations(
                        section, dataset_id, profile, observations
                    )

    logger.info("Extracted %d step observations", len(observations))
    return observations


def _extract_section_observations(
    section: Dict[str, Any],
    dataset_id: str,
    config_profile: str,
    observations: List[StepObservation],
) -> None:
    """Extract observations from a single section of aggregate data."""
    results = section.get("results", [])
    for result in results:
        steps = result.get("steps", [])
        request_id = result.get("request_id", "unknown")
        model = result.get("model", "unknown")
        baseline_tokens = result.get("baseline_tokens", 0)

        for step in steps:
            group = step.get("group", "")
            tokens_before = step.get("tokens_before", 0)
            tokens_after = step.get("tokens_after", 0)
            abs_saving = step.get("abs_saving", tokens_before - tokens_after)

            pct_saving = 0.0
            if tokens_before > 0:
                pct_saving = round(((tokens_before - tokens_after) / tokens_before) * 100, 2)

            observations.append(StepObservation(
                group=group,
                tokens_before=tokens_before,
                tokens_after=tokens_after,
                abs_saving=abs_saving,
                pct_saving=pct_saving,
                dataset_id=dataset_id,
                request_id=request_id,
                model=model,
                baseline_tokens=baseline_tokens,
                config_profile=config_profile,
            ))


def analyse_patterns(observations: List[StepObservation]) -> Dict[str, GroupPattern]:
    """Aggregate observations into per-group patterns."""
    groups: Dict[str, GroupPattern] = {}

    # Group observations by G-group
    by_group: Dict[str, List[StepObservation]] = defaultdict(list)
    for obs in observations:
        by_group[obs.group].append(obs)

    for group_name, obs_list in sorted(by_group.items()):
        pattern = GroupPattern(group=group_name)
        pattern.total_observations = len(obs_list)

        datasets = set()
        models = set()
        total_pct = 0.0

        for obs in obs_list:
            if obs.tokens_after > obs.tokens_before:
                pattern.negative_count += 1
            elif obs.tokens_after == obs.tokens_before:
                pattern.zero_count += 1
            else:
                pattern.positive_count += 1

            pattern.total_abs_saving += obs.abs_saving
            total_pct += obs.pct_saving
            datasets.add(obs.dataset_id)
            models.add(obs.model)

            # Store detail for negative observations
            if obs.tokens_after > obs.tokens_before:
                pattern.observations.append({
                    "request_id": obs.request_id,
                    "dataset_id": obs.dataset_id,
                    "model": obs.model,
                    "tokens_before": obs.tokens_before,
                    "tokens_after": obs.tokens_after,
                    "token_increase": obs.tokens_after - obs.tokens_before,
                    "config_profile": obs.config_profile,
                })

        pattern.avg_pct_saving = round(total_pct / len(obs_list), 2) if obs_list else 0.0
        pattern.negative_pct = round((pattern.negative_count / len(obs_list)) * 100, 2) if obs_list else 0.0
        pattern.datasets_affected = sorted(datasets)
        pattern.models_affected = sorted(models)

        groups[group_name] = pattern

    return groups


def generate_bypass_candidates(groups: Dict[str, GroupPattern]) -> List[BypassCandidate]:
    """Generate bypass candidates from group patterns."""
    candidates = []

    for group_name, pattern in sorted(groups.items()):
        if not pattern.is_bypass_candidate:
            continue

        # Calculate average token increase for negative observations
        avg_increase = 0.0
        if pattern.observations:
            total_increase = sum(o["token_increase"] for o in pattern.observations)
            avg_increase = round(total_increase / len(pattern.observations), 1)

        # Determine recommended action
        if pattern.confidence_score >= 0.8:
            action = "STRONGLY_RECOMMEND_BYPASS"
        elif pattern.confidence_score >= 0.5:
            action = "RECOMMEND_BYPASS"
        else:
            action = "MONITOR"

        # Build pattern descriptor
        request_pattern: Dict[str, Any] = {
            "datasets": pattern.datasets_affected,
            "models": pattern.models_affected,
        }
        # Add token range characteristics if we have enough data
        if pattern.observations:
            token_ranges = [o["tokens_before"] for o in pattern.observations]
            request_pattern["token_range"] = {
                "min": min(token_ranges),
                "max": max(token_ranges),
                "avg": round(sum(token_ranges) / len(token_ranges)),
            }

        reason = (
            f"{pattern.negative_pct:.0f}% of {pattern.total_observations} requests "
            f"show token increase (avg +{avg_increase:.0f} tokens)"
        )

        candidates.append(BypassCandidate(
            group=group_name,
            reason=reason,
            confidence=pattern.confidence_score,
            negative_pct=pattern.negative_pct,
            total_observations=pattern.total_observations,
            avg_token_increase=avg_increase,
            datasets=pattern.datasets_affected,
            models=pattern.models_affected,
            pattern=request_pattern,
            recommended_action=action,
        ))

    # Sort by confidence (highest first)
    candidates.sort(key=lambda c: c.confidence, reverse=True)
    return candidates


def query_prometheus(prometheus_url: str, days: int) -> List[StepObservation]:
    """Query Prometheus for per-group savings data (optional, requires running Prometheus)."""
    try:
        import requests
    except ImportError:
        logger.error("'requests' package required for Prometheus queries. pip install requests")
        return []

    observations = []
    end_time = datetime.now(timezone.utc)
    start_time_unix = int(end_time.timestamp()) - (days * 86400)
    end_time_unix = int(end_time.timestamp())

    # Query GROUP_TOKENS_SAVED counter for each group
    query = 'increase(token_opt_group_tokens_saved_total[1h])'
    try:
        resp = requests.get(
            f"{prometheus_url}/api/v1/query_range",
            params={
                "query": query,
                "start": start_time_unix,
                "end": end_time_unix,
                "step": "1h",
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        if data.get("status") == "success":
            for result in data.get("data", {}).get("result", []):
                group = result.get("metric", {}).get("group", "unknown")
                values = result.get("values", [])
                for ts, val in values:
                    val_f = float(val)
                    if val_f != 0:
                        observations.append(StepObservation(
                            group=group,
                            tokens_before=0,  # Prometheus doesn't give per-request detail
                            tokens_after=0,
                            abs_saving=int(val_f),
                            pct_saving=0.0,
                            dataset_id="prometheus",
                            request_id=f"prom-{ts}",
                            model="unknown",
                            baseline_tokens=0,
                        ))
        logger.info("Retrieved %d Prometheus observations", len(observations))
    except Exception as e:
        logger.warning("Prometheus query failed (will use file-based analysis): %s", e)

    return observations


def build_report(
    groups: Dict[str, GroupPattern],
    candidates: List[BypassCandidate],
    total_requests: int,
    source_type: str,
) -> PatternReport:
    """Build the final pattern report."""
    # Summary stats
    total_groups = len(groups)
    groups_with_negative = sum(1 for g in groups.values() if g.negative_count > 0)
    bypass_candidate_count = len(candidates)

    groups_dict = {}
    for name, pattern in sorted(groups.items()):
        groups_dict[name] = {
            "total_observations": pattern.total_observations,
            "positive_count": pattern.positive_count,
            "negative_count": pattern.negative_count,
            "zero_count": pattern.zero_count,
            "negative_pct": pattern.negative_pct,
            "avg_pct_saving": pattern.avg_pct_saving,
            "total_abs_saving": pattern.total_abs_saving,
            "datasets_affected": pattern.datasets_affected,
            "models_affected": pattern.models_affected,
            "is_bypass_candidate": pattern.is_bypass_candidate,
            "confidence_score": pattern.confidence_score,
        }

    candidates_dict = [asdict(c) for c in candidates]

    return PatternReport(
        generated_at=datetime.now(timezone.utc).isoformat(),
        source_type=source_type,
        total_requests_analyzed=total_requests,
        total_groups_analyzed=total_groups,
        groups=groups_dict,
        bypass_candidates=candidates_dict,
        summary={
            "total_groups": total_groups,
            "groups_with_negative_savings": groups_with_negative,
            "bypass_candidates": bypass_candidate_count,
            "high_confidence_candidates": sum(
                1 for c in candidates if c.confidence >= 0.8
            ),
        },
    )


def main():
    parser = argparse.ArgumentParser(
        description="Analyse savings patterns across ROI runs to identify bypass candidates"
    )
    parser.add_argument(
        "--run-dir",
        default="pitch-test-plan/output",
        help="Root directory containing ROI run output (default: pitch-test-plan/output)",
    )
    parser.add_argument(
        "--output",
        default="analysis/pattern_report.json",
        help="Output path for the pattern report (default: analysis/pattern_report.json)",
    )
    parser.add_argument(
        "--prometheus",
        default=None,
        help="Prometheus URL for live metric queries (e.g. http://localhost:9090)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="Number of days to query from Prometheus (default: 7)",
    )
    parser.add_argument(
        "--min-observations",
        type=int,
        default=3,
        help="Minimum observations for a group to be considered (default: 3)",
    )
    parser.add_argument(
        "--negative-threshold",
        type=float,
        default=50.0,
        help="Percentage threshold for negative savings to flag as candidate (default: 50.0)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose output",
    )
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Resolve paths relative to project root
    project_root = Path(__file__).resolve().parent.parent
    run_dir = Path(args.run_dir)
    if not run_dir.is_absolute():
        run_dir = project_root / run_dir
    output_path = Path(args.output)
    if not output_path.is_absolute():
        output_path = project_root / output_path

    # Collect observations
    observations: List[StepObservation] = []
    source_type = "roi_output"

    # File-based analysis (primary)
    if run_dir.exists():
        aggregates = load_aggregate_files(str(run_dir))
        observations.extend(extract_observations(aggregates))
    else:
        logger.warning("Run directory not found: %s", run_dir)

    # Prometheus-based analysis (supplementary)
    if args.prometheus:
        prom_obs = query_prometheus(args.prometheus, args.days)
        observations.extend(prom_obs)
        if prom_obs:
            source_type = "roi_output+prometheus"

    if not observations:
        logger.error("No observations found. Check --run-dir path or --prometheus URL.")
        sys.exit(1)

    # Filter to all-on profiles (we want full-pipeline behaviour, not isolated groups)
    all_on_obs = [o for o in observations if o.config_profile in ("all-on", "prometheus")]
    if all_on_obs:
        logger.info(
            "Using %d all-on observations (filtered from %d total)",
            len(all_on_obs), len(observations),
        )
        analysis_obs = all_on_obs
    else:
        logger.info("No all-on observations found; using all %d observations", len(observations))
        analysis_obs = observations

    # Analyse patterns
    groups = analyse_patterns(analysis_obs)

    # Filter groups below minimum observations
    groups = {
        k: v for k, v in groups.items()
        if v.total_observations >= args.min_observations
    }

    # Generate bypass candidates
    candidates = generate_bypass_candidates(groups)

    # Count unique requests
    unique_requests = len(set(
        (o.request_id, o.dataset_id, o.config_profile) for o in analysis_obs
    ))

    # Build report
    report = build_report(groups, candidates, unique_requests, source_type)

    # Write output
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(asdict(report), f, indent=2, default=str)

    # Print summary
    print("\n" + "=" * 60)
    print("  SAVINGS PATTERN ANALYSIS — SUMMARY")
    print("=" * 60)
    print(f"  Source:              {source_type}")
    print(f"  Requests analysed:   {unique_requests}")
    print(f"  G-groups analysed:   {len(groups)}")
    print(f"  Bypass candidates:   {len(candidates)}")
    print()

    if candidates:
        print("  BYPASS CANDIDATES:")
        print("  " + "-" * 56)
        for c in candidates:
            emoji = "🔴" if c.confidence >= 0.8 else "🟡" if c.confidence >= 0.5 else "⚪"
            print(f"  {emoji} {c.group} — {c.reason}")
            print(f"      Confidence: {c.confidence:.2f} | Action: {c.recommended_action}")
            print(f"      Datasets: {', '.join(c.datasets)} | Models: {', '.join(c.models)}")
            print()
    else:
        print("  ✅ No bypass candidates found — all G-groups show positive savings")
        print()

    # Per-group summary table
    print("  PER-GROUP SAVINGS SUMMARY:")
    print("  " + "-" * 56)
    print(f"  {'Group':<6} {'Obs':>4} {'Pos':>4} {'Neg':>4} {'Zero':>4} {'Neg%':>6} {'AvgPct':>7}")
    print("  " + "-" * 56)
    for name, g in sorted(groups.items()):
        flag = " ⚠" if g.is_bypass_candidate else ""
        print(
            f"  {name:<6} {g.total_observations:>4} {g.positive_count:>4} "
            f"{g.negative_count:>4} {g.zero_count:>4} {g.negative_pct:>5.1f}% "
            f"{g.avg_pct_saving:>6.1f}%{flag}"
        )
    print()
    print(f"  Report saved to: {output_path}")
    print("=" * 60 + "\n")

    # Exit with code 0 if no candidates, 1 if candidates found (for CI integration)
    sys.exit(0)


if __name__ == "__main__":
    main()
