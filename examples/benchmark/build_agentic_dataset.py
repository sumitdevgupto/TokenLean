#!/usr/bin/env python3
"""
Build agentic_dataset.jsonl for the A/B benchmark's --workload agentic path.

LIGHTER-HYBRID design (see run_ab.md): each item bundles REAL BFCL v3 multi_turn tool
schemas + the first user turn VERBATIM (Apache-2.0, gorilla-llm/Berkeley-Function-Calling-
Leaderboard), plus a disclosed long agent system prompt and mock tool_results.

BFCL ships no result payloads, so the loop's returned values are ours either way. They are
shaped from each tool's own schema and sized to the band the internal agentic dataset uses
(DS13: median ~243 chars), because a tool result re-enters the prompt on every later turn and
that is where much of an agent's context actually sits. The first version returned a
twelve-token "<name> completed", which left the request-side pruning lever nothing to act on
and reduced the slice to catalogue pruning alone. Sizes are CAPPED at the internal band's
ceiling: larger mocks would raise the measured percentage without representing anything real.

This still does NOT reproduce G14/G15 tool-OUTPUT projection: those are response-side and fire
only on pre-baked embedded `function.result` values a live model never emits, so a live A/B
cannot trigger them (measured internally instead, and tracked as backlog #49).

Quality is graded RELATIVELY by the harness (proxy vs direct arm tool trajectory), so no BFCL
ground-truth answer files are bundled.

Requires network (downloads BFCL raw files). Run once to regenerate the checked-in artifact:
    python examples/benchmark/build_agentic_dataset.py
Regenerate ONLY the mock results, offline, from the checked-in schemas:
    python examples/benchmark/build_agentic_dataset.py --results-only
Runtime of the benchmark itself needs only the checked-in agentic_dataset.jsonl (+ httpx/litellm).
"""
import argparse
import hashlib
import json
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
RAW = ("https://raw.githubusercontent.com/ShishirPatil/gorilla/main/"
       "berkeley-function-call-leaderboard/bfcl_eval/data")
HF = ("https://huggingface.co/datasets/gorilla-llm/"
      "Berkeley-Function-Calling-Leaderboard/resolve/main")
# BFCL CLASS_FILE_PATH_MAPPING: involved-class name -> multi_turn_func_doc file stem.
CLASS_DOC = {
    "GorillaFileSystem": "gorilla_file_system", "MathAPI": "math_api",
    "MessageAPI": "message_api", "TwitterAPI": "posting_api", "TicketAPI": "ticket_api",
    "TradingBot": "trading_bot", "TravelAPI": "travel_booking", "VehicleControlAPI": "vehicle_control",
}

