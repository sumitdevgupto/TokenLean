import hashlib
import json
import logging
import os
import secrets
import threading
from datetime import datetime, timezone
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

_GCP_PROJECT = os.getenv("GCP_PROJECT_ID", "")
_PROXY_KEYS_SECRET = os.getenv("PROXY_KEYS_SECRET_NAME", "token-proxy-api-keys")
_LOCAL_PROXY_KEYS_FILE = os.getenv("LOCAL_PROXY_KEYS_FILE", "")

# In-process cache — refreshed on each validate call after TTL (simple approach)
_KEY_CACHE: dict = {}
_CACHE_TTL_SECONDS = int(os.getenv("KEY_CACHE_TTL_SECONDS", "300"))
import time
_CACHE_LOADED_AT: float = 0.0

# ── Pluggable key-store backend seam (WS24) ──────────────────────────────────
# OSS default: the JSON blob (local file / Secret Manager) below — byte-identical
# behaviour. The commercial layer may install a Postgres-backed store at startup
# (auth/pg_key_store.py) to lift the ~300-key blob ceiling + cross-instance write
# races. Contract: load_fn() -> dict|None (None = "no fresh data, keep the current
# cache" — used on the event-loop thread where blocking is forbidden);
# persist_fn(store) writes the FULL store. Both are sync callables; writes always
# run on worker threads (asyncio.to_thread), so persist_fn may block.
# Optional transact_fn(mutation) runs one lifecycle change atomically: it reads the
# current store, calls mutation(store) -> (changed, result), writes the store when
# changed, and returns (result, store_after). A store shared by several instances must
# install it: the process-local write lock cannot serialise their writes.
# Optional on_change(tenant_id) is called after every lifecycle write, so the other
# instances can re-read that tenant now rather than at their next cache refresh.
_BACKEND_LOAD = None
_BACKEND_PERSIST = None
_BACKEND_TRANSACT = None
_BACKEND_ON_CHANGE = None


def install_key_store_backend(load_fn, persist_fn, name: str = "external",
                              transact_fn=None, on_change=None) -> None:
    global _BACKEND_LOAD, _BACKEND_PERSIST, _BACKEND_TRANSACT, _BACKEND_ON_CHANGE
    _BACKEND_LOAD, _BACKEND_PERSIST = load_fn, persist_fn
    _BACKEND_TRANSACT, _BACKEND_ON_CHANGE = transact_fn, on_change
    logger.info("key-store backend installed: %s", name)


def reset_key_store_backend() -> None:
    """Restore the OSS blob backend (used by tests)."""
    global _BACKEND_LOAD, _BACKEND_PERSIST, _BACKEND_TRANSACT, _BACKEND_ON_CHANGE
    _BACKEND_LOAD = _BACKEND_PERSIST = _BACKEND_TRANSACT = _BACKEND_ON_CHANGE = None


def replace_cache(store: dict) -> None:
    """Swap the in-process validate cache (background refresher for the PG backend). The
    store is current, so no check of the blob store is due yet either."""
    global _KEY_CACHE, _CACHE_LOADED_AT, _last_version_check
    _KEY_CACHE = store
    _CACHE_LOADED_AT = _last_version_check = time.monotonic()


def replace_tenants_in_cache(tenant_ids, entries: dict) -> None:
    """Swap only these tenants' keys in the validate cache for ``entries``, their current
    rows (another instance changed them). Every other tenant's keys are left as they are."""
    global _KEY_CACHE
    tenant_ids = set(tenant_ids)
    kept = {h: e for h, e in _KEY_CACHE.items()
            if not (isinstance(e, dict) and e.get("tenant_id") in tenant_ids)}
    kept.update(entries)
    _KEY_CACHE = kept
