"""E4 — the opt-in echo of the prompt the proxy ACTUALLY SENT to the provider.

Every optimisation acts on the prompt, so the prompt is the only place its defects are
visible — and it was the one thing nothing kept. On 2026-09-06 a compaction defect replaced
customer tool-result content with an unresolvable reference on billed 200s; it produced
valid, shorter JSON, so both existing guards passed it, and no artefact could show what the
model had received.

The echo is deliberately hard to turn on: the CALLER asks per request (`x_echo_prompt`) and
the OPERATOR has to have allowed it (`observability.echo_sent_prompt`). The operator gate is
not ceremony — the echoed prompt can contain content the caller never sent (retrieved
chunks, memories, templates), so a tenant must not be able to self-serve it with a request
parameter alone.

What these tests pin, in order of how much a regression would cost:
1. The provider credential can never appear in an echo.
2. `x_echo_prompt` can never reach the provider (it rides the `x_` strip that protects
   `x_no_cache`).
3. Both gates are required, and the default is off.
4. Nothing that never reached a provider is echoed as though it had.
5. The echo does not enter the G05 cache entry, and streams carry none.
"""
import os
import pathlib
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "proxy")))

import pytest  # noqa: E402

import main  # noqa: E402
from middleware import RequestContext  # noqa: E402
from providers import outgoing_params_for  # noqa: E402
from providers.openai_adapter import OpenAIAdapter  # noqa: E402
from savings.models import SavingsRecord  # noqa: E402


def _ctx(params=None, config=None):
    return RequestContext(
        request_id="req-echo-1", user_id="u@x.test",
        original_messages=[{"role": "user", "content": "hello"}],
        messages=[{"role": "user", "content": "hello"}],
        model="gpt-4o-mini", routed_model="gpt-4o-mini",
        params=dict(params or {}),
        config=config if config is not None else {"observability": {"echo_sent_prompt": True}},
        savings=SavingsRecord(
            request_id="req-echo-1", user_id="u@x.test",
            timestamp=datetime.now(timezone.utc),
            model_requested="gpt-4o-mini", routed_model="gpt-4o-mini",
            baseline_tokens=10,
        ),
    )


_OUTGOING = {"temperature": 0, "max_tokens": 128, "tools": [{"type": "function"}]}


