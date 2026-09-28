"""``FetchRequest`` → the call a real source would receive.

This is where :class:`~src.connectors.base.RequestOption` stops being
documentation. Before this module the placement metadata was carried from the
YAML through the planner and then dropped: the mock filtered rows with a dict
comprehension, so ``path`` and ``query`` were interchangeable and no test could
tell. Here it is *executed* — and because the mock transport filters using the
request this module builds, a wrong ``inject_into`` now produces wrong rows.

**Generic by construction.** Nothing below knows what GitHub or Jira is. The
placements come from the capability model, the endpoint shape from
:class:`EndpointSpec`, and the page token from the pagination strategy. A third
connector is one YAML file and one :class:`EndpointSpec` — no change here.
"""

import base64
from collections.abc import Mapping
from dataclasses import dataclass, field
from string import Formatter
from typing import Any, Literal
from urllib.parse import urlencode

from src.connectors.base import CapabilityModel, FetchRequest, header_value
from src.connectors.pagination import strategy_for
from src.connectors.validate import as_op_value

#: Header a redacted request shows instead of a credential.
REDACTED = "****"

#: Placements that can only carry equality. A path segment or a query parameter
#: has nowhere to put an operator, so a source declaring ``ops: [">"]`` on one
#: of them is a configuration error — and a silent drop of that ``>`` would
#: return rows the caller did not ask for.
_EQUALITY_ONLY = frozenset({"path", "query", "header", "body"})


@dataclass(frozen=True)
class EndpointSpec:
    """The source-shaped half of a request: where it goes and how it authenticates.

    Lives on the adapter class rather than in the capability YAML for the same
    reason ``resource`` does — it names what the class *is*, where ``capabilities``
    describes what the upstream API *accepts*.
    """

    host: str
    path_template: str
    auth_scheme: Literal["bearer", "basic"]
    method: str = "GET"
    api_headers: Mapping[str, str] = field(default_factory=dict)
    expression_param: str | None = None
    """Query parameter a composed ``jql`` expression is sent in.

    ``None`` for a source with no expression language. A capability declaring
    ``inject_into: jql`` against such an endpoint raises rather than quietly
    dropping the filter.
    """

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "EndpointSpec":
        """Build from the ``connectors.endpoint`` column.

        Read from the control plane, never hard-coded in an adapter — for the
        same reason ``capabilities`` is: an adapter that declared its own
        endpoint could disagree with the catalog about which API call it serves.
        """
        return cls(
            host=raw["host"],
            path_template=raw["path_template"],
            auth_scheme=raw["auth_scheme"],
            method=raw.get("method", "GET"),
            api_headers=dict(raw.get("api_headers", {})),
            expression_param=raw.get("expression_param"),
        )


@dataclass(frozen=True)
class OutboundRequest:
    """One concrete call, in the shape a live adapter would hand to its HTTP client."""

    method: str
    host: str
    path: str
    query: Mapping[str, str]
    headers: Mapping[str, str]
    body: Mapping[str, Any] | None = None

    @property
    def url(self) -> str:
        """The full URL, query string included."""
        suffix = f"?{urlencode(self.query)}" if self.query else ""
        return f"https://{self.host}{self.path}{suffix}"

    def header(self, name: str) -> str | None:
        """One request header, matched case-insensitively. See :func:`header_value`."""
        return header_value(self.headers, name)

    @property
    def target(self) -> str:
        """Path and query only — the request-line form, and what a test asserts on."""
        suffix = f"?{urlencode(self.query)}" if self.query else ""
        return f"{self.path}{suffix}"

    def redacted(self) -> "OutboundRequest":
        """A copy safe to print, with the credential removed but its scheme kept.

        The real value stays on the original: redaction is a property of
        *rendering*, not of the request, so the credential still flows to the
        transport exactly as it would live.
        """
        headers = dict(self.headers)
        authorization = headers.get("Authorization")
        if authorization:
            scheme = authorization.split(" ", 1)[0]
            headers["Authorization"] = f"{scheme} {REDACTED}"
        return OutboundRequest(
            method=self.method,
            host=self.host,
            path=self.path,
            query=dict(self.query),
            headers=headers,
            body=None if self.body is None else dict(self.body),
        )


def compose_endpoint(api: Mapping[str, Any], endpoint: Mapping[str, Any]) -> dict[str, Any]:
    """Flatten one connector's ``api:`` block and one resource's ``endpoint:``.

    The YAML separates them because they have different lifetimes — ``api`` is
    true of every GitHub call, ``endpoint`` of exactly one — and the seeded row
    joins them because an adapter serves exactly one resource and wants one
    object. Authoring source and read model, not duplication.
    """
    return {
        "host": api["host"],
        "auth_scheme": api["auth_scheme"],
        "api_headers": dict(api.get("headers", {})),
        "method": endpoint.get("method", "GET"),
        "path_template": endpoint["path"],
        "expression_param": endpoint.get("expression_param"),
    }