# Reload-on-miss throttle: a key just issued on another instance/worker may not be in
# this process's TTL cache yet. On a miss we force one reload, but no more often than
# this interval so a bad-key flood can't hammer Secret Manager.
_last_forced_reload: float = 0.0
_FORCED_RELOAD_MIN_INTERVAL = int(os.getenv("KEY_FORCED_RELOAD_MIN_INTERVAL_SECONDS", "5"))
# A lookup that has to reload runs in a worker thread (main._validate_key): concurrent
# lookups queue on this lock and share one fetch (waiting at most _LOCK_WAIT_SECONDS, so a
# hung fetch cannot pin worker threads), and _last_load_attempt keeps a failing store
# (Secret Manager down) from being retried more than once per interval while a stale
# cache can still answer.
_LOAD_LOCK = threading.Lock()
_LOCK_WAIT_SECONDS = 5.0
_last_load_attempt: float = 0.0
# Has the blob store changed since the cache was loaded? A lookup asks at most this often:
# the local key file by its identity (a stat), Secret Manager by the name of the keys
# secret's latest version (a metadata call: secretmanager.versions.get, which Terraform
# grants the proxy on that secret). A changed store is reloaded at once, so a key revoked or
# suspended through another instance stops working here within the interval, not at the
# cache TTL, which stays as the backstop. An installed backend (Postgres) has its own refresher.
_FILE_CHECK_SECONDS = float(os.getenv("KEY_FILE_CHECK_SECONDS", "1"))
_SECRET_CHECK_SECONDS = float(os.getenv("KEY_SECRET_CHECK_SECONDS", "5"))
_VERSION_LOCK = threading.Lock()
_cache_version = None                # the store version the cache was loaded from (None: unknown)
_last_version_check: float = 0.0
_version_check_failing = False


def key_store_backend_installed() -> bool:
    """Whether a host installed a key-store backend (Postgres): its load never blocks on
    the event loop, and its own refresher keeps the cache warm."""
    return _BACKEND_LOAD is not None


def key_cached_and_fresh(api_key: str) -> bool:
    """Whether ``api_key`` can be validated from the cache with no reload and no store check
    (either may block, so main runs any other lookup in a worker thread)."""
    now = time.monotonic()
    return (bool(_KEY_CACHE) and now - _CACHE_LOADED_AT <= _CACHE_TTL_SECONDS
            and not _version_check_due(now)
            and hashlib.sha256(api_key.encode("utf-8")).hexdigest() in _KEY_CACHE)


def _store_version():
    """The blob store's version now: the key file's identity, or the name of the keys
    secret's latest version. None when it cannot be told (no file, a failed call)."""
    global _version_check_failing
    if _using_local_backend():
        try:
            st = os.stat(_LOCAL_PROXY_KEYS_FILE)
        except OSError:
            return None
        return st.st_mtime_ns, st.st_size, st.st_ino
    try:
        from google.cloud import secretmanager

        client = secretmanager.SecretManagerServiceClient()
        name = f"projects/{_GCP_PROJECT}/secrets/{_PROXY_KEYS_SECRET}/versions/latest"
        version = client.get_secret_version(request={"name": name}).name
    except Exception as exc:
        if not _version_check_failing:
            logger.warning("Secret Manager version check failed [%s], so a revoked key keeps "
                           "working on other instances until their cache TTL: %s",
                           _PROXY_KEYS_SECRET, exc)
        _version_check_failing = True
        return None
    _version_check_failing = False
    return version


def _version_check_due(now: float) -> bool:
    """Whether a lookup at ``now`` must first ask the blob store whether it changed."""
    if _BACKEND_LOAD is not None or not _KEY_CACHE:
        return False
    interval = _FILE_CHECK_SECONDS if _using_local_backend() else _SECRET_CHECK_SECONDS
    return now - _last_version_check >= interval


def _check_store_version(asked_at: float) -> None:
    """Reload the cache now if the blob store changed since it was loaded. One check at a
    time: a lookup queued behind another's uses its answer."""
    global _last_version_check
    if not _VERSION_LOCK.acquire(timeout=_LOCK_WAIT_SECONDS):
        return                                   # answer from the cache as it is
    try:
        if _last_version_check >= asked_at:
            return
        _last_version_check = time.monotonic()
        version = _store_version()
        if version is not None and version != _cache_version:
            _reload(asked_at, lambda: _cache_version != version)
    finally:
        _VERSION_LOCK.release()


def _reload(asked_at: float, still_needed, *, miss: bool = False) -> None:
    """Reload the key cache, one reload at a time. Under the lock the caller's need is
    checked again: a reload another lookup finished meanwhile is used, not repeated, and
    neither is one attempted after this lookup began (``asked_at``), which may have failed:
    a failing store is not retried back to back. A reload for a miss is also throttled to
    one per _FORCED_RELOAD_MIN_INTERVAL."""
    global _last_forced_reload, _last_load_attempt
    if not _LOAD_LOCK.acquire(timeout=_LOCK_WAIT_SECONDS):
        return                                   # answer from the cache as it is
    try:
        if not still_needed() or _last_load_attempt >= asked_at:
            return
        if miss:
            if asked_at - _last_forced_reload < _FORCED_RELOAD_MIN_INTERVAL:
                return
            _last_forced_reload = asked_at
        _last_load_attempt = time.monotonic()
        _load_key_cache()
    finally:
        _LOAD_LOCK.release()


