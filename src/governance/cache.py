"""The freshness cache: TTL on the write, staleness on the read.

**The distinction this module exists to keep straight.** ``CACHE_TTL_MS`` is a
property of the **write** — how long an entry lives. ``max_staleness_ms`` is a
property of the **read** — how old an entry may be and still satisfy *this*
caller. They are not the same knob and must not be collapsed: if a
``max_staleness_ms=0`` request wrote a zero-TTL entry, no later request could
ever get a hit, and the freshness demo would be unreproducible.

**The entitlement trap.** The key carries
``tenant_id`` **and** ``entitlement_scope`` as mandatory segments. Omitting
either does not degrade the cache — it serves one principal's rows to another.
``entitlement_scope`` is a *data* scope and is unrelated to the OAuth ``scope``
claim in :class:`src.models.context.UserContext`; conflating the two is the
leak. The argument is **required** rather than defaulted, so that "I forgot the
scope" is a ``TypeError`` rather than a cross-tenant read.

No single-flight coalescing guard: it would only pay for itself under a real
thundering herd, which the load runs have not produced.
"""

import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from src.config import settings
from src.connectors.base import FetchRequest
from src.governance.clock import NowMs, wall_clock_ms

KEY_PREFIX = "cache"


class CacheStatus(StrEnum):
    """Why a lookup did or did not satisfy the caller."""

    HIT = "hit"
    """Present and within ``max_staleness_ms``. Serve it; spend no token."""

    STALE = "stale"
    """Present but too old for *this* caller. The caller may revalidate
    conditionally — which, on a ``304``, still costs no token."""

    MISS = "miss"
    """Absent, or expired out of Redis. A live fetch is the only option."""


@dataclass(frozen=True)
class CacheEntry:
    """What is stored: the rows, when they were fetched, and their ETag."""

    rows: list[dict[str, Any]]
    fetched_at: float
    """Epoch **seconds** — the unit ``AdapterResponse.fetched_at`` and the
    envelope's ``freshness_ms`` are derived from."""
    etag: str | None = None

    meta: dict[str, Any] = field(default_factory=dict)
    """Paging state that came back with these rows (``next_cursor``,
    ``has_more``). Stored alongside the rows because a cache hit must be able to
    answer "is there another page" identically to the live fetch it stands in
    for — a hit that silently dropped the cursor would truncate a paging
    caller's results at the first cached page."""


@dataclass(frozen=True)
class CacheLookup:
    """The outcome of one :meth:`FreshnessCacheManager.get`."""

    status: CacheStatus
    entry: CacheEntry | None = None
    age_ms: int | None = None

    @property
    def is_hit(self) -> bool:
        return self.status is CacheStatus.HIT


