"""G18 metric labels and the JSONL export stay BOUNDED against caller-chosen values.

Regression for the 2026-09-18 cardinality finding (backlog #92): x_team/x_feature were
used verbatim as labels on 7 counters, and workflow_id labelled a gauge — a distinct,
never-evicted series per value. And the local export path was built from the raw
workflow_id, so `..` escaped the export root.
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from middleware import RequestContext
from middleware.g18_observability import (
    COMPLETION_TOKENS, COST_USD, G18Observability, PROMPT_TOKENS, REQUESTS_TOTAL,
    WORKFLOW_TURN_COUNT, _bounded_label,
)
from savings.models import SavingsRecord

_RESPONSE = {"id": "r", "choices": [{"message": {"role": "assistant", "content": "ok"},
                                     "finish_reason": "stop"}],
             "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6}}


def _ctx(tenant, params, team="default", config=None):
    ctx = RequestContext(
        request_id=f"r-{uuid.uuid4().hex[:8]}", user_id=tenant, original_messages=[],
        messages=[{"role": "user", "content": "hi"}], model="gpt-4o-mini",
        routed_model="gpt-4o-mini", params=dict(params),
        config=config or {"groups": {"G18_observability": {"enabled": True}}},
        savings=SavingsRecord(request_id="r", user_id=tenant,
                              timestamp=datetime.now(timezone.utc),
                              model_requested="gpt-4o-mini", routed_model="gpt-4o-mini",
                              baseline_tokens=1),
        tenant_id=tenant, redis_prefix=f"t:{tenant}:")
    ctx.team = team
    return ctx


def _series(metric, tenant):
    idx = list(metric._labelnames).index("tenant_id")
    return {k for k in metric._metrics if k[idx] == tenant}


async def _record(ctxs):
    redis = AsyncMock()
    redis.get.return_value = None
    with patch("middleware.langfuse_tracing.finish_trace"), \
            patch("middleware.g18_observability._get_redis", return_value=redis):
        for c in ctxs:
            await G18Observability().record(c, dict(_RESPONSE))


# ── _bounded_label unit ──────────────────────────────────────────────────────

def test_bounded_label_no_allowlist_is_passthrough():
    assert _bounded_label("anything", None) == "anything"


def test_bounded_label_folds_unlisted_to_other():
    assert _bounded_label("finance", {"legal"}) == "other"
    assert _bounded_label("legal", {"legal"}) == "legal"
    assert _bounded_label("default", {"legal"}) == "default"   # always kept


# ── Series growth ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_team_is_the_trusted_value_not_the_raw_header():
    tenant = f"t-{uuid.uuid4().hex[:8]}"
    # A caller sets x_team on every request; team stays "default" (non-gateway → ctx.team).
    await _record([_ctx(tenant, {"x_team": f"spoof-{i}", "x_feature": "default"}, team="default")
                   for i in range(25)])
    assert len(_series(REQUESTS_TOTAL, tenant)) == 1


@pytest.mark.asyncio
async def test_feature_allowlist_folds_unknown_to_other():
    tenant = f"t-{uuid.uuid4().hex[:8]}"
    cfg = {"groups": {"G18_observability": {"enabled": True,
                                            "label_values": {"feature": ["billing"]}}}}
    await _record([_ctx(tenant, {"x_feature": f"f-{i}"}, config=cfg) for i in range(25)]
                  + [_ctx(tenant, {"x_feature": "billing"}, config=cfg)])
    features = {k[list(REQUESTS_TOTAL._labelnames).index("feature")]
                for k in _series(REQUESTS_TOTAL, tenant)}
    assert features == {"other", "billing"}


@pytest.mark.asyncio
async def test_gateway_teams_are_bounded_by_allowlist():
    tenant = f"t-{uuid.uuid4().hex[:8]}"
    cfg = {"groups": {"G18_observability": {"enabled": True,
                                            "label_values": {"team": ["alpha"]}}}}
    await _record([_ctx(tenant, {}, team=f"team-{i}", config=cfg) for i in range(25)]
                  + [_ctx(tenant, {}, team="alpha", config=cfg)])
    teams = {k[list(REQUESTS_TOTAL._labelnames).index("team")]
             for k in _series(REQUESTS_TOTAL, tenant)}
    assert teams == {"other", "alpha"}
    for m in (PROMPT_TOKENS, COMPLETION_TOKENS, COST_USD):
        assert len(_series(m, tenant)) <= 2


@pytest.mark.asyncio
async def test_workflow_id_is_not_a_label():
    tenant = f"t-{uuid.uuid4().hex[:8]}"
    await _record([_ctx(tenant, {"x_workflow_id": f"wf-{i}",
                                 "_token_budget": {"workflow_turn": 2}}) for i in range(25)])
    # One histogram series per tenant, regardless of how many workflow ids were seen.
    assert len(_series(WORKFLOW_TURN_COUNT, tenant)) == 1


# ── Export containment ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_export_path_cannot_escape_root(tmp_path, monkeypatch):
    root = tmp_path / "export-root"
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(root))
    ctx = _ctx("acme", {"x_workflow_id": "../../../escaped"})
    await G18Observability()._export_jsonl(ctx, {"k": "v"})
    escaped = [p for p in tmp_path.rglob("*.json")
               if root.resolve() not in p.resolve().parents]
    assert not escaped, f"export escaped its root: {[str(p) for p in escaped]}"


@pytest.mark.asyncio
async def test_export_writes_a_safe_workflow_id(tmp_path, monkeypatch):
    root = tmp_path / "export-root"
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("STORAGE_LOCAL_PATH", str(root))
    ctx = _ctx("acme", {"x_workflow_id": "wf-legit"})
    await G18Observability()._export_jsonl(ctx, {"k": "v"})
    assert list(root.rglob("*.json")), "a safe workflow id should still be written"
