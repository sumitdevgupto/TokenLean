"""Database-level protection for ``audit_events``: a restricted runtime role.

Without it, the application's database role owns ``audit_events`` and can rewrite or delete
any audit row. With it, the proxy connects as a second LOGIN role that reads and writes the
application's tables but, on ``audit_events``, may only SELECT and INSERT. The two ways audit
rows legitimately change go through SECURITY DEFINER functions that run as the owner:

  audit_pseudonymise(tenant)      right-to-erasure: user_id := NULL on one tenant's rows
  audit_delete_older_than(days)   retention: delete rows older than ``days``; refuses a
                                  ``days`` below RETENTION_FLOOR_DAYS

The runtime role is created with SQL, not a cloud console or API, so it holds no CREATEROLE
and is a member of no role: on PostgreSQL 15 a CREATEROLE role can grant itself any
non-superuser role and undo the restriction. ``apply`` is run by the owner role: the managed
deploy's schema job, or ``python -m audit.enforcement`` for a self-hosted deployment that opts
in. Without it nothing changes: ``pseudonymise`` and ``delete_older_than`` use plain SQL when
the role may, as before, and they are the only code that changes audit rows.
"""
import argparse
import asyncio
import base64
import hashlib
import hmac
import logging
import os
import re
import sys
from typing import Any, Dict
from urllib.parse import quote, urlsplit, urlunsplit

logger = logging.getLogger(__name__)

RETENTION_FLOOR_DAYS = 90
DEFAULT_RUNTIME_ROLE = "tokenlean_runtime"
_ROLE_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

# Static SQL. The 90 in it is RETENTION_FLOOR_DAYS (a unit test holds them equal).
_FUNCTIONS_SQL = """
CREATE OR REPLACE FUNCTION public.audit_pseudonymise(p_tenant text) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
DECLARE n bigint;
BEGIN
  UPDATE public.audit_events SET user_id = NULL WHERE tenant_id = p_tenant;
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END $fn$;

CREATE OR REPLACE FUNCTION public.audit_delete_older_than(p_days integer) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
DECLARE n bigint;
BEGIN
  IF p_days IS NULL OR p_days < 90 THEN
    RAISE EXCEPTION 'audit rows younger than 90 days cannot be deleted (asked for %)', p_days;
  END IF;
  DELETE FROM public.audit_events WHERE "timestamp" < now() - make_interval(days => p_days);
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END $fn$;

REVOKE ALL ON FUNCTION public.audit_pseudonymise(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.audit_delete_older_than(integer) FROM PUBLIC;
"""

# What stops the role holding the restriction; each one voids it.
_BLOCKERS = (
    ("superuser", "the role is a superuser"),
    ("createrole", "the role can create roles, and so grant itself any role"),
    ("member_of_a_role", "the role is a member of another role"),
    ("owner_rights", "the role owns the audit table or holds its owner's rights"),
    ("can_update", "the role may change audit rows directly"),
    ("can_delete", "the role may delete audit rows directly"),
    ("can_truncate", "the role may empty the audit table"),
)

_STATUS_SQL = """
SELECT current_user::text AS role,
       r.rolsuper AS superuser,
       r.rolcreaterole AS createrole,
       EXISTS (SELECT 1 FROM pg_auth_members m WHERE m.member = r.oid) AS member_of_a_role,
       pg_has_role(current_user, c.relowner, 'MEMBER') AS owner_rights,
       has_table_privilege(current_user, c.oid, 'UPDATE') AS can_update,
       has_table_privilege(current_user, c.oid, 'DELETE') AS can_delete,
       has_table_privilege(current_user, c.oid, 'TRUNCATE') AS can_truncate
FROM pg_roles r, pg_class c
WHERE r.rolname = current_user AND c.oid = to_regclass('audit_events')
"""


# ── The only code that changes audit rows ───────────────────────────────────

async def pseudonymise(conn, tenant_id: str) -> int:
    """Right-to-erasure on audit rows: set ``user_id`` to NULL for *tenant_id*, keeping the
    rows. Returns the count. Plain UPDATE when this role may; else the SECURITY DEFINER
    function, the only way a restricted runtime role can."""
    if await _may(conn, "UPDATE"):
        return _count(await conn.execute(
            "UPDATE audit_events SET user_id = NULL WHERE tenant_id = $1", tenant_id))
    return int(await conn.fetchval("SELECT public.audit_pseudonymise($1::text)", tenant_id) or 0)


