"""Reading a built request the way the source would read it.

The exact inverse of :mod:`src.connectors.request`, and the reason the mock is a
connector rather than a shortcut: **the mock transport answers the request it was
handed, not the ``FetchRequest`` that produced it.**

That is the whole load-bearing property. If the transport filtered on
``FetchRequest.predicates`` it would agree with the caller by construction, and
``inject_into`` would still be decoration — swap ``path`` and ``query`` in the
YAML and every row would come back identical. Going through the wire form means
a placement bug produces *wrong rows*, and a test catches it.

It also means the JQL this build renders has to be JQL this build can parse,
which is the cheapest possible check that the expression is well formed rather
than merely plausible.
"""

import re
from collections.abc import Mapping
from typing import Any

from src.connectors.base import CapabilityModel
from src.connectors.pagination import OffsetStrategy, decode_token
from src.connectors.request import EndpointSpec, OutboundRequest

#: One JQL term: ``assignee = "alice"``, ``updated >= "2026-09-01"``. The value
#: is double-quoted with backslash escapes, matching what ``_escape_jql`` writes.
_JQL_TERM = re.compile(r'(\w+)\s*(>=|<=|!=|>|<|=)\s*"((?:[^"\\]|\\.)*)"')


def received_predicates(
    spec: EndpointSpec, capabilities: CapabilityModel, outbound: OutboundRequest
) -> dict[str, tuple[str, Any]]:
    """What this source understands the request to be asking for.

    Keyed by *our* column names, because that is what the capability model maps
    between: the wire field is the source's spelling, the column is ours.
    """
    path_values = _path_values(spec.path_template, outbound.path)
    expression = _jql_terms(outbound.query.get(spec.expression_param or "", ""))
    buckets: dict[str, Mapping[str, Any]] = {
        "path": path_values,
        "query": outbound.query,
        "header": outbound.headers,
        "body": outbound.body or {},
    }

    received: dict[str, tuple[str, Any]] = {}
    for column, capability in capabilities.key_columns.items():
        option = capability.option
        if option.inject_into == "jql":
            if option.field in expression:
                received[column] = expression[option.field]
            continue
        bucket = buckets[option.inject_into]
        if option.field in bucket:
            received[column] = ("=", bucket[option.field])
    return received


def received_window(capabilities: CapabilityModel, outbound: OutboundRequest) -> tuple[int, int]:
    """``(offset, limit)`` — the slice of its own result set the source was asked for.

    Read back out of the wire parameters rather than off the ``FetchRequest``,
    for the same reason the predicates are: a ``token_option`` pointing at the
    wrong field must produce the wrong page, not a silently correct one.
    """
    spec = capabilities.pagination
    buckets: dict[str, Mapping[str, Any]] = {
        "query": outbound.query,
        "header": outbound.headers,
        "path": {},
    }

    raw_size = buckets[spec.size_option.inject_into].get(spec.size_option.field)
    limit = spec.page_size if raw_size is None else int(raw_size)

    raw_token = buckets[spec.token_option.inject_into].get(spec.token_option.field)
    if raw_token is None:
        return 0, limit
    if spec.strategy == OffsetStrategy.name:
        # An integer the source understands, not our opaque token.
        return int(raw_token), limit
    return decode_token(str(raw_token), spec.strategy), limit


def _path_values(template: str, path: str) -> dict[str, str]:
    """Recover the path parameters from a rendered path.

    Raises rather than returning an empty dict on a mismatch: a path the
    template cannot explain means the request was built against a different
    endpoint, and answering it anyway would serve one resource's rows under
    another's name.
    """
    pattern = re.sub(
        r"\{(\w+)\}", r"(?P<\1>.+)", re.escape(template).replace(r"\{", "{").replace(r"\}", "}")
    )
    match = re.fullmatch(pattern, path)
    if match is None:
        raise ValueError(f"path {path!r} does not match this endpoint's template {template!r}")
    return match.groupdict()


def _jql_terms(expression: str) -> dict[str, tuple[str, str]]:
    """Parse ``status = "In Progress" AND assignee = "alice"`` back into terms.

    Unparsed leftovers are an error. A silently dropped term would be a filter
    the source never applied — and when that filter is the RLS predicate, rows
    the caller was never entitled to.
    """
    if not expression:
        return {}

    terms: dict[str, tuple[str, str]] = {}
    consumed = 0
    for match in _JQL_TERM.finditer(expression):
        field, op, value = match.groups()
        terms[field] = (op, value.replace('\\"', '"').replace("\\\\", "\\"))
        consumed += len(match.group(0))

    remainder = re.sub(r"\s*AND\s*", "", _JQL_TERM.sub("", expression)).strip()
    if remainder:
        raise ValueError(f"could not parse JQL expression {expression!r}; left over: {remainder!r}")
    return terms
