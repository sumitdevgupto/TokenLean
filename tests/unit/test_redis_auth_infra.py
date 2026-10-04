"""Redis requires a password that only its clients and the Redis VM itself can read, over TLS.

Neither Redis backend had a password or TLS, and the VM's firewall admits every private address
range, so any host in the VPC could read and write every tenant's cached prompts and answers,
sessions and counters, or read them on the wire. The password lives in the `redis-auth` secret:
for the VM backend a generated one; for Memorystore its AUTH string, which exists once AUTH is
on. `redis_auth_enforced` makes Redis require it, and `redis_tls` encrypts the traffic: both on
by default. The VM reads the password and its TLS key with a service account of its own at
boot, so neither sits in the instance metadata.
"""
import re
from pathlib import Path

INFRA = Path(__file__).resolve().parents[2] / "infra"
MAIN_TF = (INFRA / "main.tf").read_text(encoding="utf-8")
VARIABLES_TF = (INFRA / "variables.tf").read_text(encoding="utf-8")
OUTPUTS_TF = (INFRA / "outputs.tf").read_text(encoding="utf-8")


def _block(text: str, head: str) -> str:
    m = re.search(rf"^{re.escape(head)} \{{\n(.*?)^\}}", text, re.M | re.S)
    assert m, f"{head} is not declared"
    return m.group(1)


def test_enforcement_is_a_switch_that_starts_on():
    body = _block(VARIABLES_TF, 'variable "redis_auth_enforced"')
    assert re.search(r"type\s*=\s*bool", body) and re.search(r"default\s*=\s*true", body)


def test_tls_is_a_switch_that_starts_on():
    body = _block(VARIABLES_TF, 'variable "redis_tls"')
    assert re.search(r"type\s*=\s*bool", body) and re.search(r"default\s*=\s*true", body)


def test_memorystore_encrypts_its_traffic_once_tls_is_on():
    body = _block(MAIN_TF, 'resource "google_redis_instance" "cache"')
    assert re.search(r'transit_encryption_mode\s*=\s*var\.redis_tls \? "SERVER_AUTHENTICATION" : "DISABLED"',
                     body)


_VM_TLS = r'count\s*=\s*var\.redis_backend == "docker" && var\.redis_tls \? 1 : 0'


def test_the_vm_gets_a_certificate_from_a_ca_made_for_it():
    providers = _block(MAIN_TF, "terraform")
    assert re.search(r'tls\s*=\s*\{\s*source\s*=\s*"hashicorp/tls"', providers)
    ca = _block(MAIN_TF, 'resource "tls_self_signed_cert" "redis_ca"')
    assert re.search(_VM_TLS, ca) and re.search(r"is_ca_certificate\s*=\s*true", ca)
    assert re.search(r'allowed_uses\s*=\s*\["cert_signing"', ca)
    server = _block(MAIN_TF, 'resource "tls_locally_signed_cert" "redis_server"')
    assert re.search(_VM_TLS, server) and re.search(r'"server_auth"', server)
    assert re.search(r"ca_private_key_pem\s*=\s*tls_private_key\.redis_ca\[0\]\.private_key_pem", server)
    assert re.search(r"ca_cert_pem\s*=\s*tls_self_signed_cert\.redis_ca\[0\]\.cert_pem", server)
    for key in ("redis_ca", "redis_server"):
        assert re.search(_VM_TLS, _block(MAIN_TF, f'resource "tls_private_key" "{key}"'))


def test_the_clients_get_the_ca_and_only_the_vm_its_key():
    ca = _block(MAIN_TF, 'resource "google_secret_manager_secret_version" "redis_ca"')
    assert re.search(r"count\s*=\s*var\.redis_tls \? 1 : 0", ca)
    assert re.search(r'secret_data\s*=\s*var\.redis_backend == "memorystore" \? '
                     r'join\("", google_redis_instance\.cache\[0\]\.server_ca_certs\[\*\]\.cert\) : '
                     r'tls_self_signed_cert\.redis_ca\[0\]\.cert_pem', ca)
    assert re.search(r'secret_id\s*=\s*"redis-ca"',
                     _block(MAIN_TF, 'resource "google_secret_manager_secret" "redis_ca"'))
    server = _block(MAIN_TF, 'resource "google_secret_manager_secret_version" "redis_tls_server"')
    assert re.search(_VM_TLS, server)
    for part in (r"tls_locally_signed_cert\.redis_server\[0\]\.cert_pem",
                 r"tls_self_signed_cert\.redis_ca\[0\]\.cert_pem",
                 r"tls_private_key\.redis_server\[0\]\.private_key_pem"):
        assert re.search(part, server), part
    reader = _block(MAIN_TF, 'resource "google_secret_manager_secret_iam_member" "redis_vm_reads_tls_key"')
    assert re.search(r"secret_id\s*=\s*google_secret_manager_secret\.redis_tls_server\[0\]\.secret_id", reader)
    assert re.search(r"google_service_account\.redis_vm\[0\]\.email", reader)
    ids = re.search(r"proxy_secret_ids = var\.least_privilege_secret_iam \? concat\(\[(.*?)\]",
                    MAIN_TF, re.S).group(1)
    assert "google_secret_manager_secret.redis_ca.secret_id" in ids
    assert "redis_tls_server" not in ids   # the private key: the VM's alone