class TestTheCredentialNeverLeaks:
    def test_a_credential_shaped_key_is_dropped_from_the_echo(self):
        """The echo is built from `outgoing_params`, which by construction carries no
        credential — the key lives in the adapter's `build_call` kwargs and is passed
        separately. This drops anything credential-SHAPED by name as well, so a future
        adapter change cannot turn a diagnostic into a credential leak."""
        ctx = _ctx({"x_echo_prompt": True})
        main.capture_sent_prompt(
            ctx, ctx.messages,
            {**_OUTGOING, "api_key": "sk-live-secret", "api_base": "https://x",
             "authorization": "Bearer y", "some_secret": "z"},
            "gpt-4o-mini",
        )
        rendered = str(ctx.sent_prompt)
        assert "sk-live-secret" not in rendered
        for k in ("api_key", "api_base", "authorization", "some_secret"):
            assert k not in ctx.sent_prompt["params"]
        assert ctx.sent_prompt["params"]["temperature"] == 0

    @pytest.mark.parametrize("keep", [
        "max_tokens", "max_completion_tokens", "temperature", "top_p", "seed", "tools",
        "tool_choice", "reasoning_effort", "stream_options", "response_format",
        "prompt_cache_key", "service_tier", "n", "stop",
    ])
    def test_real_parameters_are_never_censored(self, keep):
        """The redaction used to match the bare substring "token", which silently removed
        `max_tokens` and `max_completion_tokens` — the two parameters most worth seeing in
        an echo, since an output-budget group is exactly what changes them. A redaction that
        quietly deletes real data is the same defect class as a silently shortened prompt:
        the artefact reads as evidence while being something else.
        """
        ctx = _ctx({"x_echo_prompt": True})
        main.capture_sent_prompt(ctx, ctx.messages, {keep: 256}, "gpt-4o-mini")
        assert keep in ctx.sent_prompt["params"], f"{keep} was censored out of the echo"

    @pytest.mark.parametrize("drop", [
        "api_key", "apikey", "api_base", "authorization", "auth", "access_token",
        "auth_token", "bearer_token", "refresh_token", "password", "secret", "credential",
        "openai_api_key", "azure_secret", "db_password", "vault_token", "aws_secret_access_key",
    ])
    def test_credential_shaped_names_are_still_dropped(self, drop):
        ctx = _ctx({"x_echo_prompt": True})
        main.capture_sent_prompt(
            ctx, ctx.messages, {drop: "sk-live-secret", "max_tokens": 256}, "gpt-4o-mini")
        assert drop not in ctx.sent_prompt["params"]
        assert "sk-live-secret" not in str(ctx.sent_prompt)
        assert ctx.sent_prompt["params"]["max_tokens"] == 256

    def test_the_documented_worked_example_is_true(self):
        """`docs/config-reference.md` shows an echo containing `"max_tokens": 256`. A doc
        that promises a field the code strips is a customer-visible falsehood."""
        ctx = _ctx({"x_echo_prompt": True})
        main.capture_sent_prompt(
            ctx, ctx.messages,
            {"temperature": 0, "max_tokens": 256, "tools": [{"type": "function"}]},
            "gpt-4o-mini",
        )
        assert ctx.sent_prompt["params"] == {
            "temperature": 0, "max_tokens": 256, "tools": [{"type": "function"}]}

    def test_the_echo_is_not_built_from_the_adapter_call_kwargs(self):
        """Guards the shape, not just this instance: the capture helper takes the outgoing
        params explicitly, so a caller cannot accidentally hand it `_call_kwargs`."""
        import inspect
        sig = inspect.signature(main.capture_sent_prompt)
        assert list(sig.parameters) == ["ctx", "messages", "outgoing_params", "model"]


class TestTheFlagCannotReachTheProvider:
    def test_x_echo_prompt_is_stripped_from_outgoing_params(self):
        """The same `x_`-prefix strip that protects `x_no_cache`. If this ever regresses,
        OpenAI receives an unknown parameter and 400s every echoing request."""
        ctx = _ctx({"x_echo_prompt": True, "temperature": 0})
        outgoing = outgoing_params_for(
            ctx, OpenAIAdapter(), "gpt-4o-mini", {"providers": []}, "req-echo-1")
        assert "x_echo_prompt" not in outgoing
        assert outgoing["temperature"] == 0


class TestBothGatesAreRequired:
    def test_default_config_echoes_nothing(self):
        ctx = _ctx({"x_echo_prompt": True}, config={})
        main.capture_sent_prompt(ctx, ctx.messages, _OUTGOING, "gpt-4o-mini")
        assert ctx.sent_prompt is None

    def test_operator_flag_alone_echoes_nothing(self):
        ctx = _ctx({}, config={"observability": {"echo_sent_prompt": True}})
        main.capture_sent_prompt(ctx, ctx.messages, _OUTGOING, "gpt-4o-mini")
        assert ctx.sent_prompt is None

    def test_request_flag_alone_echoes_nothing(self):
        """The one that matters: a tenant must not be able to switch on disclosure of
        proxy-injected content by adding a parameter to its own request."""
        ctx = _ctx({"x_echo_prompt": True},
                   config={"observability": {"echo_sent_prompt": False}})
        main.capture_sent_prompt(ctx, ctx.messages, _OUTGOING, "gpt-4o-mini")
        assert ctx.sent_prompt is None

    def test_both_gates_produce_the_echo(self):
        ctx = _ctx({"x_echo_prompt": True})
        main.capture_sent_prompt(ctx, ctx.messages, _OUTGOING, "gpt-4o-mini")
        assert ctx.sent_prompt["messages"] == [{"role": "user", "content": "hello"}]
        assert ctx.sent_prompt["model"] == "gpt-4o-mini"
        assert ctx.sent_prompt["truncated"] is False

    @pytest.mark.parametrize("value", ["true", "True", "1", "yes", True])
    def test_truthy_spellings_match_the_x_no_cache_convention(self, value):
        ctx = _ctx({"x_echo_prompt": value})
        main.capture_sent_prompt(ctx, ctx.messages, _OUTGOING, "gpt-4o-mini")
        assert ctx.sent_prompt is not None

    @pytest.mark.parametrize("value", ["false", "0", "no", "", None])
    def test_falsy_spellings_do_not_echo(self, value):
        ctx = _ctx({"x_echo_prompt": value})
        main.capture_sent_prompt(ctx, ctx.messages, _OUTGOING, "gpt-4o-mini")
        assert ctx.sent_prompt is None

    def test_a_broken_config_fails_closed(self):
        """An echo that should not have happened cannot be un-sent, so anything unexpected
        resolves to 'do not echo'."""
        ctx = _ctx({"x_echo_prompt": True}, config={"observability": "not-a-dict"})
        main.capture_sent_prompt(ctx, ctx.messages, _OUTGOING, "gpt-4o-mini")
        assert ctx.sent_prompt is None


