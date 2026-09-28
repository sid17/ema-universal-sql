"""FastAPI dependencies: identity, then the tenant-status gate.

Two chained steps, deliberately kept separate:

1. **Identity** — decode the token into a :class:`UserContext`. Failure is 401.
2. **Tenant status** — refuse a tenant that is not ``active``. Failure is 403
   ``ENTITLEMENT_DENIED``.

Step 2 is the front half of offboarding a tenant: revoking it stops its queries
*before any planning happens*, so no connector is called and no secret is
resolved on behalf of a departed customer. The back half — destroying the
per-tenant encryption key — is covered by ``test_crypto_shred``.

Note this is a **coarse** gate on the tenant, not row-level entitlement. RLS and
CLS are compiled into the query plan; nothing here post-filters anything, which
is the invariant the whole design rests on.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import Depends, Header, Request

from src.control_plane.repository import ControlPlaneRepository
from src.gateway.auth import AuthContextExtractor
from src.models.context import UserContext
from src.models.errors import ApiError, ErrorCode

logger = logging.getLogger(__name__)

#: The coarse scope every query caller needs. Deliberately a single scope:
#: Deliberately one scope: inventing a permission taxonomy before there are
#: consumers for it would be designing for a caller that does not exist.
QUERY_EXECUTE_SCOPE = "query:execute"

_extractor = AuthContextExtractor()


def get_repository(request: Request) -> ControlPlaneRepository:
    """The control-plane repository built once in the app lifespan."""
    repository = getattr(request.app.state, "repository", None)
    if repository is None:
        # A missing repository is a wiring bug, not something to paper
        # over by skipping the tenant gate — that would fail open.
        raise RuntimeError("control-plane repository is not configured on app.state")
    return repository


def enforce_tenant_status(context: UserContext, repository: ControlPlaneRepository) -> UserContext:
    """Refuse a tenant that is unknown, suspended or offboarding.

    An unknown tenant is refused for the same reason as a suspended one: a token
    can name any tenant, so accepting one the control plane has never heard of
    would let a validly-signed token address a tenant that does not exist.
    """
    tenant = repository.get_tenant(context.tenant_id)

    if tenant is None or not tenant.is_active:
        # ONE message for both branches, on purpose. Distinguishing "unknown"
        # from "offboarding" hands any caller a tenant-enumeration oracle and a
        # status oracle — and since the mock IdP mints a token for any tenant
        # string, "any caller" means anyone who can reach the port. The
        # discriminating detail goes to the log, not the response body.
        logger.warning(
            "tenant gate refused request",
            extra={
                "context": {
                    "tenant_id": context.tenant_id,
                    "reason": "unknown" if tenant is None else tenant.status,
                }
            },
        )
        raise ApiError(
            code=ErrorCode.ENTITLEMENT_DENIED,
            http=403,
            message="Tenant is not permitted to run queries",
            suggested_action="Contact your administrator about this tenant's status.",
        )

    return context


#: The repository, resolved per request from app.state.
Repository = Annotated[ControlPlaneRepository, Depends(get_repository)]


def require_scope(context: UserContext, scope: str) -> UserContext:
    """Coarse OAuth-scope gate (RFC 8693 §4.2 scopes; see ``models/context``).

    This is layer 2 of four, and the layering is the point:

    ===  ====================================  ================================
    L0   is the token real?                    ``auth.py``          -> 401
    L1   is the tenant active?                 ``enforce_tenant_status`` -> 403
    L2   may this caller run queries at all?   **here**             -> 403
    L3   is the connector granted?             gateway              -> 403
    L4   which rows and columns?               **compiled into the plan**
    ===  ====================================  ================================

    L2 reads the token and nothing else. The rule that keeps the design honest:
    *an endpoint check may consult the token and the control plane, never a
    result row.* The moment a decision needs data to make, it belongs in the
    plan — putting row or column filtering here would be the post-fetch
    filtering, which this design bans outright.
    """
    if not context.has_scope(scope):
        raise ApiError(
            code=ErrorCode.ENTITLEMENT_DENIED,
            http=403,
            message=f"Token is missing the required scope: {scope}",
            suggested_action=f"Request a token with the '{scope}' scope.",
        )
    return context


def get_current_user(
    request: Request,
    repository: Repository,
    authorization: Annotated[str | None, Header()] = None,
) -> UserContext:
    """Resolve and authorise the caller, or raise 401/403.

    Order matters: identity, then tenant, then scope. Each step is cheaper than
    the next is useful — and a suspended tenant should be told so regardless of
    which scopes its token happens to carry.
    """
    context = _extractor.extract(authorization)
    context = enforce_tenant_status(context, repository)
    context = require_scope(context, QUERY_EXECUTE_SCOPE)

    # The access-log middleware reads identity from here after the handler runs.
    # Set on the resolved context only, so a rejected request logs null identity
    # rather than claiming a caller we refused.
    request.state.user = context
    return context


#: Route-level alias, so handlers read `user: CurrentUser` rather than repeating
#: the Depends wiring.
CurrentUser = Annotated[UserContext, Depends(get_current_user)]
