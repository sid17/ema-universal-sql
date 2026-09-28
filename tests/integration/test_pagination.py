"""**GATE: `test_pagination`** — result-level cursor paging over the joined rows.

Distinct from connector-level pagination, which lives in the adapters (Phase 1).
This pages the *joined, sorted, entitled* result.

The seed data gives two of alice's three issues the same `updated` value
(ADR-035) precisely so this exercises the `key ASC` tiebreaker: an offset cursor
over a non-total order lets tied rows reorder between pages, so rows get skipped
or duplicated.
"""

PAGED_SQL = """
SELECT pr.title, pr.author, issue.key, issue.status
FROM   github.pull_requests pr
JOIN   jira.issues issue ON pr.issue_key = issue.key
WHERE  pr.repo = 'ema/core' AND pr.state = 'open' AND issue.status = 'In Progress'
ORDER BY issue.updated DESC
LIMIT 2
"""


def test_pagination(envelope):
    """**GATE.** More joined rows than LIMIT -> a cursor; the next window does
    not overlap; the last window has no cursor."""
    first = envelope(sql=PAGED_SQL)
    assert len(first["rows"]) == 2
    assert first["next_cursor"] is not None

    second = envelope(sql=PAGED_SQL, cursor=first["next_cursor"])
    assert len(second["rows"]) == 1
    assert second["next_cursor"] is None

    keys = [row[2] for row in first["rows"] + second["rows"]]
    assert len(set(keys)) == 3, "pages overlapped or skipped a row"
    assert sorted(keys) == ["SUP-12", "SUP-13", "SUP-14"]


def test_the_page_boundary_falls_on_the_tie_and_is_still_stable(envelope):
    """SUP-13 and SUP-14 share an `updated` value, and the boundary sits between
    them. Without `key ASC` the two queries could order the tied pair
    differently and the pages would disagree."""
    first = envelope(sql=PAGED_SQL)
    second = envelope(sql=PAGED_SQL, cursor=first["next_cursor"])
    assert [row[2] for row in first["rows"]] == ["SUP-12", "SUP-13"]
    assert [row[2] for row in second["rows"]] == ["SUP-14"]


def test_paging_is_repeatable(envelope):
    """The same cursor must return the same window every time, or an offset
    cursor is meaningless."""
    first = envelope(sql=PAGED_SQL)
    a = envelope(sql=PAGED_SQL, cursor=first["next_cursor"])
    b = envelope(sql=PAGED_SQL, cursor=first["next_cursor"])
    assert a["rows"] == b["rows"]


def test_a_single_page_result_returns_no_cursor(envelope):
    """alice has 3 rows and the canonical LIMIT is 50 — nothing to page."""
    body = envelope()
    assert len(body["rows"]) == 3
    assert body["next_cursor"] is None


def test_the_cursor_is_opaque(envelope):
    """base64, carrying no readable offset — so nothing invites a caller to
    build one by hand and couple themselves to our ordering."""
    cursor = envelope(sql=PAGED_SQL)["next_cursor"]
    assert "offset" not in cursor
    assert cursor.isascii()

    # ...but it IS decodable by us, and carries the strategy that issued it —
    # which is what lets a connector cursor be rejected rather than silently
    # reinterpreted as a result offset.
    from src.connectors.pagination import decode_token

    assert decode_token(cursor, "result") == 2


def test_a_malformed_cursor_is_rejected_not_silently_restarted(run_query):
    """Restarting at offset 0 would hand a paging caller a duplicate page with
    no indication anything went wrong — they would blame the data."""
    response = run_query(sql=PAGED_SQL, cursor="not-a-real-cursor")
    assert response.status_code == 400
    assert response.json()["detail"] == "Cursor"


def test_a_connector_cursor_is_rejected_as_a_result_cursor(run_query, envelope):
    """The strategy tag earning its keep: reinterpreting a connector cursor as a
    result offset would return real rows for the wrong question."""
    import base64
    import json

    payload = json.dumps({"v": 1, "s": "cursor", "o": 1}, separators=(",", ":"))
    connector_cursor = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    response = run_query(sql=PAGED_SQL, cursor=connector_cursor)
    assert response.status_code == 400
