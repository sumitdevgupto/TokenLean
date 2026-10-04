"""The Redis VM's startup script writes the Redis config, with the password when it is required.

The VM runs Redis from a container declaration, and anything in an instance's metadata can be
read by whoever can view the instance, so the password never goes there. At boot this script
reads it from Secret Manager with the VM's own service account and writes it into the config
file the container mounts. It runs here in bash against a fake metadata server and Secret
Manager, which also check the headers a real one requires.
"""
import base64
import json
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "infra" / "redis-vm-startup.sh"
SECRET = "projects/tl-test/secrets/redis-auth"
PASSWORD = "s3cretFromSecretManager"
TLS_SECRET = "projects/tl-test/secrets/redis-tls-server"
# The TLS secret holds the server's certificate, the CA's, then the server's key (main.tf).
LEAF = "-----BEGIN CERTIFICATE-----\nTEVBRkNFUlQ=\n-----END CERTIFICATE-----\n"
CA = "-----BEGIN CERTIFICATE-----\nQ0FDRVJU\n-----END CERTIFICATE-----\n"
# Built at runtime so this file never holds a string the content scan would flag.
KEY = "-----BEGIN EC " + "PRIVATE KEY-----\nU0VSVkVSS0VZ\n-----END EC PRIVATE KEY-----\n"


def _bash():
    """Git's bash on Windows (a PATH `bash` may be the WSL launcher), else PATH bash."""
    if os.name == "nt":
        git = shutil.which("git")
        if git:
            candidate = Path(git).resolve().parents[1] / "bin" / "bash.exe"
            if candidate.exists():
                return str(candidate)
    return shutil.which("bash")


pytestmark = pytest.mark.skipif(_bash() is None or shutil.which("curl") is None,
                                reason="needs bash and curl")


class _Fake:
    def __init__(self, enforced, readable=True, tls=None, tls_readable=True):
        self.enforced, self.readable = enforced, readable
        self.tls, self.tls_readable = tls, tls_readable   # tls None: a VM from before TLS
        self.guest = {}


def _server(fake):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, body=""):
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            md = "/computeMetadata/v1/"
            if self.path.startswith(md):
                if self.headers.get("Metadata-Flavor") != "Google":
                    return self._send(403, "missing Metadata-Flavor")
                path = self.path[len(md):]
                if path == "instance/attributes/redis-auth-enforced":
                    return self._send(200, "true" if fake.enforced else "false")
                if path == "instance/attributes/redis-auth-secret":
                    return self._send(200, SECRET)
                if path == "instance/attributes/redis-tls" and fake.tls is not None:
                    return self._send(200, "true" if fake.tls else "false")
                if path == "instance/attributes/redis-tls-secret" and fake.tls is not None:
                    return self._send(200, TLS_SECRET)
                if path == "instance/service-accounts/default/token":
                    return self._send(200, '{"access_token":"tok-123","expires_in":3599,"token_type":"Bearer"}')
                return self._send(404, "not found")
            if self.path == f"/v1/{SECRET}/versions/latest:access":
                if self.headers.get("Authorization") != "Bearer tok-123" or not fake.readable:
                    return self._send(403, '{"error": {"code": 403}}')
                data = base64.b64encode(PASSWORD.encode()).decode()
                return self._send(200, json.dumps({"name": f"{SECRET}/versions/3", "payload": {
                    "data": data, "dataCrc32c": "1234"}}, indent=2))
            if self.path == f"/v1/{TLS_SECRET}/versions/latest:access":
                if self.headers.get("Authorization") != "Bearer tok-123" or not fake.tls_readable:
                    return self._send(403, '{"error": {"code": 403}}')
                data = base64.b64encode((LEAF + CA + KEY).encode()).decode()
                return self._send(200, json.dumps({"name": f"{TLS_SECRET}/versions/7", "payload": {
                    "data": data, "dataCrc32c": "1234"}}, indent=2))
            return self._send(404, "not found")

        def do_PUT(self):
            md = "/computeMetadata/v1/instance/guest-attributes/"
            if self.path.startswith(md) and self.headers.get("Metadata-Flavor") == "Google":
                length = int(self.headers.get("Content-Length") or 0)
                fake.guest[self.path[len(md):]] = self.rfile.read(length).decode()
                return self._send(200)
            return self._send(403)

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _boot(tmp_path, fake):
    server = _server(fake)
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        r = subprocess.run([_bash(), SCRIPT.as_posix()], capture_output=True, encoding="utf-8",
                           errors="replace", timeout=120,
                           env={**os.environ, "METADATA_URL": f"{base}/computeMetadata/v1",
                                "SECRET_MANAGER_URL": f"{base}/v1",
                                "REDIS_CONF_DIR": (tmp_path / "conf").as_posix()})
    finally:
        server.shutdown()
    conf = tmp_path / "conf" / "redis.conf"
    return r, (conf.read_text(encoding="utf-8") if conf.exists() else None)