class TestTruncationIsLoud:
    def test_an_oversized_prompt_is_marked_not_silently_shortened(self):
        """A quietly trimmed prompt reads as evidence of what the model saw while being
        something else — which is the failure mode this whole item exists to prevent."""
        ctx = _ctx({"x_echo_prompt": True},
                   config={"observability": {"echo_sent_prompt": True, "max_echo_chars": 500}})
        ctx.messages = [{"role": "user", "content": "x" * 5000}]
        main.capture_sent_prompt(ctx, ctx.messages, _OUTGOING, "gpt-4o-mini")
        assert ctx.sent_prompt["truncated"] is True
        assert ctx.sent_prompt["original_chars"] > 500
        assert len(ctx.sent_prompt["messages"][0]["content"]) <= 2000

    def test_a_prompt_under_the_ceiling_is_verbatim(self):
        ctx = _ctx({"x_echo_prompt": True})
        main.capture_sent_prompt(ctx, ctx.messages, _OUTGOING, "gpt-4o-mini")
        assert ctx.sent_prompt["truncated"] is False
        assert ctx.sent_prompt["messages"] == ctx.messages

    def test_the_echo_is_a_snapshot_not_a_live_reference(self):
        """Later pipeline stages mutate `ctx.messages` in place; the echo must record what
        was sent, not what the list became afterwards."""
        ctx = _ctx({"x_echo_prompt": True})
        main.capture_sent_prompt(ctx, ctx.messages, _OUTGOING, "gpt-4o-mini")
        ctx.messages[0]["content"] = "MUTATED AFTER THE CALL"
        assert ctx.sent_prompt["messages"][0]["content"] == "hello"


class TestNothingUnsentIsEchoedAsSent:
    @pytest.mark.parametrize("reason", ["cache_hit", "bypassed", "security_block"])
    def test_a_short_circuit_echoes_an_explicit_null(self, reason):
        """A cache hit, a bypass and a content-filter block never reach a provider. Saying
        so explicitly stops an artefact from implying a prompt was sent when none was."""
        ctx = _ctx({"x_echo_prompt": True})
        response = {}
        main._attach_sent_prompt(ctx, response, not_sent_reason=reason)
        assert response["_token_opt"]["sent"] is None
        assert response["_token_opt"]["sent_skipped_reason"] == reason

    def test_a_short_circuit_adds_nothing_when_the_echo_was_not_asked_for(self):
        ctx = _ctx({})
        response = {}
        main._attach_sent_prompt(ctx, response, not_sent_reason="cache_hit")
        assert response == {}


