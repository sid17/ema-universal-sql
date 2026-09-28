-- Phase 0: the tenant rows, and only those.
--
-- WHY THIS EXISTS. Phase 0 owns the tenant-status gate (`src/gateway/deps.py`),
-- which reads `tenants` on every authenticated request. The phase file also says
-- tables are "seeded in Phase 1/2" — but with `tenants` empty, the gate correctly
-- refuses every request with `403 Unknown tenant`, so Phase 0's own gate ("200
-- envelope shell on a valid token") could never pass. The alternative — letting
-- an unknown tenant through — would fail open, which is worse than a red test.
-- So the gate's data ships with the gate; everything else still waits for Phase 1.
--
-- SCOPE: tenants only. Connectors, the grant table, secrets, policies,
-- rate-limit budgets and the mock datasets are Phase 1's, per the execution plan.
--
-- NOTE FOR PHASE 1: this file takes the `002_` slot. The execution plan calls
-- Phase 1's seed `002_seed.sql`; it should become `003_seed.sql`.

-- The three tenants named in the locked rails (HLD §6 and §9).
INSERT INTO tenants (tenant_id, name, status, residency, deployment_mode, fernet_key) VALUES
  -- The default tenant. Everything in the demo runs here unless stated otherwise.
  ('tenant_acme',   'Acme Corp',      'active', 'us', 'multi-tenant',
   '6Th1M93KT_LGtUkcQOe11EoJX3a3Ha1Df6U9JGbylUQ='),

  -- Seeded only to prove cache and credential isolation in a Phase 1 test:
  -- tenant_globex must never read tenant_acme's cached rows or resolve its secret.
  ('tenant_globex', 'Globex Inc',     'active', 'eu', 'multi-tenant',
   'ZJ4sR2pXtNvK8mQwL6yB3cH9dF1gA5eU7iO0nS2xT4M='),

  -- The ONLY tenant k6 may target (HLD §9). tenant_acme's GitHub budget is
  -- deliberately tiny (5 req/60s) to make the 429 demo deterministic, so a load
  -- run against it would be 30k throttled requests measuring nothing.
  ('tenant_load',   'Load Test Tenant','active', 'us', 'multi-tenant',
   'pQ8wE3rT5yU7iO9pA1sD2fG4hJ6kL8zX0cV2bN4mQ6E=')
ON CONFLICT (tenant_id) DO NOTHING;

-- A deliberately inactive tenant, so the crypto-shred front half is demonstrable
-- against real data rather than only against a unit-test stub. Phase 1's
-- `test_crypto_shred` pairs the key-destroy back half with this row.
INSERT INTO tenants (tenant_id, name, status, residency, deployment_mode, fernet_key) VALUES
  ('tenant_offboarded', 'Offboarded Ltd', 'offboarding', 'us', 'multi-tenant',
   'mN5bV7cX9zL1kJ3hG5fD7sA9pO1iU3yT5rE7wQ9aZ2s=')
ON CONFLICT (tenant_id) DO NOTHING;
