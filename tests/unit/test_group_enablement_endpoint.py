"""A deployment must be able to say which groups are ON without the commercial portal.

Why this exists (2026-09-06). `run_readiness` gates every deploy — `run-readiness.sh
--quick` runs on each deploy path and a NOT-READY verdict blocks it. Its only signal for
"is this group enabled here" was the COMMERCIAL `GET /portal/groups`, which does not exist
on the free image and 404s. `fetch_enabled_map` then returned `{}` and readiness assumed
every group enabled.

That is not a harmless default. A disabled group's stage still RUNS — every group
early-returns on its own `enabled` check, inside the timed stage — so its stage-duration
metric moves either way, and that metric is what `assert_group_fired` accepts as proof of
firing for a group with no other observable. G09 ships `enabled: false` in
`config.yaml.template` and had been collecting a readiness ✓ on every OSS deployment.

`GET /v1/groups` closes it in OSS core. The tests below pin the two properties that make
it trustworthy: it reports what a REQUEST would see (same loader, same operator overlay),
and it reports nothing else.
"""
import sys
from pathlib import Path

import pytest

_PROXY = Path(__file__).resolve().parents[2] / "src" / "proxy"
if str(_PROXY) not in sys.path:
    sys.path.insert(0, str(_PROXY))

from middleware.pipeline import OptimisationPipeline  # noqa: E402


@pytest.fixture
def pipeline():
    return OptimisationPipeline()


def _cfg(monkeypatch, config):
    """Point the pipeline's config read at a literal dict."""
    import middleware.pipeline as mod
    monkeypatch.setattr(mod, "get_config", lambda: config)


class TestItReportsWhatARequestWouldSee:
    async def test_a_disabled_group_reads_false(self, pipeline, monkeypatch):
        _cfg(monkeypatch, {"groups": {"G9_context_schema": {"enabled": False},
                                      "G1_compression": {"enabled": True}}})
        out = await pipeline.effective_group_enablement()
        assert out["G9_context_schema"] is False
        assert out["G1_compression"] is True

    async def test_the_operator_per_tenant_overlay_wins(self, pipeline, monkeypatch):
        """`tenants.<id>.groups.<key>` is the operator's per-tenant path. Since the
        2026-09-06 review fix it is merged once at pipeline entry (apply_operator_overlay),
        and this endpoint runs the same merge — so it must report the tenant's value."""
        _cfg(monkeypatch, {
            "groups": {"G1_compression": {"enabled": True}},
            "tenants": {"NOVA-STG-01": {"groups": {"G1_compression": {"enabled": False}}}},
        })
        assert (await pipeline.effective_group_enablement("NOVA-STG-01"))["G1_compression"] is False
        assert (await pipeline.effective_group_enablement("other"))["G1_compression"] is True

    async def test_g00_is_answerable_even_though_it_lives_outside_groups(self, pipeline, monkeypatch):
        """G00 reads the top-level `rate_limit` block. Readiness scores it, so an
        endpoint that only walked `groups.*` would leave it permanently unknowable."""
        _cfg(monkeypatch, {"groups": {}, "rate_limit": {"enabled": False}})
        assert (await pipeline.effective_group_enablement())["rate_limit"] is False

    async def test_a_rate_limit_block_without_enabled_is_off_like_g00_says(self, pipeline, monkeypatch):
        """Review of 9336dfa: this defaulted to True while G00 itself defaults to False
        (`rl_cfg.get("enabled", False)`). A `rate_limit:` block with no `enabled` key
        would have been reported on while G00 was off — handing readiness the exact
        false tick the endpoint exists to close."""
        _cfg(monkeypatch, {"groups": {}, "rate_limit": {"default": {"requests_per_minute": 60}}})
        assert (await pipeline.effective_group_enablement())["rate_limit"] is False

    async def test_the_answer_matches_a_group_that_reads_config_directly(self, pipeline, monkeypatch):
        """Review of 9336dfa: the first cut applied the operator overlay per key through
        resolve_group_config, which only 9 of 32 groups call. G19 reads
        ctx.config["groups"]["G19_headroom"] directly, so the endpoint said `disabled`
        while G19 kept running. Now both go through the same merge at pipeline entry."""
        from middleware import apply_operator_overlay

        cfg = {"groups": {"G19_headroom": {"enabled": True}},
               "tenants": {"NOVA-STG-01": {"groups": {"G19_headroom": {"enabled": False}}}}}
        _cfg(monkeypatch, cfg)
        endpoint_view = (await pipeline.effective_group_enablement("NOVA-STG-01"))["G19_headroom"]
        request_view = apply_operator_overlay(cfg, "NOVA-STG-01")["groups"]["G19_headroom"]["enabled"]
        assert endpoint_view is request_view is False

    async def test_a_key_with_no_enabled_field_reads_none_not_false(self, pipeline, monkeypatch):
        """Unknown must not masquerade as disabled — readiness treats `False` as "do not
        score this group", so a wrong `False` would silently drop coverage."""
        _cfg(monkeypatch, {"groups": {"context_editing": {"trigger_tokens": 100}}})
        assert (await pipeline.effective_group_enablement())["context_editing"] is None

    async def test_a_malformed_group_node_does_not_raise(self, pipeline, monkeypatch):
        """`config.yaml` is operator-edited YAML; a mis-indent can make any node a string.
        `resolve_group_config` already degrades rather than raising, and this endpoint must
        inherit that — a read-only probe has no business 500ing on a typo."""
        _cfg(monkeypatch, {"groups": {"G1_compression": "oops"}})
        assert (await pipeline.effective_group_enablement())["G1_compression"] is None


