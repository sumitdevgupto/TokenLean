"""OpenLLMetry start-up must not warn about a package nobody asked for.

2026-09-18. `_init_openllmetry` imported `traceloop` before it read `openllmetry_enabled`, so
every start of every image we build (traceloop-sdk has never been a dependency) logged
"OpenLLMetry init failed: No module named 'traceloop'" - with the feature OFF in every shipped
config. The warning was then taken to be why Langfuse traces were missing. It was not:
OpenLLMetry is OTLP auto-instrumentation, and Langfuse tracing is a separate code path.
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "proxy")))

import builtins
from unittest.mock import MagicMock, patch

import main


def _cfg(enabled):
    return {"groups": {"G18_observability": {"openllmetry_enabled": enabled}}}


def _block_traceloop_import():
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("traceloop"):
            raise ImportError("No module named 'traceloop'")
        return real_import(name, *args, **kwargs)
    return patch("builtins.__import__", side_effect=fake_import)


def test_disabled_is_silent_and_never_imports_the_sdk():
    with _block_traceloop_import() as imp, patch.object(main.logger, "warning") as warning:
        main._init_openllmetry(_cfg(False))
    warning.assert_not_called()
    assert not any(str(c.args[0]).startswith("traceloop") for c in imp.call_args_list), \
        "the SDK must not even be imported when the feature is off"


def test_absent_block_counts_as_disabled():
    with _block_traceloop_import(), patch.object(main.logger, "warning") as warning:
        main._init_openllmetry({})
        main._init_openllmetry({"groups": None})
    warning.assert_not_called()


def test_enabled_without_the_package_says_what_to_do():
    with _block_traceloop_import(), patch.object(main.logger, "warning") as warning:
        main._init_openllmetry(_cfg(True))
    warning.assert_called_once()
    msg = warning.call_args.args[0]
    assert "traceloop-sdk" in msg and "openllmetry_enabled" in msg
    assert "Langfuse" in msg, "the message must not let this be mistaken for the Langfuse path"


def test_enabled_with_the_package_initialises_it():
    fake = MagicMock()
    with patch.dict(sys.modules, {"traceloop": MagicMock(), "traceloop.sdk": MagicMock(Traceloop=fake)}):
        main._init_openllmetry({"groups": {"G18_observability": {
            "openllmetry_enabled": True, "openllmetry_endpoint": "http://otel:4318"}}})
    fake.init.assert_called_once_with(app_name="token-optimisation-proxy",
                                      api_endpoint="http://otel:4318")
