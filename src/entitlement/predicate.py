"""The JSONB predicate AST, compiled into a sqlglot node.

**A policy is an AST, never a SQL string** (ADR-008). The difference is not
stylistic. A policy stored as ``"assignee = '" + user + "'"`` can only be applied
by concatenating it into a query — which is an injection surface, is impossible
to analyse (you cannot ask a string *"which source does this filter?"*), and
cannot be pushed down. An AST can be compiled into the plan, attributed to a
source, pushed down, and re-applied authoritatively by the engine.

The shape, as ``config/policies.yaml`` writes it and design-doc §8.2 publishes it::

    {"op": "eq", "col": "assignee", "value": ":user"}
    {"op": "and", "args": [ {...}, {...} ]}

``:user`` is a **parameter**, bound at query time from the caller's ``sub``
claim. It is never interpolated as text — it becomes an ``exp.Literal``, which
sqlglot renders with proper quoting when and only when it emits SQL.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlglot import exp

#: Policy operator -> sqlglot comparison node.
#:
#: Exactly the operators at least one connector declares in its capability model
#: (``=`` on both sources; the range operators on Jira's ``updated``). An
#: operator no source can filter on would compile to a predicate that is always
#: residual — legal, but a feature with no user, so it is not here (LAW 5).
COMPARISONS: Mapping[str, type[exp.Binary]] = {
    "eq": exp.EQ,
    "ne": exp.NEQ,
    "gt": exp.GT,
    "gte": exp.GTE,
    "lt": exp.LT,
    "lte": exp.LTE,
}

#: Policy operator -> the sqlglot combinator.
CONNECTORS: Mapping[str, str] = {"and": "and_", "or": "or_"}

#: The one parameter the prototype binds. Set-valued rules (*"issues in your
#: team's projects"*) would resolve against a small ``entitlement_scope`` table;
#: the design covers that and the prototype does not build it (HLD §7).
USER_PARAM = ":user"


class PolicyError(Exception):
    """A policy that cannot be compiled.

    **Deliberately not an** :class:`~src.models.errors.ApiError`, and
    deliberately **not handled** in ``gateway/handlers.py`` — so it surfaces as a
    500 with a traceback in the log.

    The alternatives are both worse. Skipping an uncompilable policy fails
    *open*: a typo in a deny rule would silently grant what it was written to
    forbid, and nothing would look wrong. Rendering it as ``403
    ENTITLEMENT_DENIED`` fails closed but lies about whose fault it is — it tells
    the caller to go request access when the real problem is our configuration,
    and it buries an operator-actionable fault inside a normal authorization
    outcome. A policy we cannot compile is an operator problem: it must be loud.
    """


def compile_predicate(
    node: Mapping[str, Any], alias: str, bindings: Mapping[str, str]
) -> exp.Expression:
    """Compile one JSONB predicate node into a sqlglot expression.

    ``alias`` is the table alias the columns belong to, so the emitted
    ``exp.Column`` is already attributed — which is what lets the planner push
    the resulting predicate to the right source without re-qualifying.
    """
    if not isinstance(node, Mapping):
        raise PolicyError(f"predicate node must be a mapping, got {type(node).__name__}")

    op = node.get("op")
    if not isinstance(op, str):
        raise PolicyError(f"predicate node has no string 'op': {node!r}")
    op = op.lower()

    if op in CONNECTORS:
        return _compile_connector(op, node, alias, bindings)
    if op in COMPARISONS:
        return _compile_comparison(op, node, alias, bindings)

    known = ", ".join(sorted([*COMPARISONS, *CONNECTORS]))
    raise PolicyError(f"unknown predicate operator {op!r}; known operators: {known}")


def _compile_connector(
    op: str, node: Mapping[str, Any], alias: str, bindings: Mapping[str, str]
) -> exp.Expression:
    args = node.get("args")
    if not isinstance(args, list) or len(args) < 2:
        raise PolicyError(f"{op!r} needs an 'args' list of at least two predicates")
    compiled = [compile_predicate(arg, alias, bindings) for arg in args]
    combine = getattr(exp, CONNECTORS[op])
    return combine(*compiled)


def _compile_comparison(
    op: str, node: Mapping[str, Any], alias: str, bindings: Mapping[str, str]
) -> exp.Binary:
    column = node.get("col")
    if not isinstance(column, str) or not column:
        raise PolicyError(f"predicate {node!r} names no column")
    if "value" not in node:
        raise PolicyError(f"predicate on {column!r} has no 'value'")

    return COMPARISONS[op](
        this=exp.column(column, alias),
        expression=_literal(bind(node["value"], bindings), column),
    )


def bind(value: Any, bindings: Mapping[str, str]) -> Any:
    """Resolve a ``:param`` placeholder, or pass a plain value through.

    An **unbound** parameter raises. It must never fall through as the literal
    string ``":user"``, which would compile to ``assignee = ':user'`` — a
    predicate that matches nothing, returns an empty result, and looks exactly
    like a user who legitimately has no rows. An entitlement bug that presents as
    a correct-looking empty answer is the hardest kind to ever notice.
    """
    if not isinstance(value, str) or not value.startswith(":"):
        return value
    name = value[1:]
    if name not in bindings:
        known = ", ".join(sorted(bindings)) or "(none)"
        raise PolicyError(f"predicate references unbound parameter {value!r}; bound: {known}")
    return bindings[name]


def _literal(value: Any, column: str) -> exp.Expression:
    """A typed sqlglot literal. Quoting is sqlglot's job, never ours."""
    if isinstance(value, bool):
        return exp.Boolean(this=value)
    if isinstance(value, int | float):
        return exp.Literal.number(value)
    if isinstance(value, str):
        return exp.Literal.string(value)
    raise PolicyError(
        f"predicate on {column!r} has an unsupported value type {type(value).__name__}"
    )
