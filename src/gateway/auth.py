"""Mock identity: mint a JWT, and turn a Bearer header back into a UserContext.

The prototype stands in a **mock HS256 token** for a real per-tenant OIDC
provider (HLD §2). The claim *shape* is the part that matters and is deliberately
OIDC-compatible — ``sub``, ``aud``, ``exp`` plus the two custom claims the
entitlement engine reads — so swapping in a real IdP later changes how the token
is obtained and verified, not what the pipeline does with it.

Every failure here is **401 UNAUTHENTICATED**, never one of the six domain codes:
a forged or expired token is a transport failure, not an entitlement decision.
The 403 for an inactive tenant lives in ``deps.py``, after identity is
established.
"""

from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Any
from uuid import uuid4

import jwt

from src.config import get_settings
from src.models.context import UserContext
from src.models.errors import UnauthenticatedError

#: How long a minted demo token stays valid.
TOKEN_TTL = timedelta(hours=1)

#: Signing algorithm. Symmetric on purpose — a real IdP would be RS256 + JWKS.
ALGORITHM = "HS256"

#: The `iss` claim. One issuer here; a real deployment has one per tenant IdP.
ISSUER = "ema-universal-sql-mock-idp"


def mint_mock_token(
    user: str,
    role: str,
    tenant: str,
    scopes: str | None = None,
) -> str:
    """Mint a signed demo token for a persona.

    Backs ``POST /v1/auth/mock-token``, which is how the console and the
    integration tests get a credential without an IdP.

    ``scopes`` is a space-delimited string (RFC 8693 §4.2). Defaults to
    ``DEFAULT_SCOPES``; pass an empty string to mint a deliberately
    under-privileged token, which is how the scope gate is tested.
    """
    settings = get_settings()
    now = datetime.now(UTC)
    claims = {
        "sub": user,
        # SCIM-style roles (RFC 9068 §2.2.3.1) -> policies.applies_to
        "roles": [role],
        # OAuth scope (RFC 8693 §4.2) is a SPACE-DELIMITED STRING, not a list.
        # Getting this wrong is the classic JWT-scope bug.
        "scope": scopes if scopes is not None else settings.DEFAULT_SCOPES,
        "tenant_id": tenant,
        "aud": settings.JWT_AUDIENCE,
        "iss": ISSUER,
        # jti: unique per token, so an audit row can name the exact credential.
        "jti": uuid4().hex,
        "iat": now,
        "exp": now + TOKEN_TTL,
    }
    return jwt.encode(claims, settings.JWT_SECRET, algorithm=ALGORITHM)


class AuthContextExtractor:
    """Decodes an ``Authorization`` header into a :class:`UserContext`.

    Verification is not optional anywhere here: signature, expiry **and**
    audience are all checked. Audience in particular is easy to skip and is what
    stops a token minted for another service from being replayed at this one.
    """

    def extract(self, authorization_header: str | None) -> UserContext:
        """Return the caller's context, or raise ``UnauthenticatedError``.

        Rejection messages distinguish *expired* from *invalid* on purpose: a
        client needs to know whether to refresh, and expiry is not a secret. They
        deliberately do **not** distinguish a bad signature from a malformed
        token, since that difference would say something about the signing setup.
        """
        token = self._bearer_token(authorization_header)
        claims = self._decode(token)
        return self._to_context(claims)

    @staticmethod
    def _bearer_token(header: str | None) -> str:
        if not header:
            raise UnauthenticatedError("Missing Authorization header")
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise UnauthenticatedError("Expected an 'Authorization: Bearer <token>' header")
        return token.strip()

    @staticmethod
    def _decode(token: str) -> dict[str, Any]:
        settings = get_settings()
        try:
            return jwt.decode(
                token,
                settings.JWT_SECRET,
                algorithms=[ALGORITHM],
                audience=settings.JWT_AUDIENCE,
                issuer=ISSUER,
                options={"require": ["exp", "sub", "aud", "iss"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise UnauthenticatedError("Token has expired") from exc
        except jwt.InvalidAudienceError as exc:
            raise UnauthenticatedError("Token audience is not accepted here") from exc
        except jwt.InvalidIssuerError as exc:
            raise UnauthenticatedError("Token issuer is not accepted here") from exc
        except jwt.InvalidTokenError as exc:
            # Covers bad signature, malformed tokens and missing required claims.
            raise UnauthenticatedError("Token is not valid") from exc

    @staticmethod
    def _parse_scopes(claims: dict[str, Any]) -> frozenset[str]:
        """Split the ``scope`` claim.

        RFC 8693 §4.2 defines ``scope`` as a **space-delimited string**. Some
        issuers send a list anyway, so accept both rather than silently reading
        zero scopes off a list and refusing a caller who actually had them —
        but never invent scopes for a token that carried none.
        """
        raw = claims.get("scope", "")
        if isinstance(raw, str):
            return frozenset(raw.split())
        if isinstance(raw, list | tuple):
            return frozenset(str(entry) for entry in raw)
        raise UnauthenticatedError("Token 'scope' claim is not a string or list")

    @staticmethod
    def _to_context(claims: dict[str, Any]) -> UserContext:
        tenant_id = claims.get("tenant_id")
        if not tenant_id:
            raise UnauthenticatedError("Token carries no tenant_id")
        # Type, not just presence. tenant_id reaches a cache key and every
        # control-plane lookup; a dict there is an unhashable key and an
        # unauthenticated 500 rather than a clean 401.
        if not isinstance(tenant_id, str):
            raise UnauthenticatedError("Token 'tenant_id' claim must be a string")
        if not isinstance(claims.get("sub"), str):
            raise UnauthenticatedError("Token 'sub' claim must be a string")

        roles = claims.get("roles", [])
        if not isinstance(roles, list):
            raise UnauthenticatedError("Token 'roles' claim must be a list")

        return UserContext(
            tenant_id=tenant_id,
            user_id=claims["sub"],
            roles=tuple(str(role) for role in roles),
            scopes=AuthContextExtractor._parse_scopes(claims),
            token_id=claims.get("jti"),
            # A read-only view: the context is the trustworthy answer to "who is
            # asking", so no downstream stage gets to edit the claims it rests on.
            raw_claims=MappingProxyType(dict(claims)),
        )
