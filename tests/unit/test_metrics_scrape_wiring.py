"""Every way of running the proxy either has a /metrics scrape token or says it opens /metrics.

/metrics lists tenant ids with their token and cost figures. With no METRICS_SCRAPE_TOKEN the
proxy served it to anyone who could reach the port; it now refuses unless
METRICS_ALLOW_UNAUTHENTICATED is on. Terraform generates a token when none is configured
(the variable defaults to empty, and its Prometheus presents the same one), and only the local
compose file turns the flag on, for its own Prometheus, which sends no token.
"""
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
_MAIN_TF = (ROOT / "infra" / "main.tf").read_text(encoding="utf-8")


def _block(text: str, header: str) -> str:
    assert header in text, f"no {header}"
    start = text.index(header)
    depth = 0
    for j in range(text.index("{", start), len(text)):
        depth += {"{": 1, "}": -1}.get(text[j], 0)
        if depth == 0:
            return text[start:j + 1]
    raise AssertionError(f"unbalanced block at {header}")


def _resolve(spec, env: dict) -> str:
    """Interpolate ${VAR:-default} the way compose does (a bare `true` loads as a bool)."""
    return re.sub(r"\$\{(\w+):-([^}]*)\}", lambda m: env.get(m.group(1)) or m.group(2), str(spec))


def test_terraform_always_has_a_token_and_prometheus_presents_it():
    generated = _block(_MAIN_TF, 'resource "random_password" "metrics_scrape_token"')
    assert "special = false" in generated
    token = _MAIN_TF.split("  metrics_scrape_token = var.metrics_scrape_token", 1)
    assert len(token) == 2, "no local.metrics_scrape_token"
    assert token[1].split("\n", 1)[0] == (
        ' != "" ? var.metrics_scrape_token : random_password.metrics_scrape_token.result')
    version = _block(_MAIN_TF,
                     'resource "google_secret_manager_secret_version" "metrics_scrape_token"')
    assert "count" not in version
    assert "secret_data = local.metrics_scrape_token" in version
    rendered = _MAIN_TF.split('templatefile("${path.module}/prometheus.yml.tmpl", {', 1)[1]
    assert "metrics_scrape_token = local.metrics_scrape_token" in rendered.split("})", 1)[0]


def test_prometheus_verifies_the_proxys_certificate():
    # The token went out over TLS with verification off: anyone on the path could present a
    # certificate and take it. The proxy's run.app certificate is publicly trusted.
    template = (ROOT / "infra" / "prometheus.yml.tmpl").read_text(encoding="utf-8")
    job = template.split("job_name: token-opt-proxy", 1)[1]
    assert "scheme: https" in job
    assert "insecure_skip_verify" not in template


def test_a_token_set_before_keeps_its_secret_version():
    moved = _block(_MAIN_TF, "moved {\n  from = google_secret_manager_secret_version.metrics")
    assert "from = google_secret_manager_secret_version.metrics_scrape_token[0]" in moved
    assert "to   = google_secret_manager_secret_version.metrics_scrape_token\n" in moved


def test_the_gcp_deploy_mounts_the_token_and_never_the_flag():
    deploy = (ROOT / "scripts" / "gcp" / "gcp-deploy.sh").read_text(encoding="utf-8")
    assert "METRICS_SCRAPE_TOKEN=token-opt-metrics-scrape-token:latest" in deploy
    assert deploy.count("${METRICS_SECRET_FLAG") == 2      # a fresh deploy and an update
    assert "METRICS_ALLOW_UNAUTHENTICATED" not in deploy


def test_only_the_local_compose_opens_metrics_and_it_can_be_turned_off():
    services = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))["services"]
    env = services["proxy"]["environment"]
    assert {"METRICS_ALLOW_UNAUTHENTICATED", "METRICS_SCRAPE_TOKEN"} <= set(env)
    assert _resolve(env["METRICS_ALLOW_UNAUTHENTICATED"], {}) == "true"
    assert _resolve(env["METRICS_ALLOW_UNAUTHENTICATED"],
                    {"METRICS_ALLOW_UNAUTHENTICATED": "false"}) == "false"
    assert _resolve(env["METRICS_SCRAPE_TOKEN"], {"METRICS_SCRAPE_TOKEN": "t"}) == "t"
    main = (ROOT / "src" / "proxy" / "main.py").read_text(encoding="utf-8")
    assert '_env_flag("METRICS_ALLOW_UNAUTHENTICATED")' in main


def test_the_env_template_documents_both():
    template = (ROOT / ".env.template").read_text(encoding="utf-8")
    assert re.search(r"^METRICS_SCRAPE_TOKEN=$", template, re.M)
    assert re.search(r"^# METRICS_ALLOW_UNAUTHENTICATED=false$", template, re.M)
