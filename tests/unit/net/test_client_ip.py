"""The caller's IP address (net/client_ip.py): counted from the right of X-Forwarded-For.

The IP allowlist and the portal login limits used the FIRST X-Forwarded-For entry, which
the client writes, so a caller with a leaked key sent an allowlisted address and got in,
and rotating the header defeated the per-IP login limits. Now the caller is the entry
network.trusted_proxy_hops places from the right, or the address a trusted forwarder (the
portal) vouches for with a Google-signed ID token, and the forwarding headers are removed
before any handler (or ctx.params) sees them.
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "proxy")))

import asyncio
import json
import logging
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from net import client_ip as cip

VECTORS = json.loads((Path(__file__).parent / "client_ip_vectors.json").read_text(encoding="utf-8"))
AUDIENCE, SA = "tokenlean-proxy", "portal-svc@example-project.iam.gserviceaccount.com"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for var in ("K_SERVICE", "CLIENT_IP_MODE", "TRUSTED_FORWARDER_AUDIENCE",
                "TRUSTED_FORWARDER_SA_EMAIL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(cip, "_warned", set())
    monkeypatch.setattr(cip, "_last_logged", {})
    monkeypatch.setattr(cip, "_env_verifier", {"key": None, "verifier": None})


# ── the parser ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("case", VECTORS["cases"], ids=[c["name"] for c in VECTORS["cases"]])
def test_parser_cases(case):
    assert cip.resolve_forwarded(case["xff"], case["peer"], case["hops"]) == case["expect"]


# ── settings ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("cfg,k_service,expect", [
    ({"network": {"trusted_proxy_hops": 2}}, None, 2),
    ({"network": {"trusted_proxy_hops": 0}}, "token-proxy", 0),
    ({"network": {"trusted_proxy_hops": "auto"}}, "token-proxy", 1),
    ({"network": {"trusted_proxy_hops": "auto"}}, None, 0),
    ({}, "token-proxy", 1),                                            # unset: auto
    ({}, None, 0),
    ({"ip_allowlist": {"trust_x_forwarded_for": True}}, None, 1),      # legacy key still counts
    ({"ip_allowlist": {"trust_x_forwarded_for": "false"}}, "token-proxy", 0),
    ({"network": {"trusted_proxy_hops": 0},
      "ip_allowlist": {"trust_x_forwarded_for": True}}, None, 0),      # the new key wins
])
def test_trusted_proxy_hops(cfg, k_service, expect, monkeypatch):
    if k_service:
        monkeypatch.setenv("K_SERVICE", k_service)
    assert cip.trusted_proxy_hops(cfg) == expect


@pytest.mark.parametrize("raw", [-1, "two", True, 1.5])
def test_an_invalid_hop_count_falls_back_to_auto_with_a_warning(raw, monkeypatch, caplog):
    monkeypatch.setenv("K_SERVICE", "token-proxy")
    with caplog.at_level(logging.WARNING):
        assert cip.trusted_proxy_hops({"network": {"trusted_proxy_hops": raw}}) == 1
    assert "not auto or an integer" in caplog.text


def test_the_deprecated_key_warns_once(caplog):
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            cip.trusted_proxy_hops({"ip_allowlist": {"trust_x_forwarded_for": True}})
    assert caplog.text.count("trust_x_forwarded_for is deprecated") == 1


def test_client_ip_mode(monkeypatch):
    assert cip.client_ip_mode({}) == "enforce"
    assert cip.client_ip_mode({"network": {"client_ip_mode": "observe"}}) == "observe"
    assert cip.client_ip_mode({"network": {"client_ip_mode": "sometimes"}}) == "enforce"
    monkeypatch.setenv("CLIENT_IP_MODE", "observe")
    assert cip.client_ip_mode({"network": {"client_ip_mode": "enforce"}}) == "observe"


# ── the middleware ───────────────────────────────────────────────────────────

class _StubVerifier:
    def __init__(self):
        self.seen = []

    async def verify(self, token):
        self.seen.append(token)
        return token == "good-token"


def _app(cfg=None, verifier=None):
    async def echo(request):
        return JSONResponse({"ip": request.state.client_ip,
                             "source": request.state.client_ip_source,
                             "headers": sorted(k for k, _ in request.headers.items())})
    app = Starlette(routes=[Route("/", echo)])
    app.add_middleware(cip.ClientIPMiddleware, get_config=lambda: cfg or {}, verifier=verifier)
    return TestClient(app)


ONE_HOP = {"network": {"trusted_proxy_hops": 1}}


def test_the_right_most_entry_is_used_and_the_forwarding_headers_are_dropped():
    r = _app(ONE_HOP).get("/", headers=[
        ("x-forwarded-for", "203.0.113.9, 8.8.8.8"), ("x-real-ip", "203.0.113.9"),
        ("forwarded", "for=203.0.113.9"), ("tokenlean-client-ip", "203.0.113.9"),
        ("tokenlean-forwarder-token", "forged"), ("x-custom", "kept")]).json()
    assert (r["ip"], r["source"]) == ("8.8.8.8", "forwarded")
    assert "x-custom" in r["headers"]
    assert not {"x-forwarded-for", "x-real-ip", "forwarded", "tokenlean-client-ip",
                "tokenlean-forwarder-token"} & set(r["headers"])


def test_two_header_lines_are_not_trusted():
    r = _app(ONE_HOP).get("/", headers=[("x-forwarded-for", "8.8.8.8"),
                                        ("x-forwarded-for", "203.0.113.9")]).json()
    assert (r["ip"], r["source"]) == ("testclient", "peer")


def test_a_trusted_forwarder_vouches_for_the_address_it_saw():
    verifier = _StubVerifier()
    r = _app(ONE_HOP, verifier).get("/", headers=[
        ("x-forwarded-for", "198.51.100.7, 34.1.2.3"),  # the browser, then the portal's egress
        ("tokenlean-client-ip", "198.51.100.7"), ("tokenlean-forwarder-token", "good-token")]).json()
    assert (r["ip"], r["source"]) == ("198.51.100.7", "vouched")
    assert verifier.seen == ["good-token"]


def test_a_vouch_that_does_not_verify_is_ignored():
    r = _app(ONE_HOP, _StubVerifier()).get("/", headers=[
        ("x-forwarded-for", "34.1.2.3"),
        ("tokenlean-client-ip", "203.0.113.9"), ("tokenlean-forwarder-token", "forged")]).json()
    assert (r["ip"], r["source"]) == ("34.1.2.3", "forwarded")


def test_without_a_configured_forwarder_nothing_is_vouched():
    r = _app(ONE_HOP).get("/", headers=[
        ("x-forwarded-for", "34.1.2.3"),
        ("tokenlean-client-ip", "203.0.113.9"), ("tokenlean-forwarder-token", "good-token")]).json()
    assert r["ip"] == "34.1.2.3"


def test_observe_mode_keeps_the_first_entry_and_logs_the_difference(caplog):
    cfg = {"network": {"trusted_proxy_hops": 1, "client_ip_mode": "observe"}}
    with caplog.at_level(logging.WARNING):
        r = _app(cfg).get("/", headers=[("x-forwarded-for", "203.0.113.9, 8.8.8.8")]).json()
    assert (r["ip"], r["source"]) == ("203.0.113.9", "observe")
    assert "enforce mode would use 8.8.8.8" in caplog.text
    assert "x-forwarded-for" not in r["headers"]  # stripped in observe mode too


def test_a_request_the_middleware_did_not_see_is_resolved_on_the_spot():
    class _Req:
        headers = {"x-forwarded-for": "203.0.113.9, 8.8.8.8"}
        client = type("C", (), {"host": "9.9.9.9"})()
    assert cip.request_client_ip(_Req(), ONE_HOP) == "8.8.8.8"

    class _Seen:
        state = type("S", (), {"client_ip": "198.51.100.7"})()
    assert cip.request_client_ip(_Seen(), ONE_HOP) == "198.51.100.7"


# ── the proxy itself: headers in, allowlist decision out ─────────────────────

class TestTheProxyEnforcesTheRightMostAddress:
    """GET /v1/models on the real app: middleware → _authenticate → IP allowlist."""

    CFG = {"network": {"trusted_proxy_hops": 1},
           "ip_allowlist": {"enabled": True, "global_cidrs": ["203.0.113.0/24"]}}

    def _get(self, headers, cfg=None):
        import main
        meta = {"tenant_id": "acme"}
        with patch.object(main, "validate_proxy_key", return_value=(True, "acme", meta)), \
             patch.object(main, "get_config", return_value=cfg or self.CFG):
            return TestClient(main.app).get(
                "/v1/models", headers=[("authorization", "Bearer tok-x"), *headers])

    def test_the_middleware_is_installed(self):
        import main
        assert any(m.cls is cip.ClientIPMiddleware for m in main.app.user_middleware)

    def test_a_spoofed_allowlisted_first_entry_is_refused(self):
        r = self._get([("x-forwarded-for", "203.0.113.9, 8.8.8.8")])
        assert r.status_code == 403

    def test_an_allowlisted_caller_passes(self):
        assert self._get([("x-forwarded-for", "10.9.9.9, 203.0.113.9")]).status_code == 200

    def test_observe_mode_still_admits_the_old_way(self):
        cfg = dict(self.CFG, network={"trusted_proxy_hops": 1, "client_ip_mode": "observe"})
        assert self._get([("x-forwarded-for", "203.0.113.9, 8.8.8.8")], cfg).status_code == 200


# ── the forwarder's Google-signed ID token ──────────────────────────────────

@pytest.fixture(scope="module")
def keys():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from google.auth import crypt

    def make():
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption())
        public = key.public_key().public_bytes(serialization.Encoding.PEM,
                                               serialization.PublicFormat.SubjectPublicKeyInfo)
        return private, public.decode()
    return {"k1": make(), "k2": make()}


def _token(keys, kid="k1", signed_with=None, **overrides):
    """A Google-style ID token whose header names ``kid``, signed with ``signed_with``'s
    private key (by default the key ``kid`` names)."""
    from google.auth import crypt, jwt as google_jwt
    now = int(time.time())
    payload = {"iss": "https://accounts.google.com", "aud": AUDIENCE, "email": SA,
               "email_verified": True, "sub": "1234567890", "iat": now, "exp": now + 3600}
    payload.update(overrides)
    signer = crypt.RSASigner.from_string(keys[signed_with or kid][0], key_id=kid)
    return google_jwt.encode(signer, payload).decode()


class _Certs:
    """Stands in for Google's certificate endpoint; counts fetches."""

    def __init__(self, keys, kids=("k1",), fail=False):
        self.keys, self.kids, self.fail, self.calls = keys, list(kids), fail, 0

    def __call__(self):
        self.calls += 1
        if self.fail:
            raise OSError("certificate endpoint unreachable")
        return {k: self.keys[k][1] for k in self.kids}, 3600


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _verify(verifier, token):
    return asyncio.run(verifier.verify(token))


