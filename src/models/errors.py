"""The error vocabulary: six domain codes, one exception type.

These six are the shared vocabulary between the prototype and design-doc §8.1.
A transport-level failure — a missing, expired or forged token — is **401
UNAUTHENTICATED** and is deliberately *not* one of them: diluting the six with
transport failures desyncs the two deliverables. It gets its own exception
below instead.
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
