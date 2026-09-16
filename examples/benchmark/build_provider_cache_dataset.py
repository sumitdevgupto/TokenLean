"""Build the provider prompt-cache workload (backlog #71) — OFFLINE, no network.

WHY THIS EXISTS
---------------
`ab_results.json` reports `a_cache_read` / `a_cache_write` / `b_cache_read` / `b_cache_write`
as **0** across every cache-eligible call, and that is not a reporting bug — it is structural.
Providers need roughly 1,024 tokens of *shared prefix* before they cache anything, and no
existing workload can produce one:

  * on the warm repeats of the `cache` workload NEITHER arm reaches the provider (the direct
    arm is memoised, the proxy arm is a G05 hit), so there is nothing for a provider to cache;
  * the 50 distinct originals share only a ~15-token system prompt.

So the public benchmark could say nothing at all about cache read/write economics, which is
the half of the bill token counts do not show — and the half customers actually ask about.

THE SHAPE
---------
Many **distinct** questions against **one long shared prefix**, with the proxy cache OFF so
every call genuinely reaches the provider. That is the internal DS8 shape, and it is also the
real enterprise pattern: a large stable dossier (or policy block) reused across many different
user turns.

The prefix is a multi-document reference dossier assembled from HotpotQA paragraphs that are
ALREADY CHECKED IN (`public_dataset.jsonl`, `rag` profile) — no download. Each question is one
of those items' own questions, so it is answerable from the dossier and the existing
`expected_facts` grade it unchanged.

Selection is deterministic and stated: the `N` rag items with the SMALLEST contexts, ties
broken by label, so the dossier is as compact as it can be for the question count. Paragraphs
are de-duplicated — a repeated paragraph would be an artificial target for the proxy's own
dedup/pruning and would muddy the very thing being measured.

WHAT THIS DOES NOT CLAIM
------------------------
It does not promise the provider will cache. It creates the only conditions under which it
*could*, and the run reports what actually happened. Zeros here are a real answer — that the
provider declined — and are published as zeros rather than hidden.

Usage
-----
  python build_provider_cache_dataset.py            # rebuild the checked-in artifact
  python build_provider_cache_dataset.py --check    # verify the checked-in file is current
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "public_dataset.jsonl"
OUT = HERE / "provider_cache_dataset.jsonl"

# 12 questions over a ~13k-token dossier. Enough distinct calls that one WRITE and many READs
# are separable, while the whole two-arm run stays well inside the per-provider spend cap.
DEFAULT_COUNT = 12

# The provider minimum this workload exists to clear. OpenAI caches prefixes from ~1,024
# tokens in 128-token increments; Anthropic's explicit-breakpoint minimum is the same order.
# Asserted at build time so a future corpus change cannot silently drop the dossier under it
# and leave the workload measuring nothing while still reporting a tidy zero.
MIN_CACHEABLE_TOKENS = 1024

INSTRUCTION = (
    "You are a research assistant answering questions over a fixed reference dossier.\n"
    "Answer using ONLY the dossier below. Be concise — one short sentence.\n"
    "If the dossier does not contain the answer, say that it does not.\n\n"
    "=== REFERENCE DOSSIER ===\n"
)

CONTEXT_PREFIX = "Context:\n"
QUESTION_SEP = "\n\nQuestion: "


def approx_tokens(text: str) -> int:
    """The same chars/4 estimate the rest of the harness uses. Deliberately not a real
    tokenizer: this is a build-time floor check, and the run reports the provider's own
    counts, which are the only numbers ever published."""
    return len(text) // 4


def split_context(item: dict) -> tuple[str, str]:
    """A rag item's user turn is `Context:\\n<paragraphs>\\n\\nQuestion: <q>`."""
    user = next(m["content"] for m in reversed(item["messages"]) if m["role"] == "user")
    if QUESTION_SEP not in user:
        raise ValueError(f"{item.get('_label')}: no question separator")
    context, question = user.split(QUESTION_SEP, 1)
    if not context.startswith(CONTEXT_PREFIX):
        raise ValueError(f"{item.get('_label')}: unexpected context prefix")
    return context[len(CONTEXT_PREFIX):].strip(), question.strip()


def load_rag(path: Path) -> list:
    out = []
    for ln in path.read_text(encoding="utf-8").splitlines():
        if not ln.strip():
            continue
        r = json.loads(ln)
        if r.get("_profile") == "rag":
            out.append(r)
    return out


