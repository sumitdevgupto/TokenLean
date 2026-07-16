CREATE TABLE IF NOT EXISTS tenant_configs (
  tenant_id        TEXT        PRIMARY KEY,
  config_overrides JSONB       NOT NULL DEFAULT '{}',
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