def _fetch_secret(secret_name: str) -> Optional[str]:
    try:
        from google.cloud import secretmanager

        client = secretmanager.SecretManagerServiceClient()
        name = f"projects/{_GCP_PROJECT}/secrets/{secret_name}/versions/latest"
        response = client.access_secret_version(request={"name": name})
        return response.payload.data.decode("UTF-8")
    except Exception as exc:
        logger.error("Secret Manager fetch failed [%s]: %s", secret_name, exc)
        return None


def _load_key_cache() -> None:
    global _KEY_CACHE, _CACHE_LOADED_AT, _cache_version, _last_version_check

    # Installed backend (e.g. Postgres) takes precedence. load_fn returning None
    # means "no fresh data right now" (loop-thread guard) — keep the current cache;
    # the background refresher keeps it warm.
    if _BACKEND_LOAD is not None:
        try:
            data = _BACKEND_LOAD()
        except Exception as exc:
            logger.warning("key-store backend load failed: %s", exc)
            return
        if data is not None:
            _KEY_CACHE = data
            _CACHE_LOADED_AT = time.monotonic()
        return

    # Local dev: read keys from a local JSON file (generated by
    # scripts/local/deploy-local.sh) instead of Secret Manager.
    # STORAGE_BACKEND=local forces file-based auth even if LOCAL_PROXY_KEYS_FILE is unset.
    # The store's version is read BEFORE its data: a write in between is then seen as a
    # change at the next check, never taken for the version the cache holds.
    storage_backend = os.getenv("STORAGE_BACKEND", "gcs").lower().strip()
    if _LOCAL_PROXY_KEYS_FILE or storage_backend == "local":
        version = _store_version()
        try:
            with open(_LOCAL_PROXY_KEYS_FILE, "r", encoding="utf-8") as f:
                _KEY_CACHE = json.load(f)
            _CACHE_LOADED_AT = time.monotonic()
            _cache_version, _last_version_check = version, _CACHE_LOADED_AT
        except FileNotFoundError:
            logger.warning("Local proxy keys file not found: %s", _LOCAL_PROXY_KEYS_FILE)
        except json.JSONDecodeError as exc:
            logger.error("Invalid local proxy keys JSON [%s]: %s", _LOCAL_PROXY_KEYS_FILE, exc)
        return

    version = _store_version()
    raw = _fetch_secret(_PROXY_KEYS_SECRET)
    if raw:
        try:
            _KEY_CACHE = json.loads(raw)
            _CACHE_LOADED_AT = time.monotonic()
            _cache_version, _last_version_check = version, _CACHE_LOADED_AT
        except json.JSONDecodeError as exc:
            logger.error("Invalid proxy keys JSON: %s", exc)


def validate_proxy_key(api_key: str) -> Tuple[bool, Optional[str], Optional[dict]]:
    """
    Validate a developer proxy API key.

    Returns (is_valid, user_id_or_tenant_id, tenant_metadata).

    The secret in Secret Manager (or local-keys.json) is JSON with two supported formats:
        Legacy: { "<sha256_of_key>": "<user_id>", ... }
        New:    { "<sha256_of_key>": {"tenant_id": "...", "tier": "..."}, ... }

    For new format, returns (True, tenant_id, metadata_dict).
    For legacy format, returns (True, user_id, None).

    Proxy admins add keys via gcp-deploy.sh / admin CLI.
    Developers never see LLM provider keys — only their proxy key.
    """
    now = time.monotonic()

    def _cold_or_stale() -> bool:
        return not _KEY_CACHE or (time.monotonic() - _CACHE_LOADED_AT) > _CACHE_TTL_SECONDS

    # Cold, or past its TTL (a failed reload is not retried within the interval: the stale
    # cache answers meanwhile).
    if not _KEY_CACHE or ((now - _CACHE_LOADED_AT) > _CACHE_TTL_SECONDS
                          and now - _last_load_attempt >= _FORCED_RELOAD_MIN_INTERVAL):
        _reload(now, _cold_or_stale)
    # Changed since it was loaded (a revoke or suspend through another instance)? Asked at
    # most once per check interval.
    if _version_check_due(now):
        _check_store_version(now)

    key_hash = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
    entry = _KEY_CACHE.get(key_hash)

    # Reload-on-miss: a freshly issued key (e.g. self-serve signup on another instance)
    # may not be in this process's TTL cache yet. On a miss, force one throttled reload
    # and re-check so new keys work within seconds everywhere, not after the full TTL.
    if entry is None:
        _reload(now, lambda: key_hash not in _KEY_CACHE, miss=True)
        entry = _KEY_CACHE.get(key_hash)

    if entry is None:
        return False, None, None
    
    # Support both legacy string format and new object format
    if isinstance(entry, dict):
        # New format: {tenant_id, tier, created}
        tenant_id = entry.get("tenant_id", "default")
        return True, tenant_id, entry
    else:
        # Legacy format: entry is a user_id string
        return True, entry, None


