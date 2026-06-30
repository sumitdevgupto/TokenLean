-- I2 — Row-Level Security (defense-in-depth) for the tenant tables.
--
-- The proxy's application-level filters (WHERE tenant_id = ...) remain the
-- primary isolation. RLS is a safety net: tenant-scoped data-plane connections
-- set the GUC `app.tenant_id` (see cache.pg_pool.tenant_conn), so a forgotten
-- WHERE cannot leak across tenants.
--
-- The policies are PERMISSIVE WHEN THE GUC IS UNSET, so cross-tenant admin/GDPR
-- queries (which intentionally leave app.tenant_id empty) keep working. This
-- makes the migration safe to apply to a running system and lets call sites
-- adopt tenant_conn incrementally.
--
-- Apply: psql "$DATABASE_URL" -f infra/migrations/rls_policies.sql  (idempotent)

DO $$
DECLARE
  t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['cache_l2', 'usage_events', 'audit_events', 'tenant_configs']
  LOOP
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = t) THEN
      EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
      -- FORCE so the policy applies even when the app role owns the table.
      EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
      EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', t);
      EXECUTE format($pol$
        CREATE POLICY tenant_isolation ON %I
        USING (
          tenant_id = current_setting('app.tenant_id', true)
          OR coalesce(current_setting('app.tenant_id', true), '') = ''
        )
        WITH CHECK (
          tenant_id = current_setting('app.tenant_id', true)
          OR coalesce(current_setting('app.tenant_id', true), '') = ''
        )
      $pol$, t);
      RAISE NOTICE 'RLS tenant_isolation policy ensured on %', t;
    END IF;
  END LOOP;
END $$;