def select(items: list, count: int) -> list:
    """Smallest contexts first, ties broken by label — deterministic and stated."""
    scored = []
    for r in items:
        context, question = split_context(r)
        scored.append((len(context), r.get("_label", ""), r, context, question))
    scored.sort(key=lambda t: (t[0], t[1]))
    return [(r, c, q) for _, _, r, c, q in scored[:count]]


def build_dossier(chosen: list) -> tuple[str, int]:
    """One paragraph-deduplicated dossier, in selection order. Returns (text, n_paragraphs)."""
    seen, paragraphs = set(), []
    for _, context, _ in chosen:
        for para in context.split("\n\n"):
            para = para.strip()
            if para and para not in seen:
                seen.add(para)
                paragraphs.append(para)
    return "\n\n".join(paragraphs), len(paragraphs)


def build(count: int = DEFAULT_COUNT) -> str:
    rag = load_rag(SOURCE)
    if len(rag) < count:
        raise SystemExit(f"need {count} rag items, found {len(rag)}")
    chosen = select(rag, count)
    dossier, n_paragraphs = build_dossier(chosen)
    system = INSTRUCTION + dossier

    prefix_tokens = approx_tokens(system)
    if prefix_tokens < MIN_CACHEABLE_TOKENS:
        raise SystemExit(
            f"shared prefix is ~{prefix_tokens} tokens, under the ~{MIN_CACHEABLE_TOKENS} a "
            "provider needs to cache anything — this workload would measure nothing")

    lines = []
    records = []
    for i, (src, _context, question) in enumerate(chosen, 1):
        label = f"pcache-{i:04d}"
        records.append({
            "request_id": label,
            "_label": label,
            "_profile": "provider_cache",
            # Byte-identical across every item: this IS the shared prefix under test.
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": question}],
            "expected_facts": src.get("expected_facts"),
            "grade": "facts",
            "max_tokens": 128,
            "_source": {**(src.get("_source") or {}), "derived_from": src.get("_label")},
        })

    shared_sha = hashlib.sha256(system.encode("utf-8")).hexdigest()
    meta = {
        "_meta": True,
        "workload": "provider_cache",
        "built_by": "build_provider_cache_dataset.py",
        "backlog": "#71",
        "n_items": len(records),
        "shared_prefix_sha256": shared_sha,
        "shared_prefix_chars": len(system),
        "shared_prefix_approx_tokens": prefix_tokens,
        "dossier_paragraphs": n_paragraphs,
        "derived_from": [src.get("_label") for src, _, _ in chosen],
        "provenance": (
            "Questions and dossier paragraphs are HotpotQA (distractor) verbatim, taken from "
            "the already-checked-in public_dataset.jsonl rag items — no download. The "
            "instruction header above the dossier is authored by this repo. Selection: the "
            "N rag items with the smallest contexts, ties broken by label; paragraphs "
            "de-duplicated in selection order."),
        "measures": (
            "Provider prompt-cache READ and WRITE tokens on both arms. NOT a savings lever "
            "and deliberately outside the illustrative blend."),
        "license": "CC BY-SA 4.0 (HotpotQA)",
    }
    lines.append(json.dumps(meta, ensure_ascii=False, sort_keys=True))
    lines.extend(json.dumps(r, ensure_ascii=False, sort_keys=True) for r in records)
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--count", type=int, default=DEFAULT_COUNT,
                    help=f"number of distinct questions (default {DEFAULT_COUNT})")
    ap.add_argument("--check", action="store_true",
                    help="verify the checked-in file matches a fresh build; write nothing")
    args = ap.parse_args()

    text = build(args.count)
    if args.check:
        if not OUT.exists():
            print(f"MISSING: {OUT.name} has not been built", file=sys.stderr)
            return 1
        current = OUT.read_bytes().decode("utf-8")
        if current != text:
            print(f"STALE: {OUT.name} differs from a fresh build", file=sys.stderr)
            return 1
        print(f"OK: {OUT.name} is current")
        return 0

    OUT.write_bytes(text.encode("utf-8"))
    meta = json.loads(text.splitlines()[0])
    print(f"wrote {OUT.name}: {meta['n_items']} questions, shared prefix "
          f"~{meta['shared_prefix_approx_tokens']} tokens "
          f"({meta['dossier_paragraphs']} paragraphs, sha {meta['shared_prefix_sha256'][:12]})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
