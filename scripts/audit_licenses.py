#!/usr/bin/env python3
# =============================================================================
# audit_licenses.py — OSS licence compliance audit
# -----------------------------------------------------------------------------
# Substantiates the compliance claim in THIRD_PARTY_LICENSES.md:
#
#   "No GPL, AGPL, or SSPL code is imported into or redistributed as part of
#    this Work."
#
# Method (two passes, so partial environments can't produce a false clean):
#   1. Scan the installed environment via importlib.metadata — the authoritative
#      source, since it sees what is actually importable.
#   2. For declared dependencies NOT installed locally, fall back to published
#      PyPI metadata, so coverage reaches 100% of the declared set rather than
#      silently auditing whatever happens to be on the machine.
#
# Scope is the *imported libraries* of the Work — the proxy runtime and its test
# deps. The sidecars (Tika, LLMLingua, RouteLLM) run as separate network services
# and are covered by the bundled-services table in THIRD_PARTY_LICENSES.md, not
# here. Add to REQ_FILES if that ever changes.
#
# Usage:
#   python scripts/audit_licenses.py              # full audit (needs network)
#   python scripts/audit_licenses.py --offline    # installed env only
#
# Exit codes:
#   0 — no strong copyleft, every declared dependency resolved
#   1 — strong copyleft found, or a licence could not be determined
#
# Wired into:
#   - nothing automatic. Run before refreshing the "Last verified" date in
#     THIRD_PARTY_LICENSES.md. Safe to add as a CI gate, but note pass 2 needs
#     outbound network to pypi.org.
# =============================================================================
"""OSS licence compliance audit — see module banner."""
from __future__ import annotations

import argparse
import importlib.metadata as md
import json
import re
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

REQ_FILES = [
    REPO_ROOT / "src" / "proxy" / "requirements.txt",
    REPO_ROOT / "tests" / "requirements-test.txt",
]

# Strong copyleft — a hit here fails the audit and the claim in
# THIRD_PARTY_LICENSES.md would need revisiting.
STRONG = re.compile(r"\b(A?GPL(?:v[23])?|SSPL|LGPL)\b", re.I)
# Weak / file-level copyleft — reported for visibility, not a failure. MPL-2.0
# does not impose copyleft on the larger Apache-2.0 Work.
WEAK = re.compile(r"\bMPL\b|Mozilla Public License", re.I)

PYPI_URL = "https://pypi.org/pypi/{name}/json"

# Packages that publish no machine-readable licence, verified by hand against the
# project's own source. The evidence URL is the audit trail — keep it, and re-check
# if the pin ever moves to a major version.
MANUAL_OVERRIDES = {
    "zep-python": (
        "Apache-2.0",
        "github.com/getzep/zep-python LICENSE, verified 2026-08-05",
    ),
}


def norm(name: str) -> str:
    """PEP 503 normalisation, so `zep-python` and `zep_python` compare equal."""
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_requirements(path: Path) -> set[str]:
    """Extract bare package names. Ignores comments, flags, and `-r` includes."""
    names: set[str] = set()
    if not path.exists():
        print(f"  warning: {path} not found — skipped", file=sys.stderr)
        return names
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        match = re.match(r"^([A-Za-z0-9._-]+)", line)
        if match:
            names.add(norm(match.group(1)))
    return names


def licence_of_installed(dist) -> str:
    """Join every licence signal a distribution exposes into one searchable blob."""
    parts: list[str] = []
    meta = dist.metadata
    for key in ("License-Expression", "License"):
        value = meta.get(key)
        if value and value.strip() and value.strip().upper() != "UNKNOWN":
            parts.append(value.strip().splitlines()[0][:120])
    parts += [
        cls.split("::")[-1].strip()
        for cls in (meta.get_all("Classifier") or [])
        if cls.startswith("License ::")
    ]
    return " | ".join(dict.fromkeys(parts))


