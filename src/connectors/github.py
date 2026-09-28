"""The mock GitHub connector.

Everything structural — the six-step ``fetch()`` order, predicate validation,
pagination, caching — lives in :class:`~src.connectors.mock_adapter.MockConnectorAdapter`.
This class carries only what is genuinely GitHub-shaped, and after Phase 6 that
is exactly one thing: **the rows a GitHub API call would return**.

Everything else arrives as a seeded row from ``config/connectors/github.yaml``:
the host, the path template, the auth scheme, the API headers, the rate-limit
dialect, and which columns can be filtered with which operators. So a second
GitHub endpoint is another entry under that file's ``resources:`` map plus one
entry in :attr:`DATASETS` — and **no new class**.

GitHub's shape, for reference — the details the YAML encodes:

- ``repo`` is **required** and injected into the **path**. There is no
  ``/pulls`` endpoint that spans every repository, so a fetch without it has no
  URL to call at all. This is the capability model's ``require: required``
  earning its keep rather than being decoration.
- ``state`` and ``author`` are optional query parameters.
- Pagination is **cursor**-style (what a real GitHub client reads out of the
  ``Link`` header).
- Its primary rate limit refuses with **403** and ``X-RateLimit-Remaining: 0``,
  not with a 429 — see the ``rate_limit`` block in the YAML.
"""

import re
from typing import Any
from urllib.parse import parse_qs, urlparse

from src.connectors.mock_adapter import MockConnectorAdapter
from src.connectors.mock_data import github_rows
from src.connectors.pagination import Page, encode_token
from src.connectors.request import OutboundRequest
from src.connectors.response import SourceResponse

#: A PR carries no `issue_key` field on the real API — an integration derives it
#: from the branch name, which is what Atlassian's own GitHub app does. Rendering
#: it into `head.ref` and parsing it back out is therefore not decoration: it is
#: the one genuinely *derived* column in this build, and the round-trip test is
#: what keeps the derivation honest.
ISSUE_KEY_IN_BRANCH = re.compile(r"^([A-Z][A-Z0-9]*-\d+)")

#: Our column name -> where it lives in a real pull-request object.
FIELD_PATHS: dict[str, tuple[str, ...]] = {
    "number": ("number",),
    "title": ("title",),
    "state": ("state",),
    "created_at": ("created_at",),
    "updated_at": ("updated_at",),
    "author": ("user", "login"),
    "repo": ("base", "repo", "full_name"),
}


class GitHubConnectorAdapter(MockConnectorAdapter):
    connector_type = "github"

    #: One entry per API call this build can answer. The keys must match the
    #: ``resources:`` map in ``config/connectors/github.yaml`` — a seeded
    #: resource with no dataset here is refused at construction rather than
    #: returning an empty page that looks like a query that matched nothing.
    DATASETS = {"pull_requests": github_rows}

    #: The faster of the two sources, so the waterfall has a contrast to show
    #: rather than two equal bars (ADR-038).
    simulated_latency_ms = 40.0

    # -- the wire shape ---------------------------------------------------

    def render_page(
        self, rows: list[dict[str, Any]], offset: int, limit: int, outbound: OutboundRequest
    ) -> SourceResponse:
        """A bare JSON array, an ``ETag``, and a ``Link`` header for the next page.

        GitHub returns the collection itself rather than an envelope — no
        ``total``, no ``startAt``. "Is there more?" is answerable only from the
        ``Link`` header, which is exactly why pagination is strategy ⊕ placement
        rather than one shape imposed on both sources.
        """
        window = rows[offset : offset + limit]
        body = [self._as_api_object(row) for row in window]
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "ETag": self._etag(body),
        }
        consumed = offset + len(window)
        if consumed < len(rows):
            token = encode_token(self.capabilities().pagination.strategy, consumed)
            # The URL that was actually called, not the template: a client
            # follows this link verbatim, and `{repo}` is not an address.
            headers["Link"] = f'<https://{outbound.host}{outbound.path}?cursor={token}>; rel="next"'

        return SourceResponse(status=200, headers=headers, body=body)

    def parse_response(self, response: SourceResponse) -> Page:
        next_cursor = self._cursor_from_link(response.header("Link"))
        return Page(
            rows=[self._as_row(item) for item in (response.body or [])],
            next_cursor=next_cursor,
            has_more=next_cursor is not None,
        )

    @staticmethod
    def _as_api_object(row: dict[str, Any]) -> dict[str, Any]:
        """One row in the shape the REST API would return it."""
        obj: dict[str, Any] = {}
        for column, path in FIELD_PATHS.items():
            if column not in row:
                continue
            target = obj
            for key in path[:-1]:
                target = target.setdefault(key, {})
            target[path[-1]] = row[column]
        if "issue_key" in row:
            obj.setdefault("head", {})["ref"] = f"{row['issue_key']}-branch"
        return obj

    @staticmethod
    def _as_row(obj: dict[str, Any]) -> dict[str, Any]:
        """The inverse: a pull-request object back into our columns.

        Missing keys are skipped rather than defaulted, so a projected fetch
        round-trips to exactly the columns it asked for — a ``None`` filled in
        here would be indistinguishable from a value the source actually sent.
        """
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
        branch = obj.get("head", {}).get("ref")
        match = ISSUE_KEY_IN_BRANCH.match(branch) if branch else None
        if match:
            row["issue_key"] = match.group(1)
        return row

    @staticmethod
    def _cursor_from_link(link: str | None) -> str | None:
        """The ``rel="next"`` cursor, or ``None`` when this is the last page."""
        if not link or 'rel="next"' not in link:
            return None
        url = link[link.index("<") + 1 : link.index(">")]
        found = parse_qs(urlparse(url).query).get("cursor")
        return found[0] if found else None
