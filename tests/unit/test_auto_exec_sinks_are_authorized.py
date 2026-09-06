"""Every place the proxy EXECUTES something on the model's say-so must be authorized.

The pipeline auto-executes CCR tools server-side. Since 2026-09-01 the two live sinks
(`g15_server_compute`, `g28_ccr`) each call `authorize_dispatch` at the dispatch site rather
than relying on G32's position earlier in the response chain — ordering is the belt, the
sink is the braces.

What nothing checked was whether OTHER dispatch sinks existed. Two did, both dormant, both
found by grep rather than by any test:

* `g15_mcp_dispatch.py` (deleted 2026-09-06, backlog #30) — built a request URL by pasting a
  model-supplied tool name into the path with no validation, and README advertised it as an
  extension point with a worked example.
* `g14_tool_combining.py` (deleted 2026-09-06) — `ToolCallBatcher._execute_single` looked a
  handler up by the model's tool name and awaited it with no `authorize_dispatch` call.

**Correcting the record on the second one:** the backlog described it as "the same shape" as
the first. Reading it, that is not accurate and the difference matters. It resolved the name
against `self._handlers`, a dict an OPERATOR populates by calling `register_handler`, and
returned an error for anything absent — an allowlist, not a URL injection. The real defect
was that if it were ever wired it would execute a model-requested tool without consulting the
tool policy, silently reintroducing exactly the hole closed in 2026-09-01. That is why it was
deleted rather than guarded: it was never registered in `pipeline.py`, its `combine_tool_calls`
knob is in no shipped config, and `docs/config-reference.md` already listed it under "pending
wiring".

These tests are source-level on purpose. The failure mode is a NEW sink appearing, and no
behavioural test of code that does not yet exist can catch that.
"""
import inspect
import pathlib
import re

import pytest

_MIDDLEWARE = pathlib.Path(__file__).resolve().parents[2] / "src" / "proxy" / "middleware"

# A handler resolved at runtime and then called. Deliberately broad — the point is to notice
# a new one, not to prejudge whether it is safe.
_DISPATCH = re.compile(r"(?:await\s+)?handler\s*\(|_handlers\s*\.\s*get\s*\(|"
                       r"_task_handlers\s*\.\s*get\s*\(")

# Reviewed 2026-09-06. Anything not on this list is a sink nobody has looked at yet.
# Both entries are LIVE and both authorize at the dispatch site.
#
# The list held two more for part of that day — `g13_kafka.py` and `g16_temporal_runtime.py`
# — until they were deleted the same afternoon. They were a genuinely different risk class
# (handler dicts keyed by a queue topic / an internally-built agent name, not by a
# model-chosen tool name), so they were never the authorization hole the two deleted sinks
# were. They went because nothing reached them: unwired, absent from every shipped config,
# referenced by no code and no test, and in Kafka's case `aiokafka` was not installed, so
# the module could not have run had anyone set its flags.
_KNOWN_DISPATCH_SINKS = {
    "g15_server_compute.py",
    "g28_ccr.py",
}

_LIVE_AUTO_EXEC_SINKS = ("g15_server_compute.py", "g28_ccr.py")


def _code(path: pathlib.Path) -> str:
    """Source with comment-only lines dropped, so prose about a call is not a call."""
    return "\n".join(line for line in path.read_text(encoding="utf-8").splitlines()
                     if not line.lstrip().startswith("#"))


class TestNoUnreviewedDispatchSinkAppears:
    def test_the_set_of_dispatch_sinks_is_exactly_the_reviewed_set(self):
        found = {p.name for p in sorted(_MIDDLEWARE.glob("*.py"))
                 if _DISPATCH.search(_code(p))}
        new = found - _KNOWN_DISPATCH_SINKS
        assert not new, (
            f"new handler-dispatch sink(s) {sorted(new)} — decide whether the model can "
            f"choose the key. If it can, call authorize_dispatch at the dispatch site and "
            f"add it to _LIVE_AUTO_EXEC_SINKS; if it cannot, add it here with the reason."
        )

    def test_the_reviewed_set_has_not_gone_stale(self):
        """A name left in the list after its file is deleted would silently weaken the
        check above — the same bookkeeping rot the deleted modules died of."""
        for name in _KNOWN_DISPATCH_SINKS:
            assert (_MIDDLEWARE / name).is_file(), (
                f"{name} no longer exists; remove it from _KNOWN_DISPATCH_SINKS"
            )