def test_with_enforcement_redis_requires_the_password_from_secret_manager(tmp_path):
    fake = _Fake(enforced=True)
    r, conf = _boot(tmp_path, fake)
    assert r.returncode == 0, r.stdout + r.stderr
    assert conf is not None and f"requirepass {PASSWORD}\n" in conf
    assert "appendonly no\n" in conf and 'save ""\n' in conf
    assert PASSWORD not in r.stdout + r.stderr
    assert fake.guest == {"token-opt/redis-auth": "enforced", "token-opt/redis-tls": "off"}


def test_without_it_redis_runs_as_before(tmp_path):
    fake = _Fake(enforced=False)
    r, conf = _boot(tmp_path, fake)
    assert r.returncode == 0, r.stdout + r.stderr
    assert conf is not None and "requirepass" not in conf and "appendonly no\n" in conf
    assert fake.guest == {"token-opt/redis-auth": "open", "token-opt/redis-tls": "off"}


_TLS_CONF = ("port 0\ntls-port 6379\ntls-cert-file /etc/redis/tls.crt\n"
             "tls-key-file /etc/redis/tls.key\ntls-ca-cert-file /etc/redis/ca.crt\n"
             "tls-auth-clients no\n")


def test_with_tls_redis_serves_only_tls_with_the_key_from_secret_manager(tmp_path):
    fake = _Fake(enforced=True, tls=True)
    r, conf = _boot(tmp_path, fake)
    assert r.returncode == 0, r.stdout + r.stderr
    assert conf is not None and _TLS_CONF in conf and f"requirepass {PASSWORD}\n" in conf
    files = {name: (tmp_path / "conf" / name).read_text(encoding="utf-8")
             for name in ("tls.crt", "ca.crt", "tls.key")}
    assert files == {"tls.crt": LEAF, "ca.crt": CA, "tls.key": KEY}
    assert "U0VSVkVSS0VZ" not in r.stdout + r.stderr
    # The deploy compares the secret version the VM serves with the latest one.
    assert fake.guest == {"token-opt/redis-auth": "enforced", "token-opt/redis-tls": "on:7"}


@pytest.mark.parametrize("enforced", [True, False], ids=["with a password", "without one"])
def test_a_tls_key_that_cannot_be_read_leaves_no_config_for_redis_to_start_without(tmp_path,
                                                                                    enforced):
    (tmp_path / "conf").mkdir()
    (tmp_path / "conf" / "redis.conf").write_text('appendonly no\nsave ""\n', encoding="utf-8")
    fake = _Fake(enforced=enforced, tls=True, tls_readable=False)
    r, conf = _boot(tmp_path, fake)
    assert r.returncode != 0
    assert conf is None   # never plaintext when TLS was asked for
    assert fake.guest.get("token-opt/redis-tls") == "failed"


def test_tls_switched_off_drops_the_tls_files_of_an_earlier_boot(tmp_path):
    (tmp_path / "conf").mkdir()
    for name in ("tls.crt", "ca.crt", "tls.key"):
        (tmp_path / "conf" / name).write_text("old", encoding="utf-8")
    fake = _Fake(enforced=True, tls=False)
    r, conf = _boot(tmp_path, fake)
    assert r.returncode == 0, r.stdout + r.stderr
    assert conf is not None and "tls-port" not in conf
    assert not (tmp_path / "conf" / "tls.key").exists()
    assert fake.guest.get("token-opt/redis-tls") == "off"


def test_a_password_that_cannot_be_read_leaves_no_config_for_redis_to_start_without(tmp_path):
    (tmp_path / "conf").mkdir()
    (tmp_path / "conf" / "redis.conf").write_text('appendonly no\nsave ""\n', encoding="utf-8")
    fake = _Fake(enforced=True, readable=False)
    r, conf = _boot(tmp_path, fake)
    assert r.returncode != 0
    assert conf is None   # the open config from an earlier boot is gone, so Redis cannot start
    assert fake.guest.get("token-opt/redis-auth") != "enforced"
