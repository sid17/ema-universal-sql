"""Auth gate: minting, verification, and the four ways a token is rejected.

Hermetic — no Postgres, no Redis, no network. The tenant-status gate (403 for a
suspended tenant) is exercised in ``test_deps.py``, which needs a control-plane
double; this file is only about identity.
"""

from datetime import UTC, datetime, timedelta

import jwt
import pytest

from src.config import get_settings
from src.gateway.auth import ALGORITHM, ISSUER, AuthContextExtractor, mint_mock_token
from src.models.errors import UnauthenticatedError


@pytest.fixture
def extractor() -> AuthContextExtractor:
    return AuthContextExtractor()


@pytest.fixture
def settings():
    return get_settings()


def bearer(token: str) -> str:
    return f"Bearer {token}"


def make_token(settings, **overrides) -> str:
    """Build a token with deliberately wrong claims, to test each rejection."""
    now = datetime.now(UTC)
    claims = {
        "sub": "alice",
        "roles": ["support"],
        "scope": "query:execute",
        "tenant_id": "tenant_acme",
        "aud": settings.JWT_AUDIENCE,
        "iss": ISSUER,
        "jti": "test-token-id",
        "iat": now,
        "exp": now + timedelta(hours=1),
    }
    claims.update(overrides)
    return jwt.encode(claims, settings.JWT_SECRET, algorithm=ALGORITHM)


# --------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------


def test_minted_token_round_trips_to_a_user_context(extractor):
    token = mint_mock_token("alice", "support", "tenant_acme")

    context = extractor.extract(bearer(token))

    assert context.user_id == "alice"
    assert context.tenant_id == "tenant_acme"
    assert context.roles == ("support",)
    assert context.raw_claims["aud"] == get_settings().JWT_AUDIENCE


def test_personas_are_distinguishable(extractor):
    """alice/bob/carol drive the RLS demo, so identity must actually differ."""
    alice = extractor.extract(bearer(mint_mock_token("alice", "support", "tenant_acme")))
    bob = extractor.extract(bearer(mint_mock_token("bob", "support", "tenant_acme")))

    assert alice.user_id != bob.user_id
    assert alice.tenant_id == bob.tenant_id


def test_context_is_frozen(extractor):
    """Entitlement is decided against this object; no stage may rewrite it."""
    context = extractor.extract(bearer(mint_mock_token("alice", "support", "tenant_acme")))

    with pytest.raises(Exception, match="frozen|immutable|cannot assign"):
        context.tenant_id = "tenant_globex"  # type: ignore[misc]


def test_context_is_deeply_immutable(extractor):
    """The fix for a real privilege-escalation seam.

    ``@dataclass(frozen=True)`` only blocks attribute *rebinding*. An earlier
    version used a ``list`` for roles and a plain ``dict`` for claims, so
    ``context.roles.append("admin")`` SUCCEEDED — inside the one object whose
    job is to be the trustworthy answer to "who is asking", and whose ``roles``
    select the policies that become the RLS predicate.
    """
    context = extractor.extract(bearer(mint_mock_token("alice", "support", "tenant_acme")))

    assert isinstance(context.roles, tuple)
    assert isinstance(context.scopes, frozenset)
    with pytest.raises(AttributeError):
        context.roles.append("admin")  # type: ignore[attr-defined]
    with pytest.raises(TypeError):
        context.raw_claims["tenant_id"] = "tenant_globex"  # type: ignore[index]


# --------------------------------------------------------------------------
# Scopes and roles are different vocabularies (RFC 9068 §2.2.3 / §2.2.3.1)
# --------------------------------------------------------------------------


def test_scope_claim_is_space_delimited_not_a_list(extractor, settings):
    """RFC 8693 §4.2. Treating it as a list is the classic JWT-scope bug."""
    token = make_token(settings, scope="query:execute connector:github")

    context = extractor.extract(bearer(token))

    assert context.scopes == {"query:execute", "connector:github"}
    assert context.has_scope("query:execute")
    assert not context.has_scope("query:admin")


def test_a_list_scope_claim_is_tolerated(extractor, settings):
    """Some issuers send a list. Read it rather than silently seeing no scopes."""
    token = make_token(settings, scope=["query:execute", "connector:jira"])

    assert extractor.extract(bearer(token)).scopes == {"query:execute", "connector:jira"}


def test_a_token_with_no_scope_claim_gets_no_scopes(extractor, settings):
    """Absent must mean none — never invent a scope the token did not carry."""
    token = make_token(settings, scope="")

    assert extractor.extract(bearer(token)).scopes == frozenset()


def test_minted_tokens_carry_the_default_scope(extractor):
    context = extractor.extract(bearer(mint_mock_token("alice", "support", "tenant_acme")))

    assert context.has_scope("query:execute")


def test_minting_can_produce_an_underprivileged_token(extractor):
    """How the scope gate gets tested end to end."""
    context = extractor.extract(
        bearer(mint_mock_token("alice", "support", "tenant_acme", scopes=""))
    )

    assert context.scopes == frozenset()


def test_jti_is_captured_and_unique(extractor):
    """The audit trail's join key back to one specific credential."""
    first = extractor.extract(bearer(mint_mock_token("alice", "support", "tenant_acme")))
    second = extractor.extract(bearer(mint_mock_token("alice", "support", "tenant_acme")))

    assert first.token_id and second.token_id
    assert first.token_id != second.token_id