def is_admin_key(metadata: Optional[dict]) -> bool:
    """Return True when a validated key's metadata grants the admin/impersonation
    scope.

    Admin keys may (a) assume another tenant via the ``X-Tenant-ID`` header
    (cross-tenant impersonation), (b) name an arbitrary RAG collection via
    ``x_rag_collection``, and (c) call the cross-tenant admin/GDPR endpoints.
    Legacy string-format keys (metadata is None) are never admin.
    """
    return bool(isinstance(metadata, dict) and metadata.get("admin"))


def is_gateway_key(metadata: Optional[dict]) -> bool:
    """Return True when a validated key belongs to a customer's API gateway.

    A gateway stamps ``X-Team`` on every request, overwriting whatever the calling app
    sent, so only such a key's ``X-Team`` may select a G00 bucket, a ``per_team`` limit or
    a team metric label (``tenancy.resolver.resolve_team``); any other key is team
    ``"default"``. Set at issuance only (``create_key(gateway=True)`` /
    ``issue-key.sh --gateway``) — never through ``create_key(extra=…)``. Strictly a JSON
    ``true``: a trust flag must not be switched on by a stray truthy string. Legacy
    string-format keys (metadata is None) are never gateway keys.
    """
    return bool(isinstance(metadata, dict) and metadata.get("gateway") is True)


def is_suspended(metadata: Optional[dict]) -> bool:
    """Return True when a validated key is flagged suspended in its metadata.

    Suspension is set out-of-band by the key-store lifecycle (management console /
    ``issue-key.sh``); this proxy only *enforces* it — a suspended key authenticates
    to a known tenant but is rejected (HTTP 403) at ``_authenticate``. Legacy
    string-format keys (metadata is None) can never be suspended.
    """
    return bool(isinstance(metadata, dict) and metadata.get("suspended"))


def is_contract_inactive(metadata: Optional[dict]) -> bool:
    """Return True when a validated key's tenant has an inactive/pending contract.

    Mirrors ``is_suspended``: the *blocking* state is stamped into the key metadata
    (``contract_inactive``) by the commercial admin lifecycle via
    ``set_contract_active``; this proxy only *enforces* it (HTTP 403 at
    ``_authenticate``). The flag's ABSENCE means "active" — so legacy keys that
    predate the contract feature (and every key of an active tenant) are never
    blocked. ``tenant_contracts.contract_status`` (per tenant; the ``companies`` row
    is only a fallback for tenants that predate it) is the source-of-record — this
    metadata field is the derived per-request enforcement copy.
    """
    return bool(isinstance(metadata, dict) and metadata.get("contract_inactive"))


def get_ip_allowlist(metadata: Optional[dict]) -> List[str]:
    """Return the per-tenant source-IP CIDR allowlist stamped in the key metadata.

    Written by the commercial admin lifecycle via ``set_ip_allowlist`` (and carried to the
    tenant's later keys); read at ``_authenticate`` alongside the global allowlist from
    ``config.yaml``. An empty list means "no per-tenant restriction" (the tenant is
    governed only by the global allowlist, if any). The key metadata is the list's only
    record: what is enforced is what is stored.
    """
    if isinstance(metadata, dict):
        val = metadata.get("ip_allowlist")
        if isinstance(val, list):
            return [str(c) for c in val]
    return []


def get_tenant_for_key(api_key: str) -> Optional[dict]:
    """
    Get tenant metadata for a validated API key.
    
    Returns the metadata dict if the key is in new format, None otherwise.
    Used by tenancy resolver to build TenantContext.
    """
    key_hash = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
    entry = _KEY_CACHE.get(key_hash)
    
    if isinstance(entry, dict):
        return entry
    return None


def get_llm_provider_key(provider: str) -> Optional[str]:
    """
    Fetch the LLM provider API key from Secret Manager.
    Called only by the proxy — developers never receive this value.
    """
    env_var = f"SECRET_{provider.upper().replace('-', '_')}_API_KEY"
    secret_name = os.getenv(env_var, f"llm-key-{provider.lower()}")
    value = os.getenv(f"LLM_KEY_{provider.upper()}")  # local dev override
    if value:
        return value
    return _fetch_secret(secret_name)


