#!/usr/bin/env python3
"""
Build agentic_dataset.jsonl for the A/B benchmark's --workload agentic path.

LIGHTER-HYBRID design (see run_ab.md): each item bundles REAL BFCL v3 multi_turn tool
schemas + the first user turn VERBATIM (Apache-2.0, gorilla-llm/Berkeley-Function-Calling-
Leaderboard), plus a disclosed long agent system prompt and simple mock tool_results (loop
continuation only). This reproduces the LIVE agentic lever = G08/G16 tool-catalogue pruning +
system-prompt cap. It deliberately does NOT try to reproduce G14/G15 tool-OUTPUT projection:
those are response-side and only fire on pre-baked embedded `function.result` values that a
real live model never emits, so a live A/B cannot trigger them (measured internally instead).

Quality is graded RELATIVELY by the harness (proxy vs direct arm tool trajectory), so no BFCL
ground-truth answer files are bundled.

Requires network (downloads BFCL raw files). Run once to regenerate the checked-in artifact:
    python examples/benchmark/build_agentic_dataset.py
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


def main() -> int:
    ap = argparse.ArgumentParser(description="Build the BFCL-derived agentic A/B dataset.")
    ap.add_argument("--count", type=int, default=15, help="number of agentic items to bundle")
    ap.add_argument("--out", default=str(HERE / "agentic_dataset.jsonl"))
    args = ap.parse_args()

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
        names = [t["function"]["name"] for t in tools]
        items.append({
            "request_id": f"agentic-{i:04d}", "_profile": "agentic", "_label": e["id"],
            "_source": {"corpus": "BFCL v3 multi_turn (base)", "record_id": e["id"],
                        "involved_classes": e["involved_classes"], "license": "Apache-2.0",
                        "origin": "gorilla-llm/Berkeley-Function-Calling-Leaderboard",
                        "note": "tool schemas + first user turn verbatim; system prompt is "
                                "disclosed harness scaffolding; tool_results are mocks (loop "
                                "continuation). Live lever = G08/G16 tool pruning; G14/G15 not "
                                "live-reproducible."},
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": first_user}],
            "tools": tools,
            "tool_results": {n: {"status": "success", "detail": f"{n} completed"} for n in names},
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