class TestLiveSinksAuthorizeAtTheSink:
    @pytest.mark.parametrize("name", _LIVE_AUTO_EXEC_SINKS)
    def test_the_sink_calls_authorize_dispatch(self, name):
        """Not "G32 runs earlier in the pipeline" — that is a property of `pipeline.py`,
        and G32's shipped default (`flag`) deliberately leaves a denied call in the
        response, so ordering alone let G15 execute it anyway."""
        assert "authorize_dispatch" in _code(_MIDDLEWARE / name)

    def test_authorize_dispatch_fails_closed(self, monkeypatch):
        """A refused call is returned unexecuted, which is safe — so an error here must
        refuse, never fall open. Deliberately opposite to the cache hoist, where failing
        closed would turn a bug into an outage.

        Driven through config resolution, which is where a real error would come from.
        The first check (`ccr_tools_injected`) sits OUTSIDE the try deliberately: it is a
        plain attribute on `RequestContext`, so it cannot raise, and it is identity rather
        than policy — a name we never advertised is refused before any config is read.
        """
        from middleware import g32_tool_eligibility as mod

        def _boom(*_a, **_k):
            raise RuntimeError("config blew up")

        monkeypatch.setattr(mod, "resolve_group_config", _boom)

        class _Ctx:
            ccr_tools_injected = True
            request_id = "req-1"

        assert mod.authorize_dispatch(_Ctx(), "headroom_retrieve") == mod.REASON_EVALUATION_ERROR

    def test_a_tool_we_never_advertised_is_refused_before_any_policy_is_read(self):
        """Identity, not policy — so no mode and no config can license it."""
        from middleware import g32_tool_eligibility as mod

        class _Ctx:
            ccr_tools_injected = False

        assert mod.authorize_dispatch(_Ctx(), "headroom_retrieve") == mod.REASON_NOT_INJECTED


class TestDeletedSinksStayDeleted:
    @pytest.mark.parametrize("name", ["g15_mcp_dispatch.py", "g14_tool_combining.py",
                                      "g13_kafka.py", "g16_temporal_runtime.py"])
    def test_the_module_is_gone(self, name):
        assert not (_MIDDLEWARE / name).exists(), (
            f"{name} was deleted 2026-09-06 as an unwired dispatch module. Re-adding one "
            f"needs authorize_dispatch at the sink, name validation, and — for the MCP "
            f"one — validate_outbound_url. Re-adding the Temporal runtime also means "
            f"re-pinning temporalio, which was dropped with it (58 MB of image)."
        )

    def test_the_dropped_dependencies_stay_dropped(self):
        """`temporalio` was pinned solely for the deleted runtime — 58 MB in the image for
        code nothing reached. `aiokafka` was never pinned at all, which is why the Kafka
        module could not have started even if someone had set its flags."""
        root = pathlib.Path(__file__).resolve().parents[2]
        reqs = (root / "src" / "proxy" / "requirements.txt").read_text(encoding="utf-8")
        pinned = {line.split("==")[0] for line in reqs.splitlines() if "==" in line
                  and not line.startswith((" ", "#"))}
        for pkg in ("temporalio", "aiokafka", "nexus-rpc"):
            assert pkg not in pinned, f"{pkg} is pinned again"

    @pytest.mark.parametrize("symbol", ["G15MCPDispatch", "MCPServerDispatch",
                                        "G14ToolCombining", "ToolCallBatcher",
                                        "G13Kafka", "KafkaBatchProcessor", "TemporalRuntime"])
    def test_nothing_imports_the_removed_classes(self, symbol):
        for path in sorted(_MIDDLEWARE.glob("*.py")):
            assert symbol not in _code(path), f"{path.name} references removed {symbol}"

    def test_the_docs_do_not_advertise_them(self):
        """What made the MCP deletion urgent rather than tidy: README documented it as an
        SDK extension point with a worked `register_handler` example, so it was code we
        were inviting people to use."""
        root = pathlib.Path(__file__).resolve().parents[2]
        for doc in ("README.md", "docs/request-flow-diagram.md"):
            text = (root / doc).read_text(encoding="utf-8")
            for symbol in ("g15_mcp_dispatch", "G15MCPDispatch",
                           "g14_tool_combining", "ToolCallBatcher",
                           "g13_kafka", "G13Kafka", "g16_temporal_runtime", "TemporalRuntime"):
                assert symbol not in text, f"{doc} still advertises {symbol}"