def build_request(
    spec: EndpointSpec,
    capabilities: CapabilityModel,
    request: FetchRequest,
    credential: str | None = None,
) -> OutboundRequest:
    """Compile one fetch into the call a real source would receive."""
    placed = _place_predicates(spec, capabilities, request)
    query = dict(placed.query)
    _place_expression(spec, placed.expression, query)
    _place_pagination(capabilities, request, query, placed.headers, placed.path_values)

    headers = dict(spec.api_headers) | placed.headers
    if credential is not None:
        headers["Authorization"] = _authorization(spec.auth_scheme, credential)

    return OutboundRequest(
        method=spec.method,
        host=spec.host,
        path=_render_path(spec.path_template, placed.path_values),
        query=query,
        headers=headers,
        body=placed.body or None,
    )


@dataclass
class _Placed:
    """Predicates sorted into the four buckets a request has."""

    path_values: dict[str, str] = field(default_factory=dict)
    query: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    body: dict[str, Any] = field(default_factory=dict)
    expression: list[str] = field(default_factory=list)


def _place_predicates(
    spec: EndpointSpec, capabilities: CapabilityModel, request: FetchRequest
) -> _Placed:
    placed = _Placed()
    # Sorted, so the same predicates always render the same call. An
    # insertion-ordered dict would make the artifact churn on planner changes
    # that did not alter a single value.
    for column in sorted(request.predicates):
        op, value = as_op_value(request.predicates[column])
        capability = capabilities.key_columns.get(column)
        if capability is None:
            raise ValueError(
                f"cannot place a predicate on {column!r}: the capability model "
                f"declares no placement for it"
            )
        option = capability.option
        if option.inject_into in _EQUALITY_ONLY and op != "=":
            raise ValueError(
                f"{column!r} is injected into the {option.inject_into} and can only "
                f"carry equality, but the predicate uses {op!r}; a source that cannot "
                f"express this operator must leave the predicate residual"
            )
        _place_one(spec, placed, option.inject_into, option.field, op, value)
    return placed


def _place_one(
    spec: EndpointSpec, placed: _Placed, inject_into: str, field_name: str, op: str, value: Any
) -> None:
    if inject_into == "path":
        placed.path_values[field_name] = str(value)
    elif inject_into == "query":
        placed.query[field_name] = str(value)
    elif inject_into == "header":
        placed.headers[field_name] = str(value)
    elif inject_into == "body":
        placed.body[field_name] = value
    elif inject_into == "jql":
        if spec.expression_param is None:
            raise ValueError(
                f"{field_name!r} declares inject_into=jql, but this endpoint has no "
                f"expression_param to send the expression in"
            )
        placed.expression.append(f'{field_name} {op} "{_escape_jql(value)}"')
    else:  # pragma: no cover - InjectInto is closed; this guards a future variant
        raise ValueError(f"unknown injection target {inject_into!r} for {field_name!r}")


def _place_expression(spec: EndpointSpec, terms: list[str], query: dict[str, str]) -> None:
    if not terms:
        return
    # `spec.expression_param is not None` is guaranteed: _place_one raises when a
    # jql placement meets an endpoint without one, so a non-empty list implies it.
    query[str(spec.expression_param)] = " AND ".join(terms)


def _place_pagination(
    capabilities: CapabilityModel,
    request: FetchRequest,
    query: dict[str, str],
    headers: dict[str, str],
    path_values: dict[str, str],
) -> None:
    spec = capabilities.pagination
    buckets = {"query": query, "header": headers, "path": path_values}

    effective = min(request.limit, spec.page_size)
    buckets[spec.size_option.inject_into][spec.size_option.field] = str(effective)

    wire = strategy_for(spec).wire_value(request.page)
    if wire is not None:
        buckets[spec.token_option.inject_into][spec.token_option.field] = wire


def _render_path(template: str, values: Mapping[str, str]) -> str:
    """Substitute path placeholders, refusing to emit a URL with a hole in it."""
    wanted = {name for _, name, _, _ in Formatter().parse(template) if name}
    missing = sorted(wanted - set(values))
    if missing:
        raise ValueError(
            f"path template {template!r} needs {', '.join(missing)}, which no "
            f"predicate supplied; the capability model should mark it required"
        )
    return template.format(**values)


def _escape_jql(value: Any) -> str:
    """JQL string literals are double-quoted, so an embedded quote must escape."""
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def _authorization(scheme: str, credential: str) -> str:
    """The ``Authorization`` value for this source's scheme.

    ``basic`` base64-encodes the credential **as stored**, because a Jira
    credential *is* the pair ``email:api_token`` — the account is credential
    material, not separate configuration. Encoding only a token would render a
    header no live adapter could send.
    """
    if scheme == "bearer":
        return f"Bearer {credential}"
    if scheme == "basic":
        return f"Basic {base64.b64encode(credential.encode()).decode()}"
    raise ValueError(f"unknown auth scheme {scheme!r}")