def licence_from_pypi(name: str) -> str:
    """Published metadata for a package that isn't installed locally."""
    with urllib.request.urlopen(PYPI_URL.format(name=name), timeout=20) as fh:
        info = json.load(fh)["info"]
    parts: list[str] = []
    if info.get("license_expression"):
        parts.append(info["license_expression"])
    if info.get("license"):
        parts.append(info["license"].strip().splitlines()[0][:120])
    parts += [
        cls.split("::")[-1].strip()
        for cls in (info.get("classifiers") or [])
        if cls.startswith("License ::")
    ]
    return " | ".join(dict.fromkeys(p for p in parts if p))


def classify(blob: str) -> str:
    if not blob:
        return "unknown"
    if STRONG.search(blob):
        return "strong"
    if WEAK.search(blob):
        return "weak"
    return "ok"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--offline",
        action="store_true",
        help="skip the PyPI pass; audits only what is installed locally",
    )
    args = parser.parse_args()

    declared: set[str] = set()
    for path in REQ_FILES:
        declared |= parse_requirements(path)

    installed = {}
    for dist in md.distributions():
        name = dist.metadata.get("Name")
        if name:
            installed[norm(name)] = dist

    strong: list[tuple[str, str]] = []
    weak: list[tuple[str, str]] = []
    unknown: list[str] = []

    # ── Pass 1: the installed environment (authoritative) ────────────────────
    resolved = set()
    for key, dist in sorted(installed.items()):
        blob = licence_of_installed(dist)
        verdict = classify(blob)
        if key in declared:
            resolved.add(key)
        if verdict == "strong":
            strong.append((key, blob))
        elif verdict == "weak":
            weak.append((key, blob))
        elif verdict == "unknown" and key in declared:
            unknown.append(key)

    # ── Pass 2: declared but not installed → published metadata ──────────────
    gaps = sorted(declared - resolved)
    if gaps and not args.offline:
        print(f"Resolving {len(gaps)} declared dependencies not installed locally…\n")
        for name in gaps:
            if name in MANUAL_OVERRIDES:
                blob, evidence = MANUAL_OVERRIDES[name]
                if classify(blob) == "strong":
                    strong.append((name, blob))
                else:
                    resolved.add(name)
                print(f"  {name:34s} {blob:22s} [manual: {evidence}]")
                continue
            try:
                blob = licence_from_pypi(name)
            except Exception as exc:  # noqa: BLE001 — network/404/parse all mean "unresolved"
                print(f"  {name:34s} LOOKUP FAILED: {exc}")
                unknown.append(name)
                continue
            verdict = classify(blob)
            if verdict == "strong":
                strong.append((name, blob))
            elif verdict == "weak":
                weak.append((name, blob))
            elif verdict == "unknown":
                print(f"  {name:34s} no published licence metadata — check the source repo")
                unknown.append(name)
            else:
                resolved.add(name)
                print(f"  {name:34s} {blob[:70]}")
        print()
    elif gaps:
        print(f"--offline: {len(gaps)} declared dependencies unaudited: {', '.join(gaps)}\n")
        unknown.extend(gaps)

    coverage = len(resolved) / len(declared) * 100 if declared else 0.0
    print(f"declared dependencies : {len(declared)}")
    print(f"resolved              : {len(resolved)}  ({coverage:.1f}% coverage)")
    print(f"distributions scanned : {len(installed)}")

    print(f"\nstrong copyleft (GPL/AGPL/SSPL/LGPL): {len(strong)}")
    for name, blob in strong:
        print(f"  ✗ {name}: {blob}")
    print(f"weak copyleft (MPL — compatible, informational): {len(weak)}")
    for name, blob in weak:
        print(f"  · {name}: {blob}")
    if unknown:
        print(f"unresolved ({len(unknown)}): {', '.join(sorted(set(unknown)))}")

    if strong or unknown:
        print("\nAUDIT FAILED — resolve the above before refreshing THIRD_PARTY_LICENSES.md.")
        return 1
    print("\nAUDIT CLEAN — no GPL/AGPL/SSPL among the declared dependencies.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
