"""Create the synthetic tenants the load test runs against.

**Why a separate script.** `scripts/seed.py` is config-driven: it reads
`config/*.yaml`, and those files describe the *demo* — three tenants with
deliberately asymmetric budgets and grants that make the 429, the
`CONNECTOR_NOT_ENABLED` and the deny-overrides demos deterministic. Twenty
identical load tenants are not configuration anyone would author by hand, and
putting them in those files would bury the demo fixtures in noise.

**Why twenty.** The number is derived, not chosen. A connector's quota is *per
tenant* — GitHub's is 5,000 requests/hour, or 1.39 req/s — so the cache hit
ratio a system must sustain falls out of the tenant count:

    h  >=  1 - (tenants * 1.39) / QPS

At 500 QPS, twenty tenants require a **94.4%** hit ratio. That is the number the
load profile targets and the load report measures. See docs/LOAD-TESTING.md §4.

Safe to re-run: every write is an upsert, and nothing here touches the three
demo tenants. `scripts/seed.py` cannot revoke these grants either — its
revocation pass is scoped to the tenants named in `config/grants.yaml`.
"""

from __future__ import annotations

import os
import sys

import psycopg
from cryptography.fernet import Fernet
from psycopg.types.json import Jsonb

from scripts.seed import generate_token
from src.governance.secrets import SecretsManagerClient

#: One tenant per cache-key cohort. See the module docstring for the arithmetic.
DEFAULT_TENANT_COUNT = 20

#: Must match `Settings.LOAD_TENANT_PREFIX` — the app decides which tenants get
#: synthetic data by this prefix, so the two have to agree.
TENANT_PREFIX = "tenant_load_"

CONNECTORS = ("github", "jira")

#: Deliberately generous BY DEFAULT. These budgets model the *mock's* ceiling,
#: not GitHub's: S1-S3 exist to measure the engine, and a tenant throttled at
#: 1.39 req/s would measure the token bucket instead.
#:
#: S4 is the exception and needs the opposite. Set LOAD_MAX_REQUESTS=84 (=
#: 5,000/hour, GitHub's documented per-installation quota) to seed the REAL
#: ceiling, which is what makes "0% hit ratio is quota-bound, not CPU-bound"
#: demonstrable rather than merely argued.
DEFAULT_MAX_REQUESTS = 5000
WINDOW_SEC = 60

#: Burst is DERIVED, not fixed. A bucket's capacity is `max_requests + burst`,
#: so a fixed burst of 500 on an 84-request budget means the first 584 calls
#: sail through and the quota never binds — which silently turned S4 into a
#: scenario that proved nothing. One tenth keeps the default at exactly 500
#: while making a realistic quota actually throttle.
BURST_FRACTION = 10