def test_a_valid_token_from_the_forwarder_verifies(keys):
    assert _verify(cip.ForwarderVerifier(AUDIENCE, SA, fetch_certs=_Certs(keys)), _token(keys))


@pytest.mark.parametrize("overrides", [
    {"aud": "another-service"},
    {"email": "someone-else@example-project.iam.gserviceaccount.com"},
    {"iss": "https://evil.example"},
    {"email_verified": False},
    {"iat": int(time.time()) - 7200, "exp": int(time.time()) - 3600},  # expired
], ids=["audience", "service-account", "issuer", "unverified-email", "expired"])
def test_a_token_that_is_not_the_forwarders_is_refused(keys, overrides):
    verifier = cip.ForwarderVerifier(AUDIENCE, SA, fetch_certs=_Certs(keys))
    assert not _verify(verifier, _token(keys, **overrides))


def test_a_token_signed_by_another_key_is_refused(keys):
    verifier = cip.ForwarderVerifier(AUDIENCE, SA, fetch_certs=_Certs(keys, kids=["k1"]))
    assert _verify(verifier, _token(keys))                              # k1, signed by k1
    assert not _verify(verifier, _token(keys, kid="k1", signed_with="k2"))  # names k1, signed by k2
    assert not _verify(verifier, "not-a-jwt")


