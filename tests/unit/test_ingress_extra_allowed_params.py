"""ingress.extra_allowed_params: the operator escape hatch for the OpenAI ingress
allowlist (main._ingress_extra_allowed_params). Global config only; malformed values
admit nothing extra."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "proxy")))

import pytest

from main import _ingress_extra_allowed_params


def test_missing_section_admits_nothing_extra():
    assert _ingress_extra_allowed_params({}) == ()
    assert _ingress_extra_allowed_params({"ingress": None}) == ()


def test_names_are_read_from_config():
    cfg = {"ingress": {"extra_allowed_params": ["metadata", "vendor_hint"]}}
    assert _ingress_extra_allowed_params(cfg) == ("metadata", "vendor_hint")


@pytest.mark.parametrize("raw", ["metadata", {"metadata": 1}, None, 5])
def test_malformed_value_admits_nothing_extra(raw):
    assert _ingress_extra_allowed_params({"ingress": {"extra_allowed_params": raw}}) == ()


def test_non_string_and_empty_entries_are_ignored():
    cfg = {"ingress": {"extra_allowed_params": ["ok", 3, None, ""]}}
    assert _ingress_extra_allowed_params(cfg) == ("ok",)
