"""Stage 3 — RLS, CLS, deny-overrides and default-deny.

**The assertions here are the design doc's central claim.** They are written
against the *plan*, not against a row count, on purpose: a post-filtering
implementation returns exactly the same rows and would pass every count-based
test in this repository. What it could not do is put `assignee = 'alice'` into
the tree before the planner splits it, which is what these assert.
"""

import pytest
from sqlglot import exp

from src.entitlement.engine import REDACTED, EntitlementEngine
from src.entitlement.predicate import PolicyError
from src.models.errors import ApiError, ErrorCode
from tests.unit.conftest import CANONICAL_SQL, CLS_DEMO_SQL, GRANTED, persona

# --- policy fixtures, mirroring config/policies.yaml ------------------------

RLS_ASSIGNEE = {
    "policy_id": "rls-jira-assignee",
    "connector_type": "jira",
    "resource": "issues",
    "kind": "RLS",
    "applies_to": "support",
    "effect": "allow",
    "predicate": {"op": "eq", "col": "assignee", "value": ":user"},
    "column_name": None,
    "mask": None,
}

CLS_REPORTER = {
    "policy_id": "cls-jira-reporter",
    "connector_type": "jira",
    "resource": "issues",
    "kind": "CLS",
    "applies_to": "support",
    "effect": "allow",
    "predicate": None,
    "column_name": "reporter_email",
    "mask": "hash",
}

DENY_JIRA = {
    "policy_id": "deny-jira-issues-auditor",
    "connector_type": "jira",
    "resource": "issues",
    "kind": "RLS",
    "applies_to": "auditor",
    "effect": "deny",
    "predicate": None,
    "column_name": None,
    "mask": None,
}


class FakePolicyStore:
    """Returns a fixed policy list, and records what it was asked for.

    Not a MagicMock: one of the tests below is specifically about the *arguments*
    of the lookup, and a mock would happily return policies for whatever it was
    handed — hiding exactly the bug where a query asks for another tenant's rules.
    """

    def __init__(self, policies) -> None:
        self.policies = list(policies)
        self.calls: list[tuple] = []

    def get_policies(self, tenant_id, connectors, resources):
        self.calls.append((tenant_id, tuple(connectors), tuple(resources)))
        return self.policies


def compile_plan(parser, policies, user, sql=CANONICAL_SQL):
    parsed = parser.parse_and_validate(sql, GRANTED)
    return EntitlementEngine(FakePolicyStore(policies)).compile(parsed, user)


def jira_predicates(plan):
    """Every `issue.<col> <op> <literal>` currently in the tree's WHERE."""
    from src.sqlparse.parser import flatten_conjunction

    where = plan.ast.args.get("where")
    if where is None:
        return {}
    found = {}
    for leaf in flatten_conjunction(where.this):
        columns = list(leaf.find_all(exp.Column))
        if len(columns) == 1 and columns[0].table == "issue":
            found[columns[0].name] = leaf.expression.this
    return found


# --- GATE: test_rls_ast -----------------------------------------------------


def test_rls_ast(parser, alice):
    """**Non-negotiable #1.** The RLS rule is in the plan, as a literal.

    The AST carries `assignee = 'alice'`. `assignee = currentUser()` is only how
    a LIVE Jira adapter would render that predicate into JQL (design-doc §3.2) —
    a rendering detail of one connector, not the plan. So the assertion is on the
    predicate, never on a JQL string.
    """
    plan = compile_plan(parser, [RLS_ASSIGNEE, CLS_REPORTER], alice)
    assert jira_predicates(plan)["assignee"] == "alice"


def test_rls_binds_the_caller_not_a_constant(parser):
    """Same query text, different plan. This is hard part 1 in one assertion."""
    for user_id in ("alice", "bob", "carol"):
        plan = compile_plan(parser, [RLS_ASSIGNEE], persona(user_id))
        assert jira_predicates(plan)["assignee"] == user_id


def test_rls_is_and_ed_into_the_existing_where_not_replacing_it(parser, alice):
    """The caller's own filters must survive. If `append=True` were dropped, the
    query would silently return In-Progress issues from every status."""
    plan = compile_plan(parser, [RLS_ASSIGNEE], alice)
    found = jira_predicates(plan)
    assert found["status"] == "In Progress"
    assert found["assignee"] == "alice"


