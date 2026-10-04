"""Database-level audit protection against a REAL PostgreSQL.

The restricted runtime role (src/proxy/audit/enforcement.py) must be unable to change audit
rows except through its two functions, and unable to regain the owner's rights. Only a real
server can show that: privileges, SECURITY DEFINER, role membership, SCRAM logins. The owner
role here mirrors a Cloud SQL API-made user: LOGIN and CREATEROLE, not a superuser.

TEST_PG_DSN must point at a THROWAWAY server, as a superuser: the tests create two roles
and a database and drop them afterwards. Skipped otherwise.
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "proxy")))

import asyncio
import secrets
from urllib.parse import urlsplit, urlunsplit

import pytest

asyncpg = pytest.importorskip("asyncpg")

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_PG_DSN"),
                                reason="set TEST_PG_DSN to a throwaway Postgres (superuser)")

from audit import enforcement  # noqa: E402
from audit.log import AUDIT_EVENTS_DDL, ensure_audit_schema  # noqa: E402
from billing.models import USAGE_EVENTS_DDL, ensure_usage_events_schema  # noqa: E402
from cache.pg_pool import may_run_ddl  # noqa: E402

OWNER, RUNTIME, DB = "audit_enf_owner", "audit_enf_runtime", "audit_enf_test"
PLAIN_DB = "audit_enf_plain"   # a single-role deployment that never opted in


def _dsn(base, user, password, database):
    p = urlsplit(base)
    host = p.netloc.rsplit("@", 1)[-1]
    return urlunsplit((p.scheme, f"{user}:{password}@{host}", f"/{database}", p.query, ""))


def _run(coro):
    return asyncio.run(coro)


async def _as(dsn, fn):
    conn = await asyncpg.connect(dsn)
    try:
        return await fn(conn)
    finally:
        await conn.close()


@pytest.fixture(scope="module")
def db():
    base = os.environ["TEST_PG_DSN"]
    owner_pw, runtime_pw = secrets.token_hex(16), secrets.token_hex(16)

    async def drop_all(su):
        for database in (DB, PLAIN_DB):
            await su.execute(f"DROP DATABASE IF EXISTS {database} WITH (FORCE)")
        for role in (RUNTIME, OWNER):
            await su.execute(f"DROP ROLE IF EXISTS {role}")

    async def setup(su):
        await drop_all(su)
        await su.execute(f"CREATE ROLE {OWNER} LOGIN CREATEROLE PASSWORD '{owner_pw}'")
        await su.execute(f"CREATE DATABASE {DB} OWNER {OWNER}")
        await su.execute(f"CREATE DATABASE {PLAIN_DB} OWNER {OWNER}")   # never set up

    _run(_as(base, setup))
    owner = _dsn(base, OWNER, owner_pw, DB)

    async def schema(conn):
        await conn.execute(AUDIT_EVENTS_DDL)
        await conn.execute(USAGE_EVENTS_DDL)
        await enforcement.apply(conn, RUNTIME, runtime_pw)

    _run(_as(owner, schema))
    p = urlsplit(base)
    yield {"base": base, "owner": owner, "runtime": _dsn(base, RUNTIME, runtime_pw, DB),
           "owner_plain": _dsn(base, OWNER, owner_pw, PLAIN_DB),
           "superuser": urlunsplit((p.scheme, p.netloc, f"/{DB}", p.query, ""))}
    _run(_as(base, drop_all))


async def _insert(conn, tenant, user, days_ago=0):
    await conn.execute(
        "INSERT INTO audit_events (tenant_id, request_id, timestamp, action, user_id) "
        "VALUES ($1, $2, now() - make_interval(days => $3), 'config.updated', $4)",
        tenant, secrets.token_hex(4), days_ago, user)


def test_the_runtime_role_logs_in_and_may_insert_and_read(db):
    async def go(conn):
        await _insert(conn, "T-READ", "u@example.com")
        return await conn.fetchval("SELECT count(*) FROM audit_events WHERE tenant_id = 'T-READ'")
    assert _run(_as(db["runtime"], go)) == 1          # the SCRAM verifier works


@pytest.mark.parametrize("statement", [
    "UPDATE audit_events SET user_id = 'forged'",
    "DELETE FROM audit_events",
    "TRUNCATE audit_events",
    "ALTER TABLE audit_events DISABLE ROW LEVEL SECURITY",
    "DROP TABLE audit_events",
])
def test_it_cannot_change_audit_rows_directly(db, statement):
    async def go(conn):
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute(statement)
    _run(_as(db["runtime"], go))


@pytest.mark.parametrize("statement", [
    f"GRANT {OWNER} TO {RUNTIME}",
    f"SET ROLE {OWNER}",
    "CREATE ROLE audit_enf_escape LOGIN",
    f"ALTER ROLE {RUNTIME} CREATEROLE",
    "CREATE OR REPLACE FUNCTION public.audit_pseudonymise(p_tenant text) RETURNS bigint "
    "LANGUAGE sql AS 'SELECT 0::bigint'",
])
def test_it_cannot_regain_the_owners_rights(db, statement):
    async def go(conn):
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute(statement)
    _run(_as(db["runtime"], go))


def test_erasure_goes_through_the_function_and_touches_one_tenant(db):
    async def go(conn):
        await _insert(conn, "T-ERASE", "a@example.com")
        await _insert(conn, "T-ERASE", "b@example.com")
        await _insert(conn, "T-KEEP", "c@example.com")
        changed = await enforcement.pseudonymise(conn, "T-ERASE")
        left = await conn.fetch("SELECT tenant_id, user_id FROM audit_events "
                                "WHERE tenant_id IN ('T-ERASE', 'T-KEEP') ORDER BY tenant_id")
        return changed, [tuple(r) for r in left]
    changed, left = _run(_as(db["runtime"], go))
    assert changed == 2
    assert left == [("T-ERASE", None), ("T-ERASE", None), ("T-KEEP", "c@example.com")]


def test_retention_cannot_reach_below_the_floor(db):
    async def go(conn):
        await _insert(conn, "T-AGE", "old", days_ago=100)
        await _insert(conn, "T-AGE", "middle", days_ago=60)
        await _insert(conn, "T-AGE", "new", days_ago=0)
        with pytest.raises(asyncpg.RaiseError, match="younger than 90 days"):
            await conn.fetchval("SELECT public.audit_delete_older_than(30)")
        deleted = await enforcement.delete_older_than(conn, 30)   # raised to the floor
        left = [r["user_id"] for r in await conn.fetch(
            "SELECT user_id FROM audit_events WHERE tenant_id = 'T-AGE' ORDER BY timestamp")]
        return deleted, left
    deleted, left = _run(_as(db["runtime"], go))
    assert deleted >= 1 and left == ["middle", "new"]


def test_the_status_holds_for_the_runtime_role_only(db):
    runtime = _run(_as(db["runtime"], enforcement.enforcement_status))
    owner = _run(_as(db["owner"], enforcement.enforcement_status))
    assert runtime["enforced"] is True, runtime["reasons"]
    assert owner["enforced"] is False
    assert any("create roles" in r for r in owner["reasons"])
    assert any("owns the audit table" in r for r in owner["reasons"])


def test_startup_schema_steps_skip_for_the_runtime_role(db):
    async def go():
        runtime = await asyncpg.create_pool(db["runtime"], min_size=1, max_size=2)
        owner = await asyncpg.create_pool(db["owner"], min_size=1, max_size=2)
        try:
            verdicts = (await may_run_ddl(runtime, "audit_events"),
                        await may_run_ddl(owner, "audit_events"),
                        await may_run_ddl(runtime, "no_such_table"))
            await ensure_audit_schema(runtime)          # skipped, not "must be owner"
            await ensure_usage_events_schema(runtime)   # would raise without the guard
            return verdicts
        finally:
            await runtime.close()
            await owner.close()
    assert _run(go()) == (False, True, True)


def test_it_reads_and_writes_the_other_tables_and_later_ones(db):
    async def owner_adds_a_table(conn):
        await conn.execute("CREATE TABLE later_feature (id bigserial PRIMARY KEY, v text)")

    _run(_as(db["owner"], owner_adds_a_table))

    async def go(conn):
        await conn.execute("INSERT INTO usage_events (tenant_id, request_id, model) "
                           "VALUES ('T-USE', 'r1', 'gpt')")
        await conn.execute("UPDATE usage_events SET model = 'gpt-4o' WHERE tenant_id = 'T-USE'")
        await conn.execute("INSERT INTO later_feature (v) VALUES ('x')")    # default privileges
        await conn.execute("CREATE TABLE runtime_own (id int)")              # CREATE on public
        await conn.execute("DELETE FROM usage_events WHERE tenant_id = 'T-USE'")
        return await conn.fetchval("SELECT count(*) FROM later_feature")
    assert _run(_as(db["runtime"], go)) == 1


def test_apply_again_revokes_a_membership_and_rotates_the_password(db):
    async def grant(su):   # a misconfiguration that would hand the runtime the owner's rights
        await su.execute(f"GRANT {OWNER} TO {RUNTIME}")
    _run(_as(db["superuser"], grant))
    assert _run(_as(db["runtime"], enforcement.enforcement_status))["enforced"] is False

    new_pw = secrets.token_hex(16)
    _run(_as(db["owner"], lambda conn: enforcement.apply(conn, RUNTIME, new_pw)))
    rotated = _dsn(db["base"], RUNTIME, new_pw, DB)
    assert _run(_as(rotated, enforcement.enforcement_status))["enforced"] is True
    with pytest.raises(asyncpg.InvalidPasswordError):
        _run(_as(db["runtime"], enforcement.enforcement_status))    # the old password is gone


def test_a_deployment_that_never_opted_in_changes_rows_with_plain_sql(db):
    """Self-host default: one role, no functions installed. Erasure and retention must still
    work there, through plain SQL, and with no floor (the owner may delete what it likes)."""
    async def go(conn):
        await conn.execute(AUDIT_EVENTS_DDL)
        assert await conn.fetchval("SELECT to_regproc('public.audit_pseudonymise')") is None
        await _insert(conn, "T-PLAIN", "p@example.com", days_ago=40)
        changed = await enforcement.pseudonymise(conn, "T-PLAIN")
        deleted = await enforcement.delete_older_than(conn, 30)
        return changed, deleted
    assert _run(_as(db["owner_plain"], go)) == (1, 1)
