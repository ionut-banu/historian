"""The shared expression-tree walks: what a node's children are, how to
rebuild a node around new ones, whether two trees have the same shape,
and whether a query aggregates.

Issue #112. Before it, `sql/binder.py`, `plan/planner.py` and
`sql/parser.py` each kept their own copy of the children table, and the
binder and the planner each kept their own shape equality, so adding a
field to an expression node took an edit in every copy and nothing
failed when one was missed - the bug #101 (`LIKE ... ESCAPE`) and #131
(`count(DISTINCT x)`) were both that. Every per-field question about an
expression node is now answered here, once, and `tests/test_walk.py`
builds every node type by reflection and fails when a field is not
handled. Reflection stays in the tests: this module is plain
`isinstance` chains and loops, one branch per node type, so it
translates to a Rust `match` (`AGENTS.md`).

Logic that computes something different per node type - the
evaluator, the `--explain` renderer, the parser's `_node_height` -
stays in its own module; only the walking of fields lives here.

`BoundColumnRef` is defined here rather than in `sql/binder.py`
because the functions below must know it and the binder imports this
module, not the other way round. `sql/binder.py` re-exports it, so
`from historian.sql.binder import BoundColumnRef` still names this
same class.

Imports only the standard library, `historian.ascii`, `sql/ast.py` and
`sql/lexer.py`: no git, no subprocess, no binder.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from historian.ascii import ascii_fold
from historian.sql.ast import (
    And,
    Between,
    BinaryOp,
    ColumnRef,
    Expr,
    FunctionCall,
    In,
    Is,
    Like,
    Literal,
    Not,
    Or,
    Star,
    UnaryOp,
)
from historian.sql.lexer import Position

__all__ = [
    "BoundColumnRef",
    "children",
    "contains_aggregate",
    "expr_shape_equal",
    "is_aggregate_query",
    "with_children",
]


@dataclass(frozen=True)
class BoundColumnRef(Expr):
    """A resolved column reference: everywhere a `ColumnRef` used to be.

    `offset` is the column's zero-based position in the FROM table's
    schema, computed once via `Schema.index_of` - the mechanism spec
    §3 describes for keeping row access by offset rather than by name.
    `name` is the column's declared schema spelling (used for an
    unaliased select-list item's output name; see `BoundSelectItem` in
    `sql/binder.py`). `position` is inherited from the original
    `ColumnRef` (or, for a `Star`-expansion item, from the `Star`
    itself), so an error found later can still point at source text.
    """

    offset: int
    name: str
    position: Position


def children(expr: Expr) -> tuple[Expr, ...]:
    """*expr*'s direct sub-expressions, left to right - field-declaration
    order, with `In.left` before its values and `Like.escape` last and
    left out when absent. Every walk visits children in this order,
    which is what keeps error order, short-circuit order and aggregate
    slot order what they are. A leaf has none."""
    if isinstance(expr, (Literal, ColumnRef, BoundColumnRef, Star)):
        return ()
    if isinstance(expr, FunctionCall):
        return expr.args
    if isinstance(expr, (UnaryOp, Not)):
        return (expr.operand,)
    if isinstance(expr, (BinaryOp, And, Or, Is)):
        return (expr.left, expr.right)
    if isinstance(expr, Like):
        if expr.escape is None:
            return (expr.left, expr.pattern)
        return (expr.left, expr.pattern, expr.escape)
    if isinstance(expr, In):
        return (expr.left, *expr.values)
    if isinstance(expr, Between):
        return (expr.operand, expr.low, expr.high)
    raise AssertionError(f"sql/walk.py: unhandled expression node type {type(expr).__name__}")


def with_children(expr: Expr, new_children: Sequence[Expr]) -> Expr:
    """*expr* rebuilt via `dataclasses.replace` with *new_children* - one
    per entry of `children(expr)`, in the same order - in place of its
    own children. A leaf takes none and comes back as it is."""
    if isinstance(expr, (Literal, ColumnRef, BoundColumnRef, Star)):
        assert len(new_children) == 0
        return expr
    if isinstance(expr, FunctionCall):
        return dataclasses.replace(expr, args=tuple(new_children))
    if isinstance(expr, (UnaryOp, Not)):
        (operand,) = new_children
        return dataclasses.replace(expr, operand=operand)
    if isinstance(expr, (BinaryOp, And, Or, Is)):
        left, right = new_children
        return dataclasses.replace(expr, left=left, right=right)
    if isinstance(expr, Like):
        if expr.escape is None:
            left, pattern = new_children
            return dataclasses.replace(expr, left=left, pattern=pattern)
        left, pattern, escape = new_children
        return dataclasses.replace(expr, left=left, pattern=pattern, escape=escape)
    if isinstance(expr, In):
        return dataclasses.replace(expr, left=new_children[0], values=tuple(new_children[1:]))
    if isinstance(expr, Between):
        operand, low, high = new_children
        return dataclasses.replace(expr, operand=operand, low=low, high=high)
    raise AssertionError(f"sql/walk.py: unhandled expression node type {type(expr).__name__}")


def expr_shape_equal(a: Expr, b: Expr) -> bool:
    """Structural equality between two expressions, ignoring `position`
    - two occurrences of the same expression written at different
    points in the query text (a `GROUP BY` key and its select-list,
    `HAVING` or `ORDER BY` occurrence, say) compare equal even though
    every node's `position` differs.

    A loop over an explicit stack of node pairs still to compare (issue
    #107): each pair's own fields are compared, then its child pairs
    are pushed. Nothing here has a side effect, so the order the pairs
    are compared in cannot change the answer."""
    pending: list[tuple[Expr, Expr]] = [(a, b)]
    while pending:
        x, y = pending.pop()
        if not _same_node_fields(x, y):
            return False
        x_children = children(x)
        y_children = children(y)
        # The child count: a function's arguments, an `IN` list's
        # length, whether `LIKE` has an `ESCAPE`.
        if len(x_children) != len(y_children):
            return False
        pending.extend(zip(x_children, y_children))
    return True


def _same_node_fields(a: Expr, b: Expr) -> bool:
    """`expr_shape_equal` for one pair of nodes, children aside: the
    same node type and the same non-child fields, `position` apart."""
    if type(a) is not type(b):
        return False
    if isinstance(a, Literal):
        # `1` and `1.0` are equal Python values but different literals.
        return type(a.value) is type(b.value) and a.value == b.value
    if isinstance(a, ColumnRef):
        return a.table == b.table and a.name == b.name
    if isinstance(a, BoundColumnRef):
        return a.offset == b.offset and a.name == b.name
    if isinstance(a, Star):
        return a.table == b.table
    if isinstance(a, FunctionCall):
        # Function names are ASCII-case-insensitive in SQLite - `COUNT`
        # in the select list and `count` in `ORDER BY` name the same
        # aggregate - so the name is folded the way the binder folds it
        # when it resolves a name against its aggregate registry. The
        # AST keeps `name` as written, for error messages. `count(x)`
        # and `count(DISTINCT x)` are different aggregates (issue #131).
        return ascii_fold(a.name) == ascii_fold(b.name) and a.distinct == b.distinct
    if isinstance(a, (UnaryOp, BinaryOp)):
        return a.op == b.op
    if isinstance(a, (Not, And, Or)):
        return True
    if isinstance(a, (Is, Like, In, Between)):
        return a.negated == b.negated
    raise AssertionError(f"sql/walk.py: unhandled expression node type {type(a).__name__}")


def contains_aggregate(expr: Expr) -> bool:
    """Whether *expr* - already bound, so every `FunctionCall` in it is
    a real, validated aggregate call (v1 has no scalar functions) -
    contains an aggregate call anywhere in its tree. A loop over an
    explicit stack of nodes still to look at (issue #107); the order
    they are visited in does not matter for a yes/no answer."""
    pending: list[Expr] = [expr]
    while pending:
        node = pending.pop()
        if isinstance(node, FunctionCall):
            return True
        if isinstance(node, ColumnRef):
            raise AssertionError("sql/walk.py: contains_aggregate needs a bound tree")
        pending.extend(children(node))
    return False


def is_aggregate_query(group_by: Sequence[Expr], exprs: Iterable[Expr]) -> bool:
    """Whether a query aggregates: `GROUP BY` is written, or one of
    *exprs* (bound) contains an aggregate call.

    Which expressions count is the caller's: `sql/binder.py` passes the
    select list alone, because SQLite decides it from the select list
    (`select path from t having count(*) > 1` is still "HAVING clause
    on a non-aggregate query"); `plan/planner.py` passes the select
    list, `HAVING` and `ORDER BY`, which for any statement `bind()`
    produced gives the same answer, since the binder rejects an
    aggregate in `HAVING` or `ORDER BY` of a query whose select list
    does not aggregate."""
    if len(group_by) > 0:
        return True
    for expr in exprs:
        if contains_aggregate(expr):
            return True
    return False
