"""Connector failures, normalized into a vocabulary Phase 2 can act on.

**What this module is, and what it deliberately is not** (ADR-022).

Research Card 2 (``airbyte-python-cdk``) contributes a match→action table
instead of ad-hoc ``try/except``: an action enum, a ``failure_type``, and a
mapping from a source's native failure to both. That *vocabulary* is built here,
because Phase 2 cannot assemble an honest envelope without it — it must tell a
**timeout** (degrade to ``partial: true``, ``join_status: incomplete``) from an
**auth error** (fail the query) from a **throttle** (``429`` with
``Retry-After``). That three-way split is exactly ``failure_type``.

The *machinery* around it — bounded exponential backoff, ``Retry-After``-driven
waits, a per-connector circuit breaker — is **not** built. These adapters make
no HTTP call, so a table keyed by HTTP status would map statuses that never
arrive, and a breaker would guard a function that cannot fail transiently. That
is a speculative abstraction (LAW 5); design-doc §5 describes the production
design and the README says plainly that it is described, not running.

Note the consequence for the mapping's key: it is keyed by :class:`FailureMode`
— what the *mock* can be told to do — not by status code. A status-keyed table
would be fiction dressed as fidelity.
"""

from dataclasses import dataclass
from enum import StrEnum

from src.models.errors import ErrorCode


class Action(StrEnum):
    """What a caller should *do* about an outcome.

    Kept complete rather than trimmed to the cases a mock can produce, because
    this enum is the shared vocabulary with design-doc §5 — a reader comparing
    the two should find the same names. Only the mapping below is restricted to
    what actually occurs.
    """

    SUCCESS = "SUCCESS"
    RETRY = "RETRY"
    RATE_LIMITED = "RATE_LIMITED"
    REFRESH_TOKEN_THEN_RETRY = "REFRESH_TOKEN_THEN_RETRY"
    FAIL = "FAIL"
    IGNORE = "IGNORE"


class FailureType(StrEnum):
    """*Why* it failed — which decides whether the query can survive it."""

    TRANSIENT_ERROR = "transient_error"
    """The source might answer later. The query MAY degrade to a partial result."""

    CONFIG_ERROR = "config_error"
    """Nothing retryable: a missing grant, a bad credential. Fail the query."""

    SYSTEM_ERROR = "system_error"
    """Our bug, not the source's. Fail the query; never present it as partial."""


class FailureMode(StrEnum):
    """The failures a *mock* source can actually produce.

    Driven by the forced-failure hooks the adapters expose, so the demo can
    reach every branch deterministically instead of waiting for a real outage.
    """

    TIMEOUT = "timeout"
    THROTTLED = "throttled"
    AUTH = "auth"
    NOT_ENABLED = "not_enabled"


@dataclass(frozen=True)
class Classification:
    """One normalized outcome: what happened, why, and what the caller does."""

    action: Action
    failure_type: FailureType
    error_code: ErrorCode

    @property
    def degradable(self) -> bool:
        """May a query survive this failure as a **partial** result?

        True only for a transient failure. A config or system error must fail
        the query outright: returning rows from the *other* source and calling
        the result merely ``partial`` would present a permanently broken
        connector as a temporary gap, which is the dishonest degradation
        ``02-DEFINITION-OF-DONE.md`` §4 exists to forbid.
        """
        return self.failure_type is FailureType.TRANSIENT_ERROR


#: The match→action table. Keyed by what a mock can do — see the module docstring.
DEFAULT_ERROR_MAPPING: dict[FailureMode, Classification] = {
    FailureMode.TIMEOUT: Classification(
        action=Action.RETRY,
        failure_type=FailureType.TRANSIENT_ERROR,
        error_code=ErrorCode.SOURCE_TIMEOUT,
    ),
    FailureMode.THROTTLED: Classification(
        action=Action.RATE_LIMITED,
        failure_type=FailureType.TRANSIENT_ERROR,
        error_code=ErrorCode.RATE_LIMIT_EXHAUSTED,
    ),
    FailureMode.AUTH: Classification(
        action=Action.FAIL,
        failure_type=FailureType.CONFIG_ERROR,
        error_code=ErrorCode.CONNECTOR_AUTH_ERROR,
    ),
    FailureMode.NOT_ENABLED: Classification(
        action=Action.FAIL,
        failure_type=FailureType.CONFIG_ERROR,
        error_code=ErrorCode.CONNECTOR_NOT_ENABLED,
    ),
}


def classify(mode: FailureMode) -> Classification:
    """Normalize a source failure. Raises on an unmapped mode — never guesses.

    LAW 4: a default branch returning something plausible would let a new
    failure mode be silently classified as retryable, and a permanently broken
    connector would then be reported to callers as a transient gap.
    """
    try:
        return DEFAULT_ERROR_MAPPING[mode]
    except KeyError:
        raise ValueError(
            f"unmapped connector failure mode {mode!r}; "
            f"add it to DEFAULT_ERROR_MAPPING with an explicit failure_type"
        ) from None


#: HTTP status per error code, from design-doc §8.1 — the published table.
#:
#: A lookup rather than an inline conditional, because the conditional it
#: replaced (``504 if SOURCE_TIMEOUT else 502``) quietly gave
#: ``CONNECTOR_AUTH_ERROR`` a 502 while every locked document says 403. HLD §9
#: makes the error vocabulary a provenance rail that must be identical across
#: the design doc, the HLD and every phase spec; a rail is much harder to break
#: from a table that states it than from an ``else`` branch (ADR-029).
HTTP_STATUS_FOR_CODE = {
    ErrorCode.SOURCE_TIMEOUT: 504,
    ErrorCode.RATE_LIMIT_EXHAUSTED: 429,
    ErrorCode.CONNECTOR_AUTH_ERROR: 403,
    ErrorCode.CONNECTOR_NOT_ENABLED: 403,
    ErrorCode.ENTITLEMENT_DENIED: 403,
    ErrorCode.STALE_DATA: 200,
}
