"""Stage 2 — parse, reject, qualify, gate, extract.

The ordering assertions here are the point of the file. Three of them encode
behaviour that was *measured* rather than assumed (v3-research), and each would
pass a naive implementation right up until it silently did the wrong thing.
"""

import pytest
import sqlglot
from sqlglot import exp
from sqlglot.optimizer.qualify import qualify

from src.models.errors import ApiError, ErrorCode, InvalidQueryError
from src.sqlparse.parser import ColumnRef, flatten_conjunction
from tests.unit.conftest import CANONICAL_SQL, CLS_DEMO_SQL, GRANTED


@pytest.fixture
def parsed(parser):
    return parser.parse_and_validate(CANONICAL_SQL, GRANTED)


# --- the ordering that ADR-027 is about -------------------------------------


def test_select_star_is_rejected_even_though_qualify_would_expand_it(parser):
    """**The ADR-027 assertion.**

    Measured: `qualify()` expands `SELECT *` against the schema into six named
    columns, so `exp.Star` no longer exists in the tree afterwards. A whitelist
    running after qualify would accept this query silently — and `SELECT *` is
    the one projection that defeats projection pushdown, because the fetch set
    becomes every column the source has.

    If someone reorders `parse_and_validate` to qualify first, this is the test
    that fails.
    """
    with pytest.raises(InvalidQueryError) as raised:
        parser.parse_and_validate("SELECT * FROM jira.issues issue", GRANTED)
    assert raised.value.detail == "Star"


def test_qualify_really_does_expand_a_star(parser, catalog):
    """The premise of the test above, asserted directly rather than trusted.

    Without this, the test above could keep passing for the wrong reason (e.g.
    a parse failure) long after `qualify`'s behaviour changed.
    """
    expanded = qualify(
        sqlglot.parse_one("SELECT * FROM jira.issues issue", read="duckdb"),
        schema=catalog.qualify_schema(),
        dialect="duckdb",
    )
    assert not list(expanded.find_all(exp.Star))
    assert {c.name for c in expanded.find_all(exp.Column)} >= {"key", "reporter_email"}


# --- step 1: parse ----------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO jira.issues VALUES (1)",
        "UPDATE jira.issues SET status = 'Done'",
        "DELETE FROM jira.issues",
        "DROP TABLE jira.issues",
        "CREATE TABLE t (a INT)",
    ],
)
def test_writes_and_ddl_are_rejected(parser, sql):
    """`into=exp.Select` makes every non-SELECT a ParseError before any walk —
    so a write is refused even if the whitelist were removed entirely."""
    with pytest.raises(InvalidQueryError) as raised:
        parser.parse_and_validate(sql, GRANTED)
    assert raised.value.detail == "ParseError"


@pytest.mark.parametrize("sql", ["", "   ", "\n\n"])
def test_an_empty_query_is_rejected_as_such(parser, sql):
    """Named distinctly: "" reaching sqlglot produces a confusing parse error."""
    with pytest.raises(InvalidQueryError) as raised:
        parser.parse_and_validate(sql, GRANTED)
    assert raised.value.detail == "EmptyQuery"


def test_malformed_sql_is_a_400(parser):
    with pytest.raises(InvalidQueryError) as raised:
        parser.parse_and_validate("SELECT FROM WHERE", GRANTED)
    assert raised.value.http == 400
    assert raised.value.error_code == "INVALID_QUERY"


# --- step 3: qualify --------------------------------------------------------


def test_every_column_is_attributed_to_its_owning_alias(parsed):
    """Without this, a predicate cannot be attributed to a source and the whole
    per-source split is impossible. It is the reason qualify is mandatory."""
    assert all(column.table for column in parsed.ast.find_all(exp.Column))


def test_an_unknown_column_is_rejected_by_qualify(parser):
    """Free validation: we pass the catalog's schema, so `validate_qualify_columns`
    catches this without us writing a column check."""
    with pytest.raises(InvalidQueryError) as raised:
        parser.parse_and_validate("SELECT issue.nope FROM jira.issues issue", GRANTED)
    assert raised.value.detail == "UnknownColumn"


# --- step 4: the connector gate ---------------------------------------------


def test_an_unknown_table_is_rejected_by_the_gate(parser):
    """**The v3 Finding 2 assertion.**

    Measured: `qualify()` raises on an unknown COLUMN but silently accepts an
    unknown TABLE — `SELECT s.a FROM slack.msgs s` passes the optimizer
    untouched. This gate is the only thing standing there.
    """
    with pytest.raises(InvalidQueryError) as raised:
        parser.parse_and_validate("SELECT s.a FROM slack.msgs s", GRANTED)
    assert raised.value.detail == "UnknownTable"
    assert "jira.issues" in str(raised.value)


def test_an_ungranted_connector_is_403_not_404(parser):
    """A different refusal from an unknown table, deliberately.

    "This does not exist" and "you may not use this" are different problems with
    different fixes — one is a typo, the other needs an administrator.
    """
    with pytest.raises(ApiError) as raised:
        parser.parse_and_validate(CANONICAL_SQL, granted_connectors={"github"})
    assert raised.value.code is ErrorCode.CONNECTOR_NOT_ENABLED
    assert raised.value.http == 403
    assert "jira" in raised.value.message
    assert raised.value.suggested_action is not None


