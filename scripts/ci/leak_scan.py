#!/usr/bin/env python3
"""
Content scan for high-confidence credential formats (private keys, cloud and provider API keys,
TokenLean proxy keys).

Path rules catch a secret FILE; they never see a real key pasted into a tracked one (a template,
a test fixture, a doc). CI runs this over the tracked tree; the release script runs it over the
staged diff of each repo before committing. Findings print as `path:line: kind`, never the
matched text. A line containing `leak-scan: allow` is skipped (for an unavoidable fixture).

Usage:
    python scripts/ci/leak_scan.py --tracked                 # every tracked text file
    python scripts/ci/leak_scan.py --staged [--git-dir DIR]  # lines added in the staged diff

Exit 0 when clean, 1 with findings, 2 when the scan could not run.
"""
import argparse
import re
import subprocess
import sys
from pathlib import Path

PATTERNS = {
    "private-key": re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----"),
    "aws-access-key": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "google-api-key": re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    "google-oauth-secret": re.compile(r"\bGOCSPX-[0-9A-Za-z_-]{28}\b"),
    "github-token": re.compile(r"\b(?:gh[pousr]_[0-9A-Za-z]{36,}|github_pat_[0-9A-Za-z_]{40,})\b"),
    "slack-token": re.compile(r"\bxox[abprs]-[0-9A-Za-z-]{10,}\b"),
    "openai-key": re.compile(r"\bsk-[A-Za-z0-9_-]*T3BlbkFJ[A-Za-z0-9_-]+"),
    "anthropic-key": re.compile(r"\bsk-ant-[a-z]+\d\d-[A-Za-z0-9_-]{80,}"),
    "stripe-live-key": re.compile(r"\b(?:sk|rk)_live_[0-9A-Za-z]{20,}\b"),
    "sendgrid-key": re.compile(r"\bSG\.[A-Za-z0-9_-]{22}\.[A-Za-z0-9_-]{43}\b"),
    "tokenlean-proxy-key": re.compile(r"\btok-[\w.-]+-[0-9a-f]{48}\b"),
}
ALLOW = "leak-scan: allow"
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_ESCAPES = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13, '"': 34, "\\": 92}


def scan_lines(path, numbered_lines):
    return [(path, n, kind) for n, line in numbered_lines if ALLOW not in line
            for kind, rx in PATTERNS.items() if rx.search(line)]


def scan_text(path, text):
    return scan_lines(path, enumerate(text.splitlines(), 1))


def _unquote(name):
    """Undo git's C-style quoting of a path ("a\\"b" -> a"b, octal escapes -> UTF-8 bytes)."""
    if not (name.startswith('"') and name.endswith('"')):
        return name
    out, body, i = bytearray(), name[1:-1], 0
    while i < len(body):
        if body[i] == "\\" and i + 1 < len(body):
            if body[i + 1] in "01234567":
                out.append(int(body[i + 1:i + 4], 8))
                i += 4
                continue
            out.append(_ESCAPES.get(body[i + 1], ord(body[i + 1])))
            i += 2
            continue
        out.extend(body[i].encode("utf-8"))
        i += 1
    return out.decode("utf-8", errors="replace")


def added_lines(diff_text):
    """{path: [(new_line_number, text), ...]} for the `+` lines of a `git diff -U0` patch."""
    found, path, line_no = {}, None, 0
    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            target = _unquote(line[4:])
            path = target[2:] if target.startswith("b/") else None
            continue
        hunk = _HUNK.match(line)
        if hunk:
            line_no = int(hunk.group(1))
        elif path and line.startswith("+"):
            found.setdefault(path, []).append((line_no, line[1:]))
            line_no += 1
    return found


def _git(root, git_dir, *args):
    base = ["git", "-c", "core.quotePath=false"]
    if git_dir:
        base += [f"--git-dir={git_dir}", "--work-tree=."]
    return subprocess.run([*base, *args], cwd=root, capture_output=True, check=True).stdout


def staged_findings(root, git_dir=None):
    diff = _git(root, git_dir, "diff", "--cached", "-U0", "--no-color", "--no-ext-diff",
                "--diff-filter=d", "--src-prefix=a/", "--dst-prefix=b/")
    lines = added_lines(diff.decode("utf-8", errors="replace"))
    return sorted(f for path, numbered in lines.items() for f in scan_lines(path, numbered))


def tracked_findings(root, git_dir=None):
    findings = []
    for rel in _git(root, git_dir, "ls-files", "-z").decode("utf-8").split("\0"):
        try:
            data = (Path(root) / rel).read_bytes() if rel else b""
        except OSError:
            continue
        if data and b"\0" not in data[:8192]:
            findings += scan_text(rel, data.decode("utf-8", errors="replace"))
    return sorted(findings)


def main(argv=None, root=None):
    parser = argparse.ArgumentParser(description="Scan for high-confidence credential formats.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--tracked", action="store_true", help="scan every tracked text file")
    mode.add_argument("--staged", action="store_true", help="scan lines added in the staged diff")
    parser.add_argument("--git-dir", help="use this git dir over the same working tree")
    args = parser.parse_args(argv)
    root = Path(root) if root else Path(__file__).resolve().parents[2]
    try:
        scan = staged_findings if args.staged else tracked_findings
        findings = scan(root, git_dir=args.git_dir)
    except subprocess.CalledProcessError as exc:
        print(f"leak_scan: git failed: {exc.stderr.decode(errors='replace').strip()}",
              file=sys.stderr)
        return 2
    for path, line_no, kind in findings:
        print(f"{path}:{line_no}: {kind}")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
