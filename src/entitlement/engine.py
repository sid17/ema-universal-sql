"""Stage 3 — the crux: entitlement compiled into the plan.

This is the module the design document rests on. `02-DEFINITION-OF-DONE.md` §4
non-negotiable #1: *"Entitlement is compiled into the plan and pushed down —
never post-filtered. The moment we fetch rows we aren't entitled to and drop them
in Python, the central claim of the design doc is false."*

So there is no filtering here. There is only **rewriting**:

- **RLS** AND-s a compiled predicate into the tree's ``WHERE``. Stage 4 then
  splits *all* predicates per source, RLS included, so the rule rides pushdown to
  Jira and the forbidden rows are never requested. Measured: the Jira fetch
  shrinks 9 → 3 → 1 across the three personas.
- **CLS** replaces a projection node in place — ``MD5(issue.reporter_email) AS
  reporter_email``. The mask is expressed exactly once, in the AST, and stage 5
  merely *executes* it. Because that execution is the final ``SELECT``, it is
  post-join by construction, which satisfies the join-key masking rule
  (design-doc §3.2) with no second code path.

**Two refusals, and they are different on purpose** (ADR-031, HLD §9):

===============================  ==========================================
An explicit ``effect='deny'``    ``403 ENTITLEMENT_DENIED``. The caller asked
matching a referenced resource   for something they are forbidden to see;
                                 "zero rows" would be a lie.
No matching ``allow`` for the    **empty**, not 403 and not open. The caller
caller's roles (default-deny)    simply has no grant for this slice, and an
                                 empty answer is the correct answer.
===============================  ==========================================

Deny always overrides a matching allow.

**Why no ACL library.** The DI shape is borrowed from ``fastapi-permissions``
(research Card 4) — a ``configure_*(get_current_user)`` factory — but not its
core flow. Object-level ACL libraries *fetch the row, then check it*. That is
precisely the post-filtering this invariant bans.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlglot import exp

from src.entitlement.predicate import PolicyError, compile_predicate
from src.models.context import UserContext
from src.models.errors import ApiError, ErrorCode
from src.sqlparse.parser import ParsedQuery

logger = logging.getLogger(__name__)

#: ``applies_to`` value that matches every role.
ANY_ROLE = "*"


def applies_to(policy: Mapping[str, Any], roles: Iterable[str]) -> bool:
    """Does this policy bind to one of the caller's roles?

    **This is the only place roles are matched.** ``get_policies`` deliberately
    returns every policy on the referenced resources regardless of role, because
    default-deny has to be able to see a policy that does *not* apply to the
    caller — that is precisely what "this resource is governed, and you are not
    on the list" means. A role-filtered read hands a caller whose roles match
    nothing an empty policy list, from which the only possible conclusion is
    "ungoverned", and the query then runs unrestricted. Fail-open, silently, for
    exactly the caller who should see least.

    Keeping the match here also keeps it reviewable: an authorization rule
    expressed half in a SQL ``WHERE`` clause and half in Python is one nobody
    can read in full.
    """
    target = policy.get("applies_to")
    return target == ANY_ROLE or target in set(roles)


#: ``mask`` enum (HLD §9). ``hash`` is the canonical choice: the column still
#: appears with a stable value, so a caller can group by it without ever
#: learning the address — which ``drop`` cannot offer and ``null`` destroys.
MASK_KINDS = ("null", "hash", "redact", "drop")

#: What ``redact`` renders. Four bullets, not the real length: a mask that
#: preserved length would leak it.
REDACTED = "••••"


@dataclass(frozen=True)
class EntitledPlan:
    """The parsed query with entitlement compiled in."""

    parsed: ParsedQuery
    """``parsed.ast`` now carries the RLS predicates and the CLS rewrites."""

    masks: Mapping[str, str] = field(default_factory=dict)
    """``"jira.issues.reporter_email" -> "hash"``. Feeds ``ColumnMeta.masked``."""

    denied_resources: frozenset[str] = frozenset()
    """Resources that must **yield nothing**, by qualified name.

    This is the **default-deny** set — a governed resource with no matching
    allow — which is what the phase file's ``# yield empty for these`` comment
    always meant. An *explicit* ``deny`` never reaches this field: it raises 403
    from :meth:`EntitlementEngine.compile`, so no plan is produced at all.

    The planner turns each entry into a source it does not fetch. That matters:
    an unsatisfiable predicate alone would make the *result* empty while still
    pulling every row out of the source first — which is the post-filtering
    non-negotiable #1 exists to forbid.
    """

    empty_aliases: frozenset[str] = frozenset()
    """The table aliases of :attr:`denied_resources`, for the planner."""

    dropped_columns: frozenset[str] = frozenset()
    """Columns a ``drop`` mask removed — they must not be fetched either."""

    entitlement_scope: str = ""
    """The resolved RLS binding, and a **mandatory** cache-key segment (ADR-025).

    For the canonical ``assignee = :user`` rule this is the ``user_id``, so the
    connector cache is per-user and alice's rows can never be served to bob.
    Set-valued rules would resolve against a small ``entitlement_scope`` table;
    stubbed to ``user_id`` here, which is the honest prototype answer.
    """

    @property
    def ast(self) -> exp.Select:
        return self.parsed.ast


class EntitlementEngine:
    """Compiles a tenant's policies into a parsed query."""

    def __init__(self, repository: Any) -> None:
        self._repository = repository

    def compile(self, parsed: ParsedQuery, user: UserContext) -> EntitledPlan:
        """Rewrite ``parsed`` so it can only ever return entitled rows."""
        # Every policy on these resources, for ANY role — see `get_policies`.
        # Role matching happens below, in `applies_to`, because default-deny has
        # to be able to see the policies that do NOT match this caller.
        policies = self._repository.get_policies(
            user.tenant_id, parsed.connectors, parsed.resources
        )
        tree = parsed.ast

        denied = self._denied_resources(policies, parsed, user.roles)
        if denied:
            # Refuse before any rewriting: there is no point compiling masks for
            # a query that is about to be refused, and refusing early keeps the
            # 403 free of any partially-applied state.
            raise self._denied_error(denied)

        bindings = {"user": user.user_id}
        allowed_resources = self._apply_rls(tree, policies, parsed, bindings, user.roles)
        masks, dropped = self._apply_cls(tree, policies, parsed, user.roles)
        empty = self._apply_default_deny(tree, parsed, policies, allowed_resources)

        return EntitledPlan(
            parsed=parsed,
            masks=masks,
            denied_resources=frozenset(name for name, _ in empty),
            empty_aliases=frozenset(alias for _, alias in empty),
            dropped_columns=frozenset(dropped),
            entitlement_scope=user.user_id,
        )

    # -- deny --------------------------------------------------------------

    @staticmethod
    def _denied_resources(
        policies: Sequence[Mapping[str, Any]], parsed: ParsedQuery, roles: Iterable[str]
    ) -> list[str]:
        """Resources an explicit ``deny`` matched.

        Deny-overrides: this runs before allows are considered at all, so a
        caller holding both a matching allow and a matching deny is denied. The
        opposite order would let a broadly-scoped allow silently defeat a
        narrowly-targeted deny, which is the failure mode deny-overrides exists
        to rule out.
        """
        referenced = {table.source.qualified_name for table in parsed.tables}
        denied = []
        for policy in policies:
            if policy.get("effect") != "deny" or not applies_to(policy, roles):
                continue
            name = f"{policy['connector_type']}.{policy['resource']}"
            if name in referenced:
                denied.append(name)
        return sorted(set(denied))

    @staticmethod
    def _denied_error(denied: Iterable[str]) -> ApiError:
        names = ", ".join(denied)
        return ApiError(
            code=ErrorCode.ENTITLEMENT_DENIED,
            http=403,
            message=f"Access to {names} is explicitly denied for your role.",
            suggested_action="Request access to this resource from an administrator.",
        )

    # -- RLS ---------------------------------------------------------------

    def _apply_rls(
        self,
        tree: exp.Select,
        policies: Sequence[Mapping[str, Any]],
        parsed: ParsedQuery,
        bindings: Mapping[str, str],
        roles: Iterable[str],
    ) -> set[str]:
        """AND every matching RLS predicate into the tree. Returns the resources
        that an allow policy covered."""
        alias_by_resource = {
            table.source.qualified_name: table.alias for table in parsed.tables
        }
        allowed: set[str] = set()

        for policy in policies:
            name = f"{policy['connector_type']}.{policy['resource']}"
            alias = alias_by_resource.get(name)
            if alias is None or policy.get("effect") != "allow":
                continue
            if not applies_to(policy, roles):
                continue
            allowed.add(name)
            if policy.get("kind") != "RLS":
                continue

            predicate = policy.get("predicate")
            if not predicate:
                raise PolicyError(
                    f"RLS allow policy {policy.get('policy_id')!r} has no predicate; "
                    f"it would allow every row while looking like a restriction"
                )
            # `append=True` merges into any existing WHERE. It routes through
            # `exp.and_`, which parenthesizes — which is exactly why the planner
            # must flatten recursively through `.unnest()`.
            tree.where(compile_predicate(predicate, alias, bindings), append=True, copy=False)
            logger.debug(
                "rls applied", extra={"context": {"policy": policy.get("policy_id")}}
            )

        return allowed

    # -- default-deny ------------------------------------------------------

    def _apply_default_deny(
        self,
        tree: exp.Select,
        parsed: ParsedQuery,
        policies: Sequence[Mapping[str, Any]],
        allowed: set[str],
    ) -> list[tuple[str, str]]:
        """Yield nothing for a *governed* resource with no matching allow.

        The distinction that keeps this coherent rather than arbitrary: a
        resource's **access** is default-deny at the grant — ``tenant_connector``
        → ``CONNECTOR_NOT_ENABLED``, layer L3 in ``gateway/deps.py``. Policies
        refine rows and columns *within* an already-granted resource (layer L4).
        So a resource no policy mentions at all is "granted, unrestricted", which
        is why ``github.pull_requests`` is readable by anyone the tenant has
        granted GitHub to; a resource that IS governed but whose allows do not
        match this caller's roles yields **empty**.

        **Two mechanisms, and both are needed.** The unsatisfiable predicate keeps
        entitlement expressed in the tree, so the *result* is empty even if every
        later stage is wrong. But a predicate alone is not enough: it is a
        constant with no owning table, so the planner cannot push it anywhere,
        and the source would be fetched **in full** and then discarded by DuckDB.
        Measured before this was fixed: a `contractor` running the canonical
        query pulled all 9 Jira rows and returned 0. The caller saw nothing, but
        the rows still crossed the source boundary — the source's own audit log
        records a read that should never have happened, and the rows sit in this
        process's memory. That is precisely the post-filtering non-negotiable #1
        forbids.

        So the aliases are returned as well, and the planner marks those sources
        **not fetched at all**. Belt (never ask) and braces (could not answer if
        we did).
        """
        governed = {
            f"{policy['connector_type']}.{policy['resource']}" for policy in policies
        }
        empty: list[tuple[str, str]] = []
        for table in parsed.tables:
            name = table.source.qualified_name
            if name in governed and name not in allowed:
                logger.info(
                    "default-deny: no matching allow",
                    extra={"context": {"resource": name}},
                )
                empty.append((name, table.alias))
                tree.where(exp.false(), append=True, copy=False)
        return empty

    # -- CLS ---------------------------------------------------------------

    def _apply_cls(
        self,
        tree: exp.Select,
        policies: Sequence[Mapping[str, Any]],
        parsed: ParsedQuery,
        roles: Iterable[str],
    ) -> tuple[dict[str, str], set[str]]:
        """Rewrite masked columns in the projection, in place."""
        alias_by_resource = {
            table.source.qualified_name: table.alias for table in parsed.tables
        }
        masks: dict[str, str] = {}
        dropped: set[str] = set()

        for policy in policies:
            if policy.get("kind") != "CLS" or policy.get("effect") != "allow":
                continue
            if not applies_to(policy, roles):
                continue
            name = f"{policy['connector_type']}.{policy['resource']}"
            alias = alias_by_resource.get(name)
            if alias is None:
                continue

            column = policy.get("column_name")
            mask = policy.get("mask")
            if not column or mask not in MASK_KINDS:
                raise PolicyError(
                    f"CLS policy {policy.get('policy_id')!r} has column {column!r} "
                    f"and mask {mask!r}; mask must be one of {MASK_KINDS}"
                )

            if self._rewrite_projection(tree, alias, column, mask):
                masks[f"{name}.{column}"] = mask
                if mask == "drop":
                    dropped.add(f"{alias}.{column}")

        return masks, dropped

    @classmethod
    def _rewrite_projection(
        cls, tree: exp.Select, alias: str, column: str, mask: str
    ) -> bool:
        """Replace the projection of ``alias.column``. ``False`` if unprojected.

        A mask on a column the query does not select is not an error — the
        canonical query masks ``reporter_email`` and never projects it. Nothing
        to rewrite is the correct outcome, and the column is not registered as
        masked because no output column of that name exists.
        """
        for projection in list(tree.selects):
            if projection.output_name != column:
                continue
            if not any(
                c.table == alias and c.name == column for c in projection.find_all(exp.Column)
            ):
                continue
            if mask == "drop":
                # Removed from the OUTPUT and, via the planner's projection
                # union, from the fetch as well — a column-pushdown saving, not
                # just a display change.
                tree.set("expressions", [p for p in tree.selects if p is not projection])
            else:
                projection.replace(cls._masked(mask, alias, column))
            return True
        return False

    @staticmethod
    def _masked(mask: str, alias: str, column: str) -> exp.Expression:
        """The replacement node. ``exp.alias_`` keeps the output name stable —
        without it the column would come back named ``md5(reporter_email)``."""
        source = exp.column(column, alias)
        if mask == "hash":
            return exp.alias_(exp.func("MD5", source), column)
        if mask == "null":
            return exp.alias_(exp.Null(), column)
        if mask == "redact":
            return exp.alias_(exp.Literal.string(REDACTED), column)
        raise PolicyError(f"unreachable: mask {mask!r} has no rewrite")
