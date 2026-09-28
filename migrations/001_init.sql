-- 001_init.sql — the control-plane schema (design-doc §8.3 shape).
--
-- ADR-013: `connectors` is a GLOBAL catalog keyed by connector_type — the
-- connector *definition* (type, version, capabilities JSONB) is data, not code,
-- and carries no tenant_id. `tenant_connector` is the per-tenant *grant* that
-- holds enabled/status/secret_ref. Splitting them is what makes "onboard a
-- connector with one YAML file plus one adapter class" true cheaply.
--
-- Every table is created EMPTY. Seeding is Phase 1; nothing here inserts rows.

CREATE TABLE tenants (
  tenant_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',          -- active|suspended|offboarding
  residency TEXT NOT NULL DEFAULT 'us',
  deployment_mode TEXT NOT NULL DEFAULT 'multi-tenant',
  fernet_key TEXT NOT NULL                        -- per-tenant key (stands in for KMS)
);

CREATE TABLE connectors (                         -- GLOBAL catalog (data-not-code); NOT per-tenant
  connector_type TEXT PRIMARY KEY,                -- 'github' | 'jira'
  version TEXT NOT NULL,
  capabilities JSONB NOT NULL
);

CREATE TABLE tenant_connector (                   -- per-tenant GRANT
  tenant_id TEXT REFERENCES tenants,
  connector_type TEXT REFERENCES connectors,
  enabled BOOLEAN NOT NULL DEFAULT true,
  status TEXT NOT NULL DEFAULT 'active',
  secret_ref TEXT NOT NULL,                       -- pointer; encrypted secret stored in `secrets`
  PRIMARY KEY (tenant_id, connector_type)
);

CREATE TABLE secrets (                            -- Fernet-encrypted; resolved by secret_ref
  secret_ref TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  ciphertext TEXT NOT NULL
);

CREATE TABLE policies (
  policy_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  connector_type TEXT NOT NULL,
  resource TEXT NOT NULL,
  kind TEXT NOT NULL,                             -- 'RLS' | 'CLS'
  applies_to TEXT NOT NULL,                       -- role name or '*'
  effect TEXT NOT NULL DEFAULT 'allow',           -- 'allow' | 'deny'
  predicate JSONB,                                -- RLS: filter AST
  column_name TEXT,
  mask TEXT,                                      -- CLS: null|hash|redact|drop
  version INT NOT NULL DEFAULT 1,
  enabled BOOLEAN NOT NULL DEFAULT true
);

-- The EntitlementEngine's only lookup shape: every policy for a tenant on one
-- connector's resource, enabled rows only.
CREATE INDEX ON policies (tenant_id, connector_type, resource, enabled);

CREATE TABLE rate_limit_policies (
  tenant_id TEXT,
  connector_type TEXT,
  max_requests INT NOT NULL,
  window_sec INT NOT NULL,
  burst INT NOT NULL,
  PRIMARY KEY (tenant_id, connector_type)
);

CREATE TABLE audit_logs (
  log_id BIGSERIAL PRIMARY KEY,
  tenant_id TEXT,
  user_id TEXT,
  query_text TEXT,
  sources_accessed TEXT[],
  rows_returned INT,
  trace_id TEXT,
  execution_ms INT,
  ts TIMESTAMPTZ DEFAULT now()
);
