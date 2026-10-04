"""The local deploys generate the stack's logins that .env lacks, and never print them.

docker-compose.yml requires REDIS_PASSWORD, QDRANT_API_KEY, LANGFUSE_INIT_USER_PASSWORD and
GRAFANA_PASSWORD. scripts/local/fill-local-env.sh fills any that has no value with a random one,
in .env (replacing the empty placeholder .env.template ships, or appending) and in the
environment of the deploy that sources it, before its first docker compose command. It runs
here in bash against a scratch .env.
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
HELPER = _ROOT / "scripts" / "local" / "fill-local-env.sh"
NAMES = ("REDIS_PASSWORD", "QDRANT_API_KEY", "LANGFUSE_INIT_USER_PASSWORD", "GRAFANA_PASSWORD")


def _bash():
    """Git's bash on Windows (a PATH `bash` may be the WSL launcher), else PATH bash."""
    if os.name == "nt":
        git = shutil.which("git")
        if git:
            candidate = Path(git).resolve().parents[1] / "bin" / "bash.exe"
            if candidate.exists():
                return str(candidate)
    return shutil.which("bash")


pytestmark = pytest.mark.skipif(_bash() is None, reason="needs bash")


def _ensure(tmp_path, env_text=None):
    env_file = tmp_path / ".env"
    if env_text is not None:
        env_file.write_text(env_text, encoding="utf-8", newline="\n")
    script = "\n".join([
        "set -euo pipefail",
        'info() { echo "[INFO] $*"; }',
        f'ENV_FILE="{env_file.as_posix()}"',
        '[[ -f "$ENV_FILE" ]] && { set -a; source "$ENV_FILE"; set +a; }',
        f'source "{HELPER.as_posix()}"',
        'fill_local_env "$ENV_FILE"',
        # What the deploy that sourced it now has in its environment, as lengths only.
        *[f'echo "LEN {n} ${{#{n}}}"' for n in NAMES],
    ])
    clean = {k: v for k, v in os.environ.items() if k not in NAMES}
    r = subprocess.run([_bash(), "-c", script], capture_output=True, encoding="utf-8",
                       timeout=60, env=clean)
    assert r.returncode == 0, r.stdout + r.stderr
    values = dict(re.findall(r"^(\w+)=(.*)$", env_file.read_text(encoding="utf-8"), re.M))
    lengths = dict(re.findall(r"^LEN (\w+) (\d+)$", r.stdout, re.M))
    return r.stdout + r.stderr, values, {k: int(v) for k, v in lengths.items()}, env_file


def test_the_template_s_empty_placeholders_are_filled_with_random_values(tmp_path):
    out, values, lengths, _ = _ensure(tmp_path, "DB_PASSWORD=keep-me\nREDIS_PASSWORD=\n"
                                                "QDRANT_API_KEY=\nGRAFANA_PASSWORD=\n")
    for name in NAMES:
        assert re.fullmatch(r"[0-9a-f]{48}", values.get(name, "")), name
        assert lengths.get(name) == 48, name                   # exported for this deploy too
        assert values[name] not in out                         # and never printed
    assert len({values[n] for n in NAMES}) == 4
    assert values.get("DB_PASSWORD") == "keep-me"
    assert "Generated REDIS_PASSWORD" in out


def test_values_already_set_are_kept_and_a_second_run_changes_nothing(tmp_path):
    _, first, _, env_file = _ensure(tmp_path, "GRAFANA_PASSWORD=my-own-choice\n")
    assert first["GRAFANA_PASSWORD"] == "my-own-choice"
    before = env_file.read_text(encoding="utf-8")
    out, _, _, _ = _ensure(tmp_path, before)
    assert env_file.read_text(encoding="utf-8") == before
    assert "Generated" not in out


def test_a_missing_env_file_is_created(tmp_path):
    _, values, _, _ = _ensure(tmp_path, None)
    assert set(NAMES) <= set(values)


@pytest.mark.parametrize("script", ["scripts/local/deploy-local.sh", "scripts/local/start-local.sh"])
def test_each_local_deploy_fills_them_before_its_first_compose_command(script):
    text = (_ROOT / script).read_text(encoding="utf-8")
    assert 'fill_local_env "$ENV_FILE"' in text and "fill-local-env.sh" in text
    call = text.index('fill_local_env "$ENV_FILE"')
    assert text.index("fill-local-env.sh") < call
    first_compose = min(m.start() for m in re.finditer(r"\$DC |docker compose |docker-compose ",
                                                       text[call:])) + call
    assert call < first_compose
