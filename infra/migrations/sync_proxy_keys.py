#!/usr/bin/env python3
"""sync_proxy_keys.py — in-VPC upsert of local-keys.json into the proxy_keys table.

WHY: the commercial proxy runs PROXY_KEYS_BACKEND=postgres and validates proxy API
keys against Cloud SQL's proxy_keys table. The Postgres backend only ingests the
GCS key blob ONCE (import_blob_store_once() is guarded on an empty table), so
harness/tenant keys minted AFTER the first deploy never reach proxy_keys and every
request 401s. Cloud SQL is private-IP, reachable only from an in-VPC Cloud Run Job
over the /cloudsql/<conn> socket — hence this runs as such a job.

Standalone by design: it does NOT import the proxy app package (runs in a minimal
image with only asyncpg), so the ON CONFLICT upsert + the CREATE TABLE DDL are
inlined here to mirror src/proxy/auth/pg_key_store.py.

Env (injected by scripts/gcp/sync-proxy-keys-job.sh):
  KEYS_JSON_PATH       path to the keys JSON  (default /app/local-keys.json)
  DB_CONNECTION_NAME   Cloud SQL connection name  <project>:<region>:token-opt-pg
  PGUSER               DB user      (default token_opt_app — matches infra/migrations/run.sh)
  PGPASSWORD           DB password  (from Secret Manager secret token-opt-db-password)
  PGDATABASE           DB name      (default token_opt)

Exits non-zero on any DB error so the job fails loudly.
"""
import asyncio
import json
import os
import sys

import asyncpg

# Inlined copy of PROXY_KEYS_DDL from src/proxy/auth/pg_key_store.py (kept byte-parallel).
PROXY_KEYS_DDL = """
CREATE TABLE IF NOT EXISTS proxy_keys (
    key_hash   TEXT    PRIMARY KEY,
    tenant_id  TEXT    NOT NULL,
    tier       TEXT    NOT NULL DEFAULT 'free',
    admin      BOOLEAN NOT NULL DEFAULT FALSE,
    suspended  BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TEXT,
    extra      JSONB   NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_proxy_keys_tenant ON proxy_keys (tenant_id);
"""

_CORE_FIELDS = ("tenant_id", "tier", "admin", "suspended", "created_at")


def _load_store(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a JSON object {{key_hash: metadata}}, got {type(data).__name__}")
    return data


async def _upsert(conn, store: dict) -> list:
    """UPSERT each {key_hash: metadata} entry (same ON CONFLICT logic as pg_key_store.upsert_keys)."""
    tenant_ids = []
    for key_hash, entry in store.items():
        if not isinstance(entry, dict):
            # Legacy string-format rows: keep them round-trippable.
            entry = {"tenant_id": str(entry), "tier": "legacy"}
        extra = {k: v for k, v in entry.items() if k not in _CORE_FIELDS}
        await conn.execute(
            "INSERT INTO proxy_keys (key_hash, tenant_id, tier, admin, suspended, created_at, extra) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb) "
            "ON CONFLICT (key_hash) DO UPDATE SET "
            "tenant_id=EXCLUDED.tenant_id, tier=EXCLUDED.tier, admin=EXCLUDED.admin, "
            "suspended=EXCLUDED.suspended, created_at=EXCLUDED.created_at, extra=EXCLUDED.extra",
            key_hash,
            entry.get("tenant_id", "default"),
            entry.get("tier", "free"),
            bool(entry.get("admin")),
            bool(entry.get("suspended")),
            entry.get("created_at"),
            json.dumps(extra),
        )
        tenant_ids.append(entry.get("tenant_id", "default"))
    return tenant_ids


async def main() -> None:
    keys_path = os.environ.get("KEYS_JSON_PATH", "/app/local-keys.json")
    db_conn = os.environ.get("DB_CONNECTION_NAME", "")
    pguser = os.environ.get("PGUSER", "token_opt_app")
    pgpassword = os.environ.get("PGPASSWORD", "")
    pgdatabase = os.environ.get("PGDATABASE", "token_opt")

    if not db_conn:
        print("ERROR: DB_CONNECTION_NAME is not set", file=sys.stderr)
        sys.exit(2)

    store = _load_store(keys_path)
    if not store:
        print(f"sync_proxy_keys: {keys_path} is empty — nothing to upsert.")
        return

    # asyncpg accepts host=/cloudsql/<conn> as the Unix socket directory (the Cloud SQL
    # Auth Proxy socket mounted by --set-cloudsql-instances). Works for public + private.
    sock_dir = f"/cloudsql/{db_conn}"
    conn = await asyncpg.connect(
        host=sock_dir,
        user=pguser,
        password=pgpassword,
        database=pgdatabase,
    )
    try:
        await conn.execute(PROXY_KEYS_DDL)
        tenant_ids = await _upsert(conn, store)
    finally:
        await conn.close()

    print(f"sync_proxy_keys: upserted {len(tenant_ids)} key(s) into proxy_keys.")
    for tid in sorted(set(tenant_ids)):
        print(f"  - {tid}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:  # fail loudly so the Cloud Run Job reports failure
        print(f"ERROR: sync_proxy_keys failed: {exc}", file=sys.stderr)
        sys.exit(1)
