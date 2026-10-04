#!/usr/bin/env python3
"""
Commercial paths of the open-core split, read from .gitignore.

The commercial sections of the public .gitignore (headers "# ─── Commercial ...") name
every path of the commercial layer. Checks that need that list read it here, so a new
commercial module is covered once its .gitignore line exists, with no copy to update.

Usage:
    python scripts/ci/commercial_paths.py                    # print the patterns
    python scripts/ci/commercial_paths.py --tracked          # tracked files matching one
    python scripts/ci/commercial_paths.py --dockerignore src/proxy [PATH ...]
        # commercial paths under the build context src/proxy (plus any PATH given, relative
        # to the repo root) that src/proxy/.dockerignore lets into the image

--tracked and --dockerignore exit 1 and print what they found, or 0 when there is
nothing; 2 means the check could not run.
"""
import argparse
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_HEADER = re.compile(r"^#\s*─{3,}\s*(?P<title>.*?)\s*─*$")
# Docker escapes these and passes every other character to the regex as it is.
_DOCKER_ESCAPE = set(".+()|{}$")


def commercial_sections(gitignore_text):
    """The path lines of each commercial section, keyed by the section's title."""
    sections, current = {}, None
    for raw in gitignore_text.splitlines():
        line = raw.strip()
        header = _HEADER.match(line)
        if header:
            title = header["title"]
            current = sections.setdefault(title, []) if title.startswith("Commercial") else None
        elif current is not None and line and not line.startswith("#"):
            current.append(line)
    return sections


def commercial_patterns(gitignore_text):
    patterns = [p for paths in commercial_sections(gitignore_text).values() for p in paths]
    if not patterns:
        # An empty list would let every check pass.
        raise ValueError("no commercial paths found in .gitignore")
    return patterns


def _docker_regex(pattern):
    """Docker's translation of a .dockerignore pattern (moby/patternmatcher)."""
    out, i = "^", 0
    while i < len(pattern):
        ch = pattern[i]
        if pattern.startswith("**", i):
            i += 2 if pattern.startswith("**/", i) else 1
            out += ".*" if i + 1 >= len(pattern) else "(.*/)?"
        elif ch == "*":
            out += "[^/]*"
        elif ch == "?":
            out += "[^/]"
        elif ch == "\\" and i + 1 < len(pattern):
            i += 1
            out += re.escape(pattern[i])
        else:
            out += "\\" + ch if ch in _DOCKER_ESCAPE else ch
        i += 1
    return re.compile(out + "$")


def parse_dockerignore(text):
    """(is_exception, regex) per pattern line, in file order."""
    rules = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        exception = line.startswith("!")
        pattern = posixpath.normpath(line[1:].strip() if exception else line).lstrip("/")
        rules.append((exception, _docker_regex(pattern)))
    return rules


def dockerignore_excludes(rules, path):
    """Whether Docker leaves `path` (relative to the build context) out of the context:
    the last pattern matching it or one of its parent directories decides."""
    path = posixpath.normpath(path).lstrip("/")
    parts = path.split("/")
    candidates = [path] + ["/".join(parts[:i]) for i in range(1, len(parts))]
    excluded = False
    for exception, regex in rules:
        if any(regex.match(c) for c in candidates):
            excluded = not exception
    return excluded


def not_dockerignored(root, context, patterns, paths=()):
    """The commercial patterns and `paths` under `context` that its .dockerignore
    does not leave out of the image."""
    rules = parse_dockerignore((Path(root) / context / ".dockerignore").read_text(encoding="utf-8"))
    prefix = posixpath.normpath(context) + "/"
    leaks = [entry for entry in [*patterns, *paths]
             if entry.lstrip("/").startswith(prefix)
             and not dockerignore_excludes(rules, entry.lstrip("/")[len(prefix):])]
    return list(dict.fromkeys(leaks))


def tracked_matching(root, patterns):
    """Files in the git index at `root` that a pattern matches, with .gitignore semantics."""
    git = shutil.which("git")             # the caller's own PATH, as a CI or operator script
    if git is None:
        raise FileNotFoundError("git is not on PATH: the tracked paths are read from its index")
    with tempfile.TemporaryDirectory() as tmp:
        exclude = Path(tmp) / "commercial.gitignore"
        exclude.write_text("\n".join(patterns) + "\n", encoding="utf-8")
        out = subprocess.run(
            [git, "ls-files", "-z", "--cached", "--ignored", f"--exclude-from={exclude}"],
            cwd=root, capture_output=True, check=True).stdout
    return sorted(p for p in out.decode("utf-8").split("\0") if p)


def main(argv=None, root=None):
    parser = argparse.ArgumentParser(description="Commercial paths of the open-core split.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--tracked", action="store_true",
                      help="list tracked files that match a commercial path")
    mode.add_argument("--dockerignore", metavar="DIR",
                      help="list commercial paths under the build context DIR that "
                           "DIR/.dockerignore does not leave out of the image")
    parser.add_argument("paths", nargs="*",
                        help="with --dockerignore: more paths to check, relative to the repo root")
    args = parser.parse_args(argv)
    if args.paths and not args.dockerignore:
        parser.error("PATH arguments need --dockerignore")
    root = Path(root) if root else Path(__file__).resolve().parents[2]
    try:
        patterns = commercial_patterns((root / ".gitignore").read_text(encoding="utf-8"))
        if args.tracked:
            found = tracked_matching(root, patterns)
        elif args.dockerignore:
            found = not_dockerignored(root, args.dockerignore, patterns, args.paths)
        else:
            print("\n".join(patterns))
            return 0
    except subprocess.CalledProcessError as exc:
        print(f"commercial_paths: git failed: {exc.stderr.decode(errors='replace').strip()}",
              file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(f"commercial_paths: {exc}", file=sys.stderr)
        return 2
    for path in found:
        print(path)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
