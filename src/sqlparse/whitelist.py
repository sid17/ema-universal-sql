"""The supported-SQL subset, as an AST node-type whitelist.

A whitelist rather than a blacklist of dangerous strings, because string
matching on SQL is exactly the reasoning this design replaced with an AST:
``SELECT/**/‌*`` and ``sElEcT *`` defeat a regex and are identical trees.

**This runs BEFORE ``qualify()``, and that ordering is load-bearing** (ADR-027).
Measured: ``qualify()`` *expands* ``SELECT *`` against the schema, so by the time
a post-qualify validator walks the tree there is no :class:`~sqlglot.exp.Star`
left to find and the one projection that defeats projection pushdown would be
accepted silently.

**It also runs before stage 3**, on the *caller's* tree. The entitlement engine
injects ``MD5(...)`` for a ``hash`` mask, which this module's own ban on
arbitrary functions would reject — so the engine's nodes are trusted and never
re-validated. Without that note a reviewer reads the CLS injection as violating
this rule.

The allowed set below is **measured, not guessed** (v3-research Finding 4): the
canonical query, once qualified, contains ``Identifier``, ``TableAlias`` and
``Ordered``, none of which appeared in the phase file's original sketch. A
whitelist missing them rejects the canonical query itself.
"""

from __future__ import annotations

from sqlglot import exp

from src.models.errors import InvalidQueryError

#: Structural nodes sqlglot always emits for the supported shape.
_STRUCTURE = (
    exp.Select,
    exp.From,
    exp.Join,
    exp.Where,
    exp.Order,
    exp.Ordered,
    exp.Limit,
    exp.Table,
    exp.TableAlias,
    exp.Alias,
    exp.Column,
    exp.Identifier,
    exp.Literal,
    exp.Boolean,
    exp.Null,
    exp.Paren,
)

#: Comparison and boolean operators.
#:
#: Every one of these is either pushable by at least one connector (``=`` on
#: both; ``>``, ``>=``, ``<``, ``<=`` on Jira's ``updated``) or re-appliable by
#: the engine as a residual filter. ``IN``, ``LIKE`` and ``NOT`` are absent
#: because no capability model declares them and nothing in the brief asks for
#: them — adding an operator no source can filter on is a feature with no user
#: (LAW 5). Residual support would make them *work*, which is precisely why
#: they would be easy to add later and are not needed now.
_OPERATORS = (
    exp.EQ,
    exp.NEQ,
    exp.GT,
    exp.GTE,
    exp.LT,
    exp.LTE,
    exp.And,
    exp.Or,
)

ALLOWED_NODES: frozenset[type[exp.Expression]] = frozenset(_STRUCTURE + _OPERATORS)

#: Why a particular construct is out, phrased as something a caller can act on.
#: "Unsupported node type: Star" is technically accurate and useless.
_HINTS: dict[type[exp.Expression], str] = {
    exp.Star: (
        "SELECT * is not supported — name the columns you need. The engine "
        "fetches only the columns a query references, and a star would pull "
        "every column from every source on every request."
    ),
    exp.Subquery: (
        "subqueries are not supported; the subset is projection, filters, joins and LIMIT"
    ),
    exp.Union: "UNION / INTERSECT / EXCEPT are not supported",
    exp.Group: "GROUP BY and aggregation are not supported",
    exp.Having: "HAVING is not supported (it requires GROUP BY)",
    exp.Like: "LIKE is not supported; use = on an indexed column",
    exp.In: "IN is not supported; use = or OR",
    exp.Window: "window functions are not supported",
}


def _describe(node: exp.Expression) -> str:
    """A caller-actionable reason, falling back to the node's own name."""
    for kind, hint in _HINTS.items():
        if isinstance(node, kind):
            return hint
    if isinstance(node, exp.Func):
        # Caught generically: there are hundreds of function nodes and listing
        # them would be a maintenance burden that silently rots.
        return (
            f"the function {node.sql_name()} is not supported; the subset has no "
            f"function calls (masking functions are applied by the entitlement "
            f"engine, not by the caller)"
        )
    return f"{type(node).__name__} is not part of the supported SQL subset"


def reject_unsupported(tree: exp.Expression) -> None:
    """Raise :class:`InvalidQueryError` for the first unsupported node.

    First, not all: a query with six problems still has to be fixed one at a
    time, and reporting six reasons makes the first one harder to find.

    LAW 4 — this never returns a boolean a caller could forget to check.
    """
    for node in tree.walk():
        if type(node) not in ALLOWED_NODES:
            raise InvalidQueryError(
                f"Unsupported SQL: {_describe(node)}",
                detail=type(node).__name__,
            )
