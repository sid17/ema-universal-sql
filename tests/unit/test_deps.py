"""The tenant-status gate — crypto-shred's front half.

Kept apart from ``test_auth.py`` because this is the first place identity and the
control plane meet: authentication succeeds and authorisation still refuses. The
distinction matters for the error vocabulary — a bad token is 401, a revoked
tenant is 403 ``ENTITLEMENT_DENIED``, and conflating them would tell a caller
their credentials are broken when the real answer is that their account is not.

Infra-free: a stub repository stands in for Postgres.
"""

import pytest

from src.control_plane.repository import Tenant
from src.gateway.deps import enforce_tenant_status, require_scope
from src.models.context import UserContext
from src.models.errors import ApiError, ErrorCode


def tenant(status: str = "active") -> Tenant:
    return Tenant(
        tenant_id="tenant_acme",
        name="Acme",
        status=status,
        residency="us",
        deployment_mode="multi-tenant",
        fernet_key="not-a-real-key",
    )


class StubRepository:
    def __init__(self, result: Tenant | None) -> None:
        self._result = result
        self.calls: list[str] = []

    def get_tenant(self, tenant_id: str) -> Tenant | None:
        self.calls.append(tenant_id)
        return self._result


@pytest.fixture
def context() -> UserContext:
    return UserContext(
        tenant_id="tenant_acme",
        user_id="alice",
        roles=["support"],
        raw_claims={},
    )


def test_active_tenant_passes_through(context):
    repo = StubRepository(tenant("active"))

    assert enforce_tenant_status(context, repo) is context
    assert repo.calls == ["tenant_acme"], "the gate must consult the control plane"


@pytest.mark.parametrize("status", ["suspended", "offboarding"])
def test_inactive_tenant_is_refused_with_403(context, status):
    repo = StubRepository(tenant(status))

    with pytest.raises(ApiError) as raised:
        enforce_tenant_status(context, repo)

    assert raised.value.code is ErrorCode.ENTITLEMENT_DENIED
    assert raised.value.http == 403
    # The message must NOT name the status: distinguishing "offboarding" from
    # "unknown" hands the caller a tenant-status oracle.
    assert status not in raised.value.message


def test_unknown_tenant_is_refused(context):
    """A validly-signed token may name any tenant; unknown must not mean allowed."""
    repo = StubRepository(None)

    with pytest.raises(ApiError) as raised:
        enforce_tenant_status(context, repo)

    assert raised.value.code is ErrorCode.ENTITLEMENT_DENIED
    assert raised.value.http == 403


def test_refusal_happens_before_any_planning(context):
    """The gate's whole point: a revoked tenant reaches no connector.

    It is the only control-plane read on this path, so if it raises, nothing
    downstream — secret resolution, connector fan-out — has run yet.
    """
    repo = StubRepository(tenant("offboarding"))

    with pytest.raises(ApiError):
        enforce_tenant_status(context, repo)

    assert repo.calls == ["tenant_acme"]


def test_denial_is_a_domain_code_not_a_401(context):
    """Authentication succeeded here — only authorisation failed."""
    repo = StubRepository(tenant("suspended"))

    with pytest.raises(ApiError) as raised:
        enforce_tenant_status(context, repo)

    assert raised.value.http != 401
    assert raised.value.code in set(ErrorCode)


# --------------------------------------------------------------------------
# L2 — the coarse scope gate
# --------------------------------------------------------------------------


def scoped(*scopes: str) -> UserContext:
    return UserContext(
        tenant_id="tenant_acme",
        user_id="alice",
        roles=("support",),
        scopes=frozenset(scopes),
    )


def test_a_token_with_the_scope_passes():
    granted = scoped("query:execute")
    assert require_scope(granted, "query:execute") is granted


def test_a_token_without_the_scope_is_refused():
    with pytest.raises(ApiError) as raised:
        require_scope(scoped(), "query:execute")

    assert raised.value.code is ErrorCode.ENTITLEMENT_DENIED
    assert raised.value.http == 403
    assert "query:execute" in raised.value.message


def test_an_unrelated_scope_does_not_satisfy_the_gate():
    """Holding some scope is not holding the right one."""
    with pytest.raises(ApiError):
        require_scope(scoped("connector:github", "profile"), "query:execute")


def test_the_scope_gate_reads_only_the_token():
    """The invariant that keeps L2 honest: no control-plane read, no data read.

    If this gate ever needed a repository, it would be making a decision that
    belongs in the query plan (DoD §4 non-negotiable #1), not at the endpoint.
    """
    import inspect

    from src.gateway.deps import require_scope as gate

    assert set(inspect.signature(gate).parameters) == {"context", "scope"}