async def delete_older_than(conn, days: int) -> int:
    """Retention on audit rows: delete those older than *days*. Returns the count. Plain
    DELETE when this role may; else the SECURITY DEFINER function, which refuses fewer than
    RETENTION_FLOOR_DAYS, so *days* is raised to that floor, with a warning."""
    if await _may(conn, "DELETE"):
        return _count(await conn.execute(
            "DELETE FROM audit_events WHERE timestamp < NOW() - ($1 || ' days')::interval",
            str(days)))
    if days < RETENTION_FLOOR_DAYS:
        logger.warning("retention: audit_days=%d is below the %d-day floor the database "
                       "enforces for this role; deleting only rows older than the floor",
                       days, RETENTION_FLOOR_DAYS)
        days = RETENTION_FLOOR_DAYS
    return int(await conn.fetchval("SELECT public.audit_delete_older_than($1::integer)", days) or 0)


async def _may(conn, privilege: str) -> bool:
    """Whether this role holds *privilege* on audit_events itself. A probe that fails (a
    test double, a missing table) answers True: plain SQL, today's behaviour, which then
    reports its own error."""
    try:
        return bool(await conn.fetchval(
            "SELECT has_table_privilege(current_user, 'audit_events', $1)", privilege))
    except Exception:
        return True


def _count(status: str) -> int:
    try:
        return int(str(status).split()[-1])
    except (ValueError, IndexError):
        return 0


# ── What the database enforces for the connected role ──────────────────────

async def enforcement_status(conn) -> Dict[str, Any]:
    """Whether the database stops the CONNECTED role from changing audit rows other than
    through the two functions: ``enforced`` plus the ``reasons`` it does not, and the raw
    checks. For the evidence pack, which reports what holds when it is generated."""
    row = await conn.fetchrow(_STATUS_SQL)
    if row is None:
        return {"enforced": False, "reasons": ["audit_events does not exist"]}
    status = dict(row)
    # Fail closed: a check that is missing or NULL counts against, not for.
    status["reasons"] = [text for key, text in _BLOCKERS if status.get(key) is not False]
    status["enforced"] = not status["reasons"]
    return status


# ── Creating the runtime role (run as the owner) ───────────────────────────

