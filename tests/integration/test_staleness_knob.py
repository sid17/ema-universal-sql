"""**GATE: `test_staleness_knob`** — DoD §2 hard part 4.

Why this test exists at Phase 2: freshness is 15% of the rubric, and its only
other proof was the Phase 3 Playwright spec — which is SHOULD-tier and would
disappear if the UI were cut. This keeps the freshness claim backed by a
MUST-tier test.
"""


def test_staleness_knob(envelope):
    """**GATE.** The same query twice: `served` flips live -> cache."""
    live = envelope(max_staleness_ms=0)
    assert {s["served"] for s in live["sources"]} == {"live"}
    assert set(live["stats"]["connector_ms"]) == {"github", "jira"}

    cached = envelope(max_staleness_ms=60_000)
    assert {s["served"] for s in cached["sources"]} == {"cache"}
    assert cached["stats"]["connector_ms"] == {}

    # Same answer either way — the knob changes provenance, never correctness.
    assert cached["rows"] == live["rows"]


def test_a_cache_hit_spends_no_token(envelope):
    """**ADR-024's observable consequence.**

    The bucket models the DOWNSTREAM API's budget, and a cache hit makes no
    downstream call — so charging for one is not conservative, it is wrong.

    Asserted as "not decremented" rather than "unchanged": the bucket refills
    against wall-clock time, so two requests straddling a refill interval
    legitimately differ upward. "Not decremented" is the observable form of "no
    token spent"; "unchanged" would be a time-dependent flake.
    """
    live = envelope(max_staleness_ms=0)
    cached = envelope(max_staleness_ms=60_000)

    for connector, budget in cached["rate_limit_status"].items():
        assert budget["remaining"] >= live["rate_limit_status"][connector]["remaining"], (
            f"{connector}: a cache hit spent a token"
        )


def test_max_staleness_zero_always_forces_a_live_fetch(envelope):
    """The knob must work in BOTH directions.

    The mock dataset is a module-level constant, so the candidate ETag always
    matches the stored one. Without an explicit `max_staleness_ms > 0` guard on
    revalidation, every conditional request would succeed and — once an entry
    existed — no value of `max_staleness_ms` could ever produce `live` again.
    The knob would be one-way and this gate would demo cache-to-cache.
    """
    envelope(max_staleness_ms=60_000)
    again = envelope(max_staleness_ms=0)
    assert {s["served"] for s in again["sources"]} == {"live"}


def test_freshness_reports_the_stalest_contributor(envelope):
    """`now - min(fetched_at)` across contributors (HLD §9)."""
    body = envelope(max_staleness_ms=0)
    assert body["freshness_ms"] is not None
    assert 0 <= body["freshness_ms"] < 60_000


def test_a_cached_answer_is_older_than_a_live_one(envelope):
    """The value must actually track age, not be a constant that happens to
    satisfy the range check above."""
    live = envelope(max_staleness_ms=0)
    cached = envelope(max_staleness_ms=60_000)
    assert cached["freshness_ms"] >= live["freshness_ms"]


def test_the_cache_is_keyed_per_user_so_alice_never_serves_bob(envelope):
    """**ADR-025.** `entitlement_scope` is a mandatory cache-key segment.

    alice warms the cache; bob must still get his own single row. If the key
    omitted the entitlement scope, bob would be served alice's three cached rows
    — a cross-user data leak dressed as a performance win, and the single
    sharpest security failure available in this design.
    """
    alice = envelope(user="alice", max_staleness_ms=0)
    assert len(alice["rows"]) == 3

    bob = envelope(user="bob", max_staleness_ms=60_000)
    assert len(bob["rows"]) == 1
    assert {row[2] for row in bob["rows"]}.isdisjoint({row[2] for row in alice["rows"]})
