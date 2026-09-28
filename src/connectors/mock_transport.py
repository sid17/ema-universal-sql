"""How the mock plays the source: build a call, answer it, parse it back.

Split from :mod:`src.connectors.mock_adapter` along a real seam rather than a
convenient cut. ``mock_adapter`` owns the
**order** a fetch happens in — cache, token, secret, call, record — which is a
governance concern. This owns **what a call looks like**, which is a wire
concern, and it is the half a live adapter replaces.

The three steps, and only the middle one is a mock:

1. ``_outbound`` — ``FetchRequest`` + capability model -> the call to make.
   Shared, generic, and identical for a live adapter.
2. ``_transport`` — send it. **The only method a live adapter overrides.**
3. ``parse_response`` — the source's body and headers -> our rows. Per source,
   because GitHub returns a bare array with a ``Link`` header and Jira returns
   ``{startAt, total, issues}`` with everything under ``fields``.
"""

from dataclasses import replace
from typing import Any

from src.connectors.base import CapabilityModel, FetchRequest
from src.connectors.errors import HTTP_STATUS_FOR_CODE
from src.connectors.inbound import received_predicates, received_window
from src.connectors.pagination import Page
from src.connectors.request import EndpointSpec, OutboundRequest, build_request
from src.connectors.response import RateLimitDialect, SourceResponse
from src.connectors.validate import COMPARATORS, as_op_value
from src.governance.ratelimit import RateLimitDecision, RateLimitPolicy
from src.models.errors import ApiError, ErrorCode


class MockTransport:
    """The wire half of a mock adapter. Mixed into ``MockConnectorAdapter``.

    Declares the attributes it borrows from its host rather than constructing
    them, so the two halves cannot disagree about which endpoint is in play.
    """

    endpoint: EndpointSpec
    rate_limit: RateLimitDialect
    _capabilities: CapabilityModel

    #: The last call made and the last answer received. Set on every fetch and
    #: read by `src/connectorlab` — which is how that tool can *print* the real
    #: request and response instead of constructing its own. A lab that built
    #: its own URL would be a brochure, and nothing from outside could tell.
    last_request: OutboundRequest | None = None
    last_response: SourceResponse | None = None

    def capabilities(self) -> CapabilityModel:  # pragma: no cover - host provides
        raise NotImplementedError

    def dataset_for(self, tenant_id: str) -> list[dict[str, Any]]:  # pragma: no cover
        raise NotImplementedError

    def _now_ms(self) -> float:  # pragma: no cover - host provides
        raise NotImplementedError

    def _outbound(
        self,
        request: FetchRequest,
        credential: str | None = None,
        if_none_match: str | None = None,
    ) -> OutboundRequest:
        """The call this fetch would make, built from the capability model."""
        outbound = build_request(self.endpoint, self._capabilities, request, credential)
        if if_none_match is None:
            return outbound
        return replace(outbound, headers={**outbound.headers, "If-None-Match": if_none_match})

    async def _transport(
        self,
        outbound: OutboundRequest,
        tenant_id: str,
        policy: RateLimitPolicy | None = None,
        decision: RateLimitDecision | None = None,
    ) -> SourceResponse:
        """Send the request. **The one method a live adapter replaces.**

        A live implementation is `await client.request(outbound.method,
        outbound.url, headers=outbound.headers)` and nothing else — everything
        above and below this line is already source-agnostic. The worked example
        is `tests/unit/test_transport_swap.py`, which drives this same adapter
        over real HTTP and asserts identical rows.

        This one answers out of memory instead, and it answers **the request it
        was handed** rather than the `FetchRequest` that produced it. That is
        the load-bearing part: reading the predicates back off the wire means a
        wrong `inject_into` returns wrong rows rather than nothing at all.

        `tenant_id` is passed explicitly rather than recovered from the
        credential in `outbound.headers`. A live source scopes the response by
        that credential; parsing it back out here would be a fragile pun on the
        seeder's token format.
        """
        rows = self._apply_predicates(
            self.dataset_for(tenant_id),
            received_predicates(self.endpoint, self._capabilities, outbound),
        )
        offset, limit = received_window(self._capabilities, outbound)
        response = self.render_page(rows, offset=offset, limit=limit, outbound=outbound)

        if outbound.header("If-None-Match") == response.header("ETag"):
            return SourceResponse(status=304, headers=response.headers)
        if policy is None or decision is None:
            return response
        return replace(
            response,
            headers={
                **response.headers,
                **self.rate_limit.headers(policy, decision, self._now_ms()),
            },
        )

    def render_page(
        self, rows: list[dict[str, Any]], offset: int, limit: int, outbound: OutboundRequest
    ) -> SourceResponse:
        """One page, in this source's own body and header shape. Per adapter.

        Takes ``outbound`` because a source that paginates by *link* has to name
        the URL it was actually called on — a template with ``{repo}`` still in
        it is not a URL a client could follow.
        """
        raise NotImplementedError

    def parse_response(self, response: SourceResponse) -> Page:
        """This source's body and headers, back into our rows. Per adapter."""
        raise NotImplementedError

    def _exhausted(
        self, tenant_id: str, policy: RateLimitPolicy, decision: RateLimitDecision
    ) -> ApiError:
        """The source's refusal, normalised into this system's one vocabulary.

        GitHub answers 403 with the remaining count at zero; Jira answers 429
        with a `Retry-After`. Both become `RATE_LIMIT_EXHAUSTED`, which is the
        connector's job: absorb the source's dialect so nothing downstream has
        to know it.
        """
        self.last_response = self.rate_limit.exhausted(policy, decision, self._now_ms())
        return ApiError(
            code=ErrorCode.RATE_LIMIT_EXHAUSTED,
            http=HTTP_STATUS_FOR_CODE[ErrorCode.RATE_LIMIT_EXHAUSTED],
            message=(
                f"Rate limit exhausted for tenant {tenant_id!r} on connector "
                f"{self.connector_type!r} ({policy.max_requests} requests per "
                f"{policy.window_sec}s, burst {policy.burst}). The source would "
                f"answer {self.last_response.status}."
            ),
            retry_after_ms=decision.retry_after_ms,
        )

    @staticmethod
    def _apply_predicates(
        rows: list[dict[str, Any]], predicates: dict[str, Any] | Any
    ) -> list[dict[str, Any]]:
        """Evaluate the predicates the source was asked for, in memory.

        Fed from `received_predicates`, i.e. from the **built request**, not
        from the `FetchRequest` — see `_transport`.
        """
        for column, condition in predicates.items():
            op, value = as_op_value(condition)
            compare = COMPARATORS[op]
            rows = [row for row in rows if column in row and compare(row[column], value)]
        return rows