async def apply(conn, role: str, password: str) -> None:
    """Create or refresh the restricted runtime *role*, as the OWNER of the application's
    tables. Idempotent. Installs the two functions; makes the role LOGIN without CREATEROLE
    and a member of no role; grants it SELECT, INSERT, UPDATE and DELETE on every table this
    owner owns, but only SELECT and INSERT on audit_events; and sets default privileges so
    tables the owner creates later are covered. The password goes over the wire only as a
    SCRAM verifier, so a statement log never records it."""
    if not _ROLE_RE.match(role):
        raise ValueError(f"invalid role name: {role!r}")
    if not password or not password.isascii() or not password.isprintable():
        raise ValueError("the runtime-role password must be printable ASCII")
    who = _ident(role)
    async with conn.transaction():
        await conn.execute(_FUNCTIONS_SQL)
        if not await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", role):
            await conn.execute(f"CREATE ROLE {who} LOGIN")
        # Only attributes a non-superuser owner may set (Cloud SQL's is one): SUPERUSER,
        # REPLICATION and BYPASSRLS are off for a new role, and enforcement_status reports
        # an existing role that has any of them.
        await conn.execute(f"ALTER ROLE {who} WITH LOGIN NOCREATEDB NOCREATEROLE "
                           f"PASSWORD {_literal(_scram_verifier(password))}")
        for member_of in await conn.fetch(
                "SELECT g.rolname FROM pg_auth_members m JOIN pg_roles g ON g.oid = m.roleid "
                "JOIN pg_roles u ON u.oid = m.member WHERE u.rolname = $1", role):
            await conn.execute(f"REVOKE {_ident(member_of['rolname'])} FROM {who}")
        database = await conn.fetchval("SELECT current_database()")
        await conn.execute(f"GRANT CONNECT, TEMPORARY ON DATABASE {_ident(database)} TO {who}")
        # CREATE: some features make their own tables at run time (the G07 pgvector
        # fallback); a table the role makes is its own, and no right over the owner's.
        await conn.execute(f"GRANT USAGE, CREATE ON SCHEMA public TO {who}")
        if not await conn.fetchval("SELECT has_schema_privilege($1, 'public', 'CREATE')", role):
            logger.warning("runtime role %s cannot create tables in schema public (this owner "
                           "may not grant it): the G07 pgvector fallback's tables will fail", role)
        for t in await conn.fetch("SELECT tablename FROM pg_tables "
                                  "WHERE schemaname = 'public' AND tableowner = current_user"):
            await conn.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON public.{_ident(t['tablename'])} TO {who}")
        for s in await conn.fetch("SELECT sequencename FROM pg_sequences "
                                  "WHERE schemaname = 'public' AND sequenceowner = current_user"):
            await conn.execute(f"GRANT USAGE, SELECT, UPDATE ON SEQUENCE public.{_ident(s['sequencename'])} TO {who}")
        await conn.execute(f"REVOKE ALL ON public.audit_events FROM {who}")
        await conn.execute(f"GRANT SELECT, INSERT ON public.audit_events TO {who}")
        await conn.execute(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                           f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {who}")
        await conn.execute(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                           f"GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO {who}")
        await conn.execute("GRANT EXECUTE ON FUNCTION public.audit_pseudonymise(text), "
                           f"public.audit_delete_older_than(integer) TO {who}")


def runtime_dsn(owner_dsn: str, role: str, password: str) -> str:
    """*owner_dsn* with the runtime role's user and password in place of the owner's."""
    parts = urlsplit(owner_dsn)
    host = parts.netloc.rsplit("@", 1)[-1] if "@" in parts.netloc else parts.netloc
    netloc = f"{quote(role, safe='')}:{quote(password, safe='')}@{host}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _scram_verifier(password: str, iterations: int = 4096) -> str:
    """PostgreSQL's stored form of a SCRAM-SHA-256 password (RFC 5802/7677)."""
    salt = os.urandom(16)
    salted = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    client_key = hmac.new(salted, b"Client Key", "sha256").digest()
    server_key = hmac.new(salted, b"Server Key", "sha256").digest()
    b64 = lambda b: base64.b64encode(b).decode("ascii")  # noqa: E731
    return (f"SCRAM-SHA-256${iterations}:{b64(salt)}"
            f"${b64(hashlib.sha256(client_key).digest())}:{b64(server_key)}")


def _ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _literal(text: str) -> str:
    return "'" + str(text).replace("'", "''") + "'"


# ── python -m audit.enforcement ─────────────────────────────────────────────

async def apply_and_verify(owner_dsn: str, role: str, password: str) -> Dict[str, Any]:
    """Apply as the owner, then connect AS the runtime role and report its status."""
    import asyncpg
    conn = await asyncpg.connect(owner_dsn)
    try:
        await apply(conn, role, password)
    finally:
        await conn.close()
    conn = await asyncpg.connect(runtime_dsn(owner_dsn, role, password))
    try:
        return await enforcement_status(conn)
    finally:
        await conn.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m audit.enforcement",
        description="Create or refresh the restricted runtime database role (run as the owner "
                    "of the tables: DATABASE_URL). The password comes from RUNTIME_DB_PASSWORD. "
                    "Then point the proxy's DATABASE_URL at the runtime role.")
    parser.add_argument("--role", default=DEFAULT_RUNTIME_ROLE)
    args = parser.parse_args(argv)
    owner_dsn, password = os.getenv("DATABASE_URL", ""), os.getenv("RUNTIME_DB_PASSWORD", "")
    if not owner_dsn or not password:
        print("DATABASE_URL (the owner) and RUNTIME_DB_PASSWORD must be set", file=sys.stderr)
        return 2
    status = asyncio.run(apply_and_verify(owner_dsn, args.role, password))
    print(f"runtime role {args.role}: " + ("audit rows protected" if status["enforced"]
                                           else "NOT protected: " + "; ".join(status["reasons"])))
    return 0 if status["enforced"] else 1


if __name__ == "__main__":
    sys.exit(main())