def test_rls_predicates_stay_separable_after_injection(parser, alice):
    """**The spike finding, at the level that matters.**

    `tree.where(append=True)` parenthesizes. If the planner's flatten did not
    recurse through `.unnest()`, the three predicates would arrive as ONE leaf
    and nothing would be pushable — the query would still return correct rows,
    entirely by post-filtering. This asserts they separate.
    """
    plan = compile_plan(parser, [RLS_ASSIGNEE], alice)
    from src.sqlparse.parser import flatten_conjunction

    leaves = list(flatten_conjunction(plan.ast.args["where"].this))
    assert len(leaves) == 4  # repo, state, status + the injected assignee


def test_no_sql_string_is_ever_concatenated(parser, alice):
    """The policy is an AST end to end; nothing renders SQL text until sqlglot
    emits the final statement."""
    plan = compile_plan(parser, [RLS_ASSIGNEE], alice)
    injected = [
        leaf
        for leaf in plan.ast.find_all(exp.EQ)
        if isinstance(leaf.this, exp.Column) and leaf.this.name == "assignee"
    ]
    assert len(injected) == 1
    assert isinstance(injected[0].expression, exp.Literal)


def test_the_policy_lookup_is_scoped_to_this_caller(parser, alice):
    store = FakePolicyStore([RLS_ASSIGNEE])
    parsed = parser.parse_and_validate(CANONICAL_SQL, GRANTED)
    EntitlementEngine(store).compile(parsed, alice)
    tenant, connectors, resources = store.calls[0]
    assert tenant == "tenant_acme"
    assert connectors == ("github", "jira")
    assert resources == ("issues", "pull_requests")


def test_entitlement_scope_is_the_resolved_binding(parser, alice):
    """ADR-025: this becomes a mandatory cache-key segment, which is what stops
    alice's cached Jira rows ever being served to bob."""
    assert compile_plan(parser, [RLS_ASSIGNEE], alice).entitlement_scope == "alice"


def test_an_rls_allow_with_no_predicate_raises(parser, alice):
    """It would allow every row while looking like a restriction — the worst
    silent failure an entitlement store can have."""
    broken = {**RLS_ASSIGNEE, "predicate": None}
    with pytest.raises(PolicyError):
        compile_plan(parser, [broken], alice)


def test_a_policy_for_an_unreferenced_resource_is_ignored(parser, alice):
    """A Slack rule must not affect a GitHub/Jira query."""
    other = {**RLS_ASSIGNEE, "connector_type": "slack", "resource": "messages"}
    plan = compile_plan(parser, [other], alice)
    assert "assignee" not in jira_predicates(plan)


# --- GATE: test_cls_mask ----------------------------------------------------


def test_cls_mask(parser, alice):
    """**The mask is in the projection AST, expressed exactly once.**"""
    plan = compile_plan(parser, [RLS_ASSIGNEE, CLS_REPORTER], alice, sql=CLS_DEMO_SQL)
    assert plan.masks == {"jira.issues.reporter_email": "hash"}
    rendered = plan.ast.sql(dialect="duckdb")
    assert "MD5" in rendered


def test_cls_keeps_the_output_column_name_stable(parser, alice):
    """Without `exp.alias_` the column comes back named `md5(reporter_email)`,
    which changes the response shape and breaks every caller."""
    plan = compile_plan(parser, [CLS_REPORTER], alice, sql=CLS_DEMO_SQL)
    assert [p.output_name for p in plan.ast.selects] == [
        "title", "author", "key", "status", "reporter_email",
    ]


def test_cls_on_an_unprojected_column_is_not_an_error(parser, alice):
    """The CANONICAL query masks `reporter_email` and never projects it. Nothing
    to rewrite is the correct outcome — and the column is not reported as masked,
    because no output column of that name exists to mark."""
    plan = compile_plan(parser, [CLS_REPORTER], alice, sql=CANONICAL_SQL)
    assert plan.masks == {}


@pytest.mark.parametrize(
    ("mask", "expected"),
    [("hash", "MD5"), ("null", "NULL"), ("redact", REDACTED)],
)
def test_every_mask_kind_rewrites_the_projection(parser, alice, mask, expected):
    plan = compile_plan(parser, [{**CLS_REPORTER, "mask": mask}], alice, sql=CLS_DEMO_SQL)
    assert expected in plan.ast.sql(dialect="duckdb")
    assert plan.masks == {"jira.issues.reporter_email": mask}


