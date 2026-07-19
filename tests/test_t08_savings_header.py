"""T08 — X-Savings-USD header and cost_savings_usd response body field."""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src", "proxy")))

import pytest


_REQUEST = {
    "model": "gpt-4o-mini",
    "messages": [{"role": "user", "content": "What is the capital of France?"}],
}


@pytest.mark.asyncio
class TestSavingsHeader:
    async def test_x_savings_usd_header_present(self, client):
        response = await client.post(
            "/v1/chat/completions",
            json=_REQUEST,
            headers={"Authorization": "Bearer test-key"},
        )
        assert response.status_code == 200
        assert "x-savings-usd" in response.headers

    async def test_x_savings_usd_is_numeric(self, client):
        response = await client.post(
            "/v1/chat/completions",
            json=_REQUEST,
            headers={"Authorization": "Bearer test-key"},
        )
        assert response.status_code == 200
        value = response.headers["x-savings-usd"]
        float(value)  # raises ValueError if not a valid float

    async def test_cost_saving_usd_in_response_body(self, client):
        response = await client.post(
            "/v1/chat/completions",
            json=_REQUEST,
            headers={"Authorization": "Bearer test-key"},
        )
        assert response.status_code == 200
        body = response.json()
        assert "_token_opt" in body
        assert "cost_saving_usd" in body["_token_opt"]

    async def test_header_format_six_decimal_places(self, client):
        response = await client.post(
            "/v1/chat/completions",
            json=_REQUEST,
            headers={"Authorization": "Bearer test-key"},
        )
        assert response.status_code == 200
        value = response.headers["x-savings-usd"]
        # Must be a decimal with up to 6 decimal places
        parts = value.split(".")
        assert len(parts) == 2
        assert len(parts[1]) <= 6

    async def test_header_matches_body_field(self, client):
        """x-savings-usd header must equal _token_opt.cost_saving_usd in body."""
        response = await client.post(
            "/v1/chat/completions",
            json=_REQUEST,
            headers={"Authorization": "Bearer test-key"},
        )
        assert response.status_code == 200
        header_val = float(response.headers["x-savings-usd"])
        body_val = response.json()["_token_opt"]["cost_saving_usd"]
        assert abs(header_val - body_val) < 1e-8


class TestSavingsHeadersShortCircuit:
    """Regression: the x-tokenlean-* headers must be BUILT for the cache-hit / bypass /
    content-filter short-circuits too. Those served-200 paths previously returned a
    header-less JSONResponse — silently breaking the advertised always-on attribution
    (esp. x-tokenlean-cache on the highest-volume cache-hit traffic) and failing the
    run-readiness header gate. This tests the exact header payload those paths now attach."""

    def _ctx(self, *, cache_hit=False, cache_level=None):
        import datetime
        from savings.models import SavingsRecord
        from middleware import RequestContext
        sav = SavingsRecord(
            request_id="r1", user_id="u",
            timestamp=datetime.datetime(2026, 7, 19, tzinfo=datetime.timezone.utc),
            model_requested="gpt-4o-mini", routed_model="gpt-4o-mini", baseline_tokens=100)
        sav.cache_hit = cache_hit
        sav.cache_level = cache_level
        sav.final_tokens_sent = 0 if cache_hit else 60
        return RequestContext(
            request_id="r1", user_id="u", original_messages=[], messages=[],
            model="gpt-4o-mini", routed_model="gpt-4o-mini", params={}, config={}, savings=sav)

    def test_cache_hit_builds_hit_header(self):
        import time
        from main import _savings_headers
        h = _savings_headers(self._ctx(cache_hit=True, cache_level="L2"), time.time() - 0.01)
        assert h["x-tokenlean-cache"] == "hit:L2"       # the fix: present + reflects the hit
        assert "x-savings-usd" in h
        assert "x-tokenlean-cost-saved-usd" in h
        assert "x-tokenlean-request-id" in h
        assert h["x-tokenlean-cost-saved-usd"] == h["x-savings-usd"]  # alias holds

    def test_miss_builds_miss_header(self):
        import time
        from main import _savings_headers
        h = _savings_headers(self._ctx(cache_hit=False), time.time() - 0.01)
        assert h["x-tokenlean-cache"] == "miss"


@pytest.mark.asyncio
class TestTokenLeanHeaderSuite:
    """The x-tokenlean-* machine-readable per-call header family (always-on)."""

    async def test_core_headers_present(self, client):
        response = await client.post(
            "/v1/chat/completions",
            json=_REQUEST,
            headers={"Authorization": "Bearer test-key"},
        )
        assert response.status_code == 200
        for h in (
            "x-tokenlean-request-id",
            "x-tokenlean-routed-model",
            "x-tokenlean-cache",
            "x-tokenlean-tokens-saved",
            "x-tokenlean-cost-saved-usd",
            "x-tokenlean-latency-ms",
        ):
            assert h in response.headers, f"missing {h}"

    async def test_cache_header_hit_or_miss(self, client):
        response = await client.post(
            "/v1/chat/completions",
            json=_REQUEST,
            headers={"Authorization": "Bearer test-key"},
        )
        assert response.status_code == 200
        cache = response.headers["x-tokenlean-cache"]
        assert cache == "miss" or cache == "hit" or cache.startswith("hit:")

    async def test_numeric_headers_parse(self, client):
        response = await client.post(
            "/v1/chat/completions",
            json=_REQUEST,
            headers={"Authorization": "Bearer test-key"},
        )
        assert response.status_code == 200
        int(response.headers["x-tokenlean-tokens-saved"])
        float(response.headers["x-tokenlean-cost-saved-usd"])
        float(response.headers["x-tokenlean-latency-ms"])
        if "x-tokenlean-pct-saved" in response.headers:
            float(response.headers["x-tokenlean-pct-saved"])

    async def test_cost_header_aliases_x_savings_usd(self, client):
        """x-tokenlean-cost-saved-usd must equal the back-compat x-savings-usd alias."""
        response = await client.post(
            "/v1/chat/completions",
            json=_REQUEST,
            headers={"Authorization": "Bearer test-key"},
        )
        assert response.status_code == 200
        assert (
            response.headers["x-tokenlean-cost-saved-usd"]
            == response.headers["x-savings-usd"]
        )

    async def test_routed_model_matches_body(self, client):
        response = await client.post(
            "/v1/chat/completions",
            json=_REQUEST,
            headers={"Authorization": "Bearer test-key"},
        )
        assert response.status_code == 200
        meta = response.json()["_token_opt"]
        expected = meta.get("routed_model") or meta.get("model_requested")
        if expected is not None:
            assert response.headers["x-tokenlean-routed-model"] == str(expected)