def test_certificates_are_cached(keys):
    certs = _Certs(keys)
    verifier = cip.ForwarderVerifier(AUDIENCE, SA, fetch_certs=certs, clock=_Clock())
    for _ in range(5):
        assert _verify(verifier, _token(keys))
    assert certs.calls == 1


def test_a_rotated_key_is_fetched_once_at_most_a_minute(keys):
    certs, clock = _Certs(keys, kids=["k1"]), _Clock()
    verifier = cip.ForwarderVerifier(AUDIENCE, SA, fetch_certs=certs, clock=clock)
    assert _verify(verifier, _token(keys))                   # fetch 1
    certs.kids = ["k1", "k2"]                                 # Google publishes a new key
    clock.now += 30
    assert not _verify(verifier, _token(keys, kid="k2"))     # within the minute: no refetch
    clock.now += 31
    assert _verify(verifier, _token(keys, kid="k2"))         # fetch 2 finds it
    assert certs.calls == 2


def test_a_failed_refresh_keeps_the_cached_certificates(keys):
    certs, clock = _Certs(keys), _Clock()
    verifier = cip.ForwarderVerifier(AUDIENCE, SA, fetch_certs=certs, clock=clock)
    assert _verify(verifier, _token(keys))
    certs.fail, clock.now = True, clock.now + 7200            # expired, and Google is down
    assert _verify(verifier, _token(keys))
    assert certs.calls == 2


def test_with_no_certificates_nothing_verifies(keys):
    verifier = cip.ForwarderVerifier(AUDIENCE, SA, fetch_certs=_Certs(keys, fail=True))
    assert not _verify(verifier, _token(keys))


def test_the_env_configures_the_forwarder(monkeypatch, caplog):
    assert cip.forwarder_verifier() is None
    monkeypatch.setenv("TRUSTED_FORWARDER_AUDIENCE", AUDIENCE)
    with caplog.at_level(logging.WARNING):
        assert cip.forwarder_verifier() is None               # half-configured: off, and says so
    assert "set both" in caplog.text
    monkeypatch.setenv("TRUSTED_FORWARDER_SA_EMAIL", SA)
    verifier = cip.forwarder_verifier()
    assert (verifier.audience, verifier.sa_email) == (AUDIENCE, SA)
    assert cip.forwarder_verifier() is verifier               # built once
