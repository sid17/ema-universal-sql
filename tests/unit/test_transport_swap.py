"""The swap proof: replace the transport with real HTTP, get the same rows.

This is the claim the whole phase exists for. Everything else can be read and
believed; this is the part that is *demonstrated* — and it runs on the commit
hook, because it needs no container.

**How it is built.** One ordinary mock adapter plays the SOURCE: it owns the
dataset and answers whatever request arrives, which is what a real API does. A
Starlette app in front of it turns that into genuine HTTP. A second adapter —
identical in every respect except :meth:`_transport` — plays the CLIENT and
reaches it through ``httpx``. Both are asked the same question and must return
the same rows, the same cursor and the same ``has_more``.

**Why this is not circular.** The two adapters share their request builder,
their capability model and their parser — deliberately, because those are *not*
what a live adapter replaces. What differs is everything in between: URL
encoding, header casing, JSON serialisation, status codes. That is exactly the
set of things a swap breaks, and one of them (httpx lowercasing ``Link``) was a
real bug this test found.

``httpx`` is a dev dependency and ``03-BUILD-PROCESS.md`` forbids a later phase
editing ``pyproject.toml``, so the live transport lives here rather than in
``src/``. That is a constraint, and it is also the right place: ``src`` declares
the seam, and this is the worked example a live adapter copies. It is nine
lines.
"""

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from src.connectors.base import FetchRequest
from src.connectors.github import GitHubConnectorAdapter
from src.connectors.jira import JiraConnectorAdapter
from src.connectors.request import OutboundRequest
from src.connectors.response import SourceResponse
from tests.unit.catalog_fixture import (
    GITHUB_CAPABILITIES,
    GITHUB_ENDPOINT,
    GITHUB_RATE_LIMIT,
    JIRA_CAPABILITIES,
    JIRA_ENDPOINT,
    JIRA_RATE_LIMIT,
)
from tests.unit.test_connectors import ACME, SCOPE

#: Headers the source sets that a real HTTP layer owns and would set itself.
_TRANSPORT_OWNED = {"content-type", "content-length"}


class HttpTransportMixin:
    """A live transport. **This is the entire diff between mock and live.**

    Everything above it (building the request from the capability model) and
    below it (parsing the source's body into rows) is unchanged and shared.
    """

    client: httpx.AsyncClient

    async def _transport(self, outbound, tenant_id, policy=None, decision=None):
        response = await self.client.request(
            outbound.method, outbound.url, headers=dict(outbound.headers)
        )
        return SourceResponse(
            status=response.status_code,
            headers=dict(response.headers),
            body=None if response.status_code == 304 else response.json(),
        )


class HttpGitHubAdapter(HttpTransportMixin, GitHubConnectorAdapter):
    pass


class HttpJiraAdapter(HttpTransportMixin, JiraConnectorAdapter):
    pass


def replay_app(source, received: list[dict[str, str]]) -> Starlette:
    """An HTTP front door for an adapter acting as the source.

    The handler hands the arriving request straight to the source's own
    transport, so what goes over the wire is the source's real body — not a
    fixture written to match it, which could drift. Everything it receives is
    recorded, so a test can assert what actually crossed the wire rather than
    what we believe was put on it.
    """

    async def handle(request):
        received.append(dict(request.headers) | {"__target__": str(request.url)})
        outbound = OutboundRequest(
            method=request.method,
            host=request.url.hostname or "",
            path=request.url.path,
            query=dict(request.query_params),
            headers=dict(request.headers),
        )
        answer = await source._transport(outbound, ACME)
        headers = {k: v for k, v in answer.headers.items() if k.lower() not in _TRANSPORT_OWNED}
        if answer.is_not_modified:
            return Response(status_code=304, headers=headers)
        return JSONResponse(answer.body, headers=headers)

    return Starlette(routes=[Route("/{path:path}", handle, methods=["GET"])])


def pair(adapter_class, http_class, resource, capabilities, endpoint, rate_limit, **wiring):
    """One source adapter and one client adapter, identical but for transport."""
    common = dict(
        resource=resource,
        capabilities=capabilities,
        endpoint=endpoint,
        rate_limit=rate_limit,
        **wiring,
    )
    source = adapter_class(**common)
    client = http_class(**common)
    received: list[dict[str, str]] = []
    client.client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=replay_app(source, received)),
        base_url=f"https://{endpoint.host}",
    )
    return source, client, received


@pytest.fixture
def github_pair(cache, limiter, secrets, control_plane, fake_clock):
    return pair(
        GitHubConnectorAdapter,
        HttpGitHubAdapter,
        "pull_requests",
        GITHUB_CAPABILITIES,
        GITHUB_ENDPOINT,
        GITHUB_RATE_LIMIT,
        cache=cache,
        limiter=limiter,
        secrets=secrets,
        control_plane=control_plane,
        now_ms=fake_clock,
    )


@pytest.fixture
def jira_pair(cache, limiter, secrets, control_plane, fake_clock):
    return pair(
        JiraConnectorAdapter,
        HttpJiraAdapter,
        "issues",
        JIRA_CAPABILITIES,
        JIRA_ENDPOINT,
        JIRA_RATE_LIMIT,
        cache=cache,
        limiter=limiter,
        secrets=secrets,
        control_plane=control_plane,
        now_ms=fake_clock,
    )


