"""G08's intent pruning does nothing for a customer who has not registered their tools.

E17, 2026-09-06. The published headline credited G08 5.8% of the savings. Driving the real
middleware over every pitch dataset at the shipped config showed where that comes from: only
**4 of 63** distinct tool names appear in `config/tool-registry.yaml`, and **two of them** —
`send_email` and `create_calendar_event`, both fixtures we wrote — produce **100%** of the
pruning. DS13 (11 tools, 50 requests) and DS4 prune exactly 0.00%.

The cause is a fail-OPEN default that is easy to miss when reading the filter:

    reg_entry    = all_available_tools.get(tool_name)          # None for an unknown tool
    tool_intents = reg_entry.get("intents", ...) if isinstance(reg_entry, dict) else ["default"]
    if any(i in tool_intents for i in intents) or "default" in tool_intents:
        relevant.append(tool)                                   # ...so it is ALWAYS kept

Keeping an unknown tool is the right call — silently dropping a tool the caller sent would break
their agent — but it means G08's pruning is inert until the operator lists their OWN tools, and
nothing said so. These tests pin the behaviour and the shipped defaults that follow from it:
`compress_descriptions` is now ON, because it is the only part of G08 that acts on anyone's tools
and it never drops one.
"""
import asyncio
import pathlib
import sys

import pytest
import yaml

_PROXY = pathlib.Path(__file__).resolve().parents[3] / "src" / "proxy"
if str(_PROXY) not in sys.path:
    sys.path.insert(0, str(_PROXY))

ROOT = pathlib.Path(__file__).resolve().parents[3]

from middleware.g08_tool_loading import G08ToolLoading  # noqa: E402


class _Sav:
    def __init__(self):
        self.steps = []

    def add_step(self, *a, **k):
        self.steps.append((a, k))


def _tool(name, desc="Perform the operation that the user has asked for."):
    return {"type": "function",
            "function": {"name": name, "description": desc,
                         "parameters": {"type": "object", "properties": {}}}}


def _ctx(tools, compress=False, monkeypatch=None):
    import types
    return types.SimpleNamespace(
        request_id="t", model="gpt-4o-mini", params={"tools": list(tools)},
        messages=[{"role": "user", "content": "please schedule a meeting for tomorrow"}],
        redis_prefix="", savings=_Sav(),
        config={"groups": {"G8_tools": {
            "enabled": True, "registry_path": "", "compress_descriptions": compress,
            "pruning": {"enabled": False},
        }}})


@pytest.fixture(autouse=True)
def _local_registry(monkeypatch):
    """Force the local-file fallback so the test never reaches GCS."""
    monkeypatch.setenv("TOOL_REGISTRY_PATH", str(ROOT / "config" / "tool-registry.yaml"))
    import middleware.g08_tool_loading as g08
    monkeypatch.setattr(g08, "_registry_cache", {})
    yield


class TestAnUnregisteredToolIsNeverPruned:
    def test_a_customers_own_tools_all_survive(self):
        """The headline finding, as a test: a realistic tool set nobody registered is returned
        intact, so G08's pruning contributes nothing for that customer."""
        tools = [_tool(n) for n in ("acme_lookup_invoice", "acme_issue_refund",
                                    "acme_open_ticket", "acme_close_ticket")]
        ctx = _ctx(tools)
        asyncio.run(G08ToolLoading().process_request(ctx))
        assert [t["function"]["name"] for t in ctx.params["tools"]] == \
               [t["function"]["name"] for t in tools]

    def test_a_registered_tool_with_a_non_matching_intent_IS_pruned(self):
        """The other half, so the test proves the mechanism works rather than that it is dead.
        `send_email` is registered with intents email/write/notify, which a scheduling request
        does not classify to."""
        tools = [_tool("send_email"), _tool("acme_lookup_invoice")]
        ctx = _ctx(tools)
        asyncio.run(G08ToolLoading().process_request(ctx))
        kept = [t["function"]["name"] for t in ctx.params["tools"]]
        assert "acme_lookup_invoice" in kept
        assert "send_email" not in kept

    def test_the_shipped_registry_holds_none_of_a_customers_names(self):
        """The registry ships seeded with OUR examples. If someone ever adds a plausible
        customer tool name here, that is a fixture leaking into a product default."""
        reg = yaml.safe_load((ROOT / "config" / "tool-registry.yaml").read_text(encoding="utf-8"))
        names = {e["name"] for e in (reg if isinstance(reg, list) else reg.get("tools", []))}
        assert names, "registry is empty — G08 would prune nothing at all"
        assert len(names) < 40, "a registry this large is no longer an example set"


class TestDescriptionCompressionIsTheDefaultBecauseItWorksForEveryone:
    def test_it_ships_on_in_both_configs(self):
        for rel in ("config/config.yaml.template", "config/config.yaml"):
            cfg = yaml.safe_load((ROOT / rel).read_text(encoding="utf-8"))
            assert cfg["groups"]["G8_tools"]["compress_descriptions"] is True, (
                f"{rel}: with this off, G08 does nothing at all for a deployment that has not "
                f"populated its own tool registry (E17)"
            )

    def test_it_shrinks_an_unregistered_tool_and_drops_none(self):
        tools = [_tool("acme_lookup_invoice",
                       "Retrieve the status of a customer order by the identifier that was "
                       "given to the user in the confirmation email.")]
        ctx = _ctx(tools, compress=True)
        before = ctx.params["tools"][0]["function"]["description"]
        asyncio.run(G08ToolLoading().process_request(ctx))
        after = ctx.params["tools"][0]["function"]["description"]
        assert len(ctx.params["tools"]) == 1, "compression must never drop a tool"
        assert len(after) < len(before)

    def test_it_does_not_mutate_the_callers_tool_dicts(self):
        """G08 deep-copies before compressing. Rewriting the caller's own objects would leak a
        proxy-side edit back into their process."""
        tools = [_tool("acme_lookup_invoice",
                       "Retrieve the status of the order that the customer has asked about.")]
        original = tools[0]["function"]["description"]
        ctx = _ctx(tools, compress=True)
        asyncio.run(G08ToolLoading().process_request(ctx))
        assert tools[0]["function"]["description"] == original
