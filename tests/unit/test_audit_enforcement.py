"""Database-level protection for audit_events: the logic, against test doubles.

tests/integration/test_audit_enforcement_pg.py proves the database side on a real server
(privileges, SECURITY DEFINER, role membership). These tests pin the Python around it: which
path each helper takes, that the status check fails closed, that apply() never sends the
password in plain text, and that startup schema steps skip for a role that owns nothing.
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "proxy")))

import asyncio
import logging

import pytest

from audit import enforcement
from cache.pg_pool import may_run_ddl


class _Conn:
    """Records statements; answers the privilege probe and the functions."""

    def __init__(self, may=True, status=None):
        self.may, self.status, self.sql = may, status, []

    async def fetchval(self, query, *args):
        self.sql.append((query, args))
        if "has_table_privilege" in query:
            if self.may is None:
                raise RuntimeError("no such table")
            return self.may
        if "audit_pseudonymise" in query or "audit_delete_older_than" in query:
            return 7
        return None

    async def execute(self, query, *args):
        self.sql.append((query, args))
        return "UPDATE 3" if query.startswith("UPDATE") else "DELETE 4"

    async def fetchrow(self, query, *args):
        return self.status


def _run(coro):
    return asyncio.run(coro)


class TestTheTwoChanges:

    def test_a_role_that_may_uses_plain_sql(self):
        conn = _Conn(may=True)
        assert _run(enforcement.pseudonymise(conn, "ACME")) == 3
        assert _run(enforcement.delete_older_than(conn, 30)) == 4
        sent = [q for q, _ in conn.sql]
        assert any(q.startswith("UPDATE audit_events SET user_id = NULL") for q in sent)
        assert any(q.startswith("DELETE FROM audit_events") for q in sent)
        assert not any("audit_pseudonymise" in q or "audit_delete_older_than" in q for q in sent)

    def test_a_restricted_role_goes_through_the_functions(self):
        conn = _Conn(may=False)
        assert _run(enforcement.pseudonymise(conn, "ACME")) == 7
        assert _run(enforcement.delete_older_than(conn, 120)) == 7
        calls = [(q, a) for q, a in conn.sql if "public.audit_" in q]
        assert calls == [("SELECT public.audit_pseudonymise($1::text)", ("ACME",)),
                         ("SELECT public.audit_delete_older_than($1::integer)", (120,))]
        assert not any(q.startswith(("UPDATE", "DELETE")) for q, _ in conn.sql)

    def test_below_the_floor_the_restricted_path_asks_for_the_floor(self, caplog):
        conn = _Conn(may=False)
        with caplog.at_level(logging.WARNING):
            _run(enforcement.delete_older_than(conn, 30))
        assert conn.sql[-1] == ("SELECT public.audit_delete_older_than($1::integer)",
                                (enforcement.RETENTION_FLOOR_DAYS,))
        assert "below the 90-day floor" in caplog.text

    def test_the_database_floor_is_the_python_floor(self):
        floor = enforcement.RETENTION_FLOOR_DAYS
        assert f"p_days < {floor} THEN" in enforcement._FUNCTIONS_SQL
        assert f"younger than {floor} days" in enforcement._FUNCTIONS_SQL

    def test_a_probe_that_fails_keeps_todays_plain_sql(self):
        conn = _Conn(may=None)
        _run(enforcement.pseudonymise(conn, "ACME"))
        assert conn.sql[-1][0].startswith("UPDATE audit_events")


_CLEAR = {"role": "tokenlean_runtime", "superuser": False, "createrole": False,
          "member_of_a_role": False, "owner_rights": False, "can_update": False,
          "can_delete": False, "can_truncate": False}


class TestEnforcementStatus:

    def test_every_check_clear_is_protected(self):
        status = _run(enforcement.enforcement_status(_Conn(status=dict(_CLEAR))))
        assert status["enforced"] is True and status["reasons"] == []

    @pytest.mark.parametrize("check", [k for k in _CLEAR if k != "role"])
    def test_any_one_failing_check_voids_it(self, check):
        status = _run(enforcement.enforcement_status(_Conn(status={**_CLEAR, check: True})))
        assert status["enforced"] is False and len(status["reasons"]) == 1

    @pytest.mark.parametrize("check", ["createrole", "can_delete"])
    def test_a_missing_or_null_check_counts_against(self, check):
        missing = {k: v for k, v in _CLEAR.items() if k != check}
        assert _run(enforcement.enforcement_status(_Conn(status=missing)))["enforced"] is False
        assert _run(enforcement.enforcement_status(_Conn(status={**_CLEAR, check: None})))["enforced"] is False

    def test_no_audit_table_is_not_protected(self):
        assert _run(enforcement.enforcement_status(_Conn(status=None)))["enforced"] is False


class _Recorder:
    def __init__(self):
        self.sql = []

    def transaction(self):
        class _Tx:
            async def __aenter__(self_):
                return self_

            async def __aexit__(self_, *exc):
                return False
        return _Tx()

    async def execute(self, query, *args):
        self.sql.append(query)

    async def fetchval(self, query, *args):
        if "current_database" in query:
            return "token_opt"
        return "has_schema_privilege" in query or None

    async def fetch(self, query, *args):
        if "pg_auth_members" in query:
            return [{"rolname": "token_opt_app"}]
        if "pg_tables" in query:
            return [{"tablename": "audit_events"}, {"tablename": 'odd"name'}]
        return []


class TestApply:

    def test_the_password_is_sent_only_as_a_scram_verifier(self):
        conn = _Recorder()
        _run(enforcement.apply(conn, "tokenlean_runtime", "s3cret-Pass'word"))
        alter = [q for q in conn.sql if q.startswith('ALTER ROLE "tokenlean_runtime"')]
        assert len(alter) == 1 and "PASSWORD 'SCRAM-SHA-256$4096:" in alter[0]
        assert not any("s3cret" in q for q in conn.sql)
        assert "NOCREATEROLE" in alter[0] and "NOCREATEDB" in alter[0]
        # A non-superuser owner may not even switch these off (a real server refuses).
        assert not any(attr in alter[0] for attr in ("SUPERUSER", "REPLICATION", "BYPASSRLS"))

    def test_audit_events_ends_with_select_and_insert_only(self):
        conn = _Recorder()
        _run(enforcement.apply(conn, "tokenlean_runtime", "pw"))
        grants = [q for q in conn.sql if "public.audit_events" in q or '"audit_events"' in q]
        assert grants[-2:] == ['REVOKE ALL ON public.audit_events FROM "tokenlean_runtime"',
                               'GRANT SELECT, INSERT ON public.audit_events TO "tokenlean_runtime"']
        assert 'REVOKE "token_opt_app" FROM "tokenlean_runtime"' in conn.sql   # no membership kept
        assert 'GRANT SELECT, INSERT, UPDATE, DELETE ON public."odd""name" TO "tokenlean_runtime"' in conn.sql

    @pytest.mark.parametrize("role", ["Runtime", "a b", "x;DROP", "", "1abc"])
    def test_an_unsafe_role_name_is_refused(self, role):
        with pytest.raises(ValueError):
            _run(enforcement.apply(_Recorder(), role, "pw"))

    @pytest.mark.parametrize("password", ["", "tab\tpw", "naïve"])
    def test_an_unusable_password_is_refused(self, password):
        with pytest.raises(ValueError):
            _run(enforcement.apply(_Recorder(), "tokenlean_runtime", password))

    def test_the_runtime_dsn_swaps_only_the_credentials(self):
        owner = "postgresql://token_opt_app:o%40wner@/token_opt?host=/cloudsql/p:r:i"
        assert enforcement.runtime_dsn(owner, "tokenlean_runtime", "p@ss/w") == (
            "postgresql://tokenlean_runtime:p%40ss%2Fw@/token_opt?host=/cloudsql/p:r:i")


class _Pool:
    def __init__(self, row=None, fail=False):
        self.row, self.fail = row, fail

    def acquire(self):
        pool = self

        class _Ctx:
            async def __aenter__(self_):
                if pool.fail:
                    raise ConnectionError("down")
                return self_

            async def __aexit__(self_, *exc):
                return False

            async def fetchrow(self_, query, *args):
                return pool.row
        return _Ctx()


class TestMayRunDdl:

    @pytest.mark.parametrize("row, expected", [
        ({"mine": True}, True),       # the role owns the table (a single-role deployment)
        ({"mine": False}, False),     # a restricted runtime role: the schema job owns it
        (None, True),                 # no such table yet: this role would create it
    ])
    def test_the_probe_decides(self, row, expected):
        assert _run(may_run_ddl(_Pool(row), "audit_events")) is expected

    def test_a_probe_that_fails_keeps_todays_behaviour(self):
        assert _run(may_run_ddl(_Pool(fail=True), "audit_events")) is True


def test_retention_deletes_audit_rows_through_the_helper(monkeypatch):
    import retention
    asked = []

    async def fake_delete(conn, days):
        asked.append(days)
        return 5

    monkeypatch.setattr(enforcement, "delete_older_than", fake_delete)

    class _P:
        def acquire(self):
            class _C:
                async def __aenter__(self_):
                    return self_

                async def __aexit__(self_, *exc):
                    return False

                async def execute(self_, *a):
                    return "DELETE 0"
            return _C()

    out = _run(retention.run_retention_pass(_P(), {"audit_days": 120, "cache_l2_expired_cleanup": False}))
    assert asked == [120] and out["audit_events"] == 5
