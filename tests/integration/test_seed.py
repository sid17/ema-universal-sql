"""`make seed` against a live Postgres: does the YAML actually land, and can it re-run?

These are integration tests because the thing under test is the round trip
through Postgres — a unit test with a fake connection would assert that the
script calls `execute`, not that the rows a connector reads back match the YAML
an admin wrote. That gap is where a JSONB column or an ON CONFLICT clause goes
wrong.

Needs `make up` first.
"""

import os

import psycopg
import pytest
import yaml
from psycopg.rows import dict_row

from src.connectors.base import CapabilityModel
from src.connectors.request import EndpointSpec, compose_endpoint
from src.connectors.response import RateLimitDialect
from src.control_plane.repository import ControlPlaneRepository
from src.governance.ratelimit import RateLimitPolicy
from src.governance.secrets import SecretsManagerClient

CONFIG_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "config")

# Postgres publishes no host port, so these run against the compose network from
# inside the container, or against an explicitly provided URL.
DATABASE_URL = os.environ.get("SEED_TEST_DATABASE_URL")


pytestmark = pytest.mark.skipif(
    DATABASE_URL is None,
    reason=(
        "Set SEED_TEST_DATABASE_URL to run seed integration tests. "
        "Inside the stack: docker compose exec -T app env "
        "SEED_TEST_DATABASE_URL=$DATABASE_URL python -m pytest tests/integration/test_seed.py"
    ),
)


def load(name: str) -> dict:
    with open(os.path.join(CONFIG_DIR, name)) as handle:
        return yaml.safe_load(handle)


@pytest.fixture
def conn():
    with psycopg.connect(DATABASE_URL) as connection:
        yield connection


@pytest.fixture
def repository():
    from psycopg_pool import ConnectionPool

    pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=2, open=True)
    try:
        # ttl_ms=0 so each read hits Postgres: these tests assert what is IN the
        # database, and a cached read would assert what a previous test saw.
        yield ControlPlaneRepository(pool, ttl_ms=0)
    finally:
        pool.close()


def count(conn, table: str) -> int:
    with conn.cursor() as cur:
        return cur.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


# --- the seed ran ----------------------------------------------------------


def test_every_configured_table_has_rows(conn):
    for table in ("connectors", "tenant_connector", "secrets", "policies", "rate_limit_policies"):
        assert count(conn, table) > 0, f"{table} is empty — did `make seed` run?"


def test_connector_count_matches_the_yaml_files(conn):
    import glob

    files = glob.glob(os.path.join(CONFIG_DIR, "connectors", "*.yaml"))
    assert count(conn, "connectors") == len(files)


# --- idempotence -----------------------------------------------------------


def test_seeding_twice_leaves_identical_row_counts(conn):
    """A reviewer reseeding after a demo must not get a crash or doubled rows."""
    from scripts.seed import seed

    before = {
        t: count(conn, t)
        for t in ("connectors", "tenant_connector", "secrets", "policies", "rate_limit_policies")
    }
    seed(DATABASE_URL)
    seed(DATABASE_URL)
    after = {t: count(conn, t) for t in before}
    assert after == before


def test_reseeding_does_not_break_secret_resolution(conn, repository):
    """Re-encrypting on every seed must still decrypt afterwards."""
    from scripts.seed import seed

    seed(DATABASE_URL)
    secrets = SecretsManagerClient(repository)
    assert secrets.resolve("tenant_acme/github")


# --- the YAML round-trips through the control-plane reads ------------------


def test_every_authored_resource_becomes_its_own_row(repository):
    """One row per `(connector_type, resource)`, and every field the adapter needs.

    A connector file declares an API and the calls it serves, so onboarding
    another GitHub endpoint is one more entry under `resources:` and no Python.
    This asserts the seeder actually expands that map rather than writing one
    row per file.
    """
    for name in ("github", "jira"):
        doc = load(os.path.join("connectors", f"{name}.yaml"))
        for resource, authored in doc["resources"].items():
            row = repository.get_connector(name, resource)
            assert row is not None, f"{name}.{resource} was not seeded"
            assert CapabilityModel.from_dict(row["capabilities"]) == CapabilityModel.from_dict(
                authored["capabilities"]
            )
            # The endpoint is the composition of the api: block and this
            # resource's endpoint: block — the split the YAML authors in.
            assert row["endpoint"] == compose_endpoint(doc["api"], authored["endpoint"])
            assert row["rate_limit"] == doc["api"]["rate_limit"]