def test_drop_removes_the_column_from_the_output_entirely(parser, alice):
    plan = compile_plan(parser, [{**CLS_REPORTER, "mask": "drop"}], alice, sql=CLS_DEMO_SQL)
    assert [p.output_name for p in plan.ast.selects] == ["title", "author", "key", "status"]
    assert plan.dropped_columns == frozenset({"issue.reporter_email"})


def test_the_raw_column_is_no_longer_selectable_after_a_hash_mask(parser, alice):
    """The masked projection must not ALSO carry the bare column — two
    projections of the same name would let the raw value back out."""
    plan = compile_plan(parser, [CLS_REPORTER], alice, sql=CLS_DEMO_SQL)
    bare = [
        p for p in plan.ast.selects
        if p.output_name == "reporter_email" and isinstance(p.this, exp.Column)
    ]
    assert bare == []


def test_an_unknown_mask_kind_raises(parser, alice):
    with pytest.raises(PolicyError):
        compile_plan(parser, [{**CLS_REPORTER, "mask": "encrypt"}], alice, sql=CLS_DEMO_SQL)


def test_a_cls_policy_with_no_column_raises(parser, alice):
    with pytest.raises(PolicyError):
        compile_plan(parser, [{**CLS_REPORTER, "column_name": None}], alice, sql=CLS_DEMO_SQL)


def test_the_mask_runs_in_the_final_select_so_it_is_post_join(parser, alice):
    """The join-key masking rule (design-doc §3.2), satisfied structurally.

    The mask lives in the projection, and the projection is executed by the
    final SELECT — which is post-join by construction. There is no second code
    path that could apply it before the join and break the join.
    """
    plan = compile_plan(parser, [CLS_REPORTER], alice, sql=CLS_DEMO_SQL)
    masked = [p for p in plan.ast.selects if p.output_name == "reporter_email"][0]
    assert "MD5" in masked.sql(dialect="duckdb")

    # ...and NOWHERE else. Every other clause is rendered and checked, so a
    # future implementation that also masked in the FROM, the JOIN condition or
    # the WHERE — any of which would run pre-join and break the join — fails
    # here. (sqlglot 30 spells the key `from_`, not `from`.)
    for clause in ("from_", "joins", "where", "order"):
        node = plan.ast.args.get(clause)
        rendered = (
            " ".join(n.sql(dialect="duckdb") for n in node)
            if isinstance(node, list)
            else (node.sql(dialect="duckdb") if node is not None else "")
        )
        assert "MD5" not in rendered, clause


# --- GATE: test_deny_overrides (unit half — ADR-031) ------------------------


def test_deny_overrides(parser):
    """**Deny beats a matching allow.**

    The caller holds BOTH roles, so both policies match. The allow must not win:
    a broadly-scoped allow silently defeating a narrowly-targeted deny is the
    exact failure mode deny-overrides exists to rule out.
    """
    both = persona("alice", "support", "auditor")
    with pytest.raises(ApiError) as raised:
        compile_plan(parser, [RLS_ASSIGNEE, CLS_REPORTER, DENY_JIRA], both)
    assert raised.value.code is ErrorCode.ENTITLEMENT_DENIED
    assert raised.value.http == 403
    assert "jira.issues" in raised.value.message


def test_deny_is_403_and_not_an_empty_result(parser):
    """The other half of ADR-031's split. Saying "zero rows" to someone who
    asked for something they are forbidden to see would be a lie, and it would
    leave `ENTITLEMENT_DENIED` declared-but-unreachable in the six-code
    vocabulary."""
    with pytest.raises(ApiError):
        compile_plan(parser, [DENY_JIRA], persona("alice", "auditor"))


def test_a_deny_on_an_unreferenced_resource_does_not_fire(parser, alice):
    """A deny on Slack must not block a GitHub/Jira query."""
    elsewhere = {**DENY_JIRA, "connector_type": "slack", "resource": "messages"}
    plan = compile_plan(parser, [RLS_ASSIGNEE, elsewhere], alice)
    assert plan.denied_resources == frozenset()


