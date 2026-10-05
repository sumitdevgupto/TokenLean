#!/usr/bin/env python3
# =============================================================================
# audit_licenses.py — the licence rule, checked
# -----------------------------------------------------------------------------
# The rule (THIRD_PARTY_LICENSES.md): everything TokenLean installs is free to use
# and to host — permissively licensed (MIT, Apache-2.0, BSD, ISC, PSF, PostgreSQL,
# BSL-1.0, CNRI-Python, Zlib, CC0, Unlicense, HPND), or MPL-2.0 for an unmodified
# transitive dependency. This script checks every pin of every lockfile the images
# and CI install against it.
#
# Method: each pin is judged by the licence of THAT release — the installed
# distribution's metadata when the same version is installed, else the release's
# published PyPI metadata (/pypi/<name>/<version>/json). Signals, most specific
# first: the SPDX License-Expression, then the License :: classifiers, then the
# License field. Every licence an expression or classifier list names must be
# allowed, so a dual licence with a disallowed alternative needs an override below.
#
# Scope: Python packages. Services, images and model weights are listed, with their
# licences, in THIRD_PARTY_LICENSES.md.
#
# Usage:
#   python scripts/audit_licenses.py              # full audit (needs network to pypi.org)
#   python scripts/audit_licenses.py --offline    # installed distributions only
#
# Exit codes:
#   0 — every pin is permissive or MPL-2.0 (pending removals are reported only)
#   1 — a pin's licence is outside the rule, or could not be determined
#
# Wired into: .github/workflows/ci.yml (the "Licence audit" step).
# =============================================================================
"""OSS licence rule audit — see module banner."""
from __future__ import annotations

import argparse
import importlib.metadata as md
import json
import re
import sys
import time
import urllib.request
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Every lockfile an image or CI installs: what scripts/compile-requirements.sh writes.
REQ_FILES = [
    REPO_ROOT / "src" / "proxy" / "requirements.txt",
    REPO_ROOT / "tests" / "requirements-test.txt",
    REPO_ROOT / "src" / "llmlingua-sidecar" / "requirements.txt",
    REPO_ROOT / "src" / "routellm-sidecar" / "requirements.txt",
    REPO_ROOT / "src" / "doc-pipeline" / "requirements.txt",
    REPO_ROOT / "src" / "finetune-pipeline" / "requirements.txt",
]

PYPI_URL = "https://pypi.org/pypi/{name}/{version}/json"

# The allowed licences, as SPDX ids and as the words classifiers and License fields use.
PERMISSIVE = re.compile(
    r"\b(MIT|MIT-0|MIT-CMU|Apache|BSD|0BSD|ISC|PSF|Python Software Foundation|Python-2\.0|"
    r"PostgreSQL|BSL-1\.0|Boost Software|CNRI-Python|Zlib|CC0|Unlicense|HPND|"
    r"Historical Permission Notice|Public Domain)\b", re.I)
MPL = re.compile(r"\bMPL\b|Mozilla Public License", re.I)
# Free-text licence fields can name several licences; any of these makes one outside the rule.
DISALLOWED = re.compile(
    r"\b(A?GPL|LGPL|SSPL|BUSL|Business Source|Commons Clause|Proprietary|Non-?Commercial|"
    r"Elastic License|RSAL)", re.I)
# An SPDX expression: licence ids joined by AND / OR / WITH, with parentheses.
_EXPRESSION = re.compile(r"^[A-Za-z0-9.+()\- ]+$")