def normalize_request(request: FetchRequest) -> str:
    """Canonical text for the part of a request that determines its rows.

    Deliberately excludes ``tenant_id`` and ``entitlement_scope``: those are
    *separate key segments*, so that a reader of a key can see them rather than
    having them dissolved into a hash. A leak caused by a missing scope must be
    visible by inspection, not only by decoding a digest.

    ``projection`` is sorted because column order does not change which rows are
    returned — the stored rows are dicts — so two orderings should share an entry.
    """
    return json.dumps(
        {
            "predicates": dict(sorted(request.predicates.items())),
            "projection": sorted(request.projection),
            "page": request.page,
            "limit": request.limit,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def build_key(
    tenant_id: str,
    entitlement_scope: str,
    connector_type: str,
    request: FetchRequest,
) -> str:
    """``cache:{tenant}:{scope}:{connector}:{sha1(normalized request)}``.

    Both identity segments are required positionally. See the module docstring.
    """
    if not tenant_id:
        raise ValueError("tenant_id is a mandatory cache-key segment")
    if not entitlement_scope:
        raise ValueError("entitlement_scope is a mandatory cache-key segment")
    digest = hashlib.sha1(normalize_request(request).encode()).hexdigest()
    return f"{KEY_PREFIX}:{tenant_id}:{entitlement_scope}:{connector_type}:{digest}"


class FreshnessCacheManager:
    """Reads and writes cached connector results, with staleness enforced on read."""

    def __init__(
        self,
        redis: Any,
        now_ms: NowMs = wall_clock_ms,
        ttl_ms: int | None = None,
    ) -> None:
        self._redis = redis
        self._now_ms = now_ms
        self._ttl_ms = settings.CACHE_TTL_MS if ttl_ms is None else ttl_ms

    @property
    def ttl_ms(self) -> int:
        """The fixed server TTL applied to every write."""
        return self._ttl_ms

    def key(self, tenant_id: str, entitlement_scope: str, connector_type: str,
            request: FetchRequest) -> str:
        return build_key(tenant_id, entitlement_scope, connector_type, request)

    async def get(self, key: str, max_staleness_ms: int) -> CacheLookup:
        """Look up ``key``, judging freshness against *this* caller's tolerance."""
        raw = await self._redis.get(key)
        if raw is None:
            return CacheLookup(status=CacheStatus.MISS)

        entry = self._decode(raw, key)
        age_ms = max(0, self._now_ms() - int(entry.fetched_at * 1000))
        status = CacheStatus.HIT if age_ms <= max_staleness_ms else CacheStatus.STALE
        return CacheLookup(status=status, entry=entry, age_ms=age_ms)

    async def set(
        self,
        key: str,
        rows: list[dict[str, Any]],
        etag: str | None = None,
        fetched_at: float | None = None,
        meta: dict[str, Any] | None = None,
    ) -> CacheEntry:
        """Write an entry under the **fixed server TTL**, not the caller's staleness."""
        entry = CacheEntry(
            rows=rows,
            fetched_at=self._now_ms() / 1000 if fetched_at is None else fetched_at,
            etag=etag,
            meta=dict(meta or {}),
        )
        await self._redis.set(key, self._encode(entry), px=self._ttl_ms)
        return entry

    async def revalidate(self, key: str, source_etag: str | None) -> CacheLookup:
        """The ``304`` path: if the source's ETag still matches, refresh the age.

        Refreshes ``fetched_at`` **in place** and returns a ``HIT`` without the
        caller spending a token — the source said "not modified", so no rows
        were transferred and no downstream budget was used. A mismatch is a
        ``MISS``: the caller must fetch live.
        """
        lookup = await self.get(key, max_staleness_ms=0)
        if lookup.entry is None:
            return CacheLookup(status=CacheStatus.MISS)
        if source_etag is None or lookup.entry.etag != source_etag:
            return CacheLookup(status=CacheStatus.MISS, entry=lookup.entry)

        refreshed = await self.set(
            key, lookup.entry.rows, etag=lookup.entry.etag, meta=lookup.entry.meta
        )
        return CacheLookup(status=CacheStatus.HIT, entry=refreshed, age_ms=0)

    async def invalidate(self, key: str) -> None:
        await self._redis.delete(key)

    # --- serialization ----------------------------------------------------

    @staticmethod
    def _encode(entry: CacheEntry) -> str:
        return json.dumps(
            {
                "fetched_at": entry.fetched_at,
                "etag": entry.etag,
                "data": entry.rows,
                "meta": entry.meta,
            },
            separators=(",", ":"),
            default=str,
        )

    @staticmethod
    def _decode(raw: bytes | str, key: str) -> CacheEntry:
        """Parse a stored entry, failing loudly on corruption.

        Treating an undecodable entry as a miss would silently convert a
        serialization bug into a permanent, invisible cache bypass — every
        request would look like a cold start and spend a token forever.
        """
        try:
            payload = json.loads(raw)
            return CacheEntry(
                rows=payload["data"],
                fetched_at=float(payload["fetched_at"]),
                etag=payload.get("etag"),
                meta=payload.get("meta") or {},
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError(f"corrupt cache entry at {key!r}: {exc}") from exc
