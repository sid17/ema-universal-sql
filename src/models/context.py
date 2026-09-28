"""The authenticated caller, resolved once per request from the JWT.

Claim names follow **RFC 9068** (JWT Profile for OAuth 2.0 Access Tokens), so the
mock token is shaped like something a real IdP would issue and swapping one in
later changes how a token is *obtained*, not what the pipeline does with it.

RFC 9068 gives authorization two separate vocabularies, and this prototype uses
both for different jobs — token → scopes/roles → RLS/CLS:

``scopes``
    From the ``scope`` claim (RFC 9068 §2.2.3 → RFC 8693 §4.2). **Coarse API
    permission**: may this caller execute queries at all? Checked at the gateway,
    before anything is planned or fetched.

``roles``
    From the ``roles`` claim (RFC 9068 §2.2.3.1, borrowed from the SCIM user
    schema, RFC 7643 §4.1.2). **Selects which policies apply** — they match
    ``policies.applies_to`` — and so shape the RLS predicate and CLS mask that
    get compiled into the plan.

Do not confuse either with ``entitlement_scope``, which is a *data* scope used as
a cache-key segment and to resolve set-valued RLS params. Conflating it with the
OAuth ``scope`` above would be a data-leak vector: one decides whether you may
call the API, the other decides which rows you may see.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any


@dataclass(frozen=True)
class UserContext:
    """Immutable identity carried through every pipeline stage.

    ``@dataclass(frozen=True)`` only blocks attribute *rebinding*; a ``list`` or
    ``dict`` field stays mutable. An earlier version of this class therefore
    allowed ``context.roles.append("admin")`` to succeed — a privilege-escalation
    seam inside the object whose whole purpose is to be the trustworthy answer to
    "who is asking", since ``roles`` selects the policies that become the RLS
    predicate.

    The guarantee lives on the **type**, not on the caller: ``__post_init__``
    coerces whatever it is handed. A first fix coerced only at the one
    construction site in ``AuthContextExtractor``, which left every test fixture
    and every future caller free to rebuild the seam.

    **Scope of the guarantee:** the container fields are immutable and
    ``raw_claims`` is a read-only view whose top level cannot be reassigned. A
    *nested* mutable value inside a claim (``raw_claims["x"]["y"] = ...``) is
    still mutable — deep-freezing arbitrary JSON is not worth the cost, and
    nothing reads nested claims for an authorization decision. Do not start.
    """

    tenant_id: str
    user_id: str
    """The ``sub`` claim. This is also the RLS subject: `assignee = :user`."""

    roles: tuple[str, ...] = ()
    """SCIM ``roles``. Matched against ``policies.applies_to``."""

    scopes: frozenset[str] = frozenset()
    """OAuth ``scope``, already split on spaces. Coarse gateway permission."""

    token_id: str | None = None
    """The ``jti`` claim — the audit trail's join key back to a single token."""

    raw_claims: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    """Everything else the token carried, read-only."""

    def __post_init__(self) -> None:
        """Coerce the containers, so the guarantee cannot be bypassed by a caller."""
        object.__setattr__(self, "roles", tuple(self.roles))
        object.__setattr__(self, "scopes", frozenset(self.scopes))
        object.__setattr__(self, "raw_claims", MappingProxyType(dict(self.raw_claims)))

    def has_scope(self, scope: str) -> bool:
        """Coarse permission check. Never consults data — see the module docstring."""
        return scope in self.scopes

    def has_any_role(self, *roles: str) -> bool:
        return any(role in self.roles for role in roles)