def test_the_vm_reads_its_tls_key_itself_and_boots_with_it():
    vm = _block(MAIN_TF, 'resource "google_compute_instance" "redis"')
    for leak in ("private_key_pem", "cert_pem", "tls_private_key"):
        assert leak not in vm, leak
    assert re.search(r'redis-tls\s*=\s*var\.redis_tls \? "true" : "false"', vm)
    assert re.search(r"redis-tls-secret\s*=\s*google_secret_manager_secret\.redis_tls_server\[0\]\.id", vm)
    # Its first boot finds both secrets: the startup script reads them only at boot.
    deps = re.search(r"depends_on\s*=\s*\[(.*?)\]", vm, re.S).group(1)
    assert "google_secret_manager_secret_version.redis_tls_server" in deps
    assert "google_secret_manager_secret_version.redis_auth" in deps


def test_the_clients_are_given_a_rediss_url_on_the_right_port():
    url = _block(OUTPUTS_TF, 'output "redis_url"')
    assert re.search(r'var\.redis_tls \? "rediss" : "redis"', url)
    assert "google_redis_instance.cache[0].port" in url   # 6378 once Memorystore has TLS
    assert "google_compute_instance.redis[0].network_interface[0].network_ip" in url


def test_memorystore_requires_auth_once_the_switch_is_on():
    body = _block(MAIN_TF, 'resource "google_redis_instance" "cache"')
    assert re.search(r"auth_enabled\s*=\s*var\.redis_auth_enforced", body)


def test_the_secret_holds_the_vm_password_always_and_memorystore_s_once_it_exists():
    secret = _block(MAIN_TF, 'resource "google_secret_manager_secret" "redis_auth"')
    assert re.search(r'secret_id\s*=\s*"redis-auth"', secret)
    version = _block(MAIN_TF, 'resource "google_secret_manager_secret_version" "redis_auth"')
    assert re.search(r'count\s*=\s*var\.redis_backend == "docker" \|\| var\.redis_auth_enforced \? 1 : 0',
                     version)
    assert re.search(r'secret_data\s*=\s*var\.redis_backend == "memorystore" \? '
                     r'one\(google_redis_instance\.cache\[\*\]\.auth_string\) : '
                     r'random_password\.redis_auth\.result', version)


def test_the_vm_reads_the_password_itself_and_never_from_its_metadata():
    vm = _block(MAIN_TF, 'resource "google_compute_instance" "redis"')
    for leak in ("requirepass", "random_password", "auth_string", "secret_data"):
        assert leak not in vm, leak
    declaration = re.search(r"gce-container-declaration = <<-EOT\n(.*?)EOT", vm, re.S).group(1)
    assert 'args: ["redis-server", "/etc/redis/redis.conf"]' in declaration
    assert "mountPath: /etc/redis" in declaration and "path: /var/lib/token-opt-redis" in declaration
    assert re.search(r'startup-script\s*=\s*replace\(file\("\$\{path\.module\}/redis-vm-startup\.sh"\), '
                     r'"\\r", ""\)', vm)
    assert re.search(r"redis-auth-secret\s*=\s*google_secret_manager_secret\.redis_auth\.id", vm)
    assert re.search(r'redis-auth-enforced\s*=\s*var\.redis_auth_enforced \? "true" : "false"', vm)
    assert re.search(r'enable-guest-attributes\s*=\s*"TRUE"', vm)
    assert re.search(r"allow_stopping_for_update\s*=\s*true", vm)
    assert re.search(r"email\s*=\s*google_service_account\.redis_vm\[0\]\.email", vm)


def test_the_vm_account_may_read_its_two_secrets_and_write_its_logs_only():
    member = _block(MAIN_TF, 'resource "google_secret_manager_secret_iam_member" "redis_vm_reads_password"')
    assert re.search(r"secret_id\s*=\s*google_secret_manager_secret\.redis_auth\.secret_id", member)
    assert re.search(r'role\s*=\s*"roles/secretmanager\.secretAccessor"', member)
    logs = _block(MAIN_TF, 'resource "google_project_iam_member" "redis_vm_log_writer"')
    assert re.search(r'role\s*=\s*"roles/logging\.logWriter"', logs)
    grants = re.findall(r'member\s*=\s*"serviceAccount:\$\{google_service_account\.redis_vm\[0\]\.email\}"',
                        MAIN_TF)
    assert len(grants) == 3   # the password, the TLS key, its logs


def test_with_least_privilege_secret_access_the_proxy_may_still_read_it():
    ids = re.search(r"proxy_secret_ids = var\.least_privilege_secret_iam \? concat\(\[(.*?)\]",
                    MAIN_TF, re.S)
    assert ids and "google_secret_manager_secret.redis_auth.secret_id" in ids.group(1)