class TestTheEchoDoesNotContaminateTheCacheEntry:
    def test_it_is_attached_after_the_response_pipeline_has_run(self):
        """G05 stores the response inside `pipeline.process_response`; the echo is attached
        in `_served_response`, which runs after. A request-specific field must never be
        baked into an entry another request will be served."""
        import inspect
        src = inspect.getsource(main._served_response)
        assert "_attach_sent_prompt" in src
        served_at = inspect.getsourcelines(main._served_response)[1]
        # `_served_response` is a finaliser: it is called with an already-processed
        # response dict, so nothing inside it can reach back into the cache store.
        assert "store_response" not in src and "process_response" not in src
        assert served_at > 0

    def test_the_streaming_path_returns_before_the_echo_is_attached(self):
        """Streams skip the response pipeline entirely and carry no `_token_opt` at all, so
        they carry no echo either. Documented, and pinned here so a future change to
        `_stream_response` cannot silently invalidate the doc."""
        import inspect
        src = inspect.getsource(main._stream_response)
        assert "_attach_sent_prompt" not in src
        assert "capture_sent_prompt" not in src


class TestTheCaptureFollowsTheCallThatServed:
    """Source-inspected on purpose: the property is WHERE the capture sits relative to the
    provider call, and no unit-level fake can assert an ordering inside a coroutine that
    only runs against a live provider. Same technique the auto-exec sink tests use.
    """

    SRC = (
        pathlib.Path(__file__).resolve().parents[2] / "src" / "proxy" / "main.py"
    ).read_text(encoding="utf-8")

    @staticmethod
    def _stream_span():
        """Line range of `_stream_response`, the one deliberately un-echoed path."""
        import inspect
        src, start = inspect.getsourcelines(main._stream_response)
        return start - 1, start - 1 + len(src)

    def test_the_capture_immediately_precedes_every_non_streaming_provider_call(self):
        """Every `litellm.acompletion` on a NON-streaming path must be preceded by a
        capture, or an echo would describe a call other than the one that served.

        Streaming is the single exception, and it is asserted as such below rather than
        quietly excluded — a stream returns before response metadata is attached at all.
        """
        lines = self.SRC.splitlines()
        lo, hi = self._stream_span()
        call_lines = [i for i, ln in enumerate(lines) if "await litellm.acompletion(" in ln]
        assert call_lines, "no provider call found — the anchor for this test moved"
        checked = 0
        for i in call_lines:
            if lo <= i < hi:
                continue
            window = "\n".join(lines[max(0, i - 12):i])
            assert "capture_sent_prompt(" in window, (
                f"the provider call at line {i + 1} is not preceded by capture_sent_prompt — "
                f"an echo would describe a different call than the one that served"
            )
            checked += 1
        assert checked >= 2, "expected at least the primary and the failover call sites"

    def test_the_streaming_call_site_is_the_only_uncaptured_one(self):
        """Pins the documented limitation. If streaming ever gains `_token_opt`, this test
        is the thing that says the doc now needs updating."""
        lines = self.SRC.splitlines()
        lo, hi = self._stream_span()
        streaming_calls = [
            i for i, ln in enumerate(lines)
            if "await litellm.acompletion(" in ln and lo <= i < hi
        ]
        assert streaming_calls, "the streaming provider call moved — re-check the doc claim"
        for i in streaming_calls:
            window = "\n".join(lines[max(0, i - 12):i])
            assert "capture_sent_prompt(" not in window

    def test_the_primary_capture_uses_the_hygienic_outgoing_params(self):
        assert "capture_sent_prompt(ctx, ctx.messages, outgoing_params, _call_model)" in self.SRC

    def test_the_failover_target_captures_its_own_sanitised_payload(self):
        """A failover sends DIFFERENT bytes — sanitised messages/tools, another model — so
        it must overwrite the primary's snapshot rather than inherit it."""
        assert (
            "capture_sent_prompt(ctx, _failover_messages, _failover_outgoing, call_model)"
            in self.SRC
        )

    def test_no_capture_call_passes_the_adapter_connection_kwargs(self):
        """`_call_kwargs` / `call_kwargs` hold the provider credential. They must never be
        an argument to the capture, in any call site, present or future."""
        for line in self.SRC.splitlines():
            if "capture_sent_prompt(" in line and "def capture_sent_prompt" not in line:
                assert "call_kwargs" not in line, line.strip()
