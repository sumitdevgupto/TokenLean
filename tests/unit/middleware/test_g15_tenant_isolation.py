"""G15's server-side dispatch must scope the CCR store to the calling tenant.

`dispatch_mcp_tool` takes a `prefix` that scopes both the Redis key and the in-process
fallback store. G28's own call site passes `ctx.redis_prefix`; G15's did not, so it fell
to the `""` default — and the retrieve path's scan is

    if key.startswith(prefix) and key[len(prefix):].startswith(sha_prefix)

which with an empty prefix matches EVERY key. An 8-char `[CCR:...]` reference therefore
resolved to any tenant's stored block, in the one of the two paths that ships enabled by
default (`G15_server_compute.enabled: true` + `headroom_mcp_server: true`; G28 is
default-off). The comment on the scan already declared the invariant this violated.

Reachable whenever two tenants share a proxy process and a model emits the CCR tool
calls — which the G32 eligibility gate only stops for a tenant that has written a policy
(the shipped policy is empty + `default: allow`, i.e. a deliberate no-op).
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

import json
from datetime import datetime, timezone

import pytest

from middleware import RequestContext
from middleware.g15_server_compute import G15ServerCompute
from middleware.g28_ccr import _local_store
from savings.models import SavingsRecord

SECRET = "ACME CONFIDENTIAL: Q3 revenue 4.2M, acquisition closes in March"


@pytest.fixture(autouse=True)
def _clean_store():
    _local_store.clear()
    yield
    _local_store.clear()


def _ctx(tenant_id):
    return RequestContext(
        request_id=f"req-{tenant_id}", user_id="u", original_messages=[], messages=[],
        model="gpt-4o-mini", routed_model="gpt-4o-mini", params={},
        config={"groups": {"G15_server_compute": {"enabled": True, "headroom_mcp_server": True},
                           "G28_ccr": {"ttl_seconds": 60}}},
        tenant_id=tenant_id, redis_prefix=f"t:{tenant_id}:",
        # These tests are about the STORE's tenant scoping, which only matters for a
        # dispatch that is allowed to happen at all. Authorization is a separate
        # guarantee, covered in test_g15_dispatch_authz.py; set the flag the proxy sets
        # when it advertises the CCR tools so the dispatch proceeds and the prefix
        # behaviour is what is actually under test.
        ccr_tools_injected=True,
        savings=SavingsRecord(request_id=f"req-{tenant_id}", user_id="u",
                              timestamp=datetime.now(timezone.utc),
                              model_requested="gpt-4o-mini", routed_model="gpt-4o-mini",
                              baseline_tokens=10),
    )


async def _dispatch(ctx, name, args):
    response = {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
        {"id": "c1", "type": "function",
         "function": {"name": name, "arguments": json.dumps(args)}}]}}]}
    out = await G15ServerCompute().process_response(ctx, response)
    return out["choices"][0]["message"]["tool_calls"][0]["function"]["result"]


class TestCcrStoreIsTenantScoped:
    async def test_another_tenant_cannot_retrieve_a_stored_block(self):
        stored = await _dispatch(_ctx("acme"), "headroom_compress", {"text": SECRET})
        leaked = await _dispatch(_ctx("globex"), "headroom_retrieve", {"ref": stored["ref"]})
        assert leaked.get("text") != SECRET, (
            "cross-tenant read: globex retrieved acme's stored block via an 8-char reference"
        )
        assert "error" in leaked

    async def test_the_owning_tenant_still_retrieves_its_own_block(self):
        """The isolation fix must not break the feature it is scoping."""
        ctx = _ctx("acme")
        stored = await _dispatch(ctx, "headroom_compress", {"text": SECRET})
        assert (await _dispatch(ctx, "headroom_retrieve", {"ref": stored["ref"]}))["text"] == SECRET

    async def test_store_keys_carry_the_tenant_prefix(self):
        await _dispatch(_ctx("acme"), "headroom_compress", {"text": SECRET})
        assert all(k.startswith("t:acme:") for k in _local_store), (
            f"unprefixed keys are readable by every tenant: {list(_local_store)}"
        )

    async def test_two_tenants_storing_identical_text_stay_separate(self):
        """Same text = same SHA. Without the prefix these collide into one key, so the
        stores are only actually separate if the prefix is part of the key."""
        await _dispatch(_ctx("acme"), "headroom_compress", {"text": SECRET})
        await _dispatch(_ctx("globex"), "headroom_compress", {"text": SECRET})
        assert len(_local_store) == 2, f"tenant stores collided: {list(_local_store)}"
