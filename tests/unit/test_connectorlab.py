"""The binary. Its one rule is that it prints rather than simulates.

The scenes need a live wiring, so the assertions here are on the parts that can
be checked without one: the renderer, the ad-hoc argument parsing, and — the
important one — that the credential cannot reach an artifact.
"""

import pytest

from src.connectorlab import render
from src.connectorlab.__main__ import build_parser, parse_where
from src.connectors.base import CapabilityModel, FetchRequest
from src.connectors.request import build_request
from src.connectors.response import SourceResponse
from tests.unit.catalog_fixture import GITHUB_CAPABILITIES, GITHUB_ENDPOINT

GITHUB = CapabilityModel.from_dict(GITHUB_CAPABILITIES)
TOKEN = "mock_github_tenant_acme_Zq8xk1"


def outbound():
    return build_request(
        GITHUB_ENDPOINT,
        GITHUB,
        FetchRequest(
            tenant_id="tenant_acme",
            entitlement_scope="support:alice",
            predicates={"repo": "ema/core", "state": "open"},
            limit=3,
        ),
        TOKEN,
    )


# --- the rule: nothing secret reaches an artifact --------------------------


def test_the_credential_never_reaches_the_rendered_request() -> None:
    """`make connectors` commits its output. A token in it would be a leak that
    survives in git history long after anyone noticed."""
    block = "\n".join(render.request_block(outbound()))

    assert TOKEN not in block
    assert "Bearer ****" in block


def test_the_request_object_still_carries_the_real_credential() -> None:
    """Redaction is a property of RENDERING. If it were a property of the
    request, the transport would send `****` and every live call would 401."""
    assert outbound().headers["Authorization"] == f"Bearer {TOKEN}"


# --- the renderer ----------------------------------------------------------


def test_the_request_block_is_shaped_like_a_request() -> None:
    lines = render.request_block(outbound())

    assert lines[0].strip() == "GET /repos/ema/core/pulls?state=open&per_page=3 HTTP/1.1"
    assert lines[1].strip() == "Host: api.github.com"


def test_the_response_block_carries_status_headers_and_body() -> None:
    lines = render.response_block(
        SourceResponse(status=403, headers={"X-RateLimit-Remaining": "0"}, body={"message": "no"})
    )

    assert lines[0].strip() == "HTTP/1.1 403"
    assert any("X-RateLimit-Remaining: 0" in line for line in lines)
    assert any('"message": "no"' in line for line in lines)


def test_a_long_body_is_truncated_with_a_count_rather_than_silently() -> None:
    """A reader must be able to tell a short page from a clipped one."""
    lines = render.response_block(SourceResponse(status=200, body=[{"n": i} for i in range(200)]))

    assert any("more lines" in line for line in lines)


def test_an_empty_row_list_says_so_rather_than_printing_nothing() -> None:
    assert render.rows_block([]) == ["  (no rows)"]


def test_the_table_sizes_itself_to_its_widest_cell() -> None:
    lines = render.table(["a", "bb"], [["xxxx", "y"]])

    assert lines[0].strip().startswith("a     bb")
    assert lines[2].strip().startswith("xxxx  y")


# --- ad-hoc arguments ------------------------------------------------------


@pytest.mark.parametrize(
    ("clause", "expected"),
    [
        ("state=open", ("state", ("=", "open"))),
        ("repo=ema/core", ("repo", ("=", "ema/core"))),
        ("updated>=2026-09-01", ("updated", (">=", "2026-09-01"))),
        ("updated<2026-09-01", ("updated", ("<", "2026-09-01"))),
        ("title=a=b", ("title", ("=", "a=b"))),
    ],
)
def test_where_clauses_parse(clause, expected) -> None:
    assert parse_where(clause) == expected


def test_a_where_clause_with_no_operator_is_refused() -> None:
    """LAW 4. Reading `state` as `state=` would filter on the empty string and
    return nothing, which looks exactly like a correct empty result."""
    with pytest.raises(SystemExit, match="no operator"):
        parse_where("state")


def test_the_two_character_operators_win_over_their_prefixes() -> None:
    """`>=` must not parse as `>` with a value beginning `=`."""
    column, (operator, value) = parse_where("updated>=2026")

    assert (column, operator, value) == ("updated", ">=", "2026")


def test_scene_names_are_validated_by_the_parser() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--scene", "not-a-scene"])


def test_the_walkthrough_runs_every_scene_by_default() -> None:
    assert build_parser().parse_args([]).scene == []


def test_fetch_takes_repeatable_where_clauses() -> None:
    args = build_parser().parse_args(
        ["fetch", "github.pull_requests", "--where", "repo=ema/core", "--where", "state=open"]
    )

    assert args.command == "fetch"
    assert args.where == ["repo=ema/core", "state=open"]