def test_deny_is_evaluated_before_any_rewriting(parser):
    """A refused query must not carry partially-applied entitlement state."""
    both = persona("alice", "support", "auditor")
    parsed = parser.parse_and_validate(CLS_DEMO_SQL, GRANTED)
    before = parsed.ast.sql(dialect="duckdb")
    with pytest.raises(ApiError):
        EntitlementEngine(FakePolicyStore([RLS_ASSIGNEE, CLS_REPORTER, DENY_JIRA])).compile(
            parsed, both
        )
    assert parsed.ast.sql(dialect="duckdb") == before


# --- default-deny (the OTHER outcome) ---------------------------------------


def test_default_deny_yields_empty_not_403(parser):
    """A governed resource with no matching allow returns nothing, successfully.

    `contractor` matches neither the support allow nor the auditor deny. The
    caller has no grant for this slice, and an empty answer is the correct
    answer — not 403, and emphatically not open.
    """
    plan = compile_plan(parser, [RLS_ASSIGNEE, DENY_JIRA], persona("dave", "contractor"))

    # The tree could not return a row even if every later stage were wrong...
    assert "FALSE" in plan.ast.sql(dialect="duckdb").upper()

    # ...and the resource is named, so the planner can skip fetching it
    # entirely. The predicate alone is not enough: `FALSE` is a constant with no
    # owning table, so it cannot be pushed, and the source would be fetched in
    # full and then discarded — post-filtering by another route (review finding).
    assert plan.denied_resources == frozenset({"jira.issues"})
    assert plan.empty_aliases == frozenset({"issue"})

    # It is NOT an explicit deny: no 403 was raised, and github is untouched.
    assert "github.pull_requests" not in plan.denied_resources


def test_an_ungoverned_resource_stays_readable(parser, alice):
    """`github.pull_requests` is named by no policy at all.

    Under a naive "no allow means deny" reading it would yield empty for
    everyone, including alice — and the headline demo would return zero rows.
    Access to a resource is default-deny at the GRANT (layer L3); policies refine
    rows and columns within an already-granted resource (layer L4).
    """
    plan = compile_plan(parser, [RLS_ASSIGNEE], alice)
    assert "FALSE" not in plan.ast.sql(dialect="duckdb").upper()


def test_no_policies_at_all_leaves_the_query_untouched(parser, alice):
    parsed = parser.parse_and_validate(CANONICAL_SQL, GRANTED)
    before = parsed.ast.sql(dialect="duckdb")
    plan = EntitlementEngine(FakePolicyStore([])).compile(parsed, alice)
    assert plan.ast.sql(dialect="duckdb") == before
    assert plan.masks == {}


# --- the role gate the engine enforces itself (defence in depth) -----------


def test_a_policy_for_another_role_does_not_apply(parser):
    """**The fail-open guard.**

    `get_policies` returns EVERY policy on the referenced resources, for every
    role — it has to, or default-deny cannot tell "ungoverned" from "governed
    and you are not on the list". So this check is not defence in depth, it is
    the only role filter there is. Without it every `allow` matches every
    caller: entitlement fails OPEN and the RLS predicate is never narrowed.
    """
    plan = compile_plan(parser, [RLS_ASSIGNEE], persona("dave", "contractor"))
    assert "assignee" not in jira_predicates(plan)


def test_a_deny_for_another_role_does_not_fire(parser, alice):
    """The same guard in the other direction — an auditor deny must not block a
    support user. Fail-closed is safer than fail-open, but it is still wrong."""
    plan = compile_plan(parser, [RLS_ASSIGNEE, DENY_JIRA], alice)
    assert plan.denied_resources == frozenset()
    assert jira_predicates(plan)["assignee"] == "alice"


def test_a_wildcard_policy_applies_to_everyone(parser):
    """`applies_to = '*'` is the documented tenant-wide form."""
    everyone = {**RLS_ASSIGNEE, "applies_to": "*"}
    plan = compile_plan(parser, [everyone], persona("dave", "contractor"))
    assert jira_predicates(plan)["assignee"] == "dave"


def test_a_cls_mask_for_another_role_does_not_apply(parser):
    plan = compile_plan(
        parser, [CLS_REPORTER], persona("dave", "contractor"), sql=CLS_DEMO_SQL
    )
    assert plan.masks == {}