def burst_for(max_requests: int) -> int:
    return max(1, max_requests // BURST_FRACTION)


def tenant_ids(count: int) -> list[str]:
    return [f"{TENANT_PREFIX}{index:02d}" for index in range(count)]


def seed_tenants(cur, tenants: list[str]) -> dict[str, str]:
    """Insert the tenant rows, returning each one's Fernet key.

    A fresh key per tenant per run. Nothing asserts a specific key — only that
    each tenant's own key is the one used — and generating them keeps yet more
    committed key material out of the repository.
    """
    keys: dict[str, str] = {}
    for tenant_id in tenants:
        key = Fernet.generate_key().decode()
        cur.execute(
            "INSERT INTO tenants "
            "  (tenant_id, name, status, residency, deployment_mode, fernet_key) "
            "VALUES (%s, %s, 'active', 'us', 'multi-tenant', %s) "
            "ON CONFLICT (tenant_id) DO UPDATE SET status = 'active' "
            "RETURNING fernet_key",
            (tenant_id, f"Load Tenant {tenant_id[-2:]}", key),
        )
        # RETURNING, not the generated value: on a re-run the row already exists
        # and DO UPDATE keeps the ORIGINAL key, so encrypting with the new one
        # would write ciphertext the tenant can never decrypt.
        keys[tenant_id] = cur.fetchone()[0]
    return keys


def seed_grants(cur, keys: dict[str, str]) -> int:
    written = 0
    for tenant_id, key in keys.items():
        for connector_type in CONNECTORS:
            secret_ref = f"{tenant_id}/{connector_type}"
            cur.execute(
                "INSERT INTO secrets (secret_ref, tenant_id, ciphertext) "
                "VALUES (%s, %s, %s) "
                "ON CONFLICT (secret_ref) DO UPDATE SET ciphertext = EXCLUDED.ciphertext",
                (
                    secret_ref,
                    tenant_id,
                    SecretsManagerClient.encrypt(key, generate_token(tenant_id, connector_type)),
                ),
            )
            cur.execute(
                "INSERT INTO tenant_connector "
                "  (tenant_id, connector_type, enabled, status, secret_ref) "
                "VALUES (%s, %s, true, 'active', %s) "
                "ON CONFLICT (tenant_id, connector_type) DO UPDATE SET "
                "  enabled = true, status = 'active', secret_ref = EXCLUDED.secret_ref",
                (tenant_id, connector_type, secret_ref),
            )
            written += 1
    return written


def seed_rate_limits(cur, tenants: list[str], max_requests: int) -> int:
    for tenant_id in tenants:
        for connector_type in CONNECTORS:
            cur.execute(
                "INSERT INTO rate_limit_policies "
                "  (tenant_id, connector_type, max_requests, window_sec, burst) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (tenant_id, connector_type) DO UPDATE SET "
                "  max_requests = EXCLUDED.max_requests, "
                "  window_sec = EXCLUDED.window_sec, burst = EXCLUDED.burst",
                (tenant_id, connector_type, max_requests, WINDOW_SEC, burst_for(max_requests)),
            )
    return len(tenants) * len(CONNECTORS)


def seed_policies(cur, tenants: list[str]) -> int:
    """The same RLS + CLS pair `tenant_acme` carries.

    Not optional scaffolding: with no policy at all the entitlement engine
    default-denies and every query returns an empty 200, so the load run would
    measure the parser against a zero-row join. These make the load path
    exercise the *entitled* tree, which is the one the engine actually executes.
    """
    for tenant_id in tenants:
        cur.execute(
            "INSERT INTO policies "
            "  (policy_id, tenant_id, connector_type, resource, kind, applies_to, "
            "   effect, predicate, version, enabled) "
            "VALUES (%s, %s, 'jira', 'issues', 'RLS', 'support', 'allow', %s, 1, true) "
            "ON CONFLICT (policy_id) DO UPDATE SET predicate = EXCLUDED.predicate",
            (
                f"rls-jira-assignee-{tenant_id}",
                tenant_id,
                Jsonb({"op": "eq", "col": "assignee", "value": ":user"}),
            ),
        )
        cur.execute(
            "INSERT INTO policies "
            "  (policy_id, tenant_id, connector_type, resource, kind, applies_to, "
            "   effect, column_name, mask, version, enabled) "
            "VALUES (%s, %s, 'jira', 'issues', 'CLS', 'support', 'allow', "
            "        'reporter_email', 'hash', 1, true) "
            "ON CONFLICT (policy_id) DO UPDATE SET mask = EXCLUDED.mask",
            (f"cls-jira-reporter-{tenant_id}", tenant_id),
        )
    return len(tenants) * 2


def seed(database_url: str, count: int, max_requests: int) -> dict[str, int]:
    tenants = tenant_ids(count)
    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            keys = seed_tenants(cur, tenants)
            grants = seed_grants(cur, keys)
            limits = seed_rate_limits(cur, tenants, max_requests)
            policies = seed_policies(cur, tenants)
        conn.commit()
    return {
        "tenants": len(tenants),
        "grants": grants,
        "rate_limits": limits,
        "policies": policies,
        "max_requests": max_requests,
    }


def main() -> int:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 1
    count = int(os.environ.get("LOAD_TENANTS", DEFAULT_TENANT_COUNT))
    max_requests = int(os.environ.get("LOAD_MAX_REQUESTS", DEFAULT_MAX_REQUESTS))

    counts = seed(database_url, count, max_requests)
    for name, value in counts.items():
        print(f"  {name}: {value}")
    print(f"seeded {counts['tenants']} load tenants ({TENANT_PREFIX}00 ...)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
