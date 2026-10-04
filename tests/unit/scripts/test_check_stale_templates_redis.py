"""scripts/check-stale-templates.py logs in to the local Redis, which requires a password.

docker-compose.yml's Redis requires REDIS_PASSWORD. The checker sends it as the `default`
user's, as the proxy does, and nothing when it is unset (a Redis with no password).
"""
import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "check-stale-templates.py"


def _checker(monkeypatch, password):
    pytest.importorskip("redis")
    import redis.asyncio as aioredis
    seen = []
    monkeypatch.setattr(aioredis, "from_url", lambda url, **kwargs: seen.append(kwargs) or object())
    if password is None:
        monkeypatch.delenv("REDIS_PASSWORD", raising=False)
    else:
        monkeypatch.setenv("REDIS_PASSWORD", password)
    spec = importlib.util.spec_from_file_location("_check_stale_templates", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.StaleTemplateChecker("redis://localhost:6379/0", 30, 7)._get_redis()
    return seen


def test_the_password_is_sent_as_the_default_user_s(monkeypatch):
    assert _checker(monkeypatch, "pw-local") == [
        {"decode_responses": True, "username": "default", "password": "pw-local"}]


def test_without_a_password_none_is_sent(monkeypatch):
    assert _checker(monkeypatch, None) == [{"decode_responses": True}]