def test_the_endpoint_builds_a_spec_the_request_builder_can_use(repository):
    """Seeded JSON must round-trip into the dataclass, not merely be present."""
    spec = EndpointSpec.from_dict(repository.get_connector("github", "pull_requests")["endpoint"])

    assert spec.host == "api.github.com"
    assert spec.path_template == "/repos/{repo}/pulls"
    assert spec.auth_scheme == "bearer"


def test_the_rate_limit_dialect_round_trips_too(repository):
    """GitHub refuses with 403, Jira with 429 — and both are rows, not code."""
    github = RateLimitDialect.from_dict(
        repository.get_connector("github", "pull_requests")["rate_limit"]
    )
    jira = RateLimitDialect.from_dict(repository.get_connector("jira", "issues")["rate_limit"])

    assert github.exhausted_status == 403
    assert github.sends_retry_after is False
    assert jira.exhausted_status == 429
    assert jira.sends_retry_after is True


def test_capability_version_is_stored(repository):
    assert repository.get_connector("github", "pull_requests")["version"] == "1.0.0"


def test_list_connectors_returns_every_seeded_resource(repository):
    """What the registry enumerates to build the catalog and the adapters."""
    listed = {(row["connector_type"], row["resource"]) for row in repository.list_connectors()}

    assert ("github", "pull_requests") in listed
    assert ("jira", "issues") in listed


def test_rate_limits_read_back_exactly_as_authored(repository):
    for budget in load("rate_limits.yaml")["rate_limits"]:
        row = repository.get_rate_limit_policy(budget["tenant_id"], budget["connector_type"])
        assert row is not None, f"{budget['tenant_id']}/{budget['connector_type']} missing"
        assert RateLimitPolicy.from_row(row) == RateLimitPolicy(
            max_requests=budget["max_requests"],
            window_sec=budget["window_sec"],
            burst=budget["burst"],
        )


def test_the_demo_budget_is_small_enough_to_drain(repository):
    """tenant_acme/github must drain inside a short loop or `make demo` is a wait."""
    policy = RateLimitPolicy.from_row(repository.get_rate_limit_policy("tenant_acme", "github"))
    assert policy.capacity <= 10


def test_the_load_budget_is_large_enough_not_to_throttle(repository):
    """tenant_load is the only tenant k6 may target (HLD §9)."""
    policy = RateLimitPolicy.from_row(repository.get_rate_limit_policy("tenant_load", "github"))
    assert policy.max_requests >= 5000


def test_policies_read_back_with_the_predicate_as_an_ast(repository):
    """ADR-008: a JSONB AST, never a SQL string.

    If this ever returns a string, the only way to apply it is concatenation —
    an injection surface, and unanalysable by the planner.
    """
    policies = repository.get_policies("tenant_acme", ["jira"], ["issues"])
    # Selected by id, not by kind: `deny-jira-issues-auditor` is also kind=RLS
    # (an RLS deny with no predicate means "deny every row of this resource"),
    # so filtering on kind alone now matches two rows.
    rls = [p for p in policies if p["policy_id"] == "rls-jira-assignee"]
    assert len(rls) == 1
    predicate = rls[0]["predicate"]
    assert isinstance(predicate, dict)
    assert predicate == {"op": "eq", "col": "assignee", "value": ":user"}


def test_the_cls_rule_masks_the_reporter_email(repository):
    policies = repository.get_policies("tenant_acme", ["jira"], ["issues"])
    cls = [p for p in policies if p["kind"] == "CLS"]
    assert len(cls) == 1
    assert cls[0]["column_name"] == "reporter_email"
    assert cls[0]["mask"] == "hash"


def test_grants_read_back_with_their_secret_refs(repository):
    grants = {g["connector_type"]: g for g in repository.get_tenant_connectors("tenant_acme")}
    assert set(grants) == {"github", "jira"}
    assert grants["github"]["secret_ref"] == "tenant_acme/github"
    assert grants["github"]["enabled"] is True


# --- secrets ---------------------------------------------------------------


def test_secret_indirection_through_real_postgres(repository):
    """GATE test_secret_indirection, against the database rather than a fake.

    Asserts the PROPERTY, not a memorised string: two tenants resolve to
    different credentials, and each resolves to the same one every time. Those
    two facts are what credential isolation means. Pinning a literal would only
    assert that someone typed the same value in two files (ADR-026).
    """
    secrets = SecretsManagerClient(repository)
    acme = secrets.resolve("tenant_acme/github")
    globex = secrets.resolve("tenant_globex/github")

    assert acme != globex, "two tenants must not share a credential"
    assert acme == secrets.resolve("tenant_acme/github"), "resolution must be stable"
    assert globex == secrets.resolve("tenant_globex/github")


