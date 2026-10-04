#!/usr/bin/env bash
# Sourced by the local deploys (scripts/local/deploy-local.sh, start-local.sh and the commercial
# local deploy) before their first docker compose command.
#
# docker-compose.yml will not start without the local stack's own logins: the Redis password,
# the Qdrant API key, and the Langfuse and Grafana admin passwords. Any that has no value is
# generated (random, never printed) into .env, replacing the empty placeholder .env.template
# ships or appended, and exported for the deploy that sourced this. A value already set, in
# .env or in the environment, is kept.
LOCAL_STACK_LOGINS=(REDIS_PASSWORD QDRANT_API_KEY LANGFUSE_INIT_USER_PASSWORD GRAFANA_PASSWORD)

_random_login() {
  openssl rand -hex 24 2>/dev/null \
    || python3 -c 'import secrets; print(secrets.token_hex(24))' 2>/dev/null \
    || python -c 'import secrets; print(secrets.token_hex(24))'
}

fill_local_env() {
  local env_file="$1" name value
  [[ -f "$env_file" ]] || : > "$env_file"
  for name in "${LOCAL_STACK_LOGINS[@]}"; do
    [[ -n "${!name:-}" ]] && continue
    value="$(_random_login)" || return 1
    [[ -n "$value" ]] || return 1
    if grep -q "^${name}=" "$env_file"; then
      # The placeholder line, rewritten in place (no other line changes).
      awk -v name="$name" -v value="$value" \
        'index($0, name "=") == 1 { print name "=" value; next } { print }' \
        "$env_file" > "${env_file}.tmp" && mv -f "${env_file}.tmp" "$env_file"
    else
      [[ -s "$env_file" && -n "$(tail -c 1 "$env_file")" ]] && printf '\n' >> "$env_file"
      printf '%s=%s\n' "$name" "$value" >> "$env_file"
    fi
    export "${name}=${value}"
    info "Generated ${name} into $(basename "$env_file")"
  done
}
