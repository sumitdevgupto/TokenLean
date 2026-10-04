"""The local stack's Redis, Qdrant, Langfuse and Grafana each require a login of their own.

docker-compose.yml ran Redis with no password and Qdrant with no API key, created Langfuse's
ADMIN user with the password changeme123, gave Grafana's admin whatever GRAFANA_PASSWORD held
(empty in .env.template), and let any web page call the proxy (CORS_ALLOW_ALL). Compose now
refuses to start without the four logins. scripts/local/deploy-local.sh, start-local.sh and the
commercial local deploy generate whichever .env lacks (scripts/local/fill-local-env.sh), and the
proxy gets them. Cross-origin browser calls are off unless .env asks for them.
"""
import re
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[2]
_TEXT = (_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
_SERVICES = yaml.safe_load(_TEXT)["services"]


def _required(value, name: str) -> bool:
    """Compose's ${NAME:?message}: no value, no start (not even an empty one)."""
    return re.fullmatch(rf"\$\{{{name}:\?[^}}]+\}}", str(value)) is not None


def test_redis_requires_its_password():
    redis = _SERVICES["redis"]
    command = redis["command"]
    assert isinstance(command, list) and command[0] == "redis-server"
    assert _required(command[command.index("--requirepass") + 1], "REDIS_PASSWORD")
    # redis-cli in the container (the healthcheck, and the scripts that docker exec it) reads
    # the password from here.
    assert _env("redis").get("REDISCLI_AUTH") == "${REDIS_PASSWORD}"
    assert redis["healthcheck"]["test"] == ["CMD", "redis-cli", "ping"]


def _env(service: str) -> dict:
    return _SERVICES[service].get("environment") or {}


def test_qdrant_requires_its_api_key():
    assert _required(_env("qdrant").get("QDRANT__SERVICE__API_KEY"), "QDRANT_API_KEY")


def test_the_admin_uis_have_no_default_password():
    assert _required(_env("langfuse").get("LANGFUSE_INIT_USER_PASSWORD"),
                     "LANGFUSE_INIT_USER_PASSWORD")
    assert _required(_env("grafana").get("GF_SECURITY_ADMIN_PASSWORD"), "GRAFANA_PASSWORD")
    assert "changeme123" not in _TEXT


def test_the_proxy_carries_the_credentials_and_takes_no_cross_origin_calls_by_default():
    env = _env("proxy")
    assert env.get("REDIS_PASSWORD") == "${REDIS_PASSWORD}"     # sent as the `default` user's
    assert env.get("QDRANT_API_KEY") == "${QDRANT_API_KEY}"
    assert env.get("CORS_ALLOW_ALL") == "${CORS_ALLOW_ALL:-false}"


def test_the_local_post_deploy_check_sends_the_qdrant_key():
    # Its collection check got 401 from a Qdrant that requires the key.
    script = (_ROOT / "scripts" / "local" / "post-deploy-check-local.sh").read_text(encoding="utf-8")
    assert re.search(r'curl -s -H "api-key: \$\{QDRANT_API_KEY:-\}" "http://localhost:6333/collections/', script)
    assert "sed -n 's/^QDRANT_API_KEY=//p'" in script     # from .env when the shell lacks it


def test_the_template_names_every_credential_for_the_scripts_to_fill():
    template = (_ROOT / ".env.template").read_text(encoding="utf-8")
    for name in ("REDIS_PASSWORD", "QDRANT_API_KEY", "LANGFUSE_INIT_USER_PASSWORD",
                 "GRAFANA_PASSWORD"):
        assert re.search(rf"^{name}=$", template, re.M), name   # empty: generated, never shared