class TestItReportsNothingElse:
    async def test_no_knob_values_leak(self, pipeline, monkeypatch):
        """The endpoint authenticates with a tenant key, so it must not become a read
        channel for infra config. Booleans only, by construction."""
        _cfg(monkeypatch, {"groups": {"G1_compression": {
            "enabled": True,
            "sidecar_url": "http://llmlingua-svc:8080/compress",
            "kompress_model": "microsoft/Kompress-v2-base",
            "compression_ratio_target": 0.5,
        }}})
        out = await pipeline.effective_group_enablement()
        assert out == {"G1_compression": True}
        assert all(v is None or isinstance(v, bool) for v in out.values())


class TestTheTenantOverlayIsBestEffort:
    async def test_a_config_store_failure_degrades_instead_of_500ing(self, pipeline, monkeypatch):
        """The Postgres overlay is the portal-written path. If it is down, the base config
        is still a better answer than an error — and an error here would block a deploy on
        a signal that is meant to make deploys safer."""
        _cfg(monkeypatch, {"groups": {"G1_compression": {"enabled": True}}})

        async def _boom(_ctx):
            raise RuntimeError("config store unavailable")

        monkeypatch.setattr(pipeline._tenant_config_loader, "load", _boom)
        assert (await pipeline.effective_group_enablement())["G1_compression"] is True


class TestTheRouteIsWiredAndAuthenticated:
    def test_the_route_exists_on_the_oss_app(self):
        import main
        paths = {r.path for r in main.app.routes}
        assert "/v1/groups" in paths, (
            "readiness reads this to tell 'disabled' from 'never fired'; without it the "
            "group section of the deploy gate is blind on every OSS deployment"
        )

    def test_an_admin_key_may_ask_about_another_tenant_but_a_plain_key_may_not(self, monkeypatch):
        """Review of 9336dfa: readiness sends X-Tenant-ID and the DSR probes honour it for
        admin keys (resolve_tenant), but the endpoint read the key's tenant only — so an
        admin's `--tenant-id OTHER` run scored OTHER's traffic against the admin tenant's
        enable map. The endpoint now resolves the tenant exactly as traffic does."""
        from fastapi.testclient import TestClient

        import main

        async def _auth_as(md):
            return "u@x.test", "tok-x", md

        seen = []

        async def _record(tenant_id="default"):
            seen.append(tenant_id)
            return {}

        monkeypatch.setattr(main._pipeline, "effective_group_enablement", _record)
        client = TestClient(main.app)

        monkeypatch.setattr(main, "_authenticate",
                            lambda request, proto=None: _auth_as({"tenant_id": "ADMIN-T", "admin": True}))
        r = client.get("/v1/groups", headers={"Authorization": "Bearer tok-x",
                                              "X-Tenant-ID": "NOVA-STG-01"})
        assert r.status_code == 200 and r.json()["tenant_id"] == "NOVA-STG-01"

        monkeypatch.setattr(main, "_authenticate",
                            lambda request, proto=None: _auth_as({"tenant_id": "SHOP-STG-01", "admin": False}))
        r = client.get("/v1/groups", headers={"Authorization": "Bearer tok-x",
                                              "X-Tenant-ID": "NOVA-STG-01"})
        assert r.status_code == 200 and r.json()["tenant_id"] == "SHOP-STG-01", (
            "a non-admin key must never read another tenant's enable map"
        )
        assert seen == ["NOVA-STG-01", "SHOP-STG-01"]

    def test_it_authenticates(self):
        """Group enablement is tenant-scoped state. Unauthenticated it would also be a
        free fingerprint of the deployment's configuration."""
        import inspect

        import main
        src = inspect.getsource(main.list_group_enablement)
        assert "_authenticate(request)" in src
        assert "resolve_tenant(" in src, (
            "the tenant must be resolved the way traffic resolves it — key authoritative, "
            "X-Tenant-ID honoured only for admin keys"
        )
