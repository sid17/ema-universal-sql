"""The three doubles the hermetic suite shares: a control plane and two spies.

Split from `conftest.py` at LAW 1's 400-line decompose threshold. `conftest`
keeps the *fixtures* — what a test asks for by name; this keeps the *classes*
those fixtures build, which are the part with behaviour worth reading.

The two spies exist because an outcome cannot distinguish "cache hit, no token
spent" from "token spent, then refunded". They write onto one shared timeline so
a test can assert the ORDER of steps across three collaborators, not merely that
each one happened.
"""

from types import SimpleNamespace

from cryptography.fernet import Fernet

from src.governance.cache import FreshnessCacheManager
from src.governance.ratelimit import TokenBucketRateLimiter
from src.governance.secrets import SecretsManagerClient
from tests.unit.catalog_fixture import connector_rows

#: One Fernet key for every fixture tenant. Generated per run rather than
#: committed: nothing here asserts a specific key, only that the right tenant's
#: key is the one used.
ACME_FERNET_KEY = Fernet.generate_key().decode()


class FakeControlPlane:
    """The four control-plane reads a connector makes, without Postgres.

    Counts its calls so a test can assert that an adapter which served from
    cache did not go on to read a rate-limit policy it had no use for.
    """

    def __init__(self, timeline: list[str] | None = None) -> None:
        self.calls: list[tuple[str, tuple]] = []
        # Shared with SpyLimiter so a test can assert the ORDER of steps across
        # both collaborators, not just that each one happened.
        self.timeline = [] if timeline is None else timeline
        self.rate_limits = {
            ("tenant_acme", "github"): {"max_requests": 5, "window_sec": 60, "burst": 2},
            ("tenant_acme", "jira"): {"max_requests": 30, "window_sec": 60, "burst": 5},
            ("tenant_load", "github"): {"max_requests": 5000, "window_sec": 60, "burst": 500},
            ("tenant_load", "jira"): {"max_requests": 5000, "window_sec": 60, "burst": 500},
        }
        self.grants = {
            "tenant_acme": [
                {
                    "connector_type": "github",
                    "enabled": True,
                    "status": "active",
                    "secret_ref": "tenant_acme/github",
                },
                {
                    "connector_type": "jira",
                    "enabled": True,
                    "status": "active",
                    "secret_ref": "tenant_acme/jira",
                },
            ],
            "tenant_load": [
                {
                    "connector_type": "github",
                    "enabled": True,
                    "status": "active",
                    "secret_ref": "tenant_load/github",
                },
                {
                    "connector_type": "jira",
                    "enabled": True,
                    "status": "active",
                    "secret_ref": "tenant_load/jira",
                },
            ],
        }
        self.secrets = {
            ref: {
                "secret_ref": ref,
                "tenant_id": ref.split("/")[0],
                "ciphertext": SecretsManagerClient.encrypt(ACME_FERNET_KEY, f"token-for-{ref}"),
            }
            for ref in (
                "tenant_acme/github",
                "tenant_acme/jira",
                "tenant_load/github",
                "tenant_load/jira",
            )
        }
        self.tenants = {
            t: SimpleNamespace(tenant_id=t, fernet_key=ACME_FERNET_KEY)
            for t in ("tenant_acme", "tenant_load")
        }

        #: One row per (connector_type, resource) — what `list_connectors()`
        #: returns and what `ConnectorRegistry` builds everything from.
        self.connectors = connector_rows()

    def list_connectors(self):
        self.calls.append(("list_connectors", ()))
        return self.connectors

    def get_connector(self, connector_type, resource):
        self.calls.append(("get_connector", (connector_type, resource)))
        for row in self.connectors:
            if row["connector_type"] == connector_type and row["resource"] == resource:
                return row
        return None

    def get_rate_limit_policy(self, tenant_id, connector_type):
        self.calls.append(("get_rate_limit_policy", (tenant_id, connector_type)))
        return self.rate_limits.get((tenant_id, connector_type))

    def read_cache_marker(self) -> None:
        """Called by the cache spy — see `cache` fixture."""
        self.timeline.append("read_cache")

    def get_tenant_connectors(self, tenant_id):
        self.calls.append(("get_tenant_connectors", (tenant_id,)))
        return self.grants.get(tenant_id, [])

    def get_secret(self, secret_ref):
        self.calls.append(("get_secret", (secret_ref,)))
        self.timeline.append("resolve_secret")
        return self.secrets.get(secret_ref)

    def get_tenant(self, tenant_id):
        self.calls.append(("get_tenant", (tenant_id,)))
        return self.tenants.get(tenant_id)


class SpyLimiter(TokenBucketRateLimiter):
    """A limiter that records every consume, so ORDER can be asserted.

    The other tests assert outcomes; an outcome cannot distinguish "cache hit,
    no token spent" from "token spent, then refunded".
    """

    def __init__(self, *args, timeline: list[str] | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.consumed: list[tuple[str, str]] = []
        self.timeline = [] if timeline is None else timeline

    async def try_consume(self, tenant_id, connector_type, policy):
        # `try_consume` rather than `consume`: it is what the adapter calls (it
        # needs the decision on the denied branch too), and `consume` delegates
        # here — so this records BOTH entry points rather than only one.
        self.consumed.append((tenant_id, connector_type))
        self.timeline.append("consume_token")
        return await super().try_consume(tenant_id, connector_type, policy)


class SpyCache(FreshnessCacheManager):
    """Records cache reads onto the shared timeline."""

    def __init__(self, *args, timeline: list[str] | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.timeline = [] if timeline is None else timeline

    async def get(self, key, max_staleness_ms):
        self.timeline.append("read_cache")
        return await super().get(key, max_staleness_ms)