# Disclosed synthetic agent operating manual (>800 tokens -> exercises the G16 system-prompt
# cap). Generic harness scaffolding — NOT BFCL content.
SYSTEM = (
    "You are an autonomous operations agent working inside a multi-domain enterprise "
    "environment. You complete each user request by planning a short sequence of tool "
    "calls, invoking the appropriate tools, inspecting their results, and continuing until "
    "the task is fully resolved. Follow these operating principles carefully.\n\n"
    "1. UNDERSTAND THE REQUEST. Read the user's message and identify the concrete end state "
    "they want. If a request implies several steps (for example, create a directory and then "
    "move a file into it), plan all of the steps before acting.\n"
    "2. SELECT TOOLS DELIBERATELY. You have access to a broad catalogue of tools spanning file "
    "systems, messaging, ticketing, travel booking, trading, vehicle control and mathematics. "
    "Only call a tool when it directly advances the task. Prefer the most specific tool for the "
    "job and pass exactly the arguments its schema requires.\n"
    "3. ONE STEP AT A TIME. Issue tool calls incrementally. After each result, decide whether "
    "the task is complete or whether another call is required. Do not fabricate results; rely "
    "only on what the tools return.\n"
    "4. VERIFY BEFORE FINISHING. Before giving a final answer, confirm that every part of the "
    "request has been satisfied. If a step failed, retry with corrected arguments or an "
    "alternative tool.\n"
    "5. HANDLE MISSING INFORMATION. If a required parameter is genuinely unavailable and cannot "
    "be derived, ask the user a single concise clarifying question rather than guessing.\n"
    "6. RESPECT SIDE EFFECTS. Tools that create, move, delete, post, purchase or transfer are "
    "irreversible in spirit; double-check arguments (names, ids, amounts, destinations) before "
    "invoking them. Never take a destructive action the user did not request.\n"
    "7. BE PRECISE WITH IDENTIFIERS. File names, directory names, ticket ids, symbols, account "
    "numbers and coordinates must match exactly. Do not invent identifiers.\n"
    "8. COMMUNICATE CLEARLY. When you finish, give the user a short, factual summary of what you "
    "did and the final state, referencing concrete values returned by the tools.\n"
    "9. STAY IN SCOPE. Do not perform actions beyond the user's request, and do not expose "
    "internal tool mechanics, credentials, or raw state dumps unless asked.\n"
    "10. EFFICIENCY. Accomplish the task in as few tool calls as correctness allows; avoid "
    "redundant lookups when a prior result already contains the needed information.\n"
    "11. DOMAIN NOTES. The file-system tools operate on a virtual workspace with directories and "
    "files; always confirm the current directory before relative operations, and create parent "
    "directories before moving files into them. The messaging and posting tools act on real "
    "accounts; keep message bodies faithful to the user's intent and never post content the user "
    "did not author or approve. The ticketing tools track support cases by id and status; look up "
    "a ticket before modifying it. The travel-booking tools reserve flights and manage itineraries; "
    "verify dates, airports, passenger names and fares before booking, and confirm availability "
    "first. The trading tools place, amend and cancel orders against live symbols and balances; "
    "check the current position and available funds before submitting an order, and never exceed "
    "the user's stated limits. The vehicle-control tools change physical actuator state such as "
    "doors, climate, lights and cruise settings; read the current state before issuing a change and "
    "avoid unsafe combinations. The math tools are pure and side-effect free; use them for exact "
    "arithmetic rather than estimating.\n"
    "12. ERROR RECOVERY. If a tool returns an error or an unexpected result, do not repeat the same "
    "call blindly; inspect the message, adjust the arguments, and if two attempts fail, report the "
    "obstacle to the user with the specific error rather than continuing to guess.\n"
    "13. STATE AWARENESS. Treat every tool result as the current source of truth for that subsystem; "
    "when a later step depends on an earlier result (an id, a path, a balance, a status), reuse the "
    "returned value exactly rather than re-deriving or assuming it.\n\n"
    "You will now receive the user's request. Plan, act with tools, and resolve it end to end, "
    "then summarise the outcome concisely and factually."
)

# BFCL parameter-type variant -> JSON-Schema type accepted by OpenAI/litellm.
_TYPE = {"dict": "object", "float": "number", "tuple": "array", "any": "string",
         "integer": "integer", "string": "string", "boolean": "boolean", "array": "array",
         "number": "number", "object": "object"}


def _get(url):
    return urllib.request.urlopen(url, timeout=60).read().decode("utf-8")


def _norm_schema(s):
    if not isinstance(s, dict):
        return {"type": "string"}
    out = dict(s)
    if "type" in out:
        out["type"] = _TYPE.get(out["type"], "string")
    if out.get("type") == "object" and isinstance(out.get("properties"), dict):
        out["properties"] = {k: _norm_schema(v) for k, v in out["properties"].items()}
    if out.get("type") == "array" and isinstance(out.get("items"), dict):
        out["items"] = _norm_schema(out["items"])
    return {k: v for k, v in out.items()
            if k in ("type", "description", "properties", "required", "items", "enum")}


def _to_openai_tool(fd):
    params = fd.get("parameters") or {"type": "object", "properties": {}}
    return {"type": "function", "function": {
        "name": fd["name"], "description": (fd.get("description") or "")[:1024],
        "parameters": _norm_schema(params)}}


