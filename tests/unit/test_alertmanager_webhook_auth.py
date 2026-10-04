"""Alertmanager authenticates to the proxy's alert webhook, and only to it.

It posted to /admin/alert-webhook with no credentials while the endpoint required an admin
key, so every alert was refused; with no proxy URL it posted to localhost inside its own
container. Terraform now generates a token the proxy reads as ALERT_WEBHOOK_TOKEN, gives it
to Alertmanager for the proxy's URL only (never to an alert_webhook_url of the operator's
own), and leaves the receiver empty, with a plan-time warning, when there is nowhere to send.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _block(text: str, header: str) -> str:
    assert header in text, f"no {header}"
    start = text.index(header)
    depth = 0
    for j in range(text.index("{", start), len(text)):
        depth += {"{": 1, "}": -1}.get(text[j], 0)
        if depth == 0:
            return text[start:j + 1]
    raise AssertionError(f"unbalanced block at {header}")


_MAIN_TF = (ROOT / "infra" / "main.tf").read_text(encoding="utf-8")


def test_the_token_goes_to_the_proxy_url_only():
    config = _block(_MAIN_TF, 'resource "google_secret_manager_secret_version" "alertmanager_config"')
    assert "localhost" not in config
    proxy_only = config.split('%{if var.alert_webhook_url == ""~}', 1)
    assert len(proxy_only) == 2, "the credentials are not conditional on the proxy URL"
    assert "${random_password.alert_webhook_token.result}" not in proxy_only[0]
    branch = proxy_only[1].split("%{endif~}", 1)[0]
    assert "type: Bearer" in branch
    assert "credentials: '${random_password.alert_webhook_token.result}'" in branch


def test_with_nowhere_to_send_the_receiver_is_empty_and_plan_warns():
    config = _block(_MAIN_TF, 'resource "google_secret_manager_secret_version" "alertmanager_config"')
    assert '%{if local.alert_webhook_target != ""~}' in config
    target = _MAIN_TF.split("alert_webhook_target =", 1)[1].split("\n}", 1)[0]
    assert "localhost" not in target and target.rstrip().endswith(': "")')
    check = _block(_MAIN_TF, 'check "alertmanager_has_a_receiver"')
    assert "local.alert_webhook_target" in check


def test_the_proxy_can_read_the_token_and_deploy_mounts_it():
    assert "google_secret_manager_secret.alert_webhook_token.secret_id," in _MAIN_TF
    deploy = (ROOT / "scripts" / "gcp" / "gcp-deploy.sh").read_text(encoding="utf-8")
    assert "ALERT_WEBHOOK_TOKEN=token-opt-alert-webhook-token:latest" in deploy
    assert deploy.count("${ALERT_WEBHOOK_SECRET_FLAG") == 2      # a fresh deploy and an update
