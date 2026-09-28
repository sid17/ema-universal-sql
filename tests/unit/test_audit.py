"""The access trail, and the PII it must not create.

The headline test here is `test_a_literal_email_never_reaches_the_audit_row`:
without normalization, the CLS mask is defeated by the logging layer — silently,
in a table nobody reads until an audit.
"""

import pytest
import sqlglot

from src.entitlement.engine import EntitlementEngine
from src.governance.audit import AuditLogger, AuditRecord, normalize_sql
from tests.unit.conftest import CANONICAL_SQL, CLS_DEMO_SQL, GRANTED, persona
from tests.unit.test_entitlement import CLS_REPORTER, RLS_ASSIGNEE, FakePolicyStore


class FakeConnection:
    def __init__(self, recorder, fail=False) -> None:
        self._recorder = recorder
        self._fail = fail

    def execute(self, sql, params):
        if self._fail:
            raise RuntimeError("audit table is unreachable")
        self._recorder.append((sql, params))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakePool:
    def __init__(self, fail=False) -> None:
        self.writes: list = []
        self._fail = fail

    def connection(self):
        return FakeConnection(self.writes, self._fail)


def record(query_text="SELECT 1", rows=3) -> AuditRecord:
    return AuditRecord(
        tenant_id="tenant_acme",
        user_id="alice",
        query_text=query_text,
        sources_accessed=("github", "jira"),
        rows_returned=rows,
        trace_id="abc123",
        execution_ms=42,
    )


# --- GATE: normalization ----------------------------------------------------


def test_a_literal_email_never_reaches_the_audit_row():
    """**The reason this module exists.**

    A query filtering on the very column CLS masks would otherwise write that
    address into `audit_logs` in plaintext, forever. The masking layer must not
    be defeated by the logging layer.
    """
    tree = sqlglot.parse_one(
        "SELECT issue.key FROM jira.issues issue "
        "WHERE issue.reporter_email = 'dana@acme.com'",
        read="duckdb",
    )
    normalized = normalize_sql(tree)
    assert "dana@acme.com" not in normalized
    assert "?" in normalized
    # The SHAPE survives — which is what an access trail is for.
    assert "reporter_email" in normalized


def test_every_literal_kind_is_replaced():
    tree = sqlglot.parse_one(
        "SELECT issue.key FROM jira.issues issue "
        "WHERE issue.project = 'SUP' AND issue.updated > '2026-01-01'",
        read="duckdb",
    )
    normalized = normalize_sql(tree)
    assert "SUP" not in normalized
    assert "2026-01-01" not in normalized


def test_a_numeric_literal_is_replaced_too():
    """A regex hunting for quoted strings would miss this one."""
    tree = sqlglot.parse_one(
        "SELECT pr.title FROM github.pull_requests pr WHERE pr.number = 12345",
        read="duckdb",
    )
    assert "12345" not in normalize_sql(tree)


def test_an_escaped_quote_is_handled():
    """The case that makes AST normalization non-negotiable: a regex over SQL
    text has to reason about escaping, and gets it wrong."""
    tree = sqlglot.parse_one(
        "SELECT issue.key FROM jira.issues issue WHERE issue.assignee = 'o''brien'",
        read="duckdb",
    )
    assert "brien" not in normalize_sql(tree)


def test_normalization_does_not_mutate_the_tree_being_executed():
    """The caller's tree is still in flight; rewriting it in place would swap
    the real predicates out from under the running query."""
    tree = sqlglot.parse_one(
        "SELECT issue.key FROM jira.issues issue WHERE issue.assignee = 'alice'",
        read="duckdb",
    )
    before = tree.sql(dialect="duckdb")
    normalize_sql(tree)
    assert tree.sql(dialect="duckdb") == before
    assert "alice" in tree.sql(dialect="duckdb")


def test_two_queries_of_the_same_shape_normalize_identically():
    """A side benefit worth having: "which query shapes are hot" becomes
    answerable from the same column, without storing anyone's data."""
    def norm(user):
        return normalize_sql(
            sqlglot.parse_one(
                f"SELECT issue.key FROM jira.issues issue WHERE issue.assignee = '{user}'",
                read="duckdb",
            )
        )

    assert norm("alice") == norm("bob")


def test_the_injected_rls_literal_is_normalized_too(parser):
    """The RLS predicate carries the caller's identity as a literal. It is not
    secret, but it should not be stored twice — `user_id` is its own column."""
    parsed = parser.parse_and_validate(CLS_DEMO_SQL, GRANTED)
    plan = EntitlementEngine(FakePolicyStore([RLS_ASSIGNEE, CLS_REPORTER])).compile(
        parsed, persona("alice")
    )
    normalized = normalize_sql(plan.ast)
    assert "'alice'" not in normalized


def test_the_canonical_query_normalizes_to_a_readable_shape(parser):
    parsed = parser.parse_and_validate(CANONICAL_SQL, GRANTED)
    normalized = normalize_sql(parsed.ast)
    for fragment in ("pull_requests", "issues", "JOIN", "ORDER BY"):
        assert fragment in normalized
    for literal in ("ema/core", "open", "In Progress"):
        assert literal not in normalized


# --- the write --------------------------------------------------------------


def test_one_row_per_query():
    pool = FakePool()
    AuditLogger(pool).write(record())
    assert len(pool.writes) == 1
    sql, params = pool.writes[0]
    assert "INSERT INTO audit_logs" in sql
    assert params[0] == "tenant_acme"
    assert params[1] == "alice"
    assert params[3] == ["github", "jira"]
    assert params[4] == 3
    assert params[5] == "abc123"


def test_parameters_are_bound_never_formatted_in():
    """An audit row carries caller-influenced text. Formatting it into the
    statement would make the compliance trail an injection surface."""
    pool = FakePool()
    AuditLogger(pool).write(record(query_text="'; DROP TABLE audit_logs; --"))
    sql, params = pool.writes[0]
    assert "DROP TABLE" not in sql
    assert params[2] == "'; DROP TABLE audit_logs; --"


def test_a_failed_write_does_not_fail_the_query(caplog):
    """**The one deliberate exception to "log or throw".**

    The query already succeeded and the rows are already correct and entitled.
    Failing the response because the audit INSERT failed turns a logging outage
    into a customer-facing one — and callers retry, adding load to the thing
    that is already broken.
    """
    pool = FakePool(fail=True)
    with caplog.at_level("ERROR"):
        AuditLogger(pool).write(record())
    assert "audit log write failed" in caplog.text


def test_a_failed_write_is_logged_loudly_not_swallowed(caplog):
    """Not silent: ERROR, with a traceback and enough context to find the gap."""
    with caplog.at_level("ERROR"):
        AuditLogger(FakePool(fail=True)).write(record())
    assert caplog.records
    assert caplog.records[0].levelname == "ERROR"
    assert caplog.records[0].exc_info is not None


@pytest.mark.parametrize("rows", [0, 1, 50])
def test_zero_rows_is_still_audited(rows):
    """An access that returned nothing is still an access. Skipping the row for
    empty results would leave the most interesting probes untracked."""
    pool = FakePool()
    AuditLogger(pool).write(record(rows=rows))
    assert pool.writes[0][1][4] == rows
