"""The services that are not the proxy run as service accounts of their own.

Grafana (public), Langfuse, Qdrant, Prometheus and Alertmanager ran as the proxy's service
account, so a compromise of any of them gave what the proxy has: its provider keys, the
database password, KMS decryption of tenants' stored provider keys, Cloud SQL and every Cloud
Run service; on the open-source default, every secret and bucket in the project as well. Each
now runs as an account holding only what that service uses, and the proxy's account reads only
its own secrets unless an operator turns `least_privilege_secret_iam` off.
"""
import re
from pathlib import Path

import pytest

INFRA = Path(__file__).resolve().parents[2] / "infra"
MAIN_TF = (INFRA / "main.tf").read_text(encoding="utf-8")
VARIABLES_TF = (INFRA / "variables.tf").read_text(encoding="utf-8")
OUTPUTS_TF = (INFRA / "outputs.tf").read_text(encoding="utf-8")

# Each service's account, and the secrets that service mounts.
OWN = {
    "qdrant": ("qdrant_sa", {"qdrant_api_key"}),
    "prometheus": ("prometheus_sa", {"prometheus_config", "prometheus_alerts"}),
    "alertmanager": ("alertmanager_sa", {"alertmanager_config"}),
    "grafana": ("grafana_sa", {"grafana_admin_password", "db_password"}),
    "langfuse": ("langfuse_sa", {"langfuse_nextauth_secret", "db_password"}),
}
ACCOUNTS = [sa for sa, _ in OWN.values()]


def _resources(kind: str) -> dict:
    """name -> body of every top-level `resource "<kind>" "<name>"` block in main.tf."""
    return {m.group(1): m.group(2) for m in re.finditer(
        rf'^resource "{re.escape(kind)}" "(\w+)" \{{\n(.*?)^\}}', MAIN_TF, re.M | re.S)}


def _all_resources():
    """(kind, name, body) of every top-level resource block in main.tf."""
    return [(m.group(1), m.group(2), m.group(3)) for m in re.finditer(
        r'^resource "(\w+)" "(\w+)" \{\n(.*?)^\}', MAIN_TF, re.M | re.S)]


def _email(sa: str) -> str:
    return f"google_service_account.{sa}.email"


def _granted_to(kind: str, sa: str) -> dict:
    """name -> body of the `kind` blocks whose member is `sa`."""
    member = f'"serviceAccount:${{{_email(sa)}}}"'
    return {name: body for name, body in _resources(kind).items()
            if re.search(rf"member\s*=\s*{re.escape(member)}", body)}


@pytest.mark.parametrize("service", ["qdrant", "prometheus", "alertmanager"])
def test_each_terraform_service_runs_as_its_own_account(service):
    body = _resources("google_cloud_run_v2_service")[service]
    sa, _ = OWN[service]
    assert re.search(rf"service_account\s*=\s*{re.escape(_email(sa))}\n", body)
    assert "proxy_sa" not in body


@pytest.mark.parametrize("service", sorted(OWN))
def test_each_account_reads_only_the_secrets_its_service_mounts(service):
    sa, secrets = OWN[service]
    bindings = _granted_to("google_secret_manager_secret_iam_member", sa)
    granted = set()
    for body in bindings.values():
        assert re.search(r'role\s*=\s*"roles/secretmanager\.secretAccessor"', body)
        granted |= set(re.findall(r"google_secret_manager_secret\.(\w+)\.secret_id", body))
    assert granted == secrets


def test_only_grafana_and_langfuse_hold_a_project_role_and_it_is_cloud_sql_client():
    for sa in ACCOUNTS:
        roles = sorted(re.search(r'role\s*=\s*"([^"]+)"', body).group(1)
                       for body in _granted_to("google_project_iam_member", sa).values())
        assert roles == (["roles/cloudsql.client"] if sa in ("grafana_sa", "langfuse_sa") else []), sa


def test_the_only_calls_they_may_make_are_grafana_to_prometheus_to_alertmanager():
    calls = {}
    for sa in ACCOUNTS:
        for body in _granted_to("google_cloud_run_v2_service_iam_member", sa).values():
            assert re.search(r'role\s*=\s*"roles/run\.invoker"', body)
            target = re.search(r"name\s*=\s*google_cloud_run_v2_service\.(\w+)\[0\]\.name", body)
            calls.setdefault(sa, []).append(target.group(1))
    assert calls == {"grafana_sa": ["prometheus"], "prometheus_sa": ["alertmanager"]}