# Licences verified by hand, with their evidence. A bare name fills a package that publishes
# no licence metadata at all; "name==version" corrects metadata that exists but is wrong, for
# that release only (a new release has to prove itself).
MANUAL_OVERRIDES = {
    "fsspec": ("BSD-3-Clause",
               "github.com/fsspec/filesystem_spec LICENSE at tag 2026.7.0, verified 2026-10-04"),
    "google-crc32c": ("Apache-2.0",
                      "github.com/googleapis/python-crc32c LICENSE at tag v1.8.0, verified 2026-10-04"),
    "py-rust-stemmers": ("MIT",
                         "github.com/qdrant/py-rust-stemmers LICENSE at tag v0.1.8, verified 2026-10-04"),
    "routellm": ("Apache-2.0",
                 "github.com/lm-sys/RouteLLM LICENSE, there since 2024-06-25, before the first "
                 "PyPI release (no release tags), verified 2026-10-04"),
    "fastembed==0.8.0": ("Apache-2.0",
                         "github.com/qdrant/fastembed LICENSE at tag v0.8.0; this release's PyPI "
                         "classifier says Other/Proprietary (0.8.1 corrected it), verified 2026-10-04"),
}

# Pins outside the rule that are already on their way out: reported, not failed, while
# src/proxy/requirements.in still marks them "drop at the next recompile" (a unit test holds that).
PENDING_REMOVAL: dict[str, str] = {}   # name -> why it is going; empty since zep-python left (2026-10-05)

Row = namedtuple("Row", "name version verdict source licence")


def norm(name: str) -> str:
    """PEP 503 normalisation, so `zep-python` and `zep_python` compare equal."""
    return re.sub(r"[-_.]+", "-", name).strip().lower()


