"""gcp-deploy.sh gives Langfuse its database URL as a secret, with the password URL-encoded.

The DSN went into langfuse-svc's plain environment, where anyone with roles/run.viewer
could read the database password, and the password went in raw: random_password emits
'#', '?', '%', '@' and '/', which Prisma reads as URL syntax, so Langfuse could not boot.
"""
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

_DEPLOY = (Path(__file__).resolve().parents[2] / "scripts" / "gcp" / "gcp-deploy.sh").read_text(
    encoding="utf-8")


def _langfuse_deploy() -> str:
    start = _DEPLOY.index("gcloud run deploy langfuse-svc")
    return _DEPLOY[start:_DEPLOY.index("--quiet", start)]


def test_the_password_is_url_encoded_before_it_enters_the_dsn():
    encoder = re.search(r'LANGFUSE_DB_PW_ENC=\$\(python3 -c "([^"]+)" "\$LANGFUSE_SECRET"\)', _DEPLOY)
    assert encoder, "the password is not encoded"
    assert re.search(r'DB_URL="postgresql://token_opt_app:\$\{LANGFUSE_DB_PW_ENC\}@', _DEPLOY)
    password = "a#b?c%d@e/f:g"
    encoded = subprocess.run([sys.executable, "-c", encoder.group(1), password],
                             capture_output=True, text=True, check=True).stdout.strip()
    dsn = urlsplit(f"postgresql://token_opt_app:{encoded}@localhost/langfuse?host=/cloudsql/x")
    assert dsn.password is not None and unquote(dsn.password) == password
    assert dsn.hostname == "localhost"
    assert dsn.path == "/langfuse" and dsn.query == "host=/cloudsql/x"


def test_langfuse_reads_the_dsn_from_a_secret():
    deploy = _langfuse_deploy()
    env_vars = re.search(r'--set-env-vars="([^"]*)"', deploy).group(1)
    assert "DATABASE_URL" not in env_vars
    secrets = re.search(r'--set-secrets="([^"]*)"', deploy).group(1)
    assert "DATABASE_URL=langfuse-database-url:latest" in secrets.split(",")
    assert "gcloud secrets versions add langfuse-database-url" in _DEPLOY
    assert re.search(r"grant_langfuse_secret\s+langfuse-database-url\b", _DEPLOY)