# --------------------------------------------------------------------------- #
# Mock tool RESULTS
#
# BFCL ships tool SCHEMAS and user turns; it ships no result payloads, so whatever the
# loop feeds back is ours either way. The first version returned
# {"status": "success", "detail": "<name> completed"} - 47-69 characters, about twelve
# tokens. Real agents receive API responses, file listings and query results, and those
# re-enter the prompt on every subsequent turn, which is where a large share of agentic
# context actually lives. At twelve tokens there was nothing for the request-side
# structured-pruning lever to act on, so the agentic slice measured catalogue pruning
# ALONE and no other lever could ever show up in it.
#
# These payloads are therefore sized to the band the internal agentic dataset uses for
# the same job (DS13 tool_results: median ~243 chars, max ~1794) and CAPPED there. The
# cap is the honesty control: bigger mocks would raise the measured percentage without
# representing anything real, which is the one thing this harness must never do.
#
# Shape is derived from each tool's OWN parameter schema, so a payload looks like it
# belongs to its tool, and every value is a pure function of (tool name, index) - no RNG,
# so rebuilds are byte-identical on any machine.
# --------------------------------------------------------------------------- #
_RESULT_MAX_CHARS = 1794          # the internal band's ceiling; never exceed it
# Matched against the FIRST underscore-delimited segment, so multi-word entries would be
# unreachable - the `get_all` family is caught by the substring test below instead.
_LIST_VERBS = ("list", "search", "find", "ls", "browse", "query")
_READ_VERBS = ("get", "read", "cat", "view", "show", "describe", "display", "retrieve",
               "fetch", "info", "detail", "stat", "lookup")
_WORDS = ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel",
          "india", "juliet", "kilo", "lima", "mike", "november", "oscar", "papa")


def _stable(mod: int, *parts) -> int:
    """Deterministic small integer from the parts - hashlib, not PRNG, so it cannot drift
    with a Python version or a platform."""
    h = hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()
    return int(h[:8], 16) % mod


def _scalar(kind: str, key: str, salt: str):
    if kind in ("integer", "number"):
        return _stable(9000, key, salt) + 100
    if kind == "boolean":
        return _stable(2, key, salt) == 1
    if kind == "array":
        return [_WORDS[_stable(len(_WORDS), key, salt, i)] for i in range(3)]
    return f"{key}_{_WORDS[_stable(len(_WORDS), key, salt)]}_{_stable(900, key, salt) + 100}"


def synth_tool_result(tool: dict) -> dict:
    """A plausible, deterministic, size-capped return payload for one tool schema."""
    fn = tool["function"]
    name = fn["name"]
    low = name.lower()
    props = ((fn.get("parameters") or {}).get("properties") or {})
    keys = sorted(props)[:5] or ["id", "name", "status"]

    def record(i: int) -> dict:
        r = {k: _scalar((props.get(k) or {}).get("type", "string"), k, f"{name}:{i}")
             for k in keys}
        r["id"] = f"{low[:14]}-{_stable(9000, name, i) + 1000}"
        return r

    # A response is not the shape of its own arguments. A read returns CONTENT it was
    # never passed, a list returns rows with fields the caller never supplied, and every
    # real API response carries a small envelope. Deriving fields from the input schema
    # alone systematically under-describes what an agent actually gets handed back.
    envelope = {"status": "success",
                "ts": f"2026-09-0{1 + _stable(9, name)}T0{_stable(9, name, 'h')}:"
                      f"{_stable(6, name, 'm')}{_stable(9, name, 'm2')}:00Z",
                "source": low}

    verb = low.split("_", 1)[0]
    if verb in _LIST_VERBS or any(v in low for v in ("list", "search", "find", "_all")):
        n = 2 + _stable(4, name)
        payload = {**envelope, "count": n, "results": [record(i) for i in range(n)]}
    elif verb in _READ_VERBS:
        body = " ".join(_WORDS[_stable(len(_WORDS), name, "body", i)] for i in range(12))
        payload = {**envelope, "result": record(0), "content": body}
    else:
        payload = {**envelope, "action": name, "applied": record(0),
                   "message": f"{name} completed; state updated"}

    # Cap by dropping whole records, never by truncating into invalid JSON.
    while len(json.dumps(payload, sort_keys=True)) > _RESULT_MAX_CHARS:
        rows = payload.get("results")
        if rows and len(rows) > 1:
            rows.pop()
            payload["count"] = len(rows)
        else:
            payload = {"status": "success", "action": name}
            break
    return payload


def _results_for(tools: list) -> dict:
    return {t["function"]["name"]: synth_tool_result(t) for t in tools}


