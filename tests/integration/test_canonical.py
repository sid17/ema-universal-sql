"""DoD §2 hard part 1 — query-time RLS, over HTTP, against the real stack.

`make demo` shows this to a human; these two tests are the machine-checkable
form. Both gate names come from the phase file.
"""



def test_canonical_alice(envelope):
    """**GATE.** The canonical query as alice returns the entitled rows."""
    body = envelope(user="alice")

    assert [column["name"] for column in body["columns"]] == [
        "title", "author", "key", "status",
    ]
    assert {column["source"] for column in body["columns"]} == {"github", "jira"}
    assert len(body["rows"]) == 3

    assert body["partial"] is False
    assert body["join_status"] == "complete"
    assert body["warnings"] == []
    assert body["trace_id"], "trace_id must resolve against the exported spans"

    # freshness_ms is the STALEST contributor. Both sources were just fetched,
    # so it is small — but it must be present and non-negative, not null.
    assert body["freshness_ms"] is not None
    assert body["freshness_ms"] >= 0


def test_rls_shrinks_bob(envelope):
    """**GATE.** The same SQL as bob returns exactly 1 row where alice got 3.

    A count that shrinks but stays non-zero is the legible proof. Zero would be
    indistinguishable from a query that is simply broken, so the demo would
    prove nothing — which is why the seed data gives bob exactly one qualifying
    issue.
    """
    alice = envelope(user="alice")
    bob = envelope(user="bob")

    assert len(alice["rows"]) == 3
    assert len(bob["rows"]) == 1

    # bob's row is one alice never saw, so this is a different slice rather
    # than a truncation of the same one.
    alice_keys = {row[2] for row in alice["rows"]}
    bob_keys = {row[2] for row in bob["rows"]}
    assert not (alice_keys & bob_keys)


def test_the_stats_show_where_the_time_went(envelope):
    """Front-loaded observability: the envelope carries the same stage timings
    the trace does, so the two cannot disagree."""
    body = envelope(user="alice", max_staleness_ms=0)
    for stage in ("parse_ms", "entitlement_ms", "plan_ms", "federation_ms", "assemble_ms"):
        assert stage in body["stats"], stage
    assert set(body["stats"]["connector_ms"]) == {"github", "jira"}


def test_rate_limit_status_is_reported_for_both_connectors(envelope):
    body = envelope(user="alice")
    assert set(body["rate_limit_status"]) == {"github", "jira"}
    for budget in body["rate_limit_status"].values():
        assert budget["throttled"] is False
        assert budget["remaining"] > 0
