"""Which X-* request headers become ctx.params and which only reach the G06 routing rules.

ctx.params is persisted (G13 writes it to the Redis batch stream), so only the headers
TokenLean reads are copied into it. Every other X-* header — an infrastructure one such as
X-Cloud-Trace-Context, a caller's own routing hint, a future internal header — reaches only
the routing rules, through ctx.routing_headers, which nothing persists.
"""
import asyncio
import re
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from middleware import RequestContext
from protocols.base import REQUEST_HEADER_PARAMS, header_params, routing_header_params
from savings.models import SavingsRecord

HEADERS = {
    "X-Template-ID": "t1", "x-session-id": "s1",                      # TokenLean headers
    "X-Cloud-Trace-Context": "abc/1;o=1", "X-Request-ID": "r1",       # infrastructure
    "X-Customer-Tier": "gold",                                        # a caller's routing hint
    "X-Api-Key": "tok-1", "X-User-ID": "u1", "X-Scenario-Tag": "s",   # read from the raw request
    "Content-Type": "application/json",
}


class TestHeaderParams:
    def test_only_the_headers_tokenlean_reads_become_params(self):
        assert header_params(HEADERS) == {"x_template_id": "t1", "x_session_id": "s1"}

    def test_the_routing_view_has_every_x_header_but_credentials_and_identity(self):
        assert routing_header_params(HEADERS) == {
            "x_template_id": "t1", "x_session_id": "s1", "x_cloud_trace_context": "abc/1;o=1",
            "x_request_id": "r1", "x_customer_tier": "gold"}

    def test_no_headers_give_nothing(self):
        assert header_params(None) == {} and routing_header_params("not headers") == {}


_SRC = Path(__file__).resolve().parents[3] / "src" / "proxy"
_READS = re.compile(r"params(?:\.get\(|\[)\s*[\"'](x_[a-z0-9_]+)[\"']")
# x_ params some code reads that a header may NOT set:
NOT_FROM_HEADERS = {
    "x_user_id": "identity: X-User-ID is resolved by the auth layer against the key's "
                 "allow-list, never copied from the header",
}


def test_every_x_param_the_code_reads_is_admitted_from_its_header_or_exempted():
    """A new reader of an x_ param must decide whether its header is admitted; otherwise the
    header would be silently dropped from ctx.params."""
    read = {m.group(1) for path in _SRC.rglob("*.py")
            for m in _READS.finditer(path.read_text(encoding="utf-8"))}
    assert read, "the source scan found nothing — the pattern is stale"
    unclassified = read - REQUEST_HEADER_PARAMS - set(NOT_FROM_HEADERS)
    assert not unclassified, f"read but not admitted from a header: {sorted(unclassified)}"
    assert not set(NOT_FROM_HEADERS) & REQUEST_HEADER_PARAMS


def _ctx():
    savings = SavingsRecord(request_id="r", user_id="u", timestamp=datetime.now(timezone.utc),
                            model_requested="gpt-4o", routed_model="gpt-4o", baseline_tokens=5)
    return RequestContext(request_id="r", user_id="u", original_messages=[], messages=[],
                          model="gpt-4o", routed_model="gpt-4o", params={},
                          config={"groups": {}}, savings=savings)


def test_the_pipeline_gives_the_routing_rules_every_x_header():
    ctx = _ctx()
    with patch("tenancy.config.TenantConfigLoader.load", new_callable=AsyncMock):
        from middleware.pipeline import OptimisationPipeline
        pipeline = OptimisationPipeline()
        with patch.object(pipeline.g00, "process_request", new_callable=AsyncMock) as g00:
            ctx.bypassed = True               # stop right after G00
            g00.return_value = ctx
            asyncio.run(pipeline.process_request(ctx, request_headers=HEADERS))
    assert ctx.routing_headers == routing_header_params(HEADERS)
    assert "x_customer_tier" not in ctx.params


def test_a_rule_matches_a_custom_header_that_is_not_a_param():
    from middleware.g06_rules import match_for_ctx
    ctx = _ctx()
    ctx.messages = [{"role": "user", "content": "hello"}]
    ctx.routing_headers = {"x_customer_tier": "gold"}
    rule = {"id": "gold", "match": {"params": {"x_customer_tier": ["gold"]}},
            "action": {"tier": "complex"}}
    matched = match_for_ctx(ctx, {"rules": [rule]})
    assert matched is not None and matched["id"] == "gold"


def test_a_header_wins_over_a_body_field_of_the_same_name():
    # As when headers were copied into params after the body.
    from middleware.g06_rules import match_for_ctx
    ctx = _ctx()
    ctx.messages = [{"role": "user", "content": "hello"}]
    ctx.params = {"x_team": "body"}
    ctx.routing_headers = {"x_team": "header"}
    rule = {"id": "h", "match": {"params": {"x_team": ["header"]}}, "action": {"tier": "simple"}}
    matched = match_for_ctx(ctx, {"rules": [rule]})
    assert matched is not None and matched["id"] == "h"
