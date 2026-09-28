"""The shared mock ``fetch()`` — and the ordering that makes it safe to share.

``BaseConnectorAdapter`` declares the seam; this class implements it once for
both mocks so GitHub and Jira cannot drift apart on the part that matters. The
two concrete adapters supply only what genuinely differs: their dataset, their
resource name, and their capability model.

**The six steps, in this order. The order is load-bearing.**

0. *(validate)* reject predicates the capability model does not declare — before
   any Redis call, because an invalid request has no meaningful cache key and
   should not cost a token.
1. **check the freshness cache.** A hit returns immediately and spends **no**
   token. A stale-but-present entry gets a conditional revalidation, which on a
   match refreshes ``fetched_at`` and *also* spends no token.
2. **consume a token** via the limiter — the first step that can 429.
3. **resolve the tenant's secret** through ``SecretsManagerClient``.
4. **apply the pushed-down predicates** to the in-memory dataset.
5. **paginate.**
6. **record** ``served`` / ``fetched_at`` / ``etag`` and write the cache.

Cache before token, never token before cache (ADR-024). The bucket models the
*downstream API's* budget, and a cache hit makes no downstream call — charging
for one is not conservative, it is wrong. Spending a token on a hit would also
break three things at once: the "``304`` refreshes ``fetched_at`` without
spending a token" guarantee, the Phase-3 rate-limit banner's determinism, and
the Phase-4 load run, which would drain ``tenant_acme``'s 5-token GitHub bucket
on request 6 instead of serving from cache.
"""

import asyncio
import hashlib
import json
from collections.abc import Callable
from typing import Any

from src.config import get_settings
from src.connectors.base import (
    AdapterResponse,
    BaseConnectorAdapter,
    CapabilityModel,
    FetchRequest,
)
from src.connectors.errors import HTTP_STATUS_FOR_CODE, FailureMode, classify
from src.connectors.mock_transport import MockTransport
from src.connectors.pagination import Page
from src.connectors.request import EndpointSpec
from src.connectors.response import RateLimitDialect
from src.connectors.synthetic import synthetic_rows
from src.connectors.validate import validate_request
from src.governance.cache import CacheStatus, FreshnessCacheManager
from src.governance.clock import NowMs, wall_clock_ms
from src.governance.ratelimit import RateLimitPolicy, TokenBucketRateLimiter
from src.governance.secrets import SecretsManagerClient
from src.models.errors import ApiError, ErrorCode


