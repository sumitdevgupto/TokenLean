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
