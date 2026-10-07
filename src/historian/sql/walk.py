"""The shared expression-tree walks: what a node's children are (and in
which order SQLite resolves their names, #144), how to
rebuild a node around new ones, whether two trees have the same shape,
whether a tree is what SQLite's parser calls constant (#144), whether a
query aggregates, how a condition splits into `AND`-terms and
joins back, whether a term reads only `GROUP BY` keys, and how a column
is replaced by a constant (#142).

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
same class. `FixedColumnRef` (#142), the column the planner has
replaced by a constant, is defined here for the same reason.

Imports only the standard library, `historian.ascii`,
`historian.values` (for the `Value` type), `sql/ast.py` and
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
from historian.values import Value

__all__ = [
    "BoundColumnRef",
    "FixedColumnRef",
    "children",
    "contains_aggregate",
    "expr_shape_equal",
    "fix_columns",
    "is_aggregate_query",
    "is_constant",
    "join_conjuncts",
    "references_only_keys",
    "replace_conjuncts",
    "resolution_children",
    "split_conjuncts",
    "with_children",
    "with_resolution_children",
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


@dataclass(frozen=True)
class FixedColumnRef(Expr):
    """A column reference the planner has replaced by a constant: SQLite's
    `EP_FixedCol` (#142, spec §3 "Constant propagation in `WHERE`").

    It evaluates to `value`, never to the row's own cell, and as an
    operand of a comparison it still has the declared affinity of the
    column at `offset` - it is the column, known to hold `value`, not a
    literal. `value` is the source's constant already converted by that
    affinity (`exec/expression.py`'s `apply_column_affinity`): the value
    the constant would have if stored in the column. `name` is the
    column's declared spelling and `position` the replaced reference's,
    as on `BoundColumnRef`. Not a subclass of `BoundColumnRef`, so no
    code that reads a row by offset or recognises a column (a scan's
    `accepts()`, `GROUP BY` key typing) can mistake it for one.
    """

    offset: int
    name: str
    value: Value
    position: Position


def children(expr: Expr) -> tuple[Expr, ...]:
    """*expr*'s direct sub-expressions, left to right - field-declaration
    order, with `In.left` before its values and `Like.escape` last and
    left out when absent. Every walk visits children in this order,
    which is what keeps error order, short-circuit order and aggregate
    slot order what they are. A leaf has none."""
    if isinstance(expr, (Literal, ColumnRef, BoundColumnRef, FixedColumnRef, Star)):
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
    if isinstance(expr, (Literal, ColumnRef, BoundColumnRef, FixedColumnRef, Star)):
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


def resolution_children(expr: Expr) -> tuple[Expr, ...]:
    """*expr*'s children in the order SQLite's name resolution walks
    them (#144): `children(expr)`, except that `LIKE` gives its pattern
    first - SQLite's tree holds `x LIKE y ESCAPE z` as the call
    `like(y, x, z)`. Only the binder's error walk uses this order;
    evaluation, short-circuiting and every other walk keep `children`.
    """
    if isinstance(expr, Like):
        if expr.escape is None:
            return (expr.pattern, expr.left)
        return (expr.pattern, expr.left, expr.escape)
    return children(expr)


def with_resolution_children(expr: Expr, new_children: Sequence[Expr]) -> Expr:
    """`with_children`, with *new_children* in `resolution_children`
    order rather than `children` order."""
    if isinstance(expr, Like):
        if expr.escape is None:
            pattern, left = new_children
            return dataclasses.replace(expr, left=left, pattern=pattern)
        pattern, left, escape = new_children
        return dataclasses.replace(expr, left=left, pattern=pattern, escape=escape)
    return with_children(expr, new_children)


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
    if isinstance(a, FixedColumnRef):
        # The value compares the way a `Literal`'s does.
        return (
            a.offset == b.offset
            and a.name == b.name
            and type(a.value) is type(b.value)
            and a.value == b.value
        )
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


def is_constant(expr: Expr) -> bool:
    """Whether *expr* is constant the way SQLite's parser decides it
    (`sqlite3ExprIsConstant`, before any name is resolved): no column
    reference and no function call anywhere in its tree. `LIKE` is
    SQLite's built-in `like()`, a constant function, so it is constant
    when its operands are. Any `FunctionCall` is not: historian's are
    aggregates or unknown names, neither constant to SQLite - and a
    built-in scalar SQLite would count (`abs(1)`) is an unknown name to
    historian until #183. SQLite's parser also folds `x AND 0` and `x IN
    ()` (with no call in `x`) to a constant first; historian does not
    fold them (#185), so here a column under such a fold still makes the
    tree not constant. (`sql/parser.py`'s `_node_height` keeps its own
    constant flag, for heights.) The binder uses this for a one-element
    `IN` list (#144). A loop over an explicit stack of nodes (#107); the
    order they are visited in does not matter for a yes/no answer."""
    pending: list[Expr] = [expr]
    while pending:
        node = pending.pop()
        if isinstance(node, (ColumnRef, BoundColumnRef, FixedColumnRef, FunctionCall, Star)):
            return False
        pending.extend(children(node))
    return True


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


def split_conjuncts(expr: Expr) -> list[Expr]:
    """The conjunctive terms of *expr*, left to right.

    Every `And` reached through `And`s from the root is split; anything
    else, `Or` and `Not` included, is one term. Parens produce no AST
    node, so `(x AND y) AND z` and `x AND (y AND z)` both give `[x, y,
    z]` - `AND` is associative, and a term's own evaluation is
    unchanged by where it came from.

    `plan/optimizer.py` splits `WHERE` with it for pushdown (#121) and
    `plan/planner.py` splits `HAVING` with it to find the terms that
    move below the aggregate (#141): one implementation of "split on
    `AND`".

    Iterative with an explicit stack rather than recursive: the parser
    builds `x1 AND x2 AND ... AND xn` as a left-deep chain, and its
    depth must not be bounded by Python's recursion limit here.
    """
    terms: list[Expr] = []
    stack: list[Expr] = [expr]
    while stack:
        node = stack.pop()
        if isinstance(node, And):
            # Right pushed first so left is popped - and emitted - first.
            stack.append(node.right)
            stack.append(node.left)
        else:
            terms.append(node)
    return terms


def join_conjuncts(terms: Sequence[Expr]) -> Expr:
    """*terms* joined by `AND`, left to right, as the left-deep chain
    the parser builds for `t1 AND t2 AND ... AND tn` - so
    `split_conjuncts` gives the same terms back, and `evaluate_condition`
    stops the chain at the first term that is not `TRUE`, in order. One
    term comes back as itself. Each `And` takes the first term's
    position, as the parser's does. A loop, not a recursion."""
    assert len(terms) > 0, "sql/walk.py: join_conjuncts needs at least one term"
    joined = terms[0]
    for term in terms[1:]:
        joined = And(left=joined, right=term, position=terms[0].position)
    return joined


def references_only_keys(expr: Expr, keys: Sequence[Expr]) -> bool:
    """Whether every column reference in *expr* - already bound - lies
    inside a subexpression that matches one of *keys* by shape
    (`expr_shape_equal`, ignoring position). An expression with no
    column reference at all qualifies.

    This is the column half of SQLite's test for a `HAVING` term that
    can move below the aggregate (#141). It says nothing about
    aggregate calls: `count(*)` has no column and qualifies, so the
    planner checks `contains_aggregate` separately.

    A loop over an explicit stack of nodes still to look at (#107). A
    node matching a key is not looked into; any other column reference
    is a `False`; every other node's children are pushed."""
    pending: list[Expr] = [expr]
    while pending:
        node = pending.pop()
        if _matches_a_key(node, keys):
            continue
        if isinstance(node, BoundColumnRef):
            return False
        if isinstance(node, ColumnRef):
            raise AssertionError("sql/walk.py: references_only_keys needs a bound tree")
        pending.extend(children(node))
    return True


def _matches_a_key(expr: Expr, keys: Sequence[Expr]) -> bool:
    for key in keys:
        if expr_shape_equal(expr, key):
            return True
    return False


def replace_conjuncts(expr: Expr, terms: Sequence[Expr]) -> Expr:
    """*expr* with its conjunctive terms - those `split_conjuncts(expr)`
    gives, left to right - replaced by *terms*, one each, in order, and
    the `And` nodes above them kept as they are: the same shape, so
    evaluation stops at the same `And`s it did. An `And` whose two sides
    come back unchanged is the same object, so *expr* comes back as it
    is when every term is.

    Not recursive (#107): *pending* holds `(node, operands_done)` pairs
    over the `And` spine only, left side first, so the terms are met in
    `split_conjuncts`' order; *results* holds the rebuilt subtrees."""
    pending: list[tuple[Expr, bool]] = [(expr, False)]
    results: list[Expr] = []
    next_term = 0
    while pending:
        node, operands_done = pending.pop()
        if operands_done:
            assert isinstance(node, And)
            right = results.pop()
            left = results.pop()
            if left is node.left and right is node.right:
                results.append(node)
            else:
                results.append(dataclasses.replace(node, left=left, right=right))
            continue
        if isinstance(node, And):
            pending.append((node, True))
            pending.append((node.right, False))
            pending.append((node.left, False))
            continue
        assert next_term < len(terms), "sql/walk.py: replace_conjuncts has fewer terms than conjuncts"
        results.append(terms[next_term])
        next_term += 1
    assert next_term == len(terms), "sql/walk.py: replace_conjuncts has more terms than conjuncts"
    (result,) = results
    return result


def fix_columns(expr: Expr, fixed: dict[int, Value]) -> Expr:
    """*expr* - already bound - with every `BoundColumnRef` whose offset
    is a key of *fixed* replaced by a `FixedColumnRef` holding that
    key's value, at any depth, in every child slot (#142). The
    reference keeps its name and position. A subtree with nothing to
    replace is returned as the same object, so *expr* itself comes back
    when nothing in it changes.

    Not recursive (#107): the same `(node, operands_done)` stack as
    `plan/planner.py`'s `_split_expr`, visiting children left to
    right."""
    pending: list[tuple[Expr, bool]] = [(expr, False)]
    results: list[Expr] = []
    while pending:
        node, operands_done = pending.pop()
        if operands_done:
            original = children(node)
            first = len(results) - len(original)
            rebuilt = results[first:]
            del results[first:]
            unchanged = True
            for old_child, new_child in zip(original, rebuilt):
                if old_child is not new_child:
                    unchanged = False
            results.append(node if unchanged else with_children(node, rebuilt))
            continue
        if isinstance(node, BoundColumnRef):
            if node.offset in fixed:
                results.append(
                    FixedColumnRef(offset=node.offset, name=node.name, value=fixed[node.offset], position=node.position)
                )
            else:
                results.append(node)
            continue
        if isinstance(node, ColumnRef):
            raise AssertionError("sql/walk.py: fix_columns needs a bound tree")
        node_children = children(node)
        if len(node_children) == 0:
            results.append(node)
            continue
        pending.append((node, True))
        for child in reversed(node_children):
            pending.append((child, False))
    (result,) = results
    return result