class MockConnectorAdapter(MockTransport, BaseConnectorAdapter):
    """A deterministic in-memory source wearing the real connector contract."""

    #: Datasets this adapter can serve, keyed by resource. The one thing that
    #: genuinely must be code: the rows a live adapter would receive over the
    #: wire. Everything else about a resource — its endpoint, its filters, its
    #: rate-limit dialect — arrives as a seeded row.
    DATASETS: dict[str, Callable[[], list[dict[str, Any]]]] = {}

    #: What a call to this source costs, in milliseconds, before
    #: ``MOCK_LATENCY_SCALE`` is applied. Overridden per adapter.
    #:
    #: HLD line 39 lists "simulated pagination/latency/429" as what the mocks
    #: provide. Pagination and the 429 were built in Phase 1; this was not, and
    #: Phase 4 is where the omission has consequences: with both sources
    #: answering in ~2ms the trace waterfall's honest reading is *"the
    #: connectors are free and DuckDB is the cost"* — the inverse both of the
    #: intended story and of how any real federated query behaves, where the
    #: remote call dominates by two orders of magnitude (ADR-038).
    #:
    #: Fixed rather than jittered on purpose. Phase 1 chose deterministic
    #: datasets so no test can flake on timing; a random sleep would reintroduce
    #: exactly that through the back door.
    simulated_latency_ms: float = 0.0

    def __init__(
        self,
        resource: str,
        capabilities: dict[str, Any],
        endpoint: EndpointSpec,
        rate_limit: RateLimitDialect,
        cache: FreshnessCacheManager,
        limiter: TokenBucketRateLimiter,
        secrets: SecretsManagerClient,
        control_plane: Any,
        now_ms: NowMs = wall_clock_ms,
        latency_scale: float | None = None,
    ) -> None:
        if resource not in self.DATASETS:
            raise ValueError(
                f"{type(self).__name__} serves no dataset for resource {resource!r}; "
                f"known: {', '.join(sorted(self.DATASETS)) or '(none)'}"
            )
        #: Instance state, not a class attribute: one class serves every resource
        #: its connector declares, so a second GitHub endpoint is a row rather
        #: than a subclass.
        self.resource = resource
        self.endpoint = endpoint
        self.rate_limit = rate_limit
        self._capabilities = CapabilityModel.from_dict(capabilities)
        self._cache = cache
        self._limiter = limiter
        self._secrets = secrets
        self._control_plane = control_plane
        self._now_ms = now_ms
        scale = get_settings().MOCK_LATENCY_SCALE if latency_scale is None else latency_scale
        self._latency_s = (self.simulated_latency_ms * scale) / 1000.0
        #: Set by :meth:`fail_next` to drive a failure path deterministically.
        self._forced_failure: FailureMode | None = None

    # -- contract ---------------------------------------------------------

    def capabilities(self) -> CapabilityModel:
        return self._capabilities

    def health(self) -> dict[str, Any]:
        return {
            "connector": self.connector_type,
            "resource": self.resource,
            "status": "ok",
            "rows": len(self.dataset()),
        }

    def dataset(self) -> list[dict[str, Any]]:
        """The rows this resource would return unfiltered.

        Looked up rather than overridden, so one class can serve several
        resources. ``__init__`` already refused an unknown one, which is why
        this cannot fail here.
        """
        return self.DATASETS[self.resource]()

    def dataset_for(self, tenant_id: str) -> list[dict[str, Any]]:
        """The rows *this tenant* would see.

        Identical to :meth:`dataset` for every tenant except the synthetic load
        tenants, which get their own generated rows so that (a) a cross-tenant
        leak is detectable in a response and (b) the load generator has a key
        space to draw from. See :mod:`src.connectors.synthetic`.

        Off unless ``SYNTHETIC_ROWS`` is set, so a normal run — and the whole
        test suite — reads the committed fixtures exactly as before.
        """
        settings = get_settings()
        if settings.SYNTHETIC_ROWS and tenant_id.startswith(settings.LOAD_TENANT_PREFIX):
            return synthetic_rows(
                tenant_id,
                self.connector_type,
                settings.SYNTHETIC_ROWS,
                settings.SYNTHETIC_KEYSPACE,
            )
        return self.dataset()

    # -- test / demo hooks ------------------------------------------------

    def fail_next(self, mode: FailureMode | None) -> None:
        """Force the next fetch down a failure path.

        The mock's stand-in for an outage: it lets ``make demo`` and the
        integration tests reach the timeout and auth branches deterministically
        instead of waiting for a real one.
        """
        self._forced_failure = mode

    # -- the six steps ----------------------------------------------------

    async def fetch(self, request: FetchRequest) -> AdapterResponse:
        self._raise_if_forced()
        validate_request(self.connector_type, self._capabilities, request)

        key = self._cache.key(
            request.tenant_id, request.entitlement_scope, self.connector_type, request
        )

        # --- 1. cache, before any token is spent --------------------------
        lookup = await self._cache.get(key, request.max_staleness_ms)
        if lookup.is_hit:
            return self._from_cache(lookup.entry, revalidated=False)

        # `max_staleness_ms == 0` means "I want a live fetch", not "revalidate
        # for me". Revalidating anyway would make the staleness knob one-way:
        # this dataset is a module-level constant, so the candidate ETag always
        # matches the stored one, every conditional request would succeed, and
        # once an entry existed NO value of max_staleness_ms could ever produce
        # `served="live"` again. That breaks DoD §2 hard part 4, whose whole
        # demonstration is the knob flipping `served` between live and cache.
        if lookup.status is CacheStatus.STALE and request.max_staleness_ms > 0:
            # A conditional request: `If-None-Match`, answered `304` with no
            # body. Authenticated, because a real conditional GET is — but it
            # spends no token, which is the guarantee this branch exists for and
            # which GitHub's own contract grants (a 304 is not charged).
            probe = self._outbound(request, if_none_match=lookup.entry.etag)
            answer = await self._transport(probe, request.tenant_id)
            if answer.is_not_modified:
                revalidated = await self._cache.revalidate(key, lookup.entry.etag)
                if revalidated.is_hit:
                    return self._from_cache(revalidated.entry, revalidated=True)

        # --- 2. consume a token -------------------------------------------
        #
        # `try_consume`, not `consume`: this adapter needs the decision on BOTH
        # branches — the allowed one feeds the source's `X-RateLimit-*` headers,
        # and the denied one is rendered in the source's own refusal shape
        # before being normalised. There is one arithmetic, in Redis, so the
        # budget the source reports and the one the envelope reports cannot
        # drift.
        policy = self._rate_limit_policy(request.tenant_id)
        decision = await self._limiter.try_consume(request.tenant_id, self.connector_type, policy)
        if not decision.allowed:
            raise self._exhausted(request.tenant_id, policy, decision)

        # --- 3. resolve the tenant's credential ---------------------------
        credential = self._secrets.resolve(self._secret_ref(request.tenant_id))

        # --- 3b. the round trip a real source would cost ------------------
        #
        # Placed HERE, and the placement is the whole decision (ADR-038). Every
        # return above this line is a cache hit, and a cache hit must stay
        # ~0.4ms: it is what makes the `max_staleness_ms` demo legible, it is
        # why the token is spent after the cache and not before (ADR-024), and
        # it is what lets the k6 profile measure this engine rather than these
        # sleeps. Moving it above the cache check would quietly break all three.
        #
        # Deliberately NOT applied to the conditional-revalidation branch. A
        # real 304 does cost a round trip even though it transfers no body, so
        # this mock is optimistic there by one round trip — named rather than
        # papered over, because the branch's value is proving no token is spent,
        # not proving what it costs.
        if self._latency_s:
            await asyncio.sleep(self._latency_s)

        # --- 4 + 5. build the call, send it, parse what comes back --------
        outbound = self._outbound(request, credential=credential)
        self.last_request = outbound
        answer = await self._transport(outbound, request.tenant_id, policy, decision)
        self.last_response = answer
        page = self.parse_response(answer)
        page = Page(
            rows=self._project(page.rows, request.projection),
            next_cursor=page.next_cursor,
            has_more=page.has_more,
        )

        # --- 6. record and cache ------------------------------------------
        #
        # The ETag is the SOURCE's, read off the response, not one we compute
        # over the parsed rows. That is what makes the conditional request in
        # the branch above meaningful: `If-None-Match` has to carry a validator
        # the source itself issued, or it can never match and the 304 path is
        # dead code that still passes its tests.
        etag = answer.header("ETag")
        entry = await self._cache.set(
            key,
            page.rows,
            etag=etag,
            meta={"next_cursor": page.next_cursor, "has_more": page.has_more},
        )
        return AdapterResponse(
            rows=page.rows,
            fetched_at=entry.fetched_at,
            served="live",
            etag=etag,
            next_cursor=page.next_cursor,
            has_more=page.has_more,
        )

    # -- steps 4 and 5 ----------------------------------------------------

    @staticmethod
    def _project(rows: list[dict[str, Any]], projection) -> list[dict[str, Any]]:
        """Narrow to the requested columns.

        An empty projection means "everything" — a caller that has not decided
        which columns it wants must not be handed zero columns.
        """
        wanted = list(projection)
        if not wanted:
            return rows
        return [{c: row[c] for c in wanted if c in row} for row in rows]

    # -- control-plane lookups --------------------------------------------

    def _rate_limit_policy(self, tenant_id: str) -> RateLimitPolicy:
        row = self._control_plane.get_rate_limit_policy(tenant_id, self.connector_type)
        if row is None:
            raise ApiError(
                code=ErrorCode.CONNECTOR_NOT_ENABLED,
                http=403,
                message=(
                    f"no rate-limit budget is configured for tenant {tenant_id!r} on "
                    f"connector {self.connector_type!r}"
                ),
            )
        return RateLimitPolicy.from_row(row)

    def _secret_ref(self, tenant_id: str) -> str:
        """The grant row's ``secret_ref``, or a refusal.

        An absent or disabled grant is ``CONNECTOR_NOT_ENABLED``, never a
        fallback to some default credential — failing open here would let a
        tenant query a connector it was never granted.
        """
        grants = self._control_plane.get_tenant_connectors(tenant_id)
        for grant in grants:
            if grant["connector_type"] != self.connector_type:
                continue
            if not grant.get("enabled", False) or grant.get("status") != "active":
                raise ApiError(
                    code=ErrorCode.CONNECTOR_NOT_ENABLED,
                    http=403,
                    message=(
                        f"connector {self.connector_type!r} is not active for tenant {tenant_id!r}"
                    ),
                )
            return grant["secret_ref"]

        raise ApiError(
            code=ErrorCode.CONNECTOR_NOT_ENABLED,
            http=403,
            message=(f"tenant {tenant_id!r} has no grant for connector {self.connector_type!r}"),
        )

    # -- helpers ----------------------------------------------------------

    def _from_cache(self, entry, revalidated: bool) -> AdapterResponse:
        return AdapterResponse(
            rows=entry.rows,
            fetched_at=entry.fetched_at,
            served="cache",
            etag=entry.etag,
            next_cursor=entry.meta.get("next_cursor"),
            has_more=bool(entry.meta.get("has_more", False)),
            revalidated=revalidated,
        )

    @staticmethod
    def _etag(rows: list[dict[str, Any]]) -> str:
        """A content hash. Stable for stable data, which is what makes 304 work."""
        body = json.dumps(rows, sort_keys=True, separators=(",", ":"), default=str)
        return f'W/"{hashlib.sha1(body.encode()).hexdigest()[:16]}"'

    def _raise_if_forced(self) -> None:
        if self._forced_failure is None:
            return
        mode, self._forced_failure = self._forced_failure, None
        classification = classify(mode)
        raise ApiError(
            code=classification.error_code,
            http=HTTP_STATUS_FOR_CODE[classification.error_code],
            message=f"{self.connector_type}: forced {mode.value} (mock failure hook)",
        )
