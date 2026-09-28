"""The mock Jira connector.

The structural half lives in
:class:`~src.connectors.mock_adapter.MockConnectorAdapter`; this class carries
only the rows a Jira API call would return. Its endpoint, auth scheme,
capabilities and rate-limit dialect are seeded rows built from
``config/connectors/jira.yaml``.

Jira's shape, and why it is the *interesting* one:

- Every filter is **optional** — unlike GitHub's required ``repo`` — so this
  adapter proves the contract handles both requirements, not just one.
- ``updated`` supports **range operators** (``>``, ``>=``, ``<``, ``<=``), where
  every other column is equality only. That asymmetry is what makes
  ``CapabilityModel.supports(column, op)`` a two-argument question instead of a
  membership test, and the JQL expression is where it is visibly *used*.
- Every filter injects into **JQL**, not a query parameter: real Jira search
  composes its filters into one expression.
- Pagination is **offset**-style (``startAt`` / ``total``), so the two adapters
  between them exercise both strategies.
- It refuses with **429** and a ``Retry-After`` where GitHub uses a 403 — the
  same fact in a different vocabulary, which is why the dialect is data.
- This is the source that carries ``assignee`` (the RLS subject) and
  ``reporter_email`` (the CLS-masked column), so the entitlement work lands
  here.
"""

from typing import Any

from src.connectors.mock_adapter import MockConnectorAdapter
from src.connectors.mock_data import jira_rows
from src.connectors.pagination import Page, encode_token
from src.connectors.request import OutboundRequest
from src.connectors.response import SourceResponse

#: Our column name -> where it lives inside a real issue object. Everything but
#: ``key`` hangs off ``fields``, and each value is an object rather than a
#: scalar — ``status`` is ``{"name": ...}``, not a string. This nesting is the
#: reason ``parse_response`` has work to do at all.
FIELD_PATHS: dict[str, tuple[str, ...]] = {
    "key": ("key",),
    "status": ("fields", "status", "name"),
    "assignee": ("fields", "assignee", "name"),
    "reporter_email": ("fields", "reporter", "emailAddress"),
    "project": ("fields", "project", "key"),
    "updated": ("fields", "updated"),
}


class JiraConnectorAdapter(MockConnectorAdapter):
    connector_type = "jira"

    DATASETS = {"issues": jira_rows}

    #: Deliberately the slow one. Jira carries the RLS subject (``assignee``)
    #: and the CLS-masked column (``reporter_email``), so "the entitled source
    #: is also the expensive one" is the shape a reviewer should read off the
    #: waterfall — and it is what makes "P95 was Jira, not the engine" a
    #: finding rather than a caption.
    simulated_latency_ms = 180.0

    # -- the wire shape ---------------------------------------------------

    def render_page(
        self, rows: list[dict[str, Any]], offset: int, limit: int, outbound: OutboundRequest
    ) -> SourceResponse:
        """An envelope with ``startAt`` / ``maxResults`` / ``total``.

        The opposite of GitHub's bare array: "is there more?" is arithmetic on
        ``total`` rather than a header, so the caller never has to follow a link
        to find out.
        """
        window = rows[offset : offset + limit]
        body = {
            "startAt": offset,
            "maxResults": limit,
            "total": len(rows),
            "issues": [self._as_api_object(row) for row in window],
        }
        return SourceResponse(
            status=200,
            headers={"Content-Type": "application/json", "ETag": self._etag(body)},
            body=body,
        )

    def parse_response(self, response: SourceResponse) -> Page:
        body = response.body or {}
        issues = body.get("issues", [])
        consumed = int(body.get("startAt", 0)) + len(issues)
        has_more = consumed < int(body.get("total", consumed))
        return Page(
            rows=[self._as_row(issue) for issue in issues],
            next_cursor=(
                encode_token(self.capabilities().pagination.strategy, consumed)
                if has_more
                else None
            ),
            has_more=has_more,
        )

    @staticmethod
    def _as_api_object(row: dict[str, Any]) -> dict[str, Any]:
        obj: dict[str, Any] = {}
        for column, path in FIELD_PATHS.items():
            if column not in row:
                continue
            target = obj
            for key in path[:-1]:
                target = target.setdefault(key, {})
            target[path[-1]] = row[column]
        return obj

    @staticmethod
    def _as_row(obj: dict[str, Any]) -> dict[str, Any]:
        """The inverse. Missing keys are skipped, never defaulted — a ``None``
        filled in here would be indistinguishable from an unassigned issue."""
        row: dict[str, Any] = {}
        for column, path in FIELD_PATHS.items():
            value: Any = obj
            for key in path:
                if not isinstance(value, dict) or key not in value:
                    value = None
                    break
                value = value[key]
            if value is not None:
                row[column] = value
        return row
