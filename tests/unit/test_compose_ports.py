"""The local stack publishes only the proxy beyond this machine.

docker-compose.yml published Redis (no password), Postgres, Qdrant (no API key), the
sidecars and the admin UIs (Langfuse's admin with a default password, Grafana, Prometheus,
Jaeger) on every interface, and Docker's published ports bypass a host firewall: on a VM with
a public address all of it was open. Each is now bound to TOKEN_OPT_BIND, 127.0.0.1 unless
set; the proxy, which authenticates every call, keeps listening on all interfaces.
"""
import re
from pathlib import Path

import yaml

_COMPOSE = Path(__file__).resolve().parents[2] / "docker-compose.yml"
_ALL_INTERFACES = {"proxy"}


def _resolve(spec: str, env: dict) -> str:
    """Interpolate ${VAR:-default} the way compose does."""
    return re.sub(r"\$\{(\w+):-([^}]*)\}", lambda m: env.get(m.group(1)) or m.group(2), spec)


def _host_address(spec: str):
    """The host address a short-syntax port publishes on; None means every interface."""
    parts = spec.split(":")
    return parts[0] if len(parts) == 3 else None


def _published(env):
    services = yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))["services"]
    return [(name, _host_address(_resolve(str(port), env)))
            for name, svc in services.items() for port in svc.get("ports") or []]


def test_only_the_proxy_listens_beyond_this_machine():
    published = _published({})
    assert {name for name, _ in published} >= {"redis", "postgres", "qdrant", "langfuse",
                                              "grafana", "prometheus", "jaeger", "proxy"}
    for name, address in published:
        if name in _ALL_INTERFACES:
            assert address is None, name
        else:
            assert address == "127.0.0.1", name


def test_the_bind_address_can_be_widened_on_purpose():
    wide = "10.0.0.5"          # e.g. the host's private-network address
    for name, address in _published({"TOKEN_OPT_BIND": wide}):
        if name not in _ALL_INTERFACES:
            assert address == wide, name
