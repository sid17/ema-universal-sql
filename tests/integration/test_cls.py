"""DoD §2 hard part 1 (the CLS half) — the column mask, end to end.

The canonical query projects four columns and none is `reporter_email`, so it
cannot itself demonstrate CLS. This exercises the console's second preset.
"""

import re

from tests.conftest import CANONICAL_SQL, CLS_DEMO_SQL

MD5_HEX = re.compile(r"^[0-9a-f]{32}$")


def test_cls_mask(envelope):
    """**GATE.** `reporter_email` is hash-masked and flagged as masked."""
    body = envelope(sql=CLS_DEMO_SQL)

    by_name = {column["name"]: column for column in body["columns"]}
    assert by_name["reporter_email"]["masked"] is True
    assert by_name["reporter_email"]["source"] == "jira"

    # Every other column is unmasked — a mask flag on everything would be
    # indistinguishable from a bug that set it unconditionally.
    assert [c["name"] for c in body["columns"] if c["masked"]] == ["reporter_email"]

    values = [row[4] for row in body["rows"]]
    assert values, "no rows, so the mask was not actually exercised"
    assert all(MD5_HEX.match(value) for value in values)


def test_the_raw_email_never_appears_anywhere_in_the_response(run_query):
    """Asserted on the whole response body, not just the masked column.

    A mask that rewrote the projection but left the address in a warning, a
    stat or a duplicated column would satisfy a narrower assertion.
    """
    response = run_query(sql=CLS_DEMO_SQL)
    assert response.status_code == 200
    assert "@acme.com" not in response.text
    assert "@" not in "".join(row[4] for row in response.json()["rows"])


def test_the_output_column_keeps_its_name(envelope):
    """Without `exp.alias_` the column comes back named `md5(reporter_email)`,
    which changes the response shape and breaks every caller."""
    body = envelope(sql=CLS_DEMO_SQL)
    assert [c["name"] for c in body["columns"]] == [
        "title", "author", "key", "status", "reporter_email",
    ]


def test_masking_does_not_change_the_row_count(envelope):
    """CLS masks values; it must not filter rows. A mask that dropped rows
    would be an RLS rule wearing the wrong name."""
    assert len(envelope(sql=CANONICAL_SQL)["rows"]) == len(
        envelope(sql=CLS_DEMO_SQL)["rows"]
    )


def test_the_mask_is_stable_so_it_can_still_be_grouped_by(envelope):
    """Why `hash` and not `drop` or `null`: the column still carries a stable
    value, so a caller can group by it without ever learning the address."""
    first = envelope(sql=CLS_DEMO_SQL)
    second = envelope(sql=CLS_DEMO_SQL)
    assert [row[4] for row in first["rows"]] == [row[4] for row in second["rows"]]