_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?==([^\s;#]+)")


def parse_pins(text: str) -> dict[str, str]:
    """{name: version} for every `name==version` line of a pip-compile lockfile."""
    pins: dict[str, str] = {}
    for line in text.splitlines():
        match = _PIN.match(line.strip())
        if match:
            pins[norm(match.group(1))] = match.group(2)
    return pins


def _first_line(value) -> str:
    text = (value or "").strip()
    first = text.splitlines()[0].strip() if text else ""
    return "" if first.upper() == "UNKNOWN" else first


def _judge(licences: list[str]) -> str:
    """'permissive' / 'mpl' / 'other' for licences that must ALL be allowed."""
    if any(not (PERMISSIVE.search(lic) or MPL.search(lic)) for lic in licences):
        return "other"
    return "mpl" if any(MPL.search(lic) for lic in licences) else "permissive"


def _expression_licences(expression: str) -> list[str]:
    """The licence ids an SPDX expression names (a WITH exception only adds permissions)."""
    terms = re.split(r"\s+(?:AND|OR)\s+|[()]", expression)
    return [t.split(" WITH ")[0].strip() for t in terms if t.strip()]


def classify(evidence: dict) -> str:
    """'permissive', 'mpl', 'other' (outside the rule) or 'unknown' (no licence signal)."""
    expression = (evidence.get("expression") or "").strip()
    if expression:
        return _judge(_expression_licences(expression))
    named = [c.split("::")[-1].strip() for c in evidence.get("classifiers") or []]
    named = [c for c in named if c and c != "OSI Approved"]   # says nothing about WHICH licence
    if named:
        return _judge(named)
    field = _first_line(evidence.get("license"))
    if not field:
        return "unknown"
    if _EXPRESSION.match(field) and re.search(r"\b(AND|OR)\b|-\d", field):
        return _judge(_expression_licences(field))
    if DISALLOWED.search(field):
        return "other"
    if MPL.search(field):
        return "mpl"
    return "permissive" if PERMISSIVE.search(field) else "other"


def resolve(name: str, version: str, evidence: dict) -> tuple[str, str]:
    """(verdict, source) for one pin, with MANUAL_OVERRIDES applied."""
    exact = MANUAL_OVERRIDES.get(f"{name}=={version}")
    if exact:
        return classify({"expression": exact[0]}), f"override: {exact[1]}"
    verdict = classify(evidence)
    if verdict == "unknown" and name in MANUAL_OVERRIDES:
        licence, why = MANUAL_OVERRIDES[name]
        return classify({"expression": licence}), f"override: {why}"
    return verdict, "metadata"


def describe(evidence: dict) -> str:
    named = [c.split("::")[-1].strip() for c in evidence.get("classifiers") or []]
    return (evidence.get("expression") or ", ".join(named) or _first_line(evidence.get("license")))[:80]


def evidence_from_metadata(meta) -> dict:
    return {"expression": (meta.get("License-Expression") or "").strip(),
            "license": meta.get("License") or "",
            "classifiers": [c for c in (meta.get_all("Classifier") or []) if c.startswith("License ::")]}


def evidence_from_pypi(info: dict) -> dict:
    return {"expression": (info.get("license_expression") or "").strip(),
            "license": info.get("license") or "",
            "classifiers": [c for c in (info.get("classifiers") or []) if c.startswith("License ::")]}


def installed_distributions() -> dict:
    found = {}
    for dist in md.distributions():
        name = dist.metadata.get("Name")
        if name:
            found[norm(name)] = dist
    return found


def fetch(name: str, version: str, attempts: int = 3) -> dict:
    """The published PyPI metadata (`info`) of one release, retried on a transient failure."""
    url = PYPI_URL.format(name=name, version=version)
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=20) as fh:  # noqa: S310 — fixed https URL
                return json.load(fh)["info"]
        except Exception:  # noqa: BLE001 — retried, then re-raised to the caller
            if attempt == attempts - 1:
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError("unreachable")


def audit(rows: list[Row]) -> int:
    """Print the report; 1 if any pin is outside the rule or undetermined (pending removals
    excepted), else 0."""
    failing = [r for r in rows if r.verdict in ("other", "unknown") and r.name not in PENDING_REMOVAL]
    pending = [r for r in rows if r.verdict in ("other", "unknown") and r.name in PENDING_REMOVAL]
    mpl = [r for r in rows if r.verdict == "mpl"]
    overridden = [r for r in rows if r.source.startswith("override")]
    print(f"pins audited: {len(rows)}  (permissive or MPL-2.0: {len(rows) - len(failing) - len(pending)})")
    if mpl:
        print(f"\nMPL-2.0 (allowed: unmodified transitive dependencies): {len(mpl)}")
        for r in mpl:
            print(f"  {r.name}=={r.version}  {r.licence}")
    if overridden:
        print(f"\nlicensed by a recorded override (its evidence, not the published metadata): {len(overridden)}")
        for r in overridden:
            print(f"  {r.name}=={r.version}  {r.source}")
    if pending:
        print(f"\npending removal (reported, not failed): {len(pending)}")
        for r in pending:
            print(f"  {r.name}=={r.version}  {PENDING_REMOVAL[r.name]}")
    if failing:
        print(f"\nOUTSIDE THE RULE or undetermined: {len(failing)}")
        for r in failing:
            print(f"  {r.name}=={r.version}  [{r.verdict}] {r.licence or r.source}")
        print("\nAUDIT FAILED — replace the package, or verify its licence and record an override.")
        return 1
    print("\nAUDIT CLEAN — every pin is permissive or MPL-2.0.")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true",
                        help="audit installed distributions only; any other pin is undetermined")
    args = parser.parse_args(argv)

    pins = sorted({pin for path in REQ_FILES
                   for pin in parse_pins(path.read_text(encoding="utf-8")).items()})
    installed = installed_distributions()

    def look_up(pin):
        name, version = pin
        dist = installed.get(name)
        if dist is not None and dist.version == version:
            return pin, evidence_from_metadata(dist.metadata), ""
        if args.offline:
            return pin, None, "not installed (--offline)"
        try:
            return pin, evidence_from_pypi(fetch(name, version)), ""
        except Exception as exc:  # noqa: BLE001 — reported as undetermined, which fails the audit
            return pin, None, f"lookup failed: {exc}"

    with ThreadPoolExecutor(max_workers=8) as pool:
        found = list(pool.map(look_up, pins))

    rows = []
    for (name, version), evidence, problem in found:
        if evidence is None:
            rows.append(Row(name, version, "unknown", problem, ""))
        else:
            verdict, source = resolve(name, version, evidence)
            rows.append(Row(name, version, verdict, source, describe(evidence)))
    return audit(rows)


if __name__ == "__main__":
    sys.exit(main())
