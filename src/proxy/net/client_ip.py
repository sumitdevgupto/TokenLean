"""The caller's IP address, resolved once per request (OSS core).

Read by the source-IP allowlist (``main._authenticate``) and by the commercial login rate
limiters. ``X-Forwarded-For`` is written left to right: a client may put anything in it,
and each proxy in front of TokenLean appends the address its connection came from. So the
only entry to believe is the one the nearest trusted proxy appended, counted from the
RIGHT: with ``network.trusted_proxy_hops`` = N it is the N-th entry from the right.

  * ``0`` — nothing in front of TokenLean: the socket peer is the caller.
  * ``1`` — one proxy, e.g. Cloud Run's front end, which appends the caller's address.
  * ``auto`` (the default) — 1 on Cloud Run (``K_SERVICE`` is set), else 0.

The header is ignored (the socket peer is used) when it has fewer entries than N, or
arrives on more than one line, since then it is unknown which entry a proxy appended. An
entry at position N that is not an IP address gives ``INVALID``; the parser never moves
further left, because everything to the left is client-written.

A trusted forwarder (the commercial portal) may vouch for the address it saw. It sends
that address in ``TokenLean-Client-IP`` and a Google-signed ID token for its service
account in ``TokenLean-Forwarder-Token``. The address is used only when the token checks
out against TRUSTED_FORWARDER_AUDIENCE and TRUSTED_FORWARDER_SA_EMAIL; with either unset,
nothing is vouched.

``ClientIPMiddleware`` resolves the address, stores it on ``request.state.client_ip`` and
removes the forwarding headers, so no handler reads them again and they never reach
``ctx.params`` (``_serve_core`` copies ``X-*`` headers there, and G13 writes ``ctx.params``
to Redis).

Rollout: ``network.client_ip_mode: observe`` (or CLIENT_IP_MODE=observe) keeps using the
first ``X-Forwarded-For`` entry, as before, and logs where the new address differs. The
default is ``enforce``.
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import logging
import os
import re
import time
import urllib.request
from typing import Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

CLIENT_IP_HEADER = "tokenlean-client-ip"
FORWARDER_TOKEN_HEADER = "tokenlean-forwarder-token"
INVALID = "invalid"  # the trusted entry is not an IP address: denied by the allowlist

# Removed from every request once the address is resolved.
_STRIPPED = frozenset((b"x-forwarded-for", b"x-real-ip", b"forwarded",
                       CLIENT_IP_HEADER.encode(), FORWARDER_TOKEN_HEADER.encode()))

_GOOGLE_CERTS_URL = "https://www.googleapis.com/oauth2/v1/certs"
_GOOGLE_ISSUERS = ("accounts.google.com", "https://accounts.google.com")
_CLOCK_SKEW_SECONDS = 10
_REFETCH_CERTS_SECONDS = 60   # at most one refresh a minute while certificates are held
_RETRY_NO_CERTS_SECONDS = 5   # before the first successful fetch
_SUMMARY_EVERY_SECONDS = 600.0


# ── Parsing ──────────────────────────────────────────────────────────────────

def normalise_ip(value: Optional[str]) -> Optional[str]:
    """``value`` as a canonical IP address, or None when it is not one. Accepts ``[v6]``,
    ``[v6]:port`` and ``v4:port``; an IPv4-mapped IPv6 address becomes plain IPv4, so it
    matches IPv4 allowlist ranges."""
    v = (value or "").strip()
    if v.startswith("["):
        end = v.find("]")
        if end == -1:
            return None
        v = v[1:end]
    elif v.count(":") == 1:  # IPv4 with a port; IPv6 has two or more colons
        v = v.split(":", 1)[0]
    try:
        addr = ipaddress.ip_address(v)
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return str(addr)


def _peer(peer: Optional[str]) -> str:
    if not peer:
        return "unknown"
    return normalise_ip(peer) or peer


def _resolve(xff_lines: Sequence[str], peer: Optional[str], hops: int) -> Tuple[str, str]:
    """(address, source): source is "peer", "forwarded" or "invalid"."""
    if hops <= 0 or len(xff_lines) != 1:
        return _peer(peer), "peer"
    entries = [e.strip() for e in xff_lines[0].split(",") if e.strip()]
    if len(entries) < hops:
        return _peer(peer), "peer"
    ip = normalise_ip(entries[-hops])
    return (ip, "forwarded") if ip else (INVALID, "invalid")


def resolve_forwarded(xff_lines: Sequence[str], peer: Optional[str], hops: int) -> str:
    """The caller's address from every ``X-Forwarded-For`` header line and the socket peer."""
    return _resolve(xff_lines, peer, hops)[0]


