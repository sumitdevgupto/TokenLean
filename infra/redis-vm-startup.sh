#!/bin/bash
# Boot script of token-opt-redis-vm (main.tf: metadata startup-script). Writes the config file of
# the VM's redis container (the container declaration mounts /var/lib/token-opt-redis at
# /etc/redis, and the container restarts until the file exists), then reports how Redis runs in
# the guest attributes token-opt/redis-auth ("enforced" or "open") and token-opt/redis-tls ("on:"
# and the version of the certificate's secret, or "off"), which gcp-deploy.sh reads.
#
# With redis-auth-enforced=true in the instance metadata, the config sets requirepass to the
# Redis password, read from the Secret Manager secret named in redis-auth-secret with the VM's
# own service account. With redis-tls=true, Redis serves only TLS, with the certificate, CA and
# key read the same way from the secret named in redis-tls-secret. Neither goes into the
# metadata, which whoever can view the instance can read. If either cannot be read, no config
# is left, so Redis cannot start without it.
#
# METADATA_URL, SECRET_MANAGER_URL and REDIS_CONF_DIR exist for the tests.
set -euo pipefail

MD="${METADATA_URL:-http://metadata.google.internal/computeMetadata/v1}"
SM="${SECRET_MANAGER_URL:-https://secretmanager.googleapis.com/v1}"
CONF_DIR="${REDIS_CONF_DIR:-/var/lib/token-opt-redis}"
CONF="$CONF_DIR/redis.conf"
IN_CONTAINER=/etc/redis   # where the container sees CONF_DIR

metadata() {
  curl -sSf --retry 10 --retry-delay 3 --retry-connrefused -H "Metadata-Flavor: Google" "$MD/$1"
}

report() {  # best effort: gcp-deploy.sh reads them, Redis does not need them
  curl -sf -X PUT --data "$2" -H "Metadata-Flavor: Google" \
    "$MD/instance/guest-attributes/token-opt/$1" >/dev/null || true
}

access() {  # the latest version of the secret named in metadata attribute $1, as Secret Manager's JSON
  local token secret
  token="$(metadata instance/service-accounts/default/token \
             | sed -n 's/.*"access_token" *: *"\([^"]*\)".*/\1/p')" || token=""
  secret="$(metadata "instance/attributes/$1")" || secret=""
  curl -sSf --retry 10 --retry-delay 3 -H "Authorization: Bearer $token" \
    "$SM/$secret/versions/latest:access"
}

payload() {  # the decoded data of a secret version read by access
  sed -n 's/.*"data" *: *"\([^"]*\)".*/\1/p' | base64 -d 2>/dev/null
}

secure() {  # readable by the redis user of the redis:7.2-alpine image only
  chmod 0400 "$1"
  chown 999:1000 "$1" 2>/dev/null || true
}

mkdir -p "$CONF_DIR"
tmp="$(mktemp "$CONF_DIR/.redis.conf.XXXXXX")"
t_crt="$(mktemp "$CONF_DIR/.tls.crt.XXXXXX")"
t_ca="$(mktemp "$CONF_DIR/.ca.crt.XXXXXX")"
t_key="$(mktemp "$CONF_DIR/.tls.key.XXXXXX")"
trap 'rm -f "$tmp" "$t_crt" "$t_ca" "$t_key"' EXIT
printf 'appendonly no\nsave ""\n' > "$tmp"

auth_enforced=false tls_wanted=false
[[ "$(metadata instance/attributes/redis-auth-enforced 2>/dev/null || true)" == "true" ]] && auth_enforced=true
[[ "$(metadata instance/attributes/redis-tls 2>/dev/null || true)" == "true" ]] && tls_wanted=true
# A config from an earlier boot, without what this boot asks for, must not stay.
if $auth_enforced || $tls_wanted; then rm -f "$CONF"; fi

state=open
if $auth_enforced; then
  password="$(access redis-auth-secret | payload)" || password=""
  if [[ -z "$password" ]]; then
    echo "redis-vm-startup: the Redis password could not be read; Redis will not start" >&2
    report redis-auth failed
    exit 1
  fi
  printf 'requirepass %s\n' "$password" >> "$tmp"
  state=enforced
fi

tls_state=off
if $tls_wanted; then
  version="" pem=""
  if response="$(access redis-tls-secret)"; then
    version="$(printf '%s\n' "$response" | sed -n 's/.*"name" *: *"[^"]*\/versions\/\([0-9]*\)".*/\1/p')"
    pem="$(printf '%s\n' "$response" | payload)" || pem=""
  fi
  # Redis's certificate first, then the CA's, then Redis's key (main.tf redis_tls_server).
  printf '%s\n' "$pem" | awk -v crt="$t_crt" -v ca="$t_ca" -v key="$t_key" '
    /-----BEGIN CERTIFICATE-----/ { n++; out = (n == 1 ? crt : ca) }
    /-----BEGIN [A-Z ]*PRIVATE KEY-----/ { out = key }
    out != "" { print > out }
    /-----END / { out = "" }'
  if [[ -z "$version" || ! -s "$t_crt" || ! -s "$t_ca" || ! -s "$t_key" ]]; then
    echo "redis-vm-startup: the Redis TLS certificate or key could not be read; Redis will not start" >&2
    report redis-tls failed
    exit 1
  fi
  for f in "$t_crt" "$t_ca" "$t_key"; do secure "$f"; done
  mv -f "$t_crt" "$CONF_DIR/tls.crt"
  mv -f "$t_ca" "$CONF_DIR/ca.crt"
  mv -f "$t_key" "$CONF_DIR/tls.key"
  # TLS only: no plaintext port. Clients are not asked for a certificate: they log in with
  # the password.
  printf 'port 0\ntls-port 6379\ntls-cert-file %s/tls.crt\ntls-key-file %s/tls.key\ntls-ca-cert-file %s/ca.crt\ntls-auth-clients no\n' \
    "$IN_CONTAINER" "$IN_CONTAINER" "$IN_CONTAINER" >> "$tmp"
  tls_state="on:$version"
else
  rm -f "$CONF_DIR/tls.crt" "$CONF_DIR/ca.crt" "$CONF_DIR/tls.key"
fi

secure "$tmp"
mv -f "$tmp" "$CONF"
rm -f "$t_crt" "$t_ca" "$t_key"
trap - EXIT
report redis-auth "$state"
report redis-tls "$tls_state"
echo "redis-vm-startup: Redis config written ($state, TLS $tls_state)"