def gh(**overrides) -> FetchRequest:
    fields = {
        "tenant_id": ACME,
        "entitlement_scope": SCOPE,
        "predicates": {"repo": "ema/core", "state": "open"},
        "limit": 3,
        "max_staleness_ms": 0,
    }
    return FetchRequest(**(fields | overrides))


def jira(**overrides) -> FetchRequest:
    fields = {
        "tenant_id": ACME,
        "entitlement_scope": SCOPE,
        "predicates": {"status": "In Progress"},
        "limit": 3,
        "max_staleness_ms": 0,
    }
    return FetchRequest(**(fields | overrides))


async def assert_same(source, client, request) -> None:
    """Both transports, same question, same answer.

    Compared on rows, cursor AND has_more: rows alone would let a pagination
    bug through, and a cursor alone would let a field-mapping bug through.
    """
    through_memory = await source.fetch(request)
    through_http = await client.fetch(request)

    assert through_http.rows == through_memory.rows
    assert through_http.next_cursor == through_memory.next_cursor
    assert through_http.has_more == through_memory.has_more


# --- the proof, page by page ------------------------------------------------


async def test_the_first_page_survives_real_http(github_pair) -> None:
    source, client, _ = github_pair
    await assert_same(source, client, gh())


async def test_a_middle_page_survives_real_http(github_pair) -> None:
    """The cursor has to make the round trip: minted into a `Link` header by the
    source, read back by the client, sent as a query parameter, decoded."""
    source, client, _ = github_pair
    first = await client.fetch(gh())
    assert first.has_more, "this tested nothing — the dataset fits in one page"

    await assert_same(source, client, gh(page=first.next_cursor))


async def test_the_last_page_reports_no_cursor_through_either_transport(github_pair) -> None:
    source, client, _ = github_pair
    await assert_same(source, client, gh(limit=100))

    assert (await client.fetch(gh(limit=100))).next_cursor is None


async def test_an_empty_result_survives_real_http(github_pair) -> None:
    """`[]` is the case a serialization bug most easily turns into `null`."""
    source, client, _ = github_pair
    request = gh(predicates={"repo": "ema/core", "state": "open", "author": "nobody"})

    await assert_same(source, client, request)
    assert (await client.fetch(request)).rows == []


async def test_a_projected_fetch_survives_real_http(github_pair) -> None:
    """Field mapping is lossy in the dangerous direction: a column the caller
    did not ask for must not reappear, and one it did must not vanish."""
    source, client, _ = github_pair
    request = gh(projection=["title", "author", "issue_key"])

    await assert_same(source, client, request)
    assert set((await client.fetch(request)).rows[0]) == {"title", "author", "issue_key"}


async def test_the_conditional_request_still_gets_a_304_over_http(
    github_pair, limiter, fake_clock
) -> None:
    """The 304 path end to end: `If-None-Match` out, an empty 304 back, no token."""
    _, client, _ = github_pair
    await client.fetch(gh(max_staleness_ms=60_000))
    spent = len(limiter.consumed)

    fake_clock.advance(120_000)
    revalidated = await client.fetch(gh(max_staleness_ms=60_000))

    assert revalidated.served == "cache"
    assert revalidated.revalidated is True
    assert len(limiter.consumed) == spent, "a 304 must not spend a token"


# --- the other source, whose request is harder to get across the wire -------


async def test_a_jql_expression_survives_url_encoding(jira_pair) -> None:
    """`status = "In Progress"` is quotes, spaces and an operator in a query
    parameter. If any of it is mangled in transit the source sees a different
    filter — and would answer, wrongly, rather than fail."""
    source, client, _ = jira_pair
    await assert_same(source, client, jira())

    rows = (await client.fetch(jira())).rows
    assert rows and all(row["status"] == "In Progress" for row in rows)


async def test_an_offset_page_survives_real_http(jira_pair) -> None:
    source, client, _ = jira_pair
    first = await client.fetch(jira(limit=2))

    await assert_same(source, client, jira(limit=2, page=first.next_cursor))


# --- and the request really did leave the process ---------------------------


async def test_the_request_really_left_the_process(github_pair) -> None:
    """Otherwise this whole file could be passing on an in-memory shortcut.

    Asserted on what the SERVER received, not on what the client built: the two
    agreeing is the only evidence that anything was serialised at all.
    """
    _, client, received = github_pair

    await client.fetch(gh())

    assert len(received) == 1, "exactly one HTTP request should have been made"
    arrived = received[0]
    # The per-tenant credential, resolved through SecretsManagerClient, crossed
    # the wire as a real Authorization header.
    assert arrived["authorization"] == client.last_request.headers["Authorization"]
    assert arrived["authorization"].startswith("Bearer ")
    # And the placement survived: `repo` in the path, `state` in the query.
    assert "/repos/ema/core/pulls" in arrived["__target__"]
    assert "state=open" in arrived["__target__"]
    assert arrived["accept"] == "application/vnd.github+json"


async def test_the_conditional_request_carried_if_none_match_on_the_wire(
    github_pair, fake_clock
) -> None:
    """The header the 304 depends on, asserted where it matters: at the source."""
    _, client, received = github_pair
    await client.fetch(gh(max_staleness_ms=60_000))
    fake_clock.advance(120_000)

    await client.fetch(gh(max_staleness_ms=60_000))

    assert "if-none-match" not in received[0], "the first call has nothing to revalidate"
    assert received[1]["if-none-match"].startswith('W/"')