def test_every_grant_resolves_to_a_distinct_credential(repository):
    """No two grants anywhere share a value — across tenants AND connectors."""
    secrets = SecretsManagerClient(repository)
    refs = [g["secret_ref"] for g in load("grants.yaml")["grants"]]
    resolved = [secrets.resolve(ref) for ref in refs]
    assert len(set(resolved)) == len(refs)


def test_generated_credentials_are_self_describing_as_mocks(repository):
    """If one surfaces in a log, nobody should open an incident over it."""
    secrets = SecretsManagerClient(repository)
    assert secrets.resolve("tenant_acme/github").startswith("mock_")


def test_grants_yaml_declares_no_credential():
    """The regression guard for ADR-026.

    If anyone re-adds a `token:` field, this fails here — and `scripts/seed.py`
    refuses to seed at all — rather than the value quietly reaching the repo.
    """
    for grant in load("grants.yaml")["grants"]:
        assert "token" not in grant, f"{grant['secret_ref']} authors a literal credential"


def test_the_seeder_refuses_an_authored_credential(monkeypatch):
    """Belt and braces: the loader rejects it, not just the linting of the file."""
    import scripts.seed as seed_module

    original = seed_module.load_yaml

    def patched(path):
        document = original(path)
        if path.name == "grants.yaml":
            first = {**document["grants"][0], "token": "hunter2"}
            document = {"grants": [first]}
        return document

    monkeypatch.setattr(seed_module, "load_yaml", patched)
    with pytest.raises(seed_module.SeedError, match="literal `token`"):
        seed_module.seed(DATABASE_URL)


def test_no_plaintext_credential_is_stored_in_the_database(conn, repository):
    """The whole point of encrypting at seed time.

    If this fails, the resolvable plaintext is sitting in Postgres in the clear
    and the Fernet indirection is decoration.
    """
    secrets = SecretsManagerClient(repository)
    with conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute("SELECT secret_ref, ciphertext FROM secrets").fetchall()
    for row in rows:
        plaintext = secrets.resolve(row["secret_ref"])
        assert plaintext not in row["ciphertext"]
        assert not any(plaintext in r["ciphertext"] for r in rows)


def test_reseeding_rotates_the_credential(repository):
    """Rotation is deliberate, and the system keeps working across it.

    Proves `fetch()` resolves through `secret_ref` on every call rather than
    through a value captured once at startup.
    """
    from scripts.seed import seed

    before = SecretsManagerClient(repository).resolve("tenant_acme/github")
    seed(DATABASE_URL)
    after = SecretsManagerClient(repository).resolve("tenant_acme/github")
    assert before != after, "re-seeding should mint a fresh credential"


def test_each_tenants_ciphertext_is_distinct(conn):
    with conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute("SELECT secret_ref, ciphertext FROM secrets").fetchall()
    ciphertexts = [r["ciphertext"] for r in rows]
    assert len(ciphertexts) == len(set(ciphertexts))


# --- failure handling ------------------------------------------------------


def test_a_policy_naming_an_unknown_tenant_fails_loudly(monkeypatch):
    """LAW 4: a silent skip would leave entitlement quietly unenforced."""
    import scripts.seed as seed_module

    original = seed_module.load_yaml

    def patched(path):
        document = original(path)
        if path.name == "policies.yaml":
            document = {
                "policies": [
                    {
                        **document["policies"][0],
                        "policy_id": "bogus",
                        "tenant_id": "tenant_does_not_exist",
                    }
                ]
            }
        return document

    monkeypatch.setattr(seed_module, "load_yaml", patched)
    with pytest.raises(seed_module.SeedError, match="does not exist"):
        seed_module.seed(DATABASE_URL)


def test_a_failed_seed_leaves_no_partial_write(conn, monkeypatch):
    """One transaction, all-or-nothing.

    A grant whose connector failed to load would otherwise surface later as a
    foreign-key error that looks nothing like the YAML typo that caused it.
    """
    import scripts.seed as seed_module

    before = count(conn, "policies")

    def explode(*args, **kwargs):
        raise seed_module.SeedError("simulated failure after connectors were written")

    monkeypatch.setattr(seed_module, "seed_rate_limits", explode)
    with pytest.raises(seed_module.SeedError):
        seed_module.seed(DATABASE_URL)

    with psycopg.connect(DATABASE_URL) as fresh:
        assert count(fresh, "policies") == before
