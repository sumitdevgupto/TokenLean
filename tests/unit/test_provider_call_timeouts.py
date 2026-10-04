"""Every provider call the proxy makes is bounded in time.

No litellm call set a timeout, so litellm's own fallback of 10 minutes applied: a provider that
accepted the connection and then stalled held the request, and a worker slot, that long, and
the breaker and failover never got to act. This walks the proxy's source and fails on a provider
call that is not bounded. It must take its timeout from `resilience.request_timeout_seconds`
(`request_timeout_for(...)`, `.request_timeout_seconds`, or the resilience layer's
`**….call_limits()`), run under `asyncio.wait_for`, or be named below with the reason.
"""
import ast
from pathlib import Path

PROXY = Path(__file__).resolve().parents[2] / "src" / "proxy"

# (file, enclosing function) → why the call is bounded without the setting.
OWN_LIMIT = {
    ("middleware/intent_orchestration.py", "_dispatch"):
        "an F2 agent's own timeout_seconds, capped at MAX_AGENT_TIMEOUT_SECONDS",
}
PASS_THROUGH = {
    ("middleware/g09_context_schema.py", "_acompletion"):
        "the callable Instructor is given; G09 runs the Instructor call under asyncio.wait_for",
}


def _provider_calls():
    """Yield (file, enclosing function, call node, parent map) for each litellm.acompletion."""
    for path in sorted(PROXY.rglob("*.py")):
        if "_docs_seed" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "acompletion"
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == "litellm"):
                fn = node
                while fn in parents and not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    fn = parents[fn]
                name = fn.name if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) else "<module>"
                yield path.relative_to(PROXY).as_posix(), name, node, parents


def _under_wait_for(node, parents) -> bool:
    parent = parents.get(node)
    return (isinstance(parent, ast.Call) and ast.unparse(parent.func) == "asyncio.wait_for"
            and parent.args and parent.args[0] is node)


def _bounded_by_the_setting(node) -> bool:
    for kw in node.keywords:
        source = ast.unparse(kw.value)
        if kw.arg == "timeout" and "request_timeout" in source:
            return True
        if kw.arg is None and source.endswith(".call_limits()"):
            return True
    return False


def test_every_provider_call_is_bounded():
    unbounded, seen = [], set()
    for rel, fn, node, parents in _provider_calls():
        where = (rel, fn)
        if _bounded_by_the_setting(node) or _under_wait_for(node, parents):
            continue
        if where in OWN_LIMIT and any(kw.arg == "timeout" for kw in node.keywords):
            seen.add(where)
            continue
        if where in PASS_THROUGH:
            seen.add(where)
            continue
        unbounded.append(f"{rel}:{node.lineno} in {fn}()")
    assert unbounded == [], "provider calls with no timeout: " + ", ".join(unbounded)
    assert seen == set(OWN_LIMIT) | set(PASS_THROUGH), "stale entries in the exceptions above"


def test_the_walk_finds_the_calls():
    calls = [(rel, fn) for rel, fn, _, _ in _provider_calls()]
    assert {"main.py", "middleware/g06_routing.py", "middleware/g11_output_format.py",
            "middleware/g13_batch.py", "middleware/history_utils.py"} <= {rel for rel, _ in calls}
    assert len(calls) >= 14, calls


def test_the_calls_this_layer_retries_leave_retrying_to_it():
    # main's calls run under call_with_resilience, which retries and fails over: the client
    # library's own retries (two, on a 429 or 5xx) must not stack underneath.
    for rel, fn, node, _ in _provider_calls():
        if rel != "main.py":
            continue
        keywords = {kw.arg: ast.unparse(kw.value) for kw in node.keywords}
        assert keywords.get("max_retries") == "0" or any(
            kw.arg is None and ast.unparse(kw.value).endswith(".call_limits()")
            for kw in node.keywords), f"main.py:{node.lineno} in {fn}()"
