import os
import re
import threading
import logging
from typing import Any, Dict

import yaml

logger = logging.getLogger(__name__)

_config: Dict[str, Any] = {}
_lock = threading.RLock()
_RELOAD_INTERVAL = int(os.getenv("CONFIG_RELOAD_INTERVAL_SECONDS", "60"))

# Matches ${VAR} and ${VAR:-default} (shell-style) inside config string values.
_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def expand_env_vars(obj: Any) -> Any:
    """Recursively resolve ``${VAR}`` / ``${VAR:-default}`` placeholders in string
    config values against ``os.environ``.

    This keeps every tunable default in config.yaml + .env and OUT of the codebase:
    config.yaml references ``${VAR}``, and the value (or its inline ``:-default``) is
    supplied via the environment — .env locally, Cloud Run / Secret Manager in prod.
    An unset var with no default resolves to an empty string (never left as the literal
    ``${...}``). Only ``${...}`` is touched, so ``str.format``-style ``{tenant_id}``
    templates are left intact.
    """
    if isinstance(obj, dict):
        return {k: expand_env_vars(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [expand_env_vars(v) for v in obj]
    if isinstance(obj, str):
        def _sub(m: "re.Match") -> str:
            var, default = m.group(1), m.group(2)
            val = os.environ.get(var)
            if val is not None:
                return val
            return default if default is not None else ""
        return _ENV_VAR_PATTERN.sub(_sub, obj)
    return obj


def _load_from_gcs(bucket: str, blob: str) -> Dict[str, Any]:
    from google.cloud import storage  # lazy import

    client = storage.Client()
    data = client.bucket(bucket).blob(blob).download_as_text()
    return yaml.safe_load(data)


def _load_from_file(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def merge_params_dir(base: Dict[str, Any], params_dir: str) -> Dict[str, Any]:
    """Merge all *.yaml files from ``params_dir`` into ``base`` config.

    Files are loaded in alphabetical order so later files win on key conflicts.
    Non-YAML files are silently skipped.  A missing directory logs a warning
    and returns ``base`` unchanged.
    """
    import copy
    if not params_dir or not os.path.isdir(params_dir):
        if params_dir:
            logger.warning("params_dir '%s' not found — skipping param merge", params_dir)
        return base

    result = copy.deepcopy(base)
    for fname in sorted(os.listdir(params_dir)):
        if not fname.endswith((".yaml", ".yml")):
            continue
        fpath = os.path.join(params_dir, fname)
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            for k, v in data.items():
                if isinstance(v, dict) and isinstance(result.get(k), dict):
                    result[k].update(v)
                else:
                    result[k] = v
            logger.debug("Merged params file: %s", fname)
        except Exception as exc:
            logger.warning("Failed to load params file %s: %s", fpath, exc)
    return result


def load_config() -> Dict[str, Any]:
    global _config
    gcs_bucket = os.getenv("CONFIG_GCS_BUCKET", "")
    gcs_blob = os.getenv("CONFIG_GCS_BLOB", "config/config.yaml")
    local_path = os.getenv("CONFIG_LOCAL_PATH", "config/config.yaml")

    storage_backend = os.getenv("STORAGE_BACKEND", "gcs").lower().strip()
    try:
        raw = (
            _load_from_gcs(gcs_bucket, gcs_blob)
            if gcs_bucket and storage_backend != "local"
            else _load_from_file(local_path)
        )
        # Merge any per-feature param files from params_dir
        params_dir = raw.get("params_dir", "")
        if params_dir:
            raw = merge_params_dir(raw, params_dir)
        # Resolve ${VAR} / ${VAR:-default} placeholders from the environment so all
        # tunable defaults live in config.yaml + .env, never hardcoded in the proxy.
        raw = expand_env_vars(raw)
        with _lock:
            _config = raw
        logger.info("Config loaded successfully")
    except Exception as exc:
        logger.error("Config load failed: %s — using last known good", exc)
    return _config


def get_config() -> Dict[str, Any]:
    with _lock:
        return _config


def get_group_config(group_key: str) -> Dict[str, Any]:
    """Return config for a G-group by its YAML key, e.g. 'G1_compression'."""
    return get_config().get("groups", {}).get(group_key, {})


def is_group_enabled(group_key: str) -> bool:
    return bool(get_group_config(group_key).get("enabled", False))


def get_proxy_config() -> Dict[str, Any]:
    return get_config().get("proxy", {})


def get_providers() -> list:
    return get_config().get("providers", [])


def get_default_model() -> str:
    """Return the default model from proxy config. Logs a warning if not set."""
    model = get_proxy_config().get("default_model", "")
    if not model:
        logger.warning("proxy.default_model not set in config — returning empty string")
    return model


def get_fallback_request_model() -> str:
    """Model used when a developer request omits the model field."""
    model = get_proxy_config().get("fallback_request_model", "")
    if not model:
        logger.warning("proxy.fallback_request_model not set in config — returning empty string")
    return model


def get_default_provider() -> str:
    """Provider used when no model_prefix matches (replaces the old hardcoded "openai").

    Reads ``proxy.default_provider``; falls back to the first configured provider, then
    "openai" so existing single-provider deployments are unchanged.
    """
    name = get_proxy_config().get("default_provider", "")
    if name:
        return name
    providers = get_providers()
    if providers:
        return providers[0].get("name", "openai")
    return "openai"


def get_pricing_table() -> dict:
    """Return the pricing table from config (model fragment → {input, output} per 1k tokens)."""
    return get_config().get("pricing", {})


def get_known_models() -> set:
    """Return the set of model names explicitly listed under providers[].models."""
    models: set = set()
    for provider in get_providers():
        for model in provider.get("models", []):
            models.add(model)
    return models


# Built-in fallback used only when no provider in config declares model_prefixes
# (e.g. before load_config() has run). Production config always overrides this —
# when providers[].model_prefixes are present, the config-derived mapping wins —
# so detection degrades gracefully instead of returning nothing.
_DEFAULT_PROVIDER_PREFIXES = {
    "gpt": "openai", "o1": "openai", "o3": "openai", "o4": "openai", "text-": "openai",
    "claude": "anthropic",
    "gemini": "gemini",
}


def get_provider_model_prefixes() -> dict:
    """Return a flat dict of {model_prefix: provider_name} built from providers[].model_prefixes.

    Falls back to ``_DEFAULT_PROVIDER_PREFIXES`` only when config supplies none,
    so provider detection still works before config has loaded.
    """
    mapping: dict = {}
    for provider in get_providers():
        name = provider.get("name", "")
        for prefix in provider.get("model_prefixes", []):
            mapping[prefix] = name
    return mapping or dict(_DEFAULT_PROVIDER_PREFIXES)


def get_tiktoken_prefixes() -> list:
    """Return the list of model-name prefixes that should use tiktoken tokenisation."""
    for provider in get_providers():
        prefixes = provider.get("tiktoken_prefixes", [])
        if prefixes:
            return prefixes
    return []


def start_hot_reload() -> None:
    """Daemon thread that reloads config every CONFIG_RELOAD_INTERVAL_SECONDS."""
    import time

    def _loop():
        while True:
            time.sleep(_RELOAD_INTERVAL)
            load_config()

    thread = threading.Thread(target=_loop, daemon=True, name="config-hot-reload")
    thread.start()
    logger.info("Config hot-reload started (interval=%ds)", _RELOAD_INTERVAL)