def test_roles_and_scopes_are_independent(extractor, settings):
    """Roles select policies; scopes gate the endpoint. Different jobs."""
    token = make_token(settings, roles=["support", "auditor"], scope="query:execute")

    context = extractor.extract(bearer(token))

    assert context.roles == ("support", "auditor")
    assert context.has_any_role("auditor")
    assert context.scopes == {"query:execute"}


# --------------------------------------------------------------------------
# Rejections — all 401, none of them a domain ErrorCode
# --------------------------------------------------------------------------


def test_expired_token_is_rejected(extractor, settings):
    expired = make_token(settings, exp=datetime.now(UTC) - timedelta(minutes=1))

    with pytest.raises(UnauthenticatedError):
        extractor.extract(bearer(expired))


def test_wrong_audience_is_rejected(extractor, settings):
    """Stops a token minted for another service being replayed at this one."""
    foreign = make_token(settings, aud="some-other-service")

    with pytest.raises(UnauthenticatedError):
        extractor.extract(bearer(foreign))


def test_bad_signature_is_rejected(extractor, settings):
    forged = jwt.encode(
        {
            "sub": "alice",
            "roles": ["support"],
            "tenant_id": "tenant_acme",
            "aud": settings.JWT_AUDIENCE,
            "iss": ISSUER,
            "exp": datetime.now(UTC) + timedelta(hours=1),
        },
        "not-the-signing-key-but-long-enough-to-not-warn",
        algorithm=ALGORITHM,
    )

    with pytest.raises(UnauthenticatedError):
        extractor.extract(bearer(forged))


def test_token_without_tenant_is_rejected(extractor, settings):
    """A token that authenticates but names no tenant cannot be scoped."""
    tenantless = make_token(settings, tenant_id=None)

    with pytest.raises(UnauthenticatedError):
        extractor.extract(bearer(tenantless))


@pytest.mark.parametrize(
    "header",
    [None, "", "   ", "Bearer", "Bearer ", "Basic abc123", "abc123"],
    ids=["none", "empty", "blank", "scheme-only", "no-token", "wrong-scheme", "no-scheme"],
)
def test_malformed_headers_are_rejected(extractor, header):
    with pytest.raises(UnauthenticatedError):
        extractor.extract(header)


def test_rejection_is_401_and_not_a_domain_code():
    """A forged token is a transport failure, not an entitlement decision."""
    from src.models.errors import ErrorCode

    error = UnauthenticatedError()

    assert error.http == 401
    assert error.error_code == "UNAUTHENTICATED"
    with pytest.raises(ValueError, match="UNAUTHENTICATED"):
        ErrorCode("UNAUTHENTICATED")


# --------------------------------------------------------------------------
# Algorithm confusion — the attacks a JWT gate is expected to refuse
# --------------------------------------------------------------------------


def test_alg_none_token_is_rejected(extractor, settings):
    """The canonical JWT bypass: drop the signature, claim no algorithm."""
    unsigned = jwt.encode(
        {
            "sub": "alice",
            "roles": ["support"],
            "scope": "query:execute",
            "tenant_id": "tenant_acme",
            "aud": settings.JWT_AUDIENCE,
            "iss": ISSUER,
            "exp": datetime.now(UTC) + timedelta(hours=1),
        },
        key="",
        algorithm="none",
    )

    with pytest.raises(UnauthenticatedError):
        extractor.extract(bearer(unsigned))


def test_a_different_algorithm_is_rejected_even_with_the_right_secret(extractor, settings):
    """Algorithm confusion: the decoder must pin `alg`, not trust the header.

    HS512 signed with the CORRECT secret must still fail, because acceptance is
    decided by our `algorithms=[...]` list and not by what the token asks for.
    """
    wrong_alg = jwt.encode(
        {
            "sub": "alice",
            "roles": ["support"],
            "scope": "query:execute",
            "tenant_id": "tenant_acme",
            "aud": settings.JWT_AUDIENCE,
            "iss": ISSUER,
            "exp": datetime.now(UTC) + timedelta(hours=1),
        },
        settings.JWT_SECRET,
        algorithm="HS512",
    )

    with pytest.raises(UnauthenticatedError):
        extractor.extract(bearer(wrong_alg))


def test_wrong_issuer_is_rejected(extractor, settings):
    with pytest.raises(UnauthenticatedError):
        extractor.extract(bearer(make_token(settings, iss="https://evil.example")))


@pytest.mark.parametrize("bad", [1234, ["tenant_acme"], {"id": "tenant_acme"}])
def test_non_string_tenant_id_is_rejected(extractor, settings, bad):
    """A dict here would reach a cache key unhashable, turning a 401 into a 500."""
    with pytest.raises(UnauthenticatedError):
        extractor.extract(bearer(make_token(settings, tenant_id=bad)))


def test_context_type_enforces_immutability_for_every_caller():
    """The guarantee must live on the TYPE, not on one construction site.

    An earlier fix coerced only inside AuthContextExtractor, so any other caller
    — every test fixture, every future phase — could hand in a list and rebuild
    the privilege-escalation seam.
    """
    from src.models.context import UserContext

    built_by_hand = UserContext(
        tenant_id="t", user_id="u", roles=["support"], scopes={"a"}, raw_claims={"k": "v"}
    )

    assert isinstance(built_by_hand.roles, tuple)
    assert isinstance(built_by_hand.scopes, frozenset)
    with pytest.raises(AttributeError):
        built_by_hand.roles.append("admin")  # type: ignore[attr-defined]
    with pytest.raises(AttributeError):
        built_by_hand.scopes.add("query:admin")  # type: ignore[attr-defined]
    with pytest.raises(TypeError):
        built_by_hand.raw_claims["k"] = "tampered"  # type: ignore[index]
