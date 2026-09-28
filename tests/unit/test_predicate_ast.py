"""The JSONB predicate AST compiler.

The failure modes matter more than the happy path here: every one of them is a
way an entitlement rule could silently stop restricting anything.
"""

import pytest
from sqlglot import exp

from src.entitlement.predicate import PolicyError, bind, compile_predicate

BINDINGS = {"user": "alice"}


def _sql(node) -> str:
    return node.sql(dialect="duckdb")


# --- the canonical rule -----------------------------------------------------


def test_the_canonical_rls_rule_compiles():
    """`config/policies.yaml`'s `rls-jira-assignee`, verbatim."""
    node = compile_predicate(
        {"op": "eq", "col": "assignee", "value": ":user"}, "issue", BINDINGS
    )
    assert isinstance(node, exp.EQ)
    assert _sql(node) == "issue.assignee = 'alice'"


def test_the_compiled_column_is_already_attributed():
    """Attribution is what lets the planner push this to the right source.

    An unattributed column would be owned by nothing, so the per-source split
    would classify the RLS predicate as multi-table residual — and it would be
    re-applied in the engine instead of pushed down, which is post-filtering by
    another name.
    """
    node = compile_predicate({"op": "eq", "col": "assignee", "value": ":user"}, "issue", BINDINGS)
    assert {c.table for c in node.find_all(exp.Column)} == {"issue"}


@pytest.mark.parametrize(
    ("op", "rendered"),
    [
        ("eq", "issue.updated = '2026-09-01'"),
        ("ne", "issue.updated <> '2026-09-01'"),
        ("gt", "issue.updated > '2026-09-01'"),
        ("gte", "issue.updated >= '2026-09-01'"),
        ("lt", "issue.updated < '2026-09-01'"),
        ("lte", "issue.updated <= '2026-09-01'"),
    ],
)
def test_every_comparison_operator(op, rendered):
    node = compile_predicate({"op": op, "col": "updated", "value": "2026-09-01"}, "issue", {})
    assert _sql(node) == rendered


def test_operators_are_case_insensitive():
    assert _sql(compile_predicate({"op": "EQ", "col": "a", "value": 1}, "t", {})) == "t.a = 1"


# --- nesting ----------------------------------------------------------------


def test_and_nesting():
    node = compile_predicate(
        {
            "op": "and",
            "args": [
                {"op": "eq", "col": "assignee", "value": ":user"},
                {"op": "eq", "col": "project", "value": "SUP"},
            ],
        },
        "issue",
        BINDINGS,
    )
    assert isinstance(node, exp.And)
    assert "issue.assignee = 'alice'" in _sql(node)
    assert "issue.project = 'SUP'" in _sql(node)


def test_or_nesting():
    node = compile_predicate(
        {
            "op": "or",
            "args": [
                {"op": "eq", "col": "project", "value": "SUP"},
                {"op": "eq", "col": "project", "value": "OPS"},
            ],
        },
        "issue",
        {},
    )
    assert isinstance(node, exp.Or)


# --- value types ------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "rendered"),
    [("SUP", "t.a = 'SUP'"), (42, "t.a = 42"), (1.5, "t.a = 1.5"), (True, "t.a = TRUE")],
)
def test_value_types(value, rendered):
    assert _sql(compile_predicate({"op": "eq", "col": "a", "value": value}, "t", {})) == rendered


def test_a_quote_in_a_value_is_escaped_by_sqlglot_not_by_us():
    """The injection property, stated directly.

    A string-concatenating policy engine would produce broken — or hostile — SQL
    here. Because the value becomes an `exp.Literal`, quoting is sqlglot's job
    and happens once, at render time.
    """
    node = compile_predicate({"op": "eq", "col": "a", "value": "o'brien"}, "t", {})
    assert _sql(node) == "t.a = 'o''brien'"


def test_a_value_that_looks_like_sql_stays_a_literal():
    hostile = "' OR 1=1 --"
    node = compile_predicate({"op": "eq", "col": "assignee", "value": hostile}, "issue", {})
    assert isinstance(node.expression, exp.Literal)
    assert _sql(node) == "issue.assignee = ''' OR 1=1 --'"


# --- failure modes: every one of these must RAISE, never degrade ------------


def test_an_unbound_parameter_raises():
    """**The important one.**

    If `:user` fell through as the literal string ":user", the predicate would
    compile to `assignee = ':user'` — matching nothing, returning an empty
    result, and looking exactly like a user who legitimately has no rows. An
    entitlement bug that presents as a plausible empty answer is the hardest
    kind to ever notice.
    """
    with pytest.raises(PolicyError) as raised:
        compile_predicate({"op": "eq", "col": "assignee", "value": ":user"}, "issue", {})
    assert ":user" in str(raised.value)


def test_an_unknown_parameter_raises_even_when_others_are_bound():
    with pytest.raises(PolicyError):
        compile_predicate({"op": "eq", "col": "a", "value": ":tenant"}, "t", BINDINGS)


def test_an_unknown_operator_raises_and_lists_what_is_known():
    with pytest.raises(PolicyError) as raised:
        compile_predicate({"op": "like", "col": "a", "value": "x%"}, "t", {})
    assert "like" in str(raised.value)
    assert "eq" in str(raised.value)


@pytest.mark.parametrize(
    "node",
    [
        {},                                        # no op
        {"op": 7, "col": "a", "value": 1},         # non-string op
        {"op": "eq", "value": 1},                  # no column
        {"op": "eq", "col": "", "value": 1},       # empty column
        {"op": "eq", "col": "a"},                  # no value key at all
        {"op": "and", "args": []},                 # empty conjunction
        {"op": "and", "args": [{"op": "eq", "col": "a", "value": 1}]},  # one-armed
        "not a mapping",
    ],
)
def test_malformed_policy_nodes_raise(node):
    with pytest.raises(PolicyError):
        compile_predicate(node, "t", BINDINGS)


def test_a_none_value_raises_rather_than_becoming_null():
    """`a = NULL` is never true in SQL, so a policy that compiled to it would
    silently deny everything — a fail-closed bug, but still a silent one."""
    with pytest.raises(PolicyError):
        compile_predicate({"op": "eq", "col": "a", "value": None}, "t", {})


def test_an_explicit_value_key_of_none_is_distinguished_from_a_missing_one():
    """Both raise, but for different reasons — `"value" in node` is the check,
    not `node.get("value")`, or a legitimate falsy value like 0 would be read as
    absent."""
    assert _sql(compile_predicate({"op": "eq", "col": "a", "value": 0}, "t", {})) == "t.a = 0"


# --- bind() -----------------------------------------------------------------


def test_bind_passes_plain_values_through():
    assert bind("ema/core", BINDINGS) == "ema/core"
    assert bind(42, BINDINGS) == 42


def test_bind_resolves_a_parameter():
    assert bind(":user", BINDINGS) == "alice"