# ─── Key-store lifecycle engine (write side) ──────────────────────────────────
# Read/enforce (validate_proxy_key / is_suspended / is_admin_key) is used by core
# main.py. These WRITE helpers are the programmatic equivalent of scripts/issue-key.sh
# (create/revoke) plus a suspend that previously had no tool. They ship in the OSS core
# as an UNWIRED engine — self-hosters can drive them, and the commercial management
# console (api/admin.py) is the paid product that exposes them over HTTP. Core main.py /
# pipeline.py never import these; the barricade holds.
#
# Store shape is unchanged: { sha256(raw_key): {tenant_id, tier, created_at, admin?, suspended?} }.
# Every mutation goes through _mutate_store: a fresh load-modify-persist under a lock (never
# trusts the TTL cache), or one backend transaction when the backend installs a transact
# hook. Either way it refreshes the in-process cache so a running proxy sees the change
# immediately.

_KEY_PREFIX = "tok-"
_STORE_WRITE_LOCK = threading.Lock()


def _using_local_backend() -> bool:
    """True when keys live in a local JSON file rather than Secret Manager."""
    storage_backend = os.getenv("STORAGE_BACKEND", "gcs").lower().strip()
    return bool(_LOCAL_PROXY_KEYS_FILE) or storage_backend == "local"


