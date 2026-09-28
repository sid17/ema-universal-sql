"""Step 0 of a fetch: reject anything the capability model does not declare.

Extracted from :mod:`src.connectors.mock_adapter`. **Validation moved, not the
six-step orchestration**: the ordering comment on ``fetch()`` is that file's most valuable
documentation, and splitting the comment from the code it describes would cost
more than the lines saved.

This runs *before* any Redis call, because an invalid request has no meaningful
cache key and should not cost a rate-limit token.
"""

from collections.abc import Callable
from typing import Any

from src.connectors.base import CapabilityModel, FetchRequest
from src.models.errors import ApiError, ErrorCode

#: Operators this build can evaluate, and the authoritative list of what a
#: capability model may legally declare. Lives here rather than beside the
#: in-memory filter because validation is what makes it a *contract*: an
#: operator absent from this table is rejected at step 0 rather than raising a
#: ``KeyError`` deep in the transport.
COMPARATORS: dict[str, Callable[[Any, Any], bool]] = {
    "=": lambda row, value: row == value,
    ">": lambda row, value: row > value,
    ">=": lambda row, value: row >= value,
    "<": lambda row, value: row < value,
    "<=": lambda row, value: row <= value,
}


def as_op_value(condition: Any) -> tuple[str, Any]:
    """Accept ``{"col": value}`` as shorthand for ``{"col": ("=", value)}``."""
    if isinstance(condition, tuple | list) and len(condition) == 2:
        return str(condition[0]), condition[1]
    return "=", condition


def validate_request(
    connector_type: str, capabilities: CapabilityModel, request: FetchRequest
) -> None:
    """Raise unless every predicate and projected column is declared.

    **Rejected, not silently ignored**, for predicates *and* projections.

    For a predicate, a silent drop would return rows the caller did not ask for
    — and that predicate may be the RLS filter, which turns a silent drop from a
    bug into a data leak.

    For a projection the failure is quieter and just as bad. The engine fetches
    ``projection ∪ every WHERE/ORDER BY column`` so that re-applying predicates
    authoritatively cannot drop a valid row. If a column in that union were
    silently omitted here, the engine would re-filter on data it never fetched
    and discard rows the caller was entitled to — wrong results, invisible at
    this boundary.
    """
    _reject_unknown_columns(connector_type, capabilities, request)
    _reject_undeclared_predicates(connector_type, capabilities, request)
    _reject_missing_required(connector_type, capabilities, request)


def _reject_unknown_columns(
    connector_type: str, capabilities: CapabilityModel, request: FetchRequest
) -> None:
    unknown = [c for c in request.projection if c not in capabilities.columns]
    if not unknown:
        return
    raise ApiError(
        code=ErrorCode.ENTITLEMENT_DENIED,
        http=400,
        message=(
            f"{connector_type} has no column(s) {', '.join(sorted(unknown))}; "
            f"available: {', '.join(capabilities.columns)}"
        ),
    )


def _reject_undeclared_predicates(
    connector_type: str, capabilities: CapabilityModel, request: FetchRequest
) -> None:
    for column, condition in request.predicates.items():
        op, _ = as_op_value(condition)
        if op not in COMPARATORS:
            raise ApiError(
                code=ErrorCode.ENTITLEMENT_DENIED,
                http=400,
                message=f"{connector_type}: unknown operator {op!r} on {column!r}",
            )
        if not capabilities.supports(column, op):
            raise ApiError(
                code=ErrorCode.ENTITLEMENT_DENIED,
                http=400,
                message=(
                    f"{connector_type} cannot filter {column!r} with {op!r}; "
                    f"this predicate must not be pushed down"
                ),
            )


def _reject_missing_required(
    connector_type: str, capabilities: CapabilityModel, request: FetchRequest
) -> None:
    missing = [c for c in capabilities.required_columns() if c not in request.predicates]
    if not missing:
        return
    raise ApiError(
        code=ErrorCode.ENTITLEMENT_DENIED,
        http=400,
        message=(
            f"{connector_type} requires a predicate on {', '.join(missing)}; "
            f"the upstream API has no endpoint without it"
        ),
    )
