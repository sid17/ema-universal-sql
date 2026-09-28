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

import hashlib
import json
from typing import Any

from src.connectors.base import (
    AdapterResponse,
    BaseConnectorAdapter,
    CapabilityModel,
    FetchRequest,
)
from src.connectors.errors import FailureMode, classify
from src.connectors.pagination import strategy_for
from src.governance.cache import CacheStatus, FreshnessCacheManager
from src.governance.clock import NowMs, wall_clock_ms
from src.governance.ratelimit import RateLimitPolicy, TokenBucketRateLimiter
from src.governance.secrets import SecretsManagerClient
from src.models.errors import ApiError, ErrorCode

#: Operators the mock datasets can evaluate in memory.
_COMPARATORS = {
    "=": lambda row, value: row == value,
    ">": lambda row, value: row > value,
    ">=": lambda row, value: row >= value,
    "<": lambda row, value: row < value,
    "<=": lambda row, value: row <= value,
}


class MockConnectorAdapter(BaseConnectorAdapter):
    """A deterministic in-memory source wearing the real connector contract."""

    #: The resource this adapter serves, e.g. ``pull_requests``.
    resource: str

    def __init__(
        self,
        capabilities: dict[str, Any],
        cache: FreshnessCacheManager,
        limiter: TokenBucketRateLimiter,
        secrets: SecretsManagerClient,
        control_plane: Any,
        now_ms: NowMs = wall_clock_ms,
    ) -> None:
        self._capabilities = CapabilityModel.from_dict(capabilities)
        self._cache = cache
        self._limiter = limiter
        self._secrets = secrets
        self._control_plane = control_plane
        self._now_ms = now_ms
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
        """The rows this source would return unfiltered. Overridden per adapter."""
        raise NotImplementedError

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
        self._validate(request)

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
            # A conditional request. Computing what the source *would* return
            # costs no downstream call in a mock and no token by contract — a
            # real 304 transfers no body either.
            candidate = self._page(request)
            revalidated = await self._cache.revalidate(key, self._etag(candidate.rows))
            if revalidated.is_hit:
                return self._from_cache(revalidated.entry, revalidated=True)

        # --- 2. consume a token -------------------------------------------
        await self._limiter.consume(
            request.tenant_id, self.connector_type, self._rate_limit_policy(request.tenant_id)
        )

        # --- 3. resolve the tenant's credential ---------------------------
        self._secrets.resolve(self._secret_ref(request.tenant_id))

        # --- 4 + 5. filter and paginate -----------------------------------
        page = self._page(request)

        # --- 6. record and cache ------------------------------------------
        etag = self._etag(page.rows)
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

    def _page(self, request: FetchRequest):
        rows = self._apply_predicates(self.dataset(), request.predicates)
        rows = self._project(rows, request.projection)
        return strategy_for(self._capabilities.pagination).paginate(
            rows, limit=request.limit, page=request.page
        )

    def _apply_predicates(
        self, rows: list[dict[str, Any]], predicates: dict[str, Any] | Any
    ) -> list[dict[str, Any]]:
        for column, condition in predicates.items():
            op, value = self._as_op_value(condition)
            compare = _COMPARATORS[op]
            rows = [row for row in rows if column in row and compare(row[column], value)]
        return rows

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

    # -- step 0 -----------------------------------------------------------

    def _validate(self, request: FetchRequest) -> None:
        """Reject anything the capability model does not declare.

        **Rejected, not silently ignored**, for predicates *and* projections.

        For a predicate, a silent drop would return rows the caller did not ask
        for — and in Phase 2 that predicate may be the RLS filter, which turns a
        silent drop from a bug into a data leak.

        For a projection the failure is quieter and just as bad. DoD §4
        non-negotiable #2 requires the engine to fetch
        ``projection ∪ every WHERE/ORDER BY column`` so that re-applying
        predicates authoritatively cannot drop a valid row. If a column in that
        union were silently omitted here, the engine would re-filter on data it
        never fetched and discard rows the caller was entitled to — wrong
        results, invisible at this boundary.
        """
        unknown = [c for c in request.projection if c not in self._capabilities.columns]
        if unknown:
            raise ApiError(
                code=ErrorCode.ENTITLEMENT_DENIED,
                http=400,
                message=(
                    f"{self.connector_type} has no column(s) {', '.join(sorted(unknown))}; "
                    f"available: {', '.join(self._capabilities.columns)}"
                ),
            )

        for column, condition in request.predicates.items():
            op, _ = self._as_op_value(condition)
            if op not in _COMPARATORS:
                raise ApiError(
                    code=ErrorCode.ENTITLEMENT_DENIED,
                    http=400,
                    message=f"{self.connector_type}: unknown operator {op!r} on {column!r}",
                )
            if not self._capabilities.supports(column, op):
                raise ApiError(
                    code=ErrorCode.ENTITLEMENT_DENIED,
                    http=400,
                    message=(
                        f"{self.connector_type} cannot filter {column!r} with {op!r}; "
                        f"this predicate must not be pushed down"
                    ),
                )

        missing = [
            c for c in self._capabilities.required_columns() if c not in request.predicates
        ]
        if missing:
            raise ApiError(
                code=ErrorCode.ENTITLEMENT_DENIED,
                http=400,
                message=(
                    f"{self.connector_type} requires a predicate on {', '.join(missing)}; "
                    f"the upstream API has no endpoint without it"
                ),
            )

    @staticmethod
    def _as_op_value(condition: Any) -> tuple[str, Any]:
        """Accept ``{"col": value}`` as shorthand for ``{"col": ("=", value)}``."""
        if isinstance(condition, tuple | list) and len(condition) == 2:
            return str(condition[0]), condition[1]
        return "=", condition

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
                        f"connector {self.connector_type!r} is not active for tenant "
                        f"{tenant_id!r}"
                    ),
                )
            return grant["secret_ref"]

        raise ApiError(
            code=ErrorCode.CONNECTOR_NOT_ENABLED,
            http=403,
            message=(
                f"tenant {tenant_id!r} has no grant for connector {self.connector_type!r}"
            ),
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
            http=504 if classification.error_code is ErrorCode.SOURCE_TIMEOUT else 502,
            message=f"{self.connector_type}: forced {mode.value} (mock failure hook)",
        )