_RESULTS_NOTE = ("tool schemas + first user turn verbatim; system prompt is disclosed "
                 "harness scaffolding; tool_results are OUR mocks - BFCL ships no result "
                 "payloads - shaped from each tool's own schema and size-capped to the "
                 "internal agentic band. Live lever = G08/G16 tool pruning plus request-side "
                 "pruning of the tool results; G14/G15 response-side projection is not "
                 "live-reproducible.")


def rebuild_results_only(path: Path) -> int:
    """OFFLINE path: regenerate tool_results in place from the checked-in schemas.

    Touches `tool_results` and the provenance note ONLY - messages, tools and n_tools are
    rewritten byte-identically, so the catalogue-pruning figure cannot move for an
    unrelated reason. Needs no network.
    """
    items = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    for it in items:
        it["tool_results"] = _results_for(it["tools"])
        if isinstance(it.get("_source"), dict):
            it["_source"]["note"] = _RESULTS_NOTE
    text = "\n".join(json.dumps(it, ensure_ascii=True, sort_keys=True) for it in items) + "\n"
    path.write_bytes(text.encode("utf-8"))
    sizes = sorted(len(json.dumps(v, sort_keys=True))
                   for it in items for v in it["tool_results"].values())
    mid = sizes[len(sizes) // 2] if sizes else 0
    print(f"rebuilt tool_results for {len(items)} items -> {path.name}")
    print(f"  result sizes (chars): min={sizes[0]} median={mid} max={sizes[-1]} "
          f"(cap {_RESULT_MAX_CHARS})")
    print(f"  sha256={hashlib.sha256(text.encode('utf-8')).hexdigest()[:12]}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Build the BFCL-derived agentic A/B dataset.")
    ap.add_argument("--count", type=int, default=15, help="number of agentic items to bundle")
    ap.add_argument("--out", default=str(HERE / "agentic_dataset.jsonl"))
    ap.add_argument("--results-only", action="store_true",
                    help="regenerate tool_results in the checked-in file from its own tool "
                         "schemas and exit. Offline - no network, no re-download.")
    args = ap.parse_args()

    if args.results_only:
        return rebuild_results_only(Path(args.out))

    class_tools = {}
    for cls, stem in CLASS_DOC.items():
        fns = [json.loads(l) for l in _get(f"{RAW}/multi_turn_func_doc/{stem}.json").splitlines() if l.strip()]
        class_tools[cls] = [_to_openai_tool(f) for f in fns]

    entries = [json.loads(l) for l in _get(f"{HF}/BFCL_v3_multi_turn_base.json").splitlines() if l.strip()]

    # Deterministic curation: first entry per distinct involved-class combo (varying tool-
    # catalogue sizes), then top up in file order. Skip any referencing an unmapped class.
    seen, chosen = set(), []
    for e in entries:
        combo = tuple(sorted(e.get("involved_classes", [])))
        if not combo or any(c not in class_tools for c in combo) or combo in seen:
            continue
        seen.add(combo)
        chosen.append(e)
        if len(chosen) >= max(1, args.count - 3):
            break
    for e in entries:
        if len(chosen) >= args.count:
            break
        if e not in chosen and all(c in class_tools for c in e.get("involved_classes", [])):
            chosen.append(e)

    items = []
    for i, e in enumerate(chosen, 1):
        tools = [t for c in e["involved_classes"] for t in class_tools[c]]
        first_user = next((m["content"] for turn in e["question"] for m in turn
                           if m.get("role") == "user"), "")
        items.append({
            "request_id": f"agentic-{i:04d}", "_profile": "agentic", "_label": e["id"],
            "_source": {"corpus": "BFCL v3 multi_turn (base)", "record_id": e["id"],
                        "involved_classes": e["involved_classes"], "license": "Apache-2.0",
                        "origin": "gorilla-llm/Berkeley-Function-Calling-Leaderboard",
                        "note": _RESULTS_NOTE},
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": first_user}],
            "tools": tools,
            "tool_results": _results_for(tools),
            "expected_facts": None, "n_tools": len(tools), "max_tokens": 512,
        })

    text = "\n".join(json.dumps(it, ensure_ascii=True, sort_keys=True) for it in items) + "\n"
    Path(args.out).write_bytes(text.encode("utf-8"))
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    print(f"wrote {len(items)} agentic items -> {Path(args.out).name}  sha256={sha[:12]}")
    print(f"  tool-catalogue sizes: {sorted(it['n_tools'] for it in items)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