@pytest.mark.parametrize("sa", ACCOUNTS)
def test_no_other_resource_grants_them_anything(sa):
    kinds = {kind for kind, name, body in _all_resources()
             if _email(sa) in body and kind != "google_service_account"}
    allowed = {"google_cloud_run_v2_service", "google_secret_manager_secret_iam_member",
               "google_project_iam_member", "google_cloud_run_v2_service_iam_member"}
    if sa == "qdrant_sa":   # reads the snapshots it restores: the next test pins that grant
        allowed.add("google_storage_bucket_iam_member")
    assert kinds <= allowed


def test_qdrants_one_bucket_grant_is_reading_its_snapshots():
    grants = [body for kind, name, body in _all_resources()
              if kind == "google_storage_bucket_iam_member" and _email("qdrant_sa") in body]
    assert len(grants) == 1
    assert re.search(r'role\s*=\s*"roles/storage\.objectViewer"', grants[0])
    assert re.search(r"bucket\s*=\s*google_storage_bucket\.qdrant_snapshots\[0\]\.name", grants[0])


@pytest.mark.parametrize("sa", ACCOUNTS)
def test_the_account_ids_are_valid_and_distinct(sa):
    body = _resources("google_service_account")[sa]
    account_id = re.search(r'account_id\s*=\s*"([^"]+)"', body).group(1)
    assert re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", account_id), account_id
    ids = re.findall(r'account_id\s*=\s*"([^"]+)"', MAIN_TF)
    assert ids.count(account_id) == 1


def test_the_proxy_reads_only_its_own_secrets_by_default():
    body = re.search(r'^variable "least_privilege_secret_iam" \{\n(.*?)^\}', VARIABLES_TF,
                     re.M | re.S).group(1)
    assert re.search(r"default\s*=\s*true", body)
    ids = re.search(r"proxy_secret_ids = var\.least_privilege_secret_iam \? concat\(\[(.*?)\]",
                    MAIN_TF, re.S).group(1)
    listed = set(re.findall(r"google_secret_manager_secret\.(\w+)\.secret_id", ids))
    assert {"db_password", "qdrant_api_key", "metrics_scrape_token"} <= listed
    # Grafana's and Langfuse's own secrets are theirs alone now.
    assert not listed & {"grafana_admin_password", "langfuse_nextauth_secret"}


def test_the_proxy_may_read_its_key_stores_version_names_and_no_more_of_it():
    # Each proxy instance asks for the keys secret's latest version name every few seconds, so
    # a key revoked elsewhere stops working within seconds. secretAccessor reads payloads only;
    # viewer on that one secret reads the names, and no secret value. In either IAM mode.
    grants = [body for kind, name, body in _all_resources()
              if kind == "google_secret_manager_secret_iam_member"
              and _email("proxy_sa") in body and "secretmanager.viewer" in body]
    assert len(grants) == 1
    assert re.search(r"secret_id\s*=\s*google_secret_manager_secret\.proxy_api_keys\.secret_id\n",
                     grants[0])
    assert not re.search(r"^\s*(count|for_each)\s*=", grants[0], re.M)


def test_the_routellm_account_only_reads_its_key_and_the_proxy_cannot_act_as_it():
    using = [(kind, name) for kind, name, body in _all_resources()
             if _email("routellm_sa") in body or "google_service_account.routellm_sa.name" in body]
    assert using == [("google_secret_manager_secret_iam_member", "routellm_secret_accessor")]


@pytest.mark.parametrize("service", ["grafana", "langfuse"])
def test_the_deploy_reads_grafana_s_and_langfuse_s_accounts_from_terraform(service):
    m = re.search(rf'^output "{service}_service_account_email" \{{\n(.*?)^\}}', OUTPUTS_TF,
                  re.M | re.S)
    assert m and re.search(rf"value\s*=\s*{re.escape(_email(service + '_sa'))}\n", m.group(1))
