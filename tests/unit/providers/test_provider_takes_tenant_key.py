"""Which providers can be served on a key the BYOK seam resolves.

A provider whose adapter signs with credentials the platform holds (Bedrock's AWS SigV4, a
providers[] entry with `requires_api_key: false` such as Vertex ADC) ignores any key passed
to it. Strict BYOK uses this to refuse such a provider rather than accept a stored "key"
that the call never uses and bill the platform's account.
"""
from pathlib import Path

import pytest
import yaml

from providers import provider_takes_tenant_key

TEMPLATE = Path(__file__).resolve().parents[3] / "config" / "config.yaml.template"
SHIPPED = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))["providers"]


@pytest.mark.parametrize("name", [p["name"] for p in SHIPPED])
def test_every_shipped_provider_but_bedrock_takes_a_key(name):
    assert provider_takes_tenant_key(name, SHIPPED) is (name != "bedrock")


def test_an_entry_that_needs_no_key_takes_none():
    assert provider_takes_tenant_key(
        "vertex", [{"name": "vertex", "adapter": "generic", "requires_api_key": False}]) is False


def test_bedrock_needs_no_entry_to_be_recognised():
    assert provider_takes_tenant_key("bedrock", []) is False


def test_an_unknown_provider_is_assumed_to_take_a_key():
    assert provider_takes_tenant_key("no-such-provider", []) is True
