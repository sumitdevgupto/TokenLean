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


def _tool(name, desc="Perform the operation that the user has asked for."):
    return {"type": "function",
            "function": {"name": name, "description": desc,
                         "parameters": {"type": "object", "properties": {}}}}


def _cfg(compress=False):
    return {"groups": {"G8_tools": {
        "enabled": True, "registry_path": "", "compress_descriptions": compress,
        "pruning": {"enabled": False},
    }}}


@pytest.fixture(autouse=True)
def _local_registry(monkeypatch):
    """Force the local-file fallback so the test never reaches GCS."""
    monkeypatch.setenv("TOOL_REGISTRY_PATH", str(ROOT / "config" / "tool-registry.yaml"))
    import middleware.g08_tool_loading as g08
    monkeypatch.setattr(g08, "_registry_cache", {})
    yield


class TestAnUnregisteredToolIsNeverPruned:
    def test_a_customers_own_tools_all_survive(self, make_ctx):
        """The headline finding, as a test: a realistic tool set nobody registered is returned
        intact, so G08's pruning contributes nothing for that customer."""
        tools = [_tool(n) for n in ("acme_lookup_invoice", "acme_issue_refund",
                                    "acme_open_ticket", "acme_close_ticket")]
        ctx = make_ctx(
            [{"role": "user", "content": "please schedule a meeting for tomorrow"}],
            params={"tools": tools}, config=_cfg())
        asyncio.run(G08ToolLoading().process_request(ctx))
        assert [t["function"]["name"] for t in ctx.params["tools"]] == \
               [t["function"]["name"] for t in tools]

    def test_a_registered_tool_with_a_non_matching_intent_IS_pruned(self, make_ctx):
        """The other half, so the test proves the mechanism works rather than that it is dead.
        `send_email` is registered with intents email/write/notify, which a scheduling request
        does not classify to."""
        tools = [_tool("send_email"), _tool("acme_lookup_invoice")]
        ctx = make_ctx(
            [{"role": "user", "content": "please schedule a meeting for tomorrow"}],
            params={"tools": tools}, config=_cfg())
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
        customer_shaped = {"acme_lookup_invoice", "acme_issue_refund",
                            "acme_open_ticket", "acme_close_ticket"}
        assert not (names & customer_shaped), (
            "a fixture from the test suite has leaked into the shipped registry"
        )


class TestDescriptionCompressionIsTheDefaultBecauseItWorksForEveryone:
    def test_it_ships_on_in_the_template(self):
        """The template is the tracked, always-present contract for what ships — the same
        source of truth `test_config_template_keys.py` reads."""
        cfg = yaml.safe_load((ROOT / "config" / "config.yaml.template").read_text(encoding="utf-8"))
        assert cfg["groups"]["G8_tools"]["compress_descriptions"] is True, (
            "with this off, G08 does nothing at all for a deployment that has not populated "
            "its own tool registry (E17)"
        )

    def test_it_ships_on_in_local_dev_config_if_present(self):
        """`config/config.yaml` is a gitignored, operator-created copy of the template — absent
        on a clean checkout, in CI, and in the `git archive HEAD` tree the OSS gate builds, so
        it must never be read unconditionally. This only checks it when a developer has one."""
        local = ROOT / "config" / "config.yaml"
        if not local.exists():
            pytest.skip("config/config.yaml is a local dev artifact — not present here")
        cfg = yaml.safe_load(local.read_text(encoding="utf-8"))
        assert cfg["groups"]["G8_tools"]["compress_descriptions"] is True

    def test_the_code_default_matches_the_documented_default(self):
        """The template value is only the SHIPPED config's default. If a hand-edited config or
        a per-tenant overlay ever omits this key, `.get(..., default)` is what actually runs —
        and until now that fallback was still `False`, silently contradicting the docs."""
        from middleware import g08_tool_loading as g08
        assert g08._COMPRESS_DESCRIPTIONS_DEFAULT is True
        import inspect
        src = inspect.getsource(G08ToolLoading.process_request)
        assert 'cfg.get("compress_descriptions", _COMPRESS_DESCRIPTIONS_DEFAULT)' in src, (
            "the code's own fallback must match the documented default — a literal "
            '.get("compress_descriptions", False) would silently revert to the old '
            "behaviour whenever the key is missing from the effective config"
        )

    def test_it_shrinks_an_unregistered_tool_and_drops_none(self, make_ctx):
        tools = [_tool("acme_lookup_invoice",
                       "Retrieve the status of a customer order by the identifier that was "
                       "given to the user in the confirmation email.")]
        ctx = make_ctx([{"role": "user", "content": "look up my order"}],
                       params={"tools": tools}, config=_cfg(compress=True))
        before = ctx.params["tools"][0]["function"]["description"]
        asyncio.run(G08ToolLoading().process_request(ctx))
        after = ctx.params["tools"][0]["function"]["description"]
        assert len(ctx.params["tools"]) == 1, "compression must never drop a tool"
        assert len(after) < len(before)

    def test_it_does_not_mutate_the_callers_tool_dicts(self, make_ctx):
        """G08 deep-copies before compressing. Rewriting the caller's own objects would leak a
        proxy-side edit back into their process."""
        tools = [_tool("acme_lookup_invoice",
                       "Retrieve the status of the order that the customer has asked about.")]
        original = tools[0]["function"]["description"]
        ctx = make_ctx([{"role": "user", "content": "look up my order"}],
                       params={"tools": tools}, config=_cfg(compress=True))
        asyncio.run(G08ToolLoading().process_request(ctx))
        assert tools[0]["function"]["description"] == original