def test_the_unknown_table_message_never_leaks_the_tenants_grants(parser):
    """The message lists what the CATALOG knows, never what this tenant has.

    `POST /v1/auth/mock-token` mints a token for any tenant string without
    authenticating, so "any caller" means anyone who can reach the port —
    listing a tenant's grants in an error body would be a free enumeration
    oracle over who has connected what.
    """
    with pytest.raises(InvalidQueryError) as raised:
        parser.parse_and_validate("SELECT s.a FROM slack.msgs s", granted_connectors={"github"})
    assert "github.pull_requests" in str(raised.value)
    assert "jira.issues" in str(raised.value)  # in the catalog, though not granted here


def test_no_connector_granted_refuses_before_anything_is_planned(parser):
    with pytest.raises(ApiError) as raised:
        parser.parse_and_validate(CANONICAL_SQL, granted_connectors=frozenset())
    assert raised.value.code is ErrorCode.CONNECTOR_NOT_ENABLED


# --- step 4: extraction -----------------------------------------------------


def test_tables_are_mapped_on_db_and_name_not_catalog(parsed):
    """`github.pull_requests` parses as db='github', name='pull_requests',
    catalog='' — mapping on catalog would find nothing."""
    by_alias = {t.alias: t for t in parsed.tables}
    assert set(by_alias) == {"pr", "issue"}
    assert by_alias["pr"].source.qualified_name == "github.pull_requests"
    assert by_alias["issue"].source.qualified_name == "jira.issues"
    assert by_alias["pr"].connector_type == "github"


def test_projection_is_extracted_in_order(parsed):
    assert parsed.projection == (
        ColumnRef("pr", "title"),
        ColumnRef("pr", "author"),
        ColumnRef("issue", "key"),
        ColumnRef("issue", "status"),
    )


def test_projection_survives_qualifys_alias_wrapping(parser):
    """`qualify` wraps every projection in an `Alias`, so the column is one level
    down. An extractor reading `tree.selects` as columns directly gets nothing."""
    parsed = parser.parse_and_validate(CLS_DEMO_SQL, GRANTED)
    assert ColumnRef("issue", "reporter_email") in parsed.projection


def test_predicates_are_grouped_by_owning_alias(parsed):
    assert {p.column.name for p in parsed.predicates["pr"]} == {"repo", "state"}
    assert {p.column.name for p in parsed.predicates["issue"]} == {"status"}


def test_predicate_values_and_operators_are_extracted(parsed):
    by_column = {p.column.name: p for p in parsed.predicates["pr"]}
    assert by_column["repo"].op == "="
    assert by_column["repo"].value == "ema/core"


def test_the_join_condition_is_not_a_predicate(parsed):
    """Two columns and no literal: residual work for the engine, not a filter a
    source could apply. If it leaked into the per-source predicates, we would
    try to push half a join to GitHub."""
    everything = [p for group in parsed.predicates.values() for p in group]
    assert all(p.value is not None for p in everything)
    assert len(everything) == 3


def test_join_keys_are_extracted(parsed):
    assert parsed.join_keys == ((ColumnRef("pr", "issue_key"), ColumnRef("issue", "key")),)


def test_order_by_carries_its_direction(parsed):
    assert parsed.order_by == ((ColumnRef("issue", "updated"), "DESC"),)


def test_limit_is_an_int(parsed):
    assert parsed.limit == 50


def test_absent_limit_is_none(parser):
    parsed = parser.parse_and_validate(
        "SELECT issue.key FROM jira.issues issue", GRANTED
    )
    assert parsed.limit is None


def test_a_query_with_no_where_has_no_predicates(parser):
    parsed = parser.parse_and_validate("SELECT issue.key FROM jira.issues issue", GRANTED)
    assert parsed.predicates == {}


def test_connectors_and_resources_are_derived(parsed):
    assert parsed.connectors == ("github", "jira")
    assert parsed.resources == ("issues", "pull_requests")


def test_the_original_sql_is_kept_for_the_audit_log(parsed):
    assert parsed.sql == CANONICAL_SQL


# --- the recursive flatten (the Phase 0 spike's headline finding) -----------


def test_flatten_recurses_through_the_paren_that_append_true_introduces():
    """**The spike correction, re-asserted against production code.**

    `tree.where(pred, append=True)` routes through `exp.and_`, which wraps the
    EXISTING where in an `exp.Paren` before AND-ing. A single `.flatten()`
    prunes at that paren and yields the whole nested AND as one leaf — so after
    stage 3 injects RLS, the caller's three predicates never separate and
    NOTHING is pushable. The query still returns correct rows, which is what
    makes this bug invisible without a test like this one.
    """
    tree = sqlglot.parse_one(
        "SELECT issue.key FROM jira.issues issue "
        "WHERE issue.status = 'In Progress' AND issue.project = 'SUP'",
        read="duckdb",
    )
    tree = tree.where(
        exp.EQ(this=exp.column("assignee", "issue"), expression=exp.Literal.string("alice")),
        append=True,
    )

    leaves = list(flatten_conjunction(tree.args["where"].this))
    assert len(leaves) == 3, "the paren was not unnested — nothing would be pushable"
    assert {leaf.this.name for leaf in leaves} == {"status", "project", "assignee"}

    # And the naive version really does fail, so the assertion above is earned.
    naive = list(tree.args["where"].this.flatten())
    assert len(naive) < 3
