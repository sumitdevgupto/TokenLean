"""The GCP deploy points G01 at the LLMLingua sidecar's /compress route, and only when asked.

It used to patch in the bare service URL, so every call 404'd and G01 fell back silently.
Switching LLMLingua on changes what tenants' compressed prompts look like, so on GCP it is
opt-in: unless LLMLINGUA_ON_GCP=true the deploy writes an empty `sidecar_url` (LLMLingua off,
which is what tenants were getting anyway, minus a failing call per message).

The config patch is a Python heredoc inside gcp-deploy.sh; it runs here from the script's own
text in a separate Python process, with the bash placeholders substituted as bash would expand
them.
"""
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
DEPLOY = (ROOT / "scripts" / "gcp" / "gcp-deploy.sh").read_text(encoding="utf-8")
SERVICE = "https://llmlingua-svc-abc123-ew.a.run.app"
TEMPLATE_URL = "http://llmlingua-svc:8080/compress"


def _patched(tmp_path, sidecar_url, on=None, service=SERVICE):
    """Run the deploy's config patch, as the deploy does (`python3 -`), on a config holding
    `sidecar_url`; return the value it leaves."""
    block = re.search(r"python3 - <<PYEOF\n(.*?)\nPYEOF\n", DEPLOY, re.S).group(1)
    path = tmp_path / "config.yaml"
    g1 = {"enabled": True} if sidecar_url is None else {"enabled": True, "sidecar_url": sidecar_url}
    path.write_text(yaml.safe_dump({"groups": {"G1_compression": g1}}), encoding="utf-8")
    code = re.sub(r"^path = '.*'$", lambda _: f"path = {path.as_posix()!r}", block, count=1,
                  flags=re.M)
    code = code.replace("${LLMLINGUA_URL}", service).replace("${LLMLINGUA_ON_GCP:-false}",
                                                             on or "false")
    code = re.sub(r"\$\{[^}]*\}", "", code)   # every other placeholder: unset
    script = tmp_path / "patch.py"
    script.write_text(code, encoding="utf-8")
    subprocess.run([sys.executable, str(script)], check=True, capture_output=True)
    return yaml.safe_load(path.read_text(encoding="utf-8"))["groups"]["G1_compression"].get(
        "sidecar_url")


class TestLlmlinguaUrlOnGcp:
    def test_off_by_default(self, tmp_path):
        assert _patched(tmp_path, TEMPLATE_URL) == ""

    @pytest.mark.parametrize("service", [SERVICE, SERVICE + "/"])
    def test_on_points_at_the_compress_route(self, tmp_path, service):
        assert _patched(tmp_path, TEMPLATE_URL, on="true", service=service) == SERVICE + "/compress"

    @pytest.mark.parametrize("earlier", [SERVICE, SERVICE + "/", ""])
    def test_an_earlier_deploys_value_is_repaired(self, tmp_path, earlier):
        # A bare URL (the old patch), or the empty value an off deploy wrote.
        assert _patched(tmp_path, earlier, on="true") == SERVICE + "/compress"

    def test_turning_it_off_again_empties_the_working_url(self, tmp_path):
        assert _patched(tmp_path, SERVICE + "/compress") == ""

    @pytest.mark.parametrize("on", ["true", "false"])
    def test_a_sidecar_of_your_own_is_left_alone(self, tmp_path, on):
        own = "https://compress.example.com/compress"
        assert _patched(tmp_path, own, on=on) == own

    def test_nothing_changes_when_the_sidecar_was_not_deployed(self, tmp_path):
        assert _patched(tmp_path, TEMPLATE_URL, on="true", service="") == TEMPLATE_URL

    def test_the_opt_in_is_in_the_env_template(self):
        env = (ROOT / ".env.gcp.template").read_text(encoding="utf-8")
        assert re.search(r"^# LLMLINGUA_ON_GCP=true$", env, re.M)


@pytest.mark.asyncio
async def test_an_empty_sidecar_url_makes_no_call(monkeypatch, caplog):
    """Off means off: no request, and no "sidecar unavailable" warning per message."""
    import httpx
    from middleware import g01_compression

    def _no_client(*args, **kwargs):
        raise AssertionError("no sidecar call expected")

    monkeypatch.setattr(httpx, "AsyncClient", _no_client)
    assert await g01_compression._call_llmlingua("", "some text", 0.5) == "some text"
    assert "unavailable" not in caplog.text
