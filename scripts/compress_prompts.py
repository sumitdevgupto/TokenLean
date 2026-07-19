#!/usr/bin/env python3
"""
Offline prompt / memory-file compressor.

Rewrites natural-language prompt artifacts (G02 template bodies, tenant system
prompts, AGENTS.md/CLAUDE.md-style memory files) into a terser on-disk form using
the deterministic regex compressor in src/proxy/middleware/prose_compress.py —
the same engine G01/G08 use in-band. Zero LLM calls; code blocks, inline code,
URLs, paths, identifiers, function calls and version numbers are preserved
byte-for-byte.

NOT in the request path — a manual/scheduled utility, sibling of
scripts/run_prompt_optimization.py.

Approach adapted from caveman's `caveman-compress` skill (github.com/JuliusBrussee/
caveman, MIT — attribution in docs/oss-licenses.md): backup before overwrite,
refuse source-code files, structural validation before commit.

Usage:
    # Preview savings without writing (safe default is to require --write)
    python scripts/compress_prompts.py AGENTS.md docs/*.md

    # Compress in place, keeping FILE.original.md backups
    python scripts/compress_prompts.py --write config/templates/*.md

    # Compress a YAML/JSON prompt field-set (e.g. tool registry descriptions)
    python scripts/compress_prompts.py --write --fields description,instructions reg.json
"""
import argparse
import glob
import json
import logging
import os
import sys
from typing import List, Tuple

# Make src/proxy importable so we reuse the in-band compressor (single source of truth).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO_ROOT, "src", "proxy"))

from middleware.prose_compress import compress, compress_descriptions_in_place  # noqa: E402

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(message)s")
logger = logging.getLogger("compress_prompts")

# Refuse to compress source code — prose_compress protects fenced code, but a whole
# .py/.js file is code, not prose, and must never be run through prose stripping.
_CODE_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rs", ".java", ".c", ".h",
    ".cpp", ".cc", ".rb", ".php", ".sh", ".ps1", ".sql", ".css", ".scss",
}
_STRUCTURED_EXTENSIONS = {".json", ".yaml", ".yml"}


def _expand(paths: List[str]) -> List[str]:
    """Expand globs (cross-platform; the shell may not on Windows) and de-dup."""
    out: List[str] = []
    for p in paths:
        matches = glob.glob(p) or ([p] if os.path.exists(p) else [])
        for m in matches:
            if os.path.isfile(m) and m not in out:
                out.append(m)
    return out


def _validate_structure(before: str, after: str) -> Tuple[bool, str]:
    """Structural safety check: markdown heading count and fenced-code-fence count
    must be preserved (prose_compress protects fences, so a mismatch signals a bug)."""
    if before.count("```") != after.count("```"):
        return False, "fenced-code-block count changed"
    b_head = sum(1 for ln in before.splitlines() if ln.lstrip().startswith("#"))
    a_head = sum(1 for ln in after.splitlines() if ln.lstrip().startswith("#"))
    if b_head != a_head:
        return False, f"heading count changed ({b_head} → {a_head})"
    return True, "ok"


def _compress_text_file(path: str, write: bool, backup: bool, min_chars: int) -> Tuple[int, int]:
    """Compress a prose/markdown file. Returns (before_chars, after_chars)."""
    with open(path, "r", encoding="utf-8") as fh:
        before = fh.read()
    if len(before) < min_chars:
        logger.info("  skip %s (%d chars < min %d)", path, len(before), min_chars)
        return len(before), len(before)

    res = compress(before)
    after = res["compressed"]
    ok, reason = _validate_structure(before, after)
    if not ok:
        logger.warning("  SKIP %s — validation failed: %s (original untouched)", path, reason)
        return len(before), len(before)

    saved = res["before"] - res["after"]
    pct = (saved / res["before"] * 100) if res["before"] else 0.0
    logger.info("  %s: %d → %d chars (−%.1f%%)", path, res["before"], res["after"], pct)

    if write and saved > 0:
        if backup:
            root, ext = os.path.splitext(path)
            bak = f"{root}.original{ext or '.md'}"
            if not os.path.exists(bak):  # never clobber an existing backup
                with open(bak, "w", encoding="utf-8") as bh:
                    bh.write(before)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(after)
    return res["before"], res["after"]


def _compress_structured_file(
    path: str, fields: List[str], write: bool, backup: bool
) -> Tuple[int, int]:
    """Compress named string fields inside a JSON/YAML doc (e.g. tool descriptions)."""
    with open(path, "r", encoding="utf-8") as fh:
        raw = fh.read()
    is_json = path.endswith(".json")
    try:
        if is_json:
            data = json.loads(raw)
        else:
            import yaml
            data = yaml.safe_load(raw)
    except Exception as exc:
        logger.warning("  SKIP %s — parse failed: %s", path, exc)
        return len(raw), len(raw)

    saved = compress_descriptions_in_place(data, fields)
    if is_json:
        out = json.dumps(data, indent=2, ensure_ascii=False)
    else:
        import yaml
        out = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    logger.info("  %s: fields=%s saved %d chars", path, ",".join(fields), saved)

    if write and saved > 0:
        if backup:
            root, ext = os.path.splitext(path)
            bak = f"{root}.original{ext}"
            if not os.path.exists(bak):
                with open(bak, "w", encoding="utf-8") as bh:
                    bh.write(raw)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(out)
    return len(raw), len(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="Offline prompt/memory-file compressor (deterministic, zero-LLM).")
    ap.add_argument("paths", nargs="+", help="Files or globs to compress.")
    ap.add_argument("--write", action="store_true", help="Write changes in place (default: preview only).")
    ap.add_argument("--no-backup", action="store_true", help="Do not write FILE.original.EXT backups.")
    ap.add_argument("--fields", default="description",
                    help="Comma-separated string fields to compress in JSON/YAML files (default: description).")
    ap.add_argument("--min-chars", type=int, default=200,
                    help="Skip prose files smaller than this many chars (default: 200).")
    ap.add_argument("--force-code", action="store_true",
                    help="Allow compressing files with source-code extensions (unsafe; off by default).")
    args = ap.parse_args()

    files = _expand(args.paths)
    if not files:
        logger.error("No matching files.")
        return 2

    fields = [f.strip() for f in args.fields.split(",") if f.strip()]
    total_before = total_after = 0
    logger.info("%s %d file(s):", "Compressing" if args.write else "Previewing", len(files))
    for path in files:
        ext = os.path.splitext(path)[1].lower()
        if ext in _CODE_EXTENSIONS and not args.force_code:
            logger.warning("  REFUSE %s — source-code file (use --force-code to override)", path)
            continue
        if ext in _STRUCTURED_EXTENSIONS:
            b, a = _compress_structured_file(path, fields, args.write, not args.no_backup)
        else:
            b, a = _compress_text_file(path, args.write, not args.no_backup, args.min_chars)
        total_before += b
        total_after += a

    saved = total_before - total_after
    pct = (saved / total_before * 100) if total_before else 0.0
    logger.info("Total: %d → %d chars (−%d, −%.1f%%)%s",
                total_before, total_after, saved, pct,
                "" if args.write else "  [preview — re-run with --write to apply]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
