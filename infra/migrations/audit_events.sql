CREATE TABLE IF NOT EXISTS audit_events (
  id             BIGSERIAL    PRIMARY KEY,
  tenant_id      TEXT         NOT NULL,
  request_id     TEXT         NOT NULL,
  timestamp      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
  action         TEXT         NOT NULL DEFAULT 'proxy_request',
  user_id        TEXT,
  groups_applied TEXT[]       NOT NULL DEFAULT '{}',
  tokens_saved   INT          NOT NULL DEFAULT 0,
  otel_trace_id  TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_events_tenant_id
  ON audit_events (tenant_id);
CREATE INDEX IF NOT EXISTS idx_audit_events_timestamp
  ON audit_events (timestamp DESC);
ALTER TABLE audit_events ADD COLUMN IF NOT EXISTS details JSONB;
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT FROM pg_roles WHERE rolname = 'proxy_audit_role'
  ) THEN
    CREATE ROLE proxy_audit_role;
  END IF;
END $$;
REVOKE ALL ON audit_events FROM proxy_audit_role;
GRANT INSERT ON audit_events TO proxy_audit_role;