def _first_entry(xff_lines: Sequence[str], peer: Optional[str], hops: int) -> str:
    """What the proxy used before this module: the first entry, which the client writes."""
    if hops > 0 and xff_lines:
        first = xff_lines[0].split(",")[0].strip()
        if first:
            return first
    return _peer(peer)


# ── Settings ─────────────────────────────────────────────────────────────────

_warned: set = set()


def _warn_once(key: str, msg: str, *args) -> None:
    if key not in _warned:
        _warned.add(key)
        logger.warning(msg, *args)


def _auto_hops() -> int:
    return 1 if os.getenv("K_SERVICE") else 0


def _as_bool(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def trusted_proxy_hops(cfg: Optional[dict]) -> int:
    """``network.trusted_proxy_hops``: an integer >= 0, or ``auto``. When it is unset, the
    deprecated ``ip_allowlist.trust_x_forwarded_for`` still counts (true = 1, false = 0)."""
    cfg = cfg or {}
    raw = (cfg.get("network") or {}).get("trusted_proxy_hops")
    if raw is None:
        legacy = (cfg.get("ip_allowlist") or {}).get("trust_x_forwarded_for")
        if legacy is not None:
            hops = 1 if _as_bool(legacy) else 0
            _warn_once("legacy", "ip_allowlist.trust_x_forwarded_for is deprecated: set "
                       "network.trusted_proxy_hops to the number of proxies in front of TokenLean "
                       "that append to X-Forwarded-For. Using %d. With no proxy in front, 1 lets a "
                       "caller choose its own address; behind an external load balancer it is 2.",
                       hops)
            return hops
        return _auto_hops()
    if isinstance(raw, str) and raw.strip().lower() == "auto":
        return _auto_hops()
    if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
        return raw
    _warn_once("invalid", "network.trusted_proxy_hops=%r is not auto or an integer >= 0; "
               "using auto (%d)", raw, _auto_hops())
    return _auto_hops()


def client_ip_mode(cfg: Optional[dict]) -> str:
    """``enforce`` (default) or ``observe``. CLIENT_IP_MODE overrides
    ``network.client_ip_mode``; anything else counts as ``enforce``."""
    raw = os.getenv("CLIENT_IP_MODE") or ((cfg or {}).get("network") or {}).get("client_ip_mode")
    return "observe" if str(raw or "").strip().lower() == "observe" else "enforce"


# ── Trusted forwarder (Google-signed ID token) ───────────────────────────────

def _max_age(cache_control: str) -> Optional[int]:
    m = re.search(r"max-age=(\d+)", cache_control or "")
    return int(m.group(1)) if m else None


def _fetch_google_certs() -> Tuple[dict, int]:
    """Google's current ID-token signing certificates ``{key id: PEM}`` and how long to
    keep them (the response's max-age, else an hour)."""
    # A fixed https URL, never caller input (bandit B310).
    with urllib.request.urlopen(_GOOGLE_CERTS_URL, timeout=5) as resp:  # nosec B310
        certs = json.loads(resp.read().decode("utf-8"))
        return certs, _max_age(resp.headers.get("Cache-Control", "")) or 3600


def _token_kid(token: str) -> Optional[str]:
    try:
        header = token.split(".", 1)[0]
        header += "=" * (-len(header) % 4)
        return json.loads(base64.urlsafe_b64decode(header)).get("kid")
    except Exception:
        return None


class ForwarderVerifier:
    """Checks a Google-signed ID token: signature, expiry, issuer, audience, and that it
    names the forwarder's service account.

    Google's signing certificates are fetched off the event loop and cached for their
    max-age. A token naming a key the cache lacks (Google rotated keys) triggers an early
    refresh, at most one a minute. A failed refresh keeps the cached set, so a Google
    outage does not stop verification once certificates were fetched."""

    def __init__(self, audience: str, sa_email: str, fetch_certs=None, clock=time.monotonic):
        self.audience = audience
        self.sa_email = sa_email
        self._fetch = fetch_certs or _fetch_google_certs
        self._clock = clock
        self._certs: Optional[dict] = None
        self._expires = 0.0
        self._fetched_at = float("-inf")  # last attempt, successful or not
        self._lock = asyncio.Lock()

    def _needs_fetch(self, kid: Optional[str]) -> bool:
        since = self._clock() - self._fetched_at
        if self._certs is None:
            return since >= _RETRY_NO_CERTS_SECONDS
        if since < _REFETCH_CERTS_SECONDS:
            return False
        return self._clock() >= self._expires or (kid is not None and kid not in self._certs)

    async def _certs_for(self, kid: Optional[str]) -> Optional[dict]:
        if self._needs_fetch(kid):
            async with self._lock:
                if self._needs_fetch(kid):
                    self._fetched_at = self._clock()
                    try:
                        certs, max_age = await asyncio.to_thread(self._fetch)
                        self._certs, self._expires = certs, self._clock() + max_age
                    except Exception as exc:
                        logger.warning("trusted forwarder: could not fetch Google's signing "
                                       "certificates: %s", exc)
        return self._certs

    async def verify(self, token: str) -> bool:
        certs = await self._certs_for(_token_kid(token))
        if not certs:
            return False
        try:
            from google.auth import jwt as google_jwt
            claims = google_jwt.decode(token, certs=certs, audience=self.audience,
                                       clock_skew_in_seconds=_CLOCK_SKEW_SECONDS)
        except Exception as exc:
            _log_throttled("token", logging.WARNING, "trusted forwarder: token rejected: %s", exc)
            return False
        if claims.get("iss") not in _GOOGLE_ISSUERS or claims.get("email") != self.sa_email \
                or claims.get("email_verified") is False:
            _log_throttled("claims", logging.WARNING,
                           "trusted forwarder: token from %r (issuer %r) is not from %s",
                           claims.get("email"), claims.get("iss"), self.sa_email)
            return False
        return True


_env_verifier: dict = {"key": None, "verifier": None}


def forwarder_verifier() -> Optional[ForwarderVerifier]:
    """The verifier TRUSTED_FORWARDER_AUDIENCE and TRUSTED_FORWARDER_SA_EMAIL configure,
    or None when either is unset (no vouching)."""
    key = (os.getenv("TRUSTED_FORWARDER_AUDIENCE", "").strip(),
           os.getenv("TRUSTED_FORWARDER_SA_EMAIL", "").strip())
    if _env_verifier["key"] != key:
        _env_verifier["key"] = key
        _env_verifier["verifier"] = ForwarderVerifier(*key) if all(key) else None
        if any(key) and not all(key):
            logger.warning("trusted forwarder: set both TRUSTED_FORWARDER_AUDIENCE and "
                           "TRUSTED_FORWARDER_SA_EMAIL; until then nothing is vouched")
    return _env_verifier["verifier"]


# ── Logging ──────────────────────────────────────────────────────────────────

_last_logged: dict = {}
_source_counts: dict = {}
_summary = {"at": time.monotonic()}


def _log_throttled(key: str, level: int, msg: str, *args, every: float = 60.0) -> None:
    now = time.monotonic()
    last = _last_logged.get(key)
    if last is None or now - last >= every:
        _last_logged[key] = now
        logger.log(level, msg, *args)


def _note(source: str) -> None:
    """Count how addresses were found (in observe mode: would have been); logged every ten
    minutes, so the observe phase of a rollout shows whether portal requests arrive vouched."""
    _source_counts[source] = _source_counts.get(source, 0) + 1
    now = time.monotonic()
    if now - _summary["at"] >= _SUMMARY_EVERY_SECONDS:
        logger.info("client IP sources over the last %d s: %s",
                    int(now - _summary["at"]), dict(sorted(_source_counts.items())))
        _summary["at"] = now
        _source_counts.clear()


# ── Resolution ───────────────────────────────────────────────────────────────

async def _resolve_request(xff_lines, peer, vouched_ip, token, cfg, verifier) -> Tuple[str, str]:
    hops = trusted_proxy_hops(cfg)
    ip, source = _resolve(xff_lines, peer, hops)
    if token is not None and vouched_ip is not None:
        if verifier is not None and await verifier.verify(token):
            ip, source = normalise_ip(vouched_ip) or INVALID, "vouched"
        else:
            _note("vouch-rejected")
    _note(source)
    if client_ip_mode(cfg) == "observe":
        kept = _first_entry(xff_lines, peer, hops)
        if kept != ip:
            _note("observe-differs")
            _log_throttled("observe", logging.WARNING,
                           "client IP (observe mode): kept %s, the first X-Forwarded-For entry; "
                           "enforce mode would use %s (%s)", kept, ip, source)
        return kept, "observe"
    return ip, source


def request_client_ip(request, cfg: Optional[dict] = None) -> str:
    """The caller's address, as ClientIPMiddleware resolved it. A request that did not pass
    through the middleware (a unit test's stand-in) is resolved here from its
    ``X-Forwarded-For`` and socket peer, never from a vouch."""
    ip = getattr(getattr(request, "state", None), "client_ip", None)
    if ip:
        return ip
    if cfg is None:
        from config_loader import get_config
        cfg = get_config() or {}
    headers = getattr(request, "headers", None) or {}
    if hasattr(headers, "getlist"):
        lines = headers.getlist("x-forwarded-for")
    else:
        value = headers.get("x-forwarded-for") or headers.get("X-Forwarded-For")
        lines = [value] if value else []
    peer = getattr(getattr(request, "client", None), "host", None)
    hops = trusted_proxy_hops(cfg)
    if client_ip_mode(cfg) == "observe":
        return _first_entry(lines, peer, hops)
    return _resolve(lines, peer, hops)[0]


class ClientIPMiddleware:
    """ASGI middleware: resolve the caller's address once, store it on
    ``request.state.client_ip`` (how it was found on ``client_ip_source``), and drop the
    forwarding headers so no handler reads them again."""

    def __init__(self, app, get_config=None, verifier=None):
        self.app = app
        self._get_config = get_config
        self._verifier = verifier

    def _config(self) -> dict:
        if self._get_config is None:
            return {}
        try:
            return self._get_config() or {}
        except Exception:
            return {}

    async def __call__(self, scope, receive, send):
        if scope.get("type") not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        xff_lines, vouched_ip, token, kept = [], None, None, []
        for name, value in scope.get("headers") or []:
            lname = name.lower()
            if lname == b"x-forwarded-for":
                xff_lines.append(value.decode("latin-1"))
            elif lname == CLIENT_IP_HEADER.encode():
                vouched_ip = value.decode("latin-1")
            elif lname == FORWARDER_TOKEN_HEADER.encode():
                token = value.decode("latin-1")
            if lname not in _STRIPPED:
                kept.append((name, value))
        peer = (scope.get("client") or (None,))[0]
        verifier = self._verifier if self._verifier is not None else forwarder_verifier()
        ip, source = await _resolve_request(xff_lines, peer, vouched_ip, token,
                                            self._config(), verifier)
        state = scope.setdefault("state", {})
        state["client_ip"] = ip
        state["client_ip_source"] = source
        await self.app(dict(scope, headers=kept), receive, send)