def _load_full_store() -> dict:
    """Read the COMPLETE key store fresh from the active backend (bypasses the TTL cache).

    Lifecycle writes operate on this, never on ``_KEY_CACHE``, so a mutation can't drop
    entries that were added since the last cache load. Returns ``{}`` if the store is
    missing/empty; re-raises on corrupt JSON (refuse to overwrite a broken store).
    """
    if _BACKEND_LOAD is not None:
        data = _BACKEND_LOAD()
        if data is None:
            # Writes always run on worker threads where the backend can block for a
            # fresh read; None here means the seam was misused — refuse to write blind.
            raise RuntimeError("key-store backend returned no data for a write path")
        return data
    if _using_local_backend():
        if not _LOCAL_PROXY_KEYS_FILE:
            raise RuntimeError("LOCAL_PROXY_KEYS_FILE must be set for key-store writes")
        try:
            with open(_LOCAL_PROXY_KEYS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return {}
    raw = _fetch_secret(_PROXY_KEYS_SECRET)
    if not raw:
        return {}
    return json.loads(raw)


def _write_secret_version(secret_name: str, payload: str) -> None:
    """Add a new version to a Secret Manager secret (GCP backend)."""
    from google.cloud import secretmanager

    client = secretmanager.SecretManagerServiceClient()
    parent = f"projects/{_GCP_PROJECT}/secrets/{secret_name}"
    client.add_secret_version(
        request={"parent": parent, "payload": {"data": payload.encode("utf-8")}}
    )


def _persist_store(store: dict) -> None:
    """Write the full store back to the active backend + refresh the in-process cache."""
    global _KEY_CACHE, _CACHE_LOADED_AT
    if _BACKEND_PERSIST is not None:
        _BACKEND_PERSIST(store)
        _KEY_CACHE = store
        _CACHE_LOADED_AT = time.monotonic()
        return
    payload = json.dumps(store, indent=2)
    if _using_local_backend():
        if not _LOCAL_PROXY_KEYS_FILE:
            raise RuntimeError("LOCAL_PROXY_KEYS_FILE must be set for key-store writes")
        # Atomic write: temp file + os.replace so a crash never truncates the store.
        tmp = f"{_LOCAL_PROXY_KEYS_FILE}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp, _LOCAL_PROXY_KEYS_FILE)
    else:
        _write_secret_version(_PROXY_KEYS_SECRET, payload)
    _KEY_CACHE = store
    _CACHE_LOADED_AT = time.monotonic()


def _mutate_store(mutation, tenant_id: str):
    """Apply ``mutation(store) -> (changed, result)`` to the current full store, persist
    it when ``changed``, refresh the cache and return ``result``.

    With a transact hook the change is one backend transaction whose read happens inside
    it, so a write from another instance is never overwritten by a stale snapshot. An
    exception from ``mutation`` propagates and nothing is written. The process lock is
    held on both paths, so one process never queues more than one write on the backend.
    A successful call is then announced for ``tenant_id`` (see ``_announce_change``).
    """
    global _KEY_CACHE, _CACHE_LOADED_AT
    with _STORE_WRITE_LOCK:
        if _BACKEND_TRANSACT is not None:
            result, store = _BACKEND_TRANSACT(mutation)
            _KEY_CACHE = store
            _CACHE_LOADED_AT = time.monotonic()
        else:
            store = _load_full_store()
            changed, result = mutation(store)
            if changed:
                _persist_store(store)
    _announce_change(tenant_id)
    return result


def _announce_change(tenant_id: str) -> None:
    """Pass a tenant whose keys were just written to the on_change hook, if one is
    installed. A write that changed nothing is announced too; that costs the other
    instances one tenant re-read. Best-effort: the write has already committed, and the
    other instances still catch up at their next full cache refresh."""
    if _BACKEND_ON_CHANGE is None:
        return
    try:
        _BACKEND_ON_CHANGE(tenant_id)
    except Exception as exc:
        logger.warning("key-store change announcement failed tenant=%s: %s", tenant_id, exc)


def _tenant_allowlist(store: dict, tenant_id: str) -> List[str]:
    """The tenant's source-IP allowlist as its keys carry it: the newest non-empty list among
    them (set_ip_allowlist stamps every key alike, so they only differ when a key was issued
    before keys inherited the list, and the intent then was the restriction)."""
    lists = [(e.get("created_at") or "", e.get("ip_allowlist")) for e in store.values()
             if isinstance(e, dict) and e.get("tenant_id") == tenant_id
             and isinstance(e.get("ip_allowlist"), list) and e.get("ip_allowlist")]
    return [str(c) for c in max(lists, key=lambda pair: pair[0])[1]] if lists else []


def get_tenant_ip_allowlist(tenant_id: str) -> List[str]:
    """The allowlist enforced for ``tenant_id``, read from the key store (a fresh read: run
    it on a worker thread where the backend may block)."""
    return _tenant_allowlist(_load_full_store(), (tenant_id or "").strip())


def create_key(
    tenant_id: str,
    tier: str = "free",
    admin: bool = False,
    raw_key: Optional[str] = None,
    extra: Optional[dict] = None,
    *,
    gateway: bool = False,
) -> Tuple[str, str, dict]:
    """Create a proxy key for a tenant, persist it, refresh the cache.

    Returns ``(raw_key, key_hash, metadata)``. The RAW key is returned exactly once —
    the store only ever holds its sha256 hash, so it can never be recovered later.
    Pass ``raw_key`` only for deterministic tests; production always auto-generates.
    ``extra`` merges additional metadata fields (e.g. ``owner_domain`` for the
    per-tenant X-User-ID allowlist — WS25); reserved keys are not overridable.
    ``gateway`` marks the key as a customer API gateway's (see ``is_gateway_key``); like
    ``admin`` it is a trust flag, so it is reserved in ``extra`` and set only here.
    """
    tenant_id = (tenant_id or "").strip()
    if not tenant_id:
        raise ValueError("tenant_id is required")
    raw = raw_key or f"{_KEY_PREFIX}{secrets.token_hex(24)}"
    key_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    metadata: dict = {}
    if isinstance(extra, dict):
        metadata.update({k: v for k, v in extra.items()
                         if k not in ("tenant_id", "tier", "created_at", "admin", "suspended",
                                      "contract_inactive", "ip_allowlist", "gateway")})
    metadata.update({
        "tenant_id": tenant_id,
        "tier": (tier or "free").strip().lower(),
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    if admin:
        metadata["admin"] = True
    if gateway:
        metadata["gateway"] = True

    def add(store):
        if key_hash in store:
            raise ValueError("key already exists")
        # The tenant's source-IP restriction applies to every key it holds: a key issued
        # after the CIDRs were set would otherwise be an unrestricted way in.
        cidrs = _tenant_allowlist(store, tenant_id)
        if cidrs:
            metadata["ip_allowlist"] = cidrs
        store[key_hash] = metadata
        return True, None

    _mutate_store(add, tenant_id)
    logger.info(
        "Created proxy key tenant=%s tier=%s admin=%s", tenant_id, metadata["tier"], admin
    )
    return raw, key_hash, metadata


def set_suspended(tenant_id: str, suspended: bool = True) -> int:
    """Set/clear the ``suspended`` flag on ALL keys for a tenant. Returns count changed.

    Enforcement already exists (``is_suspended`` → HTTP 403 at ``main.py`` ``_authenticate``);
    this is the tool that was missing to actually set the flag. Idempotent.

    Pattern note: ``set_contract_active`` and ``set_ip_allowlist`` mirror this exact
    load-modify-persist shape — the invariant across all three is that the commercial
    source-of-record (``companies``) is written by ``api/admin.py``, and the derived
    per-request enforcement copy rides the key metadata that core reads here.
    """
    tenant_id = (tenant_id or "").strip()

    def apply(store):
        changed = 0
        for entry in store.values():
            if isinstance(entry, dict) and entry.get("tenant_id") == tenant_id:
                if bool(entry.get("suspended")) != bool(suspended):
                    if suspended:
                        entry["suspended"] = True
                    else:
                        entry.pop("suspended", None)
                    changed += 1
        return bool(changed), changed

    changed = _mutate_store(apply, tenant_id)
    logger.info(
        "%s %d key(s) tenant=%s", "Suspended" if suspended else "Unsuspended", changed, tenant_id
    )
    return changed


def set_contract_active(tenant_id: str, active: bool = True) -> int:
    """Set/clear the ``contract_inactive`` flag on ALL keys for a tenant.

    Mirrors ``set_suspended`` exactly, but with the OPPOSITE sense: the *blocking*
    state (``contract_inactive``) is stored, and its absence means active — so a key
    that predates this feature is never accidentally locked out. When ``active`` is
    False the flag is stamped; when True it is popped. Enforcement lives in
    ``is_contract_inactive`` → HTTP 403 at ``main.py`` ``_authenticate``. Idempotent.
    Returns the number of keys changed.
    """
    tenant_id = (tenant_id or "").strip()
    want_inactive = not active

    def apply(store):
        changed = 0
        for entry in store.values():
            if isinstance(entry, dict) and entry.get("tenant_id") == tenant_id:
                if bool(entry.get("contract_inactive")) != want_inactive:
                    if want_inactive:
                        entry["contract_inactive"] = True
                    else:
                        entry.pop("contract_inactive", None)
                    changed += 1
        return bool(changed), changed

    changed = _mutate_store(apply, tenant_id)
    logger.info(
        "Contract %s %d key(s) tenant=%s",
        "deactivated" if want_inactive else "activated", changed, tenant_id,
    )
    return changed


def set_ip_allowlist(tenant_id: str, cidrs: List[str]) -> int:
    """Set the per-tenant source-IP CIDR allowlist on ALL keys for a tenant.

    An empty/None ``cidrs`` clears the restriction (the ``ip_allowlist`` key is
    popped). CIDR *validation* is the caller's responsibility (the commercial admin
    endpoint validates with ``ipaddress`` before calling this) — the engine stays
    dumb. Enforcement lives in ``get_ip_allowlist`` + the ``net.ip_allowlist``
    checker at ``main.py`` ``_authenticate``. Idempotent. Returns keys changed.
    """
    tenant_id = (tenant_id or "").strip()
    new_val = [str(c) for c in (cidrs or [])]

    def apply(store):
        changed = 0
        for entry in store.values():
            if isinstance(entry, dict) and entry.get("tenant_id") == tenant_id:
                cur = entry.get("ip_allowlist") or []
                if list(cur) != new_val:
                    if new_val:
                        entry["ip_allowlist"] = list(new_val)
                    else:
                        entry.pop("ip_allowlist", None)
                    changed += 1
        return bool(changed), changed

    changed = _mutate_store(apply, tenant_id)
    logger.info("Set ip_allowlist (%d cidr) on %d key(s) tenant=%s",
                len(new_val), changed, tenant_id)
    return changed


def delete_tenant_keys(tenant_id: str) -> int:
    """Remove ALL keys for a tenant from the store. Returns count removed.

    Revokes access but does NOT erase the tenant's stored data — use the GDPR erase
    endpoint (``api/gdpr.py``) for right-to-erasure. Irreversible.
    """
    tenant_id = (tenant_id or "").strip()

    def remove(store):
        to_remove = [
            h for h, e in store.items()
            if isinstance(e, dict) and e.get("tenant_id") == tenant_id
        ]
        for h in to_remove:
            del store[h]
        return bool(to_remove), len(to_remove)

    removed = _mutate_store(remove, tenant_id)
    logger.info("Deleted %d key(s) tenant=%s", removed, tenant_id)
    return removed


def rotate_tenant_keys(tenant_id: str) -> Tuple[str, str, dict, int]:
    """Atomically issue a fresh key and revoke ALL existing keys for a tenant (WS24).

    One atomic store write (``_mutate_store``), so there is never a window with
    zero valid keys nor one where both old and new coexist across a crash. The new key
    inherits tier/admin/gateway from the newest existing key; a suspended tenant stays
    suspended (rotation must not be a self-unsuspend loophole).
    Returns ``(raw_key, key_hash, metadata, revoked_count)``. Raises ValueError when
    the tenant has no keys.
    """
    tenant_id = (tenant_id or "").strip()
    if not tenant_id:
        raise ValueError("tenant_id is required")

    def rotate(store):
        old = {
            h: e for h, e in store.items()
            if isinstance(e, dict) and e.get("tenant_id") == tenant_id
        }
        if not old:
            raise ValueError("tenant has no keys to rotate")
        newest = max(old.values(), key=lambda e: e.get("created_at") or "")
        raw = f"{_KEY_PREFIX}{secrets.token_hex(24)}"
        key_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        metadata: dict = {
            "tenant_id": tenant_id,
            "tier": (newest.get("tier") or "free"),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        # Carry over extra metadata (owner_domain etc.) so rotation doesn't drop
        # the per-tenant X-User-ID allowlist or other stamped fields.
        for k, v in newest.items():
            if k not in metadata and k not in ("admin", "suspended", "gateway", "ip_allowlist"):
                metadata[k] = v
        # The tenant's source-IP restriction, even if the newest key was issued without it.
        cidrs = _tenant_allowlist(old, tenant_id)
        if cidrs:
            metadata["ip_allowlist"] = cidrs
        if newest.get("admin"):
            metadata["admin"] = True
        # Trust flags are carried explicitly, never as a generic extra: a rotated gateway key
        # stays a gateway key, and only a strict JSON true survives (is_gateway_key).
        if newest.get("gateway") is True:
            metadata["gateway"] = True
        if any(e.get("suspended") for e in old.values()):
            metadata["suspended"] = True
        for h in old:
            del store[h]
        store[key_hash] = metadata
        return True, (raw, key_hash, metadata, len(old))

    raw, key_hash, metadata, revoked = _mutate_store(rotate, tenant_id)
    logger.info("Rotated proxy key tenant=%s (revoked %d)", tenant_id, revoked)
    return raw, key_hash, metadata, revoked


def list_tenants() -> List[dict]:
    """Aggregate the key store by tenant for the management console.

    Returns ``[{tenant_id, tier, admin, suspended, contract_inactive, key_count,
    created_at, ip_allowlist, ip_allowlist_mixed}]`` sorted by tenant_id — NEVER any raw
    key or hash. Legacy string-format entries group under their user_id with tier
    ``"legacy"``. ``contract_inactive`` is the metadata-derived enforcement copy; the admin
    router joins the authoritative per-tenant contract status on top for display.
    ``ip_allowlist`` is what the keys enforce (:func:`_tenant_allowlist`), and
    ``ip_allowlist_mixed`` says the tenant's keys do not all carry the same list.
    """
    store = _load_full_store()
    tenants: dict = {}
    key_lists: dict = {}
    for entry in store.values():
        if isinstance(entry, dict):
            tid = entry.get("tenant_id", "default")
            agg = tenants.setdefault(tid, {
                "tenant_id": tid, "tier": entry.get("tier", "free"),
                "admin": False, "suspended": False, "contract_inactive": False,
                "key_count": 0, "created_at": entry.get("created_at"),
            })
            cidrs = entry.get("ip_allowlist")
            cidrs = [str(c) for c in cidrs] if isinstance(cidrs, list) else []
            key_lists.setdefault(tid, set()).add(tuple(cidrs))
            if cidrs and (entry.get("created_at") or "") >= agg.get("_cidrs_at", ""):
                agg["_cidrs_at"], agg["ip_allowlist"] = entry.get("created_at") or "", cidrs
            agg["key_count"] += 1
            agg["admin"] = agg["admin"] or bool(entry.get("admin"))
            agg["suspended"] = agg["suspended"] or bool(entry.get("suspended"))
            agg["contract_inactive"] = agg["contract_inactive"] or bool(entry.get("contract_inactive"))
            ca = entry.get("created_at")
            if ca and (agg["created_at"] is None or ca < agg["created_at"]):
                agg["created_at"] = ca
        else:
            tid = str(entry)
            agg = tenants.setdefault(tid, {
                "tenant_id": tid, "tier": "legacy", "admin": False,
                "suspended": False, "contract_inactive": False,
                "key_count": 0, "created_at": None,
            })
            agg["key_count"] += 1
    for tid, agg in tenants.items():
        agg.pop("_cidrs_at", None)
        agg.setdefault("ip_allowlist", [])            # as _tenant_allowlist chooses it
        agg["ip_allowlist_mixed"] = len(key_lists.get(tid, ())) > 1
    return sorted(tenants.values(), key=lambda t: t["tenant_id"])
