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

-- ---------------------------------------------------------------------------
-- ABOUT `fernet_key`: WHAT THIS PROVES, AND WHAT IT DOES NOT
--
-- This column holds raw key MATERIAL, in the same database as the ciphertext it
-- decrypts (`secrets.ciphertext`), and the four values below are committed to
-- this repository. Both facts are deliberate, and both are limitations worth
-- stating rather than discovering.
--
-- Encryption at rest only protects against an attacker who reaches the data
-- without reaching the key. Here a single `pg_dump` yields BOTH halves, so
-- against the most likely breach — a leaked backup, a stale snapshot, a stolen
-- replica — this encryption provides no confidentiality. Treat it as
-- obfuscation, not as protection.
--
-- What it DOES prove, and what it is here for:
--   * INDIRECTION — `tenant_connector.secret_ref` -> `secrets.ciphertext` ->
--     decrypt. A credential is never inline, never in a config file, and is
--     resolved per fetch. That code shape is correct and unchanged in production.
--   * CRYPTO-SHRED — the key is PER TENANT and lives outside the ciphertext, so
--     offboarding is one row write that renders that tenant's data permanently
--     unreadable, while every other tenant is untouched. This property does not
--     depend on the key being secret, only on it being per-tenant and
--     destroyable. `test_crypto_shred` asserts it.
--   * NO CROSS-LOAD — one tenant's `secret_ref` can never be decrypted with
--     another tenant's key (`test_secret_indirection`).
--
-- PRODUCTION (take-home line 99: "Vault + cloud KMS (tenant-scoped); rotation
-- and break-glass"): this column stores a WRAPPED data-encryption key, not key
-- material. The key-encryption key lives in KMS or an HSM and never touches the
-- database, so a dump alone is useless. Envelope encryption keeps everything
-- above intact — per-tenant keys, crypto-shred, runtime tenant onboarding — and
-- adds confidentiality. It is a change of custody, not of design.
--
-- Note the connector credentials themselves are NOT committed: `scripts/seed.py`
-- generates them at seed time (ADR-026). These keys are the one remaining piece
-- of committed key material, and the same reasoning applies to them as to
-- JWT_SECRET, which `src/main.py` warns about loudly at startup.
-- ---------------------------------------------------------------------------

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
