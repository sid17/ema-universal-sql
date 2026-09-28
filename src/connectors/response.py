"""What a source answers — status, headers, body — and its rate-limit dialect.

Two sources, two vocabularies for the same fact. GitHub reports its budget in
``x-ratelimit-*`` and refuses with **403** carrying ``x-ratelimit-remaining: 0``;
Jira refuses with **429** and a ``Retry-After``. Both normalise to one
``ApiError(RATE_LIMIT_EXHAUSTED)``, which is the connector's job stated in code
rather than in a comment.

**The numbers are not invented.** Every header below is derived from the
:class:`~src.governance.ratelimit.RateLimitDecision` the token bucket already
returned, so the source's reported budget and ``QueryEnvelope.rate_limit_status``
cannot drift — there is one arithmetic, in Redis, and both readings come from
the same call.
"""

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from src.connectors.base import header_value
from src.governance.ratelimit import RateLimitDecision, RateLimitPolicy


@dataclass(frozen=True)
class SourceResponse:
    """One answer from a source, in the shape a live HTTP client would see.

    ``body`` is already-parsed JSON rather than bytes: the transport seam is
    "what did the source say", not "how was it framed on the wire", and making
    every adapter re-parse would buy nothing a live client does not get free.
    """

    status: int
    headers: Mapping[str, str] = field(default_factory=dict)
    body: Any = None

    def header(self, name: str) -> str | None:
        """One response header, matched case-insensitively. See :func:`header_value`."""
        return header_value(self.headers, name)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def is_not_modified(self) -> bool:
        """``304`` — the conditional request matched, and no body was sent."""
        return self.status == 304


@dataclass(frozen=True)
class RateLimitDialect:
    """How one source talks about its budget, and how it refuses.

    Declared per adapter beside ``endpoint``. A third connector supplies its own
    spelling here; nothing in the adapter changes.
    """

    limit_header: str
    remaining_header: str
    reset_header: str
    exhausted_status: int
    exhausted_body: Mapping[str, Any]
    sends_retry_after: bool
    extra_headers: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RateLimitDialect":
        """Build from the ``connectors.rate_limit`` column (the YAML's ``api.rate_limit``)."""
        return cls(
            limit_header=raw["limit_header"],
            remaining_header=raw["remaining_header"],
            reset_header=raw["reset_header"],
            exhausted_status=int(raw["exhausted_status"]),
            exhausted_body=dict(raw["exhausted_body"]),
            sends_retry_after=bool(raw["sends_retry_after"]),
            extra_headers=dict(raw.get("extra_headers", {})),
        )

    def headers(
        self, policy: RateLimitPolicy, decision: RateLimitDecision, now_ms: float
    ) -> dict[str, str]:
        """What the source reports about the budget after this call.

        ``reset`` is the epoch second at which the bucket would be **full**
        again — the token-bucket analogue of a window boundary, and the only
        reading that stays true for a partially drained bucket.
        """
        owed = max(policy.capacity - decision.remaining, 0)
        reset_ms = now_ms + owed * policy.refill_ms
        return {
            self.limit_header: str(policy.capacity),
            self.remaining_header: str(max(decision.remaining, 0)),
            self.reset_header: str(int(reset_ms // 1000)),
            **self.extra_headers,
        }

    def exhausted(
        self, policy: RateLimitPolicy, decision: RateLimitDecision, now_ms: float
    ) -> SourceResponse:
        """The refusal, in this source's own shape.

        Produced when *our* governor denies, because in this system the token
        bucket **is** the source's quota (the bucket models the
        downstream API's budget). No call goes out — we refuse on the source's
        behalf, in the source's vocabulary, and the adapter then normalises it.
        """
        headers = self.headers(policy, decision, now_ms)
        if self.sends_retry_after:
            headers["Retry-After"] = str(math.ceil(decision.retry_after_ms / 1000))
        return SourceResponse(
            status=self.exhausted_status, headers=headers, body=dict(self.exhausted_body)
        )
