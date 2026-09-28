"""The error vocabulary: six domain codes, one exception type.

These six are the shared vocabulary between the prototype and design-doc §8.1.
Two failures are deliberately *not* among them, and both get their own exception
type below rather than a code:

``UnauthenticatedError`` (401)
    A missing, expired or forged token. A transport-level failure.

``InvalidQueryError`` (400)
    SQL outside the supported subset. A request-shape failure.

Diluting the six with either would desync this prototype from the submitted
design doc, which is the one thing the shared vocabulary exists to prevent.
"""

from enum import StrEnum

#: The async reroute a rate-limited caller should take (brief lines 110/158).
ASYNC_REROUTE_ACTION = (
    "Rate limit exhausted for this tenant. Retry after the window resets, or "
    "reroute this query to the async path: POST /v1/query/async"
)


class ErrorCode(StrEnum):
    """The six domain codes. Adding a seventh means changing the design doc."""

    RATE_LIMIT_EXHAUSTED = "RATE_LIMIT_EXHAUSTED"
    STALE_DATA = "STALE_DATA"
    ENTITLEMENT_DENIED = "ENTITLEMENT_DENIED"
    SOURCE_TIMEOUT = "SOURCE_TIMEOUT"
    CONNECTOR_NOT_ENABLED = "CONNECTOR_NOT_ENABLED"
    CONNECTOR_AUTH_ERROR = "CONNECTOR_AUTH_ERROR"


class ApiError(Exception):
    """A domain failure, carrying everything the HTTP layer needs to render it.

    A ``RATE_LIMIT_EXHAUSTED`` raised without a ``suggested_action`` gets the
    async-reroute pointer filled in here, so the friendly error names the way
    out no matter which module raised it.
    """

    def __init__(
        self,
        code: ErrorCode,
        http: int,
        message: str,
        retry_after_ms: int | None = None,
        suggested_action: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.http = http
        self.message = message
        self.retry_after_ms = retry_after_ms
        if suggested_action is None and code is ErrorCode.RATE_LIMIT_EXHAUSTED:
            suggested_action = ASYNC_REROUTE_ACTION
        self.suggested_action = suggested_action


class UnauthenticatedError(Exception):
    """401. Not an ``ErrorCode`` — see the module docstring."""

    error_code = "UNAUTHENTICATED"
    http = 401

    def __init__(self, message: str = "Authentication required") -> None:
        super().__init__(message)
        self.message = message


class InvalidQueryError(Exception):
    """400. Also **not** an ``ErrorCode``, for the same reason as the 401 above.

    The SQL is outside the supported subset: a parse failure, a banned construct
    (``SELECT *``, a subquery, an aggregate, a join we do not support), or a
    column the connector schema does not declare.

    **Why not one of the six** (ADR-028). The six are the vocabulary this
    prototype shares verbatim with design-doc §8.1, and every one of them
    describes entitlement, throttling, freshness or connector state. None
    describes *"your query is not in the supported subset"*. The obvious
    shortcut — reusing ``ENTITLEMENT_DENIED`` — would be actively wrong: it tells
    a caller to go request access for what is a typo, and it makes a graded
    security code mean two unrelated things. Adding a seventh code would mean
    changing the submitted design doc. So this follows the precedent
    :class:`UnauthenticatedError` already set: a request-shape failure lives
    outside the domain vocabulary and is documented as such.

    ``detail`` names the offending construct where one can be identified, because
    "unsupported SQL" with no pointer is the least actionable error a query API
    can return.
    """

    error_code = "INVALID_QUERY"
    http = 400

    def __init__(self, message: str, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail
