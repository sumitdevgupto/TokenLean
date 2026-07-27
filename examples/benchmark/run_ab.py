#!/usr/bin/env python3
"""
True A/B benchmark: proxy vs direct-to-provider, on PROVIDER-BILLED usage.

For every request in the bundled recognized-standard dataset we fire two calls:

  arm A (direct)  litellm.completion(model="<provider>/<model>") straight to the
                  provider — the same library the proxy uses internally.
  arm B (proxy)   the same request through the TokenLean proxy.

We compare the PROVIDER'S OWN billed usage on both arms (arm A: `usage`; arm B:
`_token_opt.tokens_provider_billed` — the real provider prompt tokens the proxy
saw), priced from a checked-in dated `prices.json` applied identically to both.
Nothing is self-reported: a proxy cache hit shows as 0 provider tokens on arm B
(that's the product working), and quality is gated on both arms.

Two numbers from ONE pass (no cache cross-contamination):
  cold    = the first-occurrence 'original' entries (genuine cache misses) —
            savings from the stateless optimisations only. The indisputable floor.
  replay  = the whole run, incl. disclosed repeats/paraphrases — the cache/dedup
            ceiling that models production traffic.

Providers: all 10 first-class adapters, auto-detected by configured credentials.
Default `--providers openai` (the one key most people have) stays < $1; the
maintainer runs `--providers all` for the published scorecard.

See examples/benchmark/README.md for methodology, disclosure, and budget math.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from run_benchmark import check_facts  # noqa: E402 — reuse the deterministic facts gate

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None

DATASET = HERE / "public_dataset.jsonl"
SCHEDULE = HERE / "replay_schedule.json"
CACHE_SCHEDULE = HERE / "cache_schedule.json"
AGENTIC_DATASET = HERE / "agentic_dataset.jsonl"
PRICES = HERE / "prices.json"
RESULTS = HERE / "ab_results.json"
COST_LOG = HERE / "ab_cost_log.jsonl"

# provider -> {model requested (proxy-facing), litellm model string (direct arm),
#              native litellm env var for the key, list of route targets}.
PROVIDER_MODELS = {
    "openai":   {"model": "gpt-4o-mini",                 "litellm": "gpt-4o-mini",
                 "key_env": "OPENAI_API_KEY",   "routes": ["gpt-4o", "o4-mini"]},
    "anthropic":{"model": "claude-haiku-4-5",            "litellm": "anthropic/claude-haiku-4-5",
                 "key_env": "ANTHROPIC_API_KEY","routes": ["claude-sonnet-5"]},
    "gemini":   {"model": "gemini-2.5-flash-lite",       "litellm": "gemini/gemini-2.5-flash-lite",
                 "key_env": "GEMINI_API_KEY",   "routes": ["gemini-2.5-pro"]},
    "azure":    {"model": "azure/gpt-4o-mini",           "litellm": "azure/gpt-4o-mini",
                 "key_env": "AZURE_API_KEY",    "routes": ["azure/gpt-4o"], "needs": ["AZURE_API_BASE"]},
    "bedrock":  {"model": "bedrock/amazon.nova-lite-v1:0","litellm": "bedrock/amazon.nova-lite-v1:0",
                 "key_env": "AWS_ACCESS_KEY_ID","routes": ["bedrock/amazon.nova-pro-v1:0"],
                 "needs": ["AWS_SECRET_ACCESS_KEY", "AWS_REGION_NAME"]},
    "mistral":  {"model": "mistral-small-latest",        "litellm": "mistral/mistral-small-latest",
                 "key_env": "MISTRAL_API_KEY",  "routes": ["mistral-large-latest"]},
    "groq":     {"model": "groq/llama-3.1-8b-instant",   "litellm": "groq/llama-3.1-8b-instant",
                 "key_env": "GROQ_API_KEY",     "routes": ["groq/llama-3.3-70b-versatile"]},
    "deepseek": {"model": "deepseek/deepseek-chat",      "litellm": "deepseek/deepseek-chat",
                 "key_env": "DEEPSEEK_API_KEY", "routes": []},
    "xai":      {"model": "xai/grok-4.3",                "litellm": "xai/grok-4.3",
                 "key_env": "XAI_API_KEY",      "routes": []},
    "cohere":   {"model": "command-r-08-2024",           "litellm": "cohere/command-r-08-2024",
                 "key_env": "COHERE_API_KEY",   "routes": ["command-r-plus-08-2024"]},
}


# --------------------------------------------------------------------------- #
# Pricing
# --------------------------------------------------------------------------- #
def load_prices(path=PRICES):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _bare(model: str) -> str:
    """Strip a leading 'provider/' so 'azure/gpt-4o-mini' -> 'gpt-4o-mini'."""
    return model.split("/", 1)[1] if "/" in model else model


def price(model: str, in_tok: int, out_tok: int, prices: dict) -> float:
    """USD for a call. Unknown model = hard error (never a silent $0)."""
    row = prices.get("models", {}).get(_bare(model))
    if row is None:
        raise KeyError(f"no price for model {model!r} (bare {_bare(model)!r}) in prices.json — "
                       "add it before running")
    return (in_tok * row["input_per_1m"] + out_tok * row["output_per_1m"]) / 1_000_000.0


# --------------------------------------------------------------------------- #
# Spend cap — per provider, plus an overall ceiling
# --------------------------------------------------------------------------- #
class SpendMeter:
    def __init__(self, per_provider_cap: float, overall_cap: float):
        self.per_provider_cap = per_provider_cap
        self.overall_cap = overall_cap
        self.by_provider: dict = defaultdict(float)

    def add(self, provider: str, usd: float) -> float:
        self.by_provider[provider] += usd
        return self.by_provider[provider]

    def provider_tripped(self, provider: str) -> bool:
        return self.by_provider[provider] >= self.per_provider_cap

    def overall_tripped(self) -> bool:
        return sum(self.by_provider.values()) >= self.overall_cap

    def total(self) -> float:
        return sum(self.by_provider.values())


# --------------------------------------------------------------------------- #
# Provider credential detection
# --------------------------------------------------------------------------- #
def _has_key(provider: str, spec: dict, env: dict) -> bool:
    native = env.get(spec["key_env"])
    fallback = env.get(f"LLM_KEY_{provider.upper()}")
    if not (native or fallback):
        return False
    for extra in spec.get("needs", []):
        if not env.get(extra):
            return False
    return True


def detect_providers(env: dict) -> dict:
    """{provider: {'configured': bool, 'reason': str}} for all 10 first-class providers."""
    out = {}
    for provider, spec in PROVIDER_MODELS.items():
        ok = _has_key(provider, spec, env)
        if ok:
            reason = "configured"
        elif not (env.get(spec["key_env"]) or env.get(f"LLM_KEY_{provider.upper()}")):
            reason = f"no key ({spec['key_env']} or LLM_KEY_{provider.upper()})"
        else:
            missing = [x for x in spec.get("needs", []) if not env.get(x)]
            reason = f"missing config: {', '.join(missing)}"
        out[provider] = {"configured": ok, "reason": reason}
    return out


def apply_litellm_env(provider: str, env: dict) -> None:
    """Let litellm find the key: if only LLM_KEY_<P> is set, mirror it to the
    native var litellm expects (e.g. LLM_KEY_ANTHROPIC -> ANTHROPIC_API_KEY)."""
    spec = PROVIDER_MODELS[provider]
    native, fb = spec["key_env"], f"LLM_KEY_{provider.upper()}"
    if not env.get(native) and env.get(fb):
        os.environ[native] = env[fb]


# --------------------------------------------------------------------------- #
# Trace loading — cold subset vs full replay, paraphrase expansion
# --------------------------------------------------------------------------- #
def load_items(path=DATASET) -> dict:
    items = {}
    for ln in Path(path).read_text(encoding="utf-8").splitlines():
        if ln.strip():
            r = json.loads(ln)
            items[r["request_id"]] = r
    return items


def _apply_paraphrase(item: dict, prefix: str) -> dict:
    """Prepend a prefix to the last user message — a near-duplicate (G05 L2 / G22),
    still the same question so the facts/judge gate is unchanged."""
    clone = json.loads(json.dumps(item))
    for m in reversed(clone["messages"]):
        if m.get("role") == "user":
            m["content"] = prefix + m["content"]
            break
    return clone


def load_trace(mode: str, items: dict, schedule: dict) -> list:
    """Return [(entry_kind, item), ...]. cold = originals only; replay/both = full
    schedule (originals first, then repeats/paraphrases — invariant from the builder)."""
    trace = []
    for e in schedule["entries"]:
        item = items.get(e["ref"])
        if item is None:
            continue
        kind = e["kind"]
        if mode == "cold" and kind != "original":
            continue
        if kind == "paraphrase":
            item = _apply_paraphrase(item, e.get("prefix", ""))
        trace.append((kind, item))
    return trace


# --------------------------------------------------------------------------- #
# Quality gate — relative facts (arm B must not drop a fact arm A had)
# --------------------------------------------------------------------------- #
def relative_tool_gate(a_calls: list, b_calls: list) -> dict:
    """Agentic trajectory gate — RELATIVE: the proxy arm (with G08/G16 tool pruning) must not
    DROP a tool the direct arm actually called. Compares the two arms' tool-NAME sets (not a
    ground-truth answer), so a legitimate pruning that keeps behaviour identical passes, while
    pruning that removes a tool the task needed (the direct arm used it, the proxy couldn't)
    is flagged. Graded only when the direct arm called at least one tool."""
    a_names = {t["function"]["name"] for t in (a_calls or [])}
    b_names = {t["function"]["name"] for t in (b_calls or [])}
    dropped = sorted(a_names - b_names)
    return {"graded": bool(a_names), "passed": not dropped, "dropped_tools": dropped}


def relative_facts_gate(item: dict, ans_a: str, ans_b: str) -> dict:
    """PASS unless arm B (proxy) misses a required fact that arm A (direct) had.
    Mirrors the internal relative gate: we never penalise the proxy for a fact the
    direct model itself failed to produce."""
    facts = item.get("expected_facts")
    if not facts:
        return {"graded": False, "passed": True}
    a = check_facts(ans_a, facts)
    b = check_facts(ans_b, facts)
    # facts present in A (not missing from A) but missing from B == a real regression.
    missing_a = {json.dumps(x) for x in a["missing"]}
    missing_b = {json.dumps(x) for x in b["missing"]}
    regressed = missing_b - missing_a
    return {"graded": True, "passed": not regressed,
            "regressed": [json.loads(x) for x in regressed]}


# --------------------------------------------------------------------------- #
# Optional graders: HumanEval exec (pass@1) + LLM judge
# --------------------------------------------------------------------------- #
def exec_humaneval(item: dict, answer: str, timeout: float = 8.0) -> bool | None:
    """Run the HumanEval canonical test against the model's completion in a
    restricted subprocess. Returns True/False, or None if not a code item.
    SECURITY: this executes model-generated code — opt-in via --exec-humaneval only."""
    src = item.get("_source") or {}
    test, entry = src.get("test"), src.get("entry_point")
    if not (test and entry):
        return None
    code = answer
    if "```" in code:  # strip a markdown fence if the model added one
        parts = code.split("```")
        code = max(parts, key=len)
        code = code[len("python"):] if code.lstrip().startswith("python") else code
    program = (item["messages"][-1]["content"] if False else "") + code + "\n" + test + \
        f"\ncheck({entry})\nprint('HUMANEVAL_OK')\n"
    import subprocess
    try:
        r = subprocess.run([sys.executable, "-c", program], capture_output=True,
                           text=True, timeout=timeout)
        return "HUMANEVAL_OK" in r.stdout
    except Exception:  # noqa: BLE001 — a crash/timeout is a fail, not an error
        return False


def _judge_one(question: str, answer: str, model: str, api_key: str, base: str) -> int | None:
    sys_prompt = ("You are a strict QA judge. Given a QUESTION and an ANSWER, rate whether "
                  "the answer is correct, specific and helpful. Respond ONLY compact JSON: "
                  '{"score": <int 1-5>}. 5 = fully correct+specific; 1 = wrong/useless.')
    try:
        resp = httpx.post(f"{base}/chat/completions",
                          headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                          json={"model": model, "temperature": 0,
                                "response_format": {"type": "json_object"},
                                "messages": [{"role": "system", "content": sys_prompt},
                                             {"role": "user", "content": f"QUESTION:\n{question}\n\nANSWER:\n{answer}"}]},
                          timeout=60.0)
        resp.raise_for_status()
        content = ((resp.json().get("choices") or [{}])[0].get("message") or {}).get("content", "")
        return int(round(float(json.loads(content).get("score"))))
    except Exception:  # noqa: BLE001
        return None


def run_judge(records: list, model: str) -> dict:
    """Score BOTH arms with an LLM judge (direct OpenAI, never the proxy). Returns
    means + a warn flag if the proxy arm drops >0.5 below the direct arm."""
    api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("LLM_KEY_OPENAI", "")
    if not api_key:
        return {"ran": False, "reason": "no OPENAI_API_KEY / LLM_KEY_OPENAI for the judge"}
    base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    a_scores, b_scores = [], []
    for r in records:
        q = next((m["content"] for m in reversed(r.get("_messages", [])) if m.get("role") == "user"), "")
        sa = _judge_one(q, r["a"]["content"], model, api_key, base)
        sb = _judge_one(q, r["b"]["content"], model, api_key, base)
        if sa is not None:
            a_scores.append(sa)
        if sb is not None:
            b_scores.append(sb)
    if not a_scores:
        return {"ran": False, "reason": "judge returned no scores"}
    ma, mb = sum(a_scores) / len(a_scores), sum(b_scores) / len(b_scores)
    return {"ran": True, "direct_mean": round(ma, 2), "proxy_mean": round(mb, 2),
            "n": len(a_scores), "warn": mb < ma - 0.5}


# --------------------------------------------------------------------------- #
# Provider calls
# --------------------------------------------------------------------------- #
def _norm_tool_calls(raw) -> list:
    """Normalise provider/proxy tool_calls (dicts OR SDK objects) to a canonical
    [{'id', 'function': {'name', 'arguments'}}] form the episode loop can round-trip."""
    out = []
    for i, tc in enumerate(raw or []):
        if isinstance(tc, dict):
            tid = tc.get("id") or f"call_{i}"
            fn = tc.get("function") or {}
            name = fn.get("name") if isinstance(fn, dict) else getattr(fn, "name", None)
            args = fn.get("arguments") if isinstance(fn, dict) else getattr(fn, "arguments", None)
        else:  # SDK object
            tid = getattr(tc, "id", None) or f"call_{i}"
            fn = getattr(tc, "function", None)
            name = getattr(fn, "name", None)
            args = getattr(fn, "arguments", None)
        out.append({"id": tid, "function": {"name": name or "", "arguments": args or "{}"}})
    return out


def _assistant_msg(content: str, tcs: list) -> dict:
    """The assistant message to append to the conversation before tool results."""
    m = {"role": "assistant", "content": content or None}
    if tcs:
        m["tool_calls"] = [{"id": t["id"], "type": "function",
                            "function": {"name": t["function"]["name"],
                                         "arguments": t["function"]["arguments"]}} for t in tcs]
    return m


def call_direct(litellm_model: str, messages: list, max_tokens: int, tools: list = None) -> dict:
    import litellm
    kw = {"model": litellm_model, "messages": messages, "temperature": 0, "max_tokens": max_tokens}
    if tools:
        kw["tools"] = tools
    resp = litellm.completion(**kw)
    usage = resp.get("usage") if isinstance(resp, dict) else resp.usage
    pt = int(usage["prompt_tokens"] if isinstance(usage, dict) else usage.prompt_tokens)
    ct = int(usage["completion_tokens"] if isinstance(usage, dict) else usage.completion_tokens)
    choice = (resp["choices"] if isinstance(resp, dict) else resp.choices)[0]
    msg = choice["message"] if isinstance(choice, dict) else choice.message
    content = (msg["content"] if isinstance(msg, dict) else msg.content) or ""
    raw_tcs = (msg.get("tool_calls") if isinstance(msg, dict) else getattr(msg, "tool_calls", None))
    tcs = _norm_tool_calls(raw_tcs)
    return {"content": content, "prompt_tokens": pt, "completion_tokens": ct,
            "tool_calls": tcs, "assistant_msg": _assistant_msg(content, tcs)}


def call_proxy(base_url: str, api_key: str, model: str, messages: list, max_tokens: int,
               x_controls: dict, tenant: str | None, timeout: float, tools: list = None) -> dict:
    url = base_url.rstrip("/") + "/v1/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    if tenant:  # only the local/admin benchmark path sets this; tenant self-verify omits it
        headers["X-Tenant-ID"] = tenant
    body = {"model": model, "messages": messages, "temperature": 0, "max_tokens": max_tokens}
    if tools:
        body["tools"] = tools
    body.update(x_controls)
    # G00 rate-limiting is a throughput guard, not a savings lever. run.sh's pin lifts it
    # for the burst, but an un-pinned proxy (e.g. run.ps1 --workload cache, no pin step)
    # will 429 the cache burst. Retry 429s with backoff so a throttled request is served,
    # not dropped as an ERROR that corrupts the measurement.
    for attempt in range(6):
        resp = httpx.post(url, headers=headers, json=body, timeout=timeout)
        if resp.status_code != 429:
            break
        time.sleep(min(2.0 * (attempt + 1), 10.0))
    resp.raise_for_status()
    data = resp.json()
    opt = data.get("_token_opt") or {}
    z = opt.get("tokens_provider_billed")
    cache_hit = bool(opt.get("cache_hit"))
    prompt_tokens = int(z) if (z is not None and not cache_hit) else 0
    completion_tokens = 0 if cache_hit else int(opt.get("response_tokens") or 0)
    routed = opt.get("routed_model") or model
    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    content = msg.get("content") or ""
    tcs = _norm_tool_calls(msg.get("tool_calls") or [])
    return {"content": content, "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens, "routed_model": routed,
            "cache_hit": cache_hit, "tool_calls": tcs,
            "assistant_msg": _assistant_msg(content, tcs)}


def run_episode(arm_fn, messages: list, tools: list, tool_results: dict,
                max_turns: int = 6) -> dict:
    """Run a multi-turn tool loop on ONE arm and return per-EPISODE totals.

    ``arm_fn(convo, tools) -> result`` is a call_direct/call_proxy closure. On each turn:
    call the arm; if it returned tool_calls, execute each locally against ``tool_results``
    (deterministic mocks keyed by tool name), append the assistant tool_calls message + one
    ``role:"tool"`` message per call, and loop — until no tool_calls or ``max_turns``.
    Provider-billed tokens are summed across every turn (that is the honest agentic A/B unit:
    the whole episode, not one call). Returns content (final), summed prompt/completion tokens,
    every tool_call made (for the trajectory gate), cache_hit (any turn), routed_model, turns."""
    convo = [dict(m) for m in messages]
    tot_pt = tot_ct = 0
    all_calls, any_cache = [], False
    routed, final = None, ""
    turns = 0
    for turns in range(1, max_turns + 1):
        r = arm_fn(convo, tools)
        tot_pt += r["prompt_tokens"]
        tot_ct += r["completion_tokens"]
        any_cache = any_cache or bool(r.get("cache_hit"))
        routed = r.get("routed_model") or routed
        if r.get("content"):
            final = r["content"]
        tcs = r.get("tool_calls") or []
        if not tcs:
            break
        all_calls.extend(tcs)
        convo.append(r["assistant_msg"])
        for tc in tcs:
            name = tc["function"]["name"]
            result = tool_results.get(name, {"error": f"no mock result for {name!r}"})
            convo.append({"role": "tool", "tool_call_id": tc["id"],
                          "content": result if isinstance(result, str) else json.dumps(result)})
    return {"content": final, "prompt_tokens": tot_pt, "completion_tokens": tot_ct,
            "tool_calls": all_calls, "cache_hit": any_cache, "routed_model": routed or "",
            "turns": turns}


# --------------------------------------------------------------------------- #
# Aggregation — per provider -> per mode (cold/replay) -> per profile + total
# --------------------------------------------------------------------------- #
def _blank():
    return {"a_prompt": 0, "a_completion": 0, "a_cost": 0.0, "a_calls": 0,
            "b_prompt": 0, "b_completion": 0, "b_cost": 0.0, "b_calls": 0,
            "cache_hits": 0, "facts_checked": 0, "facts_regressed": 0}


def _accumulate(bucket, rec):
    bucket["a_prompt"] += rec["a"]["prompt_tokens"]
    bucket["a_completion"] += rec["a"]["completion_tokens"]
    bucket["a_cost"] += rec["a"]["cost"]
    bucket["a_calls"] += 1
    bucket["b_prompt"] += rec["b"]["prompt_tokens"]
    bucket["b_completion"] += rec["b"]["completion_tokens"]
    bucket["b_cost"] += rec["b"]["cost"]
    bucket["b_calls"] += 1
    if rec["b"]["cache_hit"]:
        bucket["cache_hits"] += 1
    if rec["facts"]["graded"]:
        bucket["facts_checked"] += 1
        if not rec["facts"]["passed"]:
            bucket["facts_regressed"] += 1


def _finalize(bucket):
    ap, bp = bucket["a_prompt"], bucket["b_prompt"]
    ac, bc = bucket["a_cost"], bucket["b_cost"]
    bucket["token_saving_pct"] = round(100.0 * (ap - bp) / ap, 2) if ap else 0.0
    bucket["cost_saving_pct"] = round(100.0 * (ac - bc) / ac, 2) if ac else 0.0
    bucket["a_cost"] = round(ac, 6)
    bucket["b_cost"] = round(bc, 6)
    return bucket


# Stable display/registry order for slices across workloads (standard + cache + future agentic).
SLICE_ORDER = ["cold", "replay", "cache", "agentic"]


def aggregate(records: list, mode: str = None) -> dict:
    """records: per-call pair dicts with keys provider, slice, kind, profile, a, b, facts.
    Returns {provider: {slice: {'by_profile': {...}, 'total': {...}}}} for whichever slices
    are actually PRESENT in the records (cold = G05-bypassed floor, replay = cache ceiling,
    cache = warm-cache burst). Slice-driven so any workload's records aggregate uniformly;
    `mode` is accepted for backwards-compatibility but ignored."""
    present = {r.get("slice") for r in records}
    wanted = [s for s in SLICE_ORDER if s in present]

    out: dict = defaultdict(dict)
    for slice_name in wanted:
        per_prov_prof: dict = defaultdict(lambda: defaultdict(_blank))
        per_prov_total: dict = defaultdict(_blank)
        for r in records:
            if r.get("slice") != slice_name:
                continue
            _accumulate(per_prov_prof[r["provider"]][r["profile"]], r)
            _accumulate(per_prov_total[r["provider"]], r)
        for prov in per_prov_total:
            out[prov][slice_name] = {
                "by_profile": {p: _finalize(b) for p, b in per_prov_prof[prov].items()},
                "total": _finalize(per_prov_total[prov]),
            }
    return dict(out)


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def render(agg: dict, meta: dict) -> None:
    line = "=" * 68
    print("\n" + line)
    print("  TOKEN OPTIMISATION - A/B BENCHMARK  (proxy vs direct, provider-billed)")
    print(line)
    print(f"  Prices as-of {meta['prices_as_of']}   seed {meta['seed']}   "
          f"dataset {meta['dataset_sha256'][:12]}")
    print(f"  Providers run: {', '.join(meta['providers_run']) or '(none)'}")
    if meta["providers_skipped"]:
        print(f"  Skipped: " + "; ".join(f"{p} ({why})" for p, why in meta["providers_skipped"].items()))
    print(f"  Total spend this run: ${meta['spend_total_usd']:.4f}"
          + ("  [CAP HIT]" if meta.get("stopped_at_cap") else ""))
    _tags = {"cold": "cold floor  ", "replay": "replay ceil.", "cache": "cache burst "}
    for prov, slices in agg.items():
        print("\n  " + "-" * 64)
        print(f"  PROVIDER: {prov}")
        for slice_name in SLICE_ORDER:
            if slice_name not in slices:
                continue
            t = slices[slice_name]["total"]
            tag = _tags.get(slice_name, f"{slice_name:<12}")
            print(f"    {tag}  tokens saved {t['token_saving_pct']:>6.1f}%   "
                  f"cost saved {t['cost_saving_pct']:>6.1f}%   "
                  f"cache {t['cache_hits']}/{t['b_calls']}   "
                  f"facts {t['facts_checked']-t['facts_regressed']}/{t['facts_checked']} ok")
            # Per-profile transparency — WHERE the savings come from (already computed).
            for prof in sorted(slices[slice_name]["by_profile"]):
                b = slices[slice_name]["by_profile"][prof]
                print(f"        {prof:<8} tokens {b['token_saving_pct']:>6.1f}%   "
                      f"cache {b['cache_hits']}/{b['b_calls']}")
    print("\n" + line)
    print("  cold  = first-occurrence items, G05 caching BYPASSED -> pure stateless-optimisation floor")
    print("  replay= full schedule, caching ON (disclosed repeats/paraphrases) -> cache/dedup ceiling")
    print("  cache = disclosed warm-cache burst (verbatim repeats) -> reproduces the cache lever")
    print("  Both arms priced from prices.json; arm B tokens = provider-billed (z).")
    print(line)


# --------------------------------------------------------------------------- #
# Illustrative production-mix blend (--workload full)
# --------------------------------------------------------------------------- #
# The blend is a DISCLOSED weighted average of the four independently-reproducible
# per-workload numbers. There is no authoritative public breakdown of enterprise LLM
# traffic at the token level, so these default weights are ILLUSTRATIVE — informed by
# directional public figures, NOT calibrated to our own data — and fully overridable via
# --weights. The per-workload numbers are the primary artifact; this is transparent
# arithmetic a reader can recompute or re-weight for their own traffic.
DEFAULT_WEIGHTS = {"cache": 0.30, "prose": 0.35, "agentic": 0.20, "reasoning": 0.15}
WEIGHT_CITATIONS = [
    "cache-eligible ~30%: ~31% of LLM queries are semantically similar to a prior request; "
    "production cache-hit rates 30-70% (FAQ/agent 40-65%, creative/multi-turn ~0).",
    "agentic ~20%: Gartner - 40% of enterprise apps embed task agents by end-2026 (from <5% in 2025); "
    "token-share still a minority of traffic.",
    "prose ~35% / reasoning ~15%: chat/support/knowledge dominate use-case penetration (code ~70%, "
    "support ~58%, knowledge ~55%); reasoning-heavy math/logic is a small slice.",
    "NO authoritative token-level traffic split exists -> weights are ILLUSTRATIVE + tunable (--weights).",
]


def parse_weights(spec: str) -> dict:
    """Parse 'cache=0.3,prose=0.35,agentic=0.2,reasoning=0.15' into a weight dict.
    Weights must be non-negative and not all zero (they are renormalised, so they need
    not sum to 1); a malformed spec raises ValueError rather than silently skewing the blend."""
    if not spec:
        return dict(DEFAULT_WEIGHTS)
    w = dict(DEFAULT_WEIGHTS)
    for part in spec.split(","):
        k, _, v = part.partition("=")
        k = k.strip()
        if k not in w:
            raise ValueError(f"unknown blend lever {k!r}; valid: {', '.join(w)}")
        val = float(v)
        if val < 0:
            raise ValueError(f"blend weight for {k!r} must be non-negative, got {val}")
        w[k] = val
    if sum(w.values()) <= 0:
        raise ValueError("blend weights cannot all be zero")
    return w


def _combined_saving(by_profile: dict, profiles: list) -> tuple:
    """Combine several profile buckets into one (token%, cost%) from raw tokens/cost."""
    ap = sum(by_profile[p]["a_prompt"] for p in profiles if p in by_profile)
    bp = sum(by_profile[p]["b_prompt"] for p in profiles if p in by_profile)
    ac = sum(by_profile[p]["a_cost"] for p in profiles if p in by_profile)
    bc = sum(by_profile[p]["b_cost"] for p in profiles if p in by_profile)
    return (100.0 * (ap - bp) / ap if ap else 0.0,
            100.0 * (ac - bc) / ac if ac else 0.0)


def blend(agg: dict, weights: dict) -> dict:
    """Map the aggregated slices onto the four traffic levers and return the disclosed
    weighted-average blend per provider. Levers: cache=cache slice; agentic=agentic slice;
    prose=COLD rag+chat; reasoning=COLD reason. Prose/reasoning are read from the COLD
    (stateless, x_no_cache) slice ON PURPOSE — the repeated/cached prose benefit is already
    captured by the separate `cache` lever, so using the cache-on replay number here would
    DOUBLE-COUNT cache. Weights renormalise over whichever levers are present."""
    out = {}
    for prov, slices in agg.items():
        levers = {}
        if "cache" in slices:
            t = slices["cache"]["total"]
            levers["cache"] = (t["token_saving_pct"], t["cost_saving_pct"])
        if "agentic" in slices:
            t = slices["agentic"]["total"]
            levers["agentic"] = (t["token_saving_pct"], t["cost_saving_pct"])
        # Stateless prose/reasoning floor (cache counted once, in the cache lever).
        base = slices.get("cold", slices.get("replay", {})).get("by_profile", {})
        if base:
            levers["prose"] = _combined_saving(base, ["rag", "chat"])
            levers["reasoning"] = _combined_saving(base, ["reason"])
        present = {k: weights[k] for k in levers if weights.get(k, 0) > 0}
        wsum = sum(present.values()) or 1.0
        tok = sum(present[k] * levers[k][0] for k in present) / wsum
        cost = sum(present[k] * levers[k][1] for k in present) / wsum
        out[prov] = {"token_saving_pct": round(tok, 2), "cost_saving_pct": round(cost, 2),
                     "weights": present, "levers": {k: round(v[0], 1) for k, v in levers.items()}}
    return out


def render_blend(bl: dict) -> None:
    line = "=" * 68
    print("\n" + line)
    print("  ILLUSTRATIVE PRODUCTION-MIX BLEND (disclosed weighted average, --weights tunable)")
    print(line)
    for prov, b in bl.items():
        wtxt = " ".join(f"{k}={b['weights'][k]:.2f}" for k in b["weights"])
        ltxt = " ".join(f"{k} {b['levers'][k]:.0f}%" for k in b["levers"])
        print(f"  {prov}: blended tokens {b['token_saving_pct']:.1f}%  cost {b['cost_saving_pct']:.1f}%")
        print(f"     weights: {wtxt}")
        print(f"     levers:  {ltxt}")
    print("  " + "-" * 64)
    for c in WEIGHT_CITATIONS:
        print(f"  * {c}")
    print(line)


# --------------------------------------------------------------------------- #
def _fail(msg: str) -> int:
    print(f"ERROR: {msg}", file=sys.stderr)
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description="True A/B benchmark: proxy vs direct provider.")
    ap.add_argument("--providers", default="openai",
                    help="'openai' (default), 'all', or a comma list (e.g. openai,anthropic)")
    ap.add_argument("--mode", choices=["cold", "replay", "both"], default="both")
    ap.add_argument("--workload", choices=["standard", "cache", "agentic", "full"], default="standard",
                    help="'standard' = cold+replay on the neutral mix (<$1); 'cache' = disclosed "
                         "warm-cache burst (~90%%); 'agentic' = multi-turn tool-loop episodes; "
                         "'full' = ALL levers + the illustrative production-mix blend (costs more)")
    ap.add_argument("--weights", default="",
                    help="override blend weights for --workload full, e.g. "
                         "'cache=0.3,prose=0.35,agentic=0.2,reasoning=0.15' (illustrative + tunable)")
    ap.add_argument("--proxy-url", default=os.environ.get("PROXY_URL", "http://localhost:4000"))
    ap.add_argument("--api-key", default=os.environ.get("PROXY_API_KEY", ""))
    ap.add_argument("--tenant", default=os.environ.get("BENCHMARK_TENANT", ""),
                    help="X-Tenant-ID for the local admin-key benchmark; OMIT for tenant self-verify")
    ap.add_argument("--prices", default=str(PRICES))
    ap.add_argument("--max-spend-per-provider", type=float, default=1.0)
    ap.add_argument("--max-spend", type=float, default=10.0)
    ap.add_argument("--limit", type=int, default=0, help="cap trace length per provider (0 = all)")
    ap.add_argument("--judge", action="store_true", help="LLM-judge both arms (extra cost)")
    ap.add_argument("--exec-humaneval", action="store_true",
                    help="run HumanEval canonical tests (executes model code in a subprocess)")
    ap.add_argument("--require-direct", action="store_true",
                    help="hard-fail if a selected provider has no direct-arm key (tenant self-verify)")
    ap.add_argument("--no-cache-flush", action="store_true",
                    help="do not attempt a local cache flush (always true for a remote proxy)")
    ap.add_argument("--timeout", type=float, default=180.0)
    args = ap.parse_args()

    if httpx is None:
        return _fail("httpx is required — pip install httpx")
    if not args.api_key:
        return _fail("set --api-key (or PROXY_API_KEY) to your proxy key (tok-...)")
    if not DATASET.exists() or not SCHEDULE.exists():
        return _fail("dataset artifacts missing — run build_public_dataset.py first")

    prices = load_prices(args.prices)
    env = dict(os.environ)
    detected = detect_providers(env)

    if args.providers == "all":
        selected = list(PROVIDER_MODELS)
    else:
        selected = [p.strip() for p in args.providers.split(",") if p.strip()]
        for p in selected:
            if p not in PROVIDER_MODELS:
                return _fail(f"unknown provider {p!r}; known: {', '.join(PROVIDER_MODELS)}")

    run_providers, skipped = [], {}
    for p in selected:
        if detected[p]["configured"]:
            run_providers.append(p)
        elif args.require_direct:
            return _fail(f"provider {p!r} has no direct-arm credentials ({detected[p]['reason']}). "
                         "This flow requires a provider key for the true A/B — set it and retry.")
        else:
            skipped[p] = detected[p]["reason"]
    if not run_providers:
        return _fail("no runnable providers (none have configured credentials). "
                     f"Detected: {json.dumps({k: v['reason'] for k, v in detected.items()})}")

    items = load_items()
    schedule = json.loads(SCHEDULE.read_text(encoding="utf-8"))
    meta_ds = json.loads((HERE / "public_dataset.meta.json").read_text(encoding="utf-8"))
    if args.workload in ("cache", "full") and not CACHE_SCHEDULE.exists():
        return _fail("cache_schedule.json missing — run build_public_dataset.py first")
    if args.workload in ("agentic", "full") and not AGENTIC_DATASET.exists():
        return _fail("agentic_dataset.jsonl missing — build the agentic pack first, "
                     "or use --workload standard/cache")
    try:
        weights = parse_weights(args.weights)
    except ValueError as exc:
        return _fail(f"--weights: {exc}")

    # Load the workload artifacts ONCE (not per provider). cache_schedule = the disclosed
    # warm-cache burst; agentic_items = the BFCL-derived tool-loop tasks.
    cache_sched = (json.loads(CACHE_SCHEDULE.read_text(encoding="utf-8"))
                   if args.workload in ("cache", "full") else None)
    agentic_items = ([json.loads(ln) for ln in
                      AGENTIC_DATASET.read_text(encoding="utf-8").splitlines() if ln.strip()]
                     if args.workload in ("agentic", "full") else None)

    spend = SpendMeter(args.max_spend_per_provider, args.max_spend)
    records: list = []
    stopped_at_cap = False
    COST_LOG.write_text("", encoding="utf-8")

    for provider in run_providers:
        apply_litellm_env(provider, env)
        spec = PROVIDER_MODELS[provider]
        # Pass registry per workload:
        #   * cold   — originals only, G05 fully BYPASSED (x_no_cache): zero cache lookups AND
        #              zero STORES, so it is a pure stateless floor that also leaves nothing
        #              cached — which is why a cache pass right after it starts genuinely cold.
        #   * replay — full schedule, caching ON; the cache/dedup ceiling for the standard run.
        #   * cache  — disclosed warm-cache burst: each cacheable item once (cold, populates L1)
        #              then N verbatim repeats (warm, exact-hit). x_cache_semantic:false isolates
        #              the EXACT-cache lever (0 quality loss). Reproduces the published cache lever.
        #   * agentic— multi-turn tool-loop episodes (BFCL tools) exercising G08/G16 pruning.
        # The direct arm is memoised per (messages, max_tokens) so it is never billed twice.
        #
        # --workload full deliberately runs cold + cache + agentic (NOT replay): the blend's
        # prose/reasoning levers are read from COLD (stateless — so cache is NOT double-counted
        # against the separate cache lever), and skipping replay means the cache pass follows
        # only the store-less cold pass, so its cold-populate originals are genuinely cold.
        def _cache_pass():
            return ("cache", load_trace("replay", items, cache_sched), {"x_cache_semantic": False})

        def _agentic_pass():
            return ("agentic", [("original", it) for it in agentic_items], {})

        passes = []
        if args.workload == "full":
            passes.append(("cold", load_trace("cold", items, schedule), {"x_no_cache": True}))
            passes.append(_cache_pass())
            passes.append(_agentic_pass())
        elif args.workload == "cache":
            passes.append(_cache_pass())
        elif args.workload == "agentic":
            passes.append(_agentic_pass())
        else:
            if args.mode in ("cold", "both"):
                passes.append(("cold", load_trace("cold", items, schedule), {"x_no_cache": True}))
            if args.mode in ("replay", "both"):
                passes.append(("replay", load_trace("replay", items, schedule), {}))
        direct_memo: dict = {}

        for slice_name, trace, x_extra in passes:
            if args.limit:
                trace = trace[: args.limit]
            _tag = {"cold": ", G05 bypassed)", "cache": ", warm-cache burst)"}.get(slice_name, ", caching on)")
            print(f"\n[{provider}] {slice_name}: {len(trace)} proxy calls (model {spec['model']}{_tag}")
            for i, (kind, item) in enumerate(trace, 1):
                if spend.overall_tripped() or spend.provider_tripped(provider):
                    stopped_at_cap = True
                    print(f"  spend cap reached for {provider} — stopping this provider")
                    break
                messages = item["messages"]
                mt = int(item.get("max_tokens", 256))
                x_controls = {k: v for k, v in item.items() if k.startswith("x_")}
                x_controls.update(x_extra)
                try:
                    if slice_name == "agentic":
                        # Multi-turn tool-loop episode on BOTH arms; provider-billed tokens are
                        # summed across the whole episode. No memo (episodes are unique).
                        tools = item.get("tools") or []
                        tool_results = item.get("tool_results") or {}
                        a_billed = True
                        a = run_episode(lambda m, t: call_direct(spec["litellm"], m, mt, tools=t),
                                        messages, tools, tool_results)
                        b = run_episode(lambda m, t: call_proxy(
                            args.proxy_url, args.api_key, spec["model"], m, mt,
                            x_controls, args.tenant or None, args.timeout, tools=t),
                            messages, tools, tool_results)
                    else:
                        # Direct arm memoised per (messages, max_tokens): never billed twice.
                        memo_key = json.dumps(messages, sort_keys=True) + f"|{mt}"
                        a = direct_memo.get(memo_key)
                        a_billed = a is None          # only a real provider call costs money
                        if a is None:
                            a = call_direct(spec["litellm"], messages, mt)
                            direct_memo[memo_key] = a
                        b = call_proxy(args.proxy_url, args.api_key, spec["model"], messages, mt,
                                       x_controls, args.tenant or None, args.timeout)
                except Exception as exc:  # noqa: BLE001 — report and continue
                    print(f"  [{i}/{len(trace)}] {item['_label']:<16} ERROR: {exc}")
                    continue

                a_cost = price(spec["model"], a["prompt_tokens"], a["completion_tokens"], prices)
                b_cost = price(b["routed_model"], b["prompt_tokens"], b["completion_tokens"], prices)
                # Spend = real money: arm B always; arm A only when actually called (memo miss).
                spend.add(provider, (a_cost if a_billed else 0.0) + b_cost)
                if slice_name == "agentic":
                    # Agentic quality = the tool trajectory (proxy must not drop a tool the
                    # direct arm called), not final-answer facts.
                    facts = relative_tool_gate(a.get("tool_calls", []), b.get("tool_calls", []))
                else:
                    facts = relative_facts_gate(item, a["content"], b["content"])
                if args.exec_humaneval and item["_profile"] == "code":
                    pa, pb = exec_humaneval(item, a["content"]), exec_humaneval(item, b["content"])
                    if pa is not None:  # regression = A passed the tests but B did not
                        facts = {"graded": True, "passed": not (pa and not pb),
                                 "exec": {"direct": pa, "proxy": pb}}
                rec = {
                    "provider": provider, "slice": slice_name, "kind": kind,
                    "profile": item["_profile"], "_messages": messages,
                    "a": {**a, "cost": a_cost},
                    "b": {**b, "cost": b_cost},
                    "facts": facts,
                }
                records.append(rec)
                with COST_LOG.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"provider": provider, "slice": slice_name, "kind": kind,
                                         "label": item["_label"], "a_cost": round(a_cost, 6),
                                         "b_cost": round(b_cost, 6), "cache_hit": b["cache_hit"]}) + "\n")
                print(f"  [{i}/{len(trace)}] {item['_label']:<16} "
                      f"A={a['prompt_tokens']:>5}tok/${a_cost:.5f}  "
                      f"B={b['prompt_tokens']:>5}tok/${b_cost:.5f}  "
                      f"{'HIT' if b['cache_hit'] else '   '}  {'facts!' if not facts['passed'] else ''}")

    if not records:
        return _fail("no successful A/B pairs — check proxy URL, keys, and provider credentials")

    agg = aggregate(records)
    judge = run_judge(records, os.environ.get("QUALITY_JUDGE_MODEL", "gpt-4o-mini")) if args.judge else None
    meta = {
        "prices_as_of": prices.get("as_of"),
        "seed": meta_ds.get("seed"),
        "dataset_sha256": meta_ds.get("dataset_sha256", ""),
        "build_source": meta_ds.get("build_source"),
        "providers_run": run_providers,
        "providers_skipped": skipped,
        "spend_total_usd": round(spend.total(), 6),
        "stopped_at_cap": stopped_at_cap,
        "workload": args.workload,
        "mode": args.mode,
        "cache_burst": meta_ds.get("cache_burst") if args.workload in ("cache", "full") else None,
    }
    blended = blend(agg, weights) if args.workload == "full" else None
    if blended is not None:
        meta["blend"] = {"weights": weights, "citations": WEIGHT_CITATIONS, "result": blended}
    if judge is not None:
        meta["judge"] = judge
    RESULTS.write_text(json.dumps({"meta": meta, "results": agg}, indent=2), encoding="utf-8")
    render(agg, meta)
    if blended is not None:
        render_blend(blended)
    if judge and judge.get("ran"):
        flag = "  [WARN: proxy quality drop]" if judge["warn"] else ""
        print(f"  Judge ({judge['n']}): direct {judge['direct_mean']}/5  proxy {judge['proxy_mean']}/5{flag}")
    print(f"  Full detail -> {RESULTS}")

    # Non-zero exit if a cap tripped or a real quality regression occurred.
    def _reg(sl):
        return sum(1 for r in records if r.get("slice") == sl
                   and r["facts"]["graded"] and not r["facts"]["passed"])
    present = [s for s in SLICE_ORDER if any(r.get("slice") == s for r in records)]
    per_slice = {s: _reg(s) for s in present}
    regressed = sum(per_slice.values())
    if regressed:
        detail = ", ".join(f"{s} {n}" for s, n in per_slice.items())
        print(f"\n  QUALITY: proxy dropped a fact the direct arm had — {detail} record(s)")
    if stopped_at_cap:
        return 3
    return 2 if regressed else 0


if __name__ == "__main__":
    raise SystemExit(main())
