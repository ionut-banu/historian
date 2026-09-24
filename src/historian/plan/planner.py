"""`BoundSelectStatement` -> operator tree: the planner stage of the
pipeline (`_docs/spec.md` §3 - "planner    AST -> operator tree").

Issue #13, closing out M2. Everything below the planner is already
merged: `sql/binder.py` produces a `BoundSelectStatement` with every
column reference resolved to an integer offset, and
`exec/operators.py` (#34) already implements `Scan`, `Filter`,
`Project`. This module's whole job is assembly - deciding which of
those three classes to build, and in what order, from one bound
statement plus a repository path.

One plan representation, not two
----------------------------------

Per §3's own section of that name: v1 has no logical/physical split,
because every logical operation here has exactly one implementation,
so a second tree type plus a translation pass between them would be
ceremony with no decision behind it. `plan()` therefore builds
`exec/operators.py`'s actual `Operator` instances directly - `Scan`,
optionally `Filter`, then `Project` - and returns that tree as-is.
There is no separate optimize/rewrite step (that begins at M4, #13's
own sibling "Scan capability negotiation and predicate splitting" -
unrelated to this issue's number, next milestone's work): the tree
`plan()` returns is exactly what `main()` iterates.

The table -> scan-factory mapping
------------------------------------

Grooming settled this as the one real design decision here, because
phase 2 (`_docs/spec.md` §2: `commits`, `commit_files`, `refs`,
`tree`) repeats it five more times. `TABLES` maps a catalog table name
to a *factory* - `Callable[[Path], ScanSource]` - not to a
pre-constructed scan, because a scan needs the repository path and
that path is not known until `plan()` is called. `plan()` takes this
mapping as a parameter with a default, mirroring `sql/binder.py`'s own
`bind(stmt, catalog=TABLES)` precedent exactly: `tests/test_planner.py`
overrides it with a fake `ScanSource` factory and exercises no git
subprocess at all, while `cli.py` calls `plan()` with no `tables`
argument and gets the real `{"blame": BlameScan}` default. `cli.py`
therefore never imports `tables/blame.py` itself - only this module
does, and only to build that default.

This mirrors, and does not fix, the layering gap #35 already tracks:
importing this module (to reach its own default `TABLES`) transitively
imports `tables/blame.py`, which imports `subprocess` at module level.
`AGENTS.md`'s "no git and no subprocess" promise for the planner is
therefore about *behaviour* (this module never calls a scan's `.scan()`
itself, never shells out, and is fully testable with a fake source and
no repository - see `tests/test_planner.py`), not about the import
graph, which #35 is already the place to fix.

`Scan` gets nothing to negotiate
------------------------------------

`exec/operators.py`'s own `Scan` already documents that pushdown does
not exist yet: every `Scan` calls `source.scan(pushed=())`,
unconditionally, whatever `source.capabilities()` reports. This
module does not change that - it never calls `capabilities()` and
never builds a `Predicate` or `PushdownKind` (neither type exists
yet). Predicate splitting and negotiation is M4 (§6 items 13-14), not
this issue.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from pathlib import Path

from collections.abc import Sequence

from historian.exec.operators import (
    Aggregate,
    AggregateCall,
    Filter,
    Limit,
    Operator,
    Project,
    Scan,
    ScanSource,
    Sort,
    SortKey,
)
from historian.sql.ast import (
    And,
    Between,
    BinaryOp,
    Expr,
    FunctionCall,
    In,
    Is,
    Like,
    Literal,
    Not,
    OrderDirection,
    Or,
    Star,
    UnaryOp,
)
from historian.sql.binder import BoundColumnRef, BoundOrderByItem, BoundSelectItem, BoundSelectStatement
from historian.tables.blame import BlameScan

__all__ = ["ScanFactory", "TABLES", "plan"]

#: A table's entry in the catalog: given the repository path, produce
#: the `ScanSource` `Scan` will adapt. A factory rather than a
#: ready-made instance, because the repository path is only known at
#: `plan()` time.
ScanFactory = Callable[[Path], ScanSource]

#: The table catalog: FROM-clause name -> `ScanFactory`. Phase 1 has
#: exactly one table, matching `sql/binder.py`'s own `TABLES`
#: (name -> `Schema`) - the two catalogs are keyed identically by
#: design, but are deliberately two separate dicts (one maps to a
#: `Schema`, this one to a factory), not one dict serving both call
#: sites.
TABLES: dict[str, ScanFactory] = {"blame": BlameScan}


# --- The aggregate/scalar split (issue #60, extended by #69) ----------------
#
# `_docs/spec.md` §3's "Expression evaluation": "The planner splits
# each SELECT and HAVING expression into aggregate calls, computed by
# the Aggregate operator, and the surrounding scalar expression,
# computed here [exec/expression.py] over the aggregate's output row."
# #60 built the SELECT half; #69 reuses the same split for HAVING
# (see `plan()`) and extends it for GROUP BY: a select-list or HAVING
# subexpression that matches a GROUP BY key by shape is *also*
# rewritten into a reference into `Aggregate`'s output row, at that
# key's own column offset - the group-key columns `Aggregate` now puts
# first, ahead of every aggregate call's column (`exec/operators.py`'s
# own `Aggregate.__init__`).
#
# `_split_expr` walks one already-bound expression left to right.
# Any subtree matching a `group_by` key by shape (`_expr_shape_equal`,
# ignoring position - the same rule `sql/binder.py` already used to
# decide the expression was legal at bind time) is replaced wholesale
# with a `BoundColumnRef` into that key's own slot in `Aggregate`'s
# output row - `dataclasses.replace` structural equality, not `==` on
# the raw AST node, because the two occurrences (SELECT/HAVING vs.
# GROUP BY) were bound independently and never share `position`.
# Every `FunctionCall` still found after that check - by
# `sql/binder.py`'s own guarantee, always a real, correctly-arity
# aggregate call - is replaced with a `BoundColumnRef` into `Aggregate`'s
# own output row, at `len(group_by) + len(calls)` (group-key columns
# come first), the offset its `AggregateCall` occupies once appended
# to `calls`; every other node type is walked and rebuilt via
# `dataclasses.replace`, mirroring `sql/binder.py`'s own `_bind_expr`
# structure exactly (one boring, explicit isinstance branch per
# `sql/ast.py` node type - AGENTS.md: no dynamic dispatch). `calls`
# accumulates across the *entire* select list and then HAVING, in one
# flat, ordered, undeduplicated list, shared between the two so their
# offsets never collide - `count(*)` written twice gets two slots, not
# one shared one; `Aggregate` computing the same thing twice is cheap,
# and correctness needs no identity/equality bookkeeping to get that
# just as right.
#
# `evaluate()` needs no change for this: a `BoundColumnRef` it reads
# `row[offset]` from works identically whether `row` came from a table
# scan or from `Aggregate`'s own output - `exec/expression.py` has no
# reason to know which.


def _expr_shape_equal(a: Expr, b: Expr) -> bool:
    """Structural equality between two already-bound expressions,
    ignoring `position` - mirrors `sql/binder.py`'s own
    `_expr_shape_equal` exactly (the same question, asked again here
    because a `GROUP BY` key and its matching `SELECT`/`HAVING`
    occurrence are bound independently and never share a `position`).
    Kept as this module's own copy rather than importing a private
    name across modules - `sql/binder.py`'s `_bind_expr`/`plan/
    planner.py`'s `_split_expr` are already two independent, mirrored
    walks of the same shape for the same reason."""
    if type(a) is not type(b):
        return False
    if isinstance(a, Literal):
        return type(a.value) is type(b.value) and a.value == b.value
    if isinstance(a, BoundColumnRef):
        return a.offset == b.offset
    if isinstance(a, Star):
        return a.table == b.table
    if isinstance(a, FunctionCall):
        return (
            a.name == b.name
            and len(a.args) == len(b.args)
            and all(_expr_shape_equal(x, y) for x, y in zip(a.args, b.args))
        )
    if isinstance(a, UnaryOp):
        return a.op == b.op and _expr_shape_equal(a.operand, b.operand)
    if isinstance(a, Not):
        return _expr_shape_equal(a.operand, b.operand)
    if isinstance(a, BinaryOp):
        return a.op == b.op and _expr_shape_equal(a.left, b.left) and _expr_shape_equal(a.right, b.right)
    if isinstance(a, And):
        return _expr_shape_equal(a.left, b.left) and _expr_shape_equal(a.right, b.right)
    if isinstance(a, Or):
        return _expr_shape_equal(a.left, b.left) and _expr_shape_equal(a.right, b.right)
    if isinstance(a, Is):
        return (
            a.negated == b.negated
            and _expr_shape_equal(a.left, b.left)
            and _expr_shape_equal(a.right, b.right)
        )
    if isinstance(a, Like):
        return (
            a.negated == b.negated
            and _expr_shape_equal(a.left, b.left)
            and _expr_shape_equal(a.pattern, b.pattern)
        )
    if isinstance(a, In):
        return (
            a.negated == b.negated
            and len(a.values) == len(b.values)
            and _expr_shape_equal(a.left, b.left)
            and all(_expr_shape_equal(x, y) for x, y in zip(a.values, b.values))
        )
    if isinstance(a, Between):
        return (
            a.negated == b.negated
            and _expr_shape_equal(a.operand, b.operand)
            and _expr_shape_equal(a.low, b.low)
            and _expr_shape_equal(a.high, b.high)
        )
    raise AssertionError(f"plan/planner.py: unhandled expression node type {type(a).__name__}")


def _group_key_index(expr: Expr, group_by: Sequence[Expr]) -> int | None:
    """The offset of *expr* among `Aggregate`'s group-key columns, if
    it matches one of `group_by`'s keys by shape - `None` otherwise.
    The first match wins, matching `group_by`'s own declared order
    (which is also `Aggregate`'s own output column order)."""
    for index, key in enumerate(group_by):
        if _expr_shape_equal(expr, key):
            return index
    return None


def _build_aggregate_call(call: FunctionCall) -> AggregateCall:
    """`sql/binder.py` has already validated `call.name` (ASCII-folded)
    against its own aggregate registry and checked its arity - nothing
    else can reach this far - so a plain `.lower()` is safe here rather
    than a third copy of that module's own ASCII-only fold: the two can
    only disagree on a non-ASCII character, and a name that ASCII-folds
    to `count`/`sum`/`avg`/`min`/`max` is, by construction, already
    pure ASCII letters differing from the target only in case."""
    kind = call.name.lower()
    if kind == "count":
        if len(call.args) == 0:
            arg: Expr | None = None
        else:
            (only,) = call.args
            arg = None if isinstance(only, Star) else only
    else:
        (only,) = call.args
        arg = only
    return AggregateCall(kind=kind, arg=arg, position=call.position)


def _split_expr(expr: Expr, calls: list[AggregateCall], group_by: Sequence[Expr]) -> Expr:
    key_index = _group_key_index(expr, group_by)
    if key_index is not None:
        return BoundColumnRef(offset=key_index, name=f"group_{key_index + 1}", position=expr.position)
    if isinstance(expr, FunctionCall):
        slot = len(group_by) + len(calls)
        calls.append(_build_aggregate_call(expr))
        return BoundColumnRef(offset=slot, name=expr.name, position=expr.position)
    if isinstance(expr, Literal):
        return expr
    if isinstance(expr, BoundColumnRef):
        return expr
    if isinstance(expr, UnaryOp):
        return dataclasses.replace(expr, operand=_split_expr(expr.operand, calls, group_by))
    if isinstance(expr, Not):
        return dataclasses.replace(expr, operand=_split_expr(expr.operand, calls, group_by))
    if isinstance(expr, BinaryOp):
        return dataclasses.replace(
            expr,
            left=_split_expr(expr.left, calls, group_by),
            right=_split_expr(expr.right, calls, group_by),
        )
    if isinstance(expr, And):
        return dataclasses.replace(
            expr,
            left=_split_expr(expr.left, calls, group_by),
            right=_split_expr(expr.right, calls, group_by),
        )
    if isinstance(expr, Or):
        return dataclasses.replace(
            expr,
            left=_split_expr(expr.left, calls, group_by),
            right=_split_expr(expr.right, calls, group_by),
        )
    if isinstance(expr, Is):
        return dataclasses.replace(
            expr,
            left=_split_expr(expr.left, calls, group_by),
            right=_split_expr(expr.right, calls, group_by),
        )
    if isinstance(expr, Like):
        return dataclasses.replace(
            expr,
            left=_split_expr(expr.left, calls, group_by),
            pattern=_split_expr(expr.pattern, calls, group_by),
        )
    if isinstance(expr, In):
        return dataclasses.replace(
            expr,
            left=_split_expr(expr.left, calls, group_by),
            values=tuple(_split_expr(v, calls, group_by) for v in expr.values),
        )
    if isinstance(expr, Between):
        return dataclasses.replace(
            expr,
            operand=_split_expr(expr.operand, calls, group_by),
            low=_split_expr(expr.low, calls, group_by),
            high=_split_expr(expr.high, calls, group_by),
        )
    raise AssertionError(f"plan/planner.py: unhandled expression node type {type(expr).__name__}")


def _split_select_list(
    select_list: tuple[BoundSelectItem, ...], calls: list[AggregateCall], group_by: Sequence[Expr]
) -> tuple[BoundSelectItem, ...]:
    """Split every item in *select_list*, in order, into a rewritten
    select list (every aggregate call and every `GROUP BY`-key match
    replaced by a reference into `Aggregate`'s output row), appending
    to the shared, flat, ordered *calls* list every `AggregateCall`
    found along the way."""
    return tuple(
        dataclasses.replace(item, expr=_split_expr(item.expr, calls, group_by)) for item in select_list
    )


def _split_order_by(
    order_by: tuple[BoundOrderByItem, ...], calls: list[AggregateCall], group_by: Sequence[Expr]
) -> tuple[SortKey, ...]:
    """Split every `ORDER BY` key's expression through `_split_expr`,
    exactly like `_split_select_list`/`HAVING`'s own call, appending to
    the same shared *calls* list - issue #61's own acceptance
    criterion: an `ORDER BY` expression containing an aggregate call
    (legal even when that call is absent from the select list, per
    `sql/binder.py`'s own aggregate-legality rule) must not collide
    with a select-list or `HAVING` aggregate's own slot. Must run
    *before* `Aggregate` is constructed in `plan()` - exactly like the
    select-list and `HAVING` splits already do - since `Aggregate`
    snapshots *calls* at construction time; splitting `ORDER BY` any
    later would hand `Sort` a `BoundColumnRef` offset `Aggregate`
    never built a column for."""
    return tuple(
        SortKey(
            expr=_split_expr(item.expr, calls, group_by),
            descending=item.direction is OrderDirection.DESC,
        )
        for item in order_by
    )


def plan(stmt: BoundSelectStatement, repo: Path, tables: dict[str, ScanFactory] = TABLES) -> Operator:
    """Build the operator tree for *stmt*, a repository at *repo*.

    `tables` maps `stmt.from_table` (already resolved against
    `sql/binder.py`'s own catalog, so the lookup here cannot fail for
    any statement `bind()` actually produced) to the factory that
    builds this query's `ScanSource`. Defaults to this module's own
    `TABLES`, but is a parameter - never hardcoded - so it can be
    swapped for a fake in tests with no repository and no git
    subprocess, per this module's own docstring.

    Tree shape, per `_docs/spec.md` §3 and issue #77's own acceptance
    criteria (extending #61's/#69's): `Scan -> Filter (WHERE) ->
    Aggregate (grouped or whole-table) -> Filter (HAVING) -> Sort ->
    Project -> Limit`. `Limit` is the new outermost operator, inserted
    only when `stmt.limit is not None` - `stmt.offset` defaults to 0
    when absent (`OFFSET` cannot appear without `LIMIT` per §1's
    grammar, so there is no case of `Limit` present for `OFFSET`
    alone). It wraps `Project` unconditionally rather than being
    inserted anywhere below it - issue #77's own tree-placement
    decision, which leaves `DISTINCT`'s future slot ("12c") between
    `Project` and `Limit` for whenever that operator is built.
    `Aggregate` (and, above it, `HAVING`'s `Filter`) is inserted only
    when the query needs it - `stmt.group_by` is non-empty, or the
    aggregate/scalar split (`_split_select_list`/`_split_expr`, run
    over the select list, then `HAVING`, then `ORDER BY`, sharing one
    flat `calls` list so their offsets never collide) found at least
    one aggregate call anywhere in any of the three. A `GROUP BY`-free,
    aggregate-free query keeps issue #13's original two shapes exactly
    - neither `Aggregate` nor `HAVING`'s `Filter` ever appears for it.
    `Sort` is inserted only when `stmt.order_by` is non-empty, and
    always directly below `Project` - `ORDER BY` may legally reference
    a column or aggregate absent from the final select list (`select k
    from g group by k order by count(*) desc` - confirmed against
    sqlite3 during this issue's grooming), so `Sort` needs the wider
    pre-`Project` row, never the narrower projected one, regardless of
    whether the query aggregates.

    All three splits - select list, `HAVING`, `ORDER BY` - must run
    *before* `Aggregate` is constructed: `Aggregate.__init__` snapshots
    `calls` (and builds its own output schema from that snapshot)
    immediately, so any split run afterwards would silently hand a
    downstream operator an offset `Aggregate` never built a column for.

    `sql/binder.py` (issue #69) refuses to bind a `HAVING` clause on a
    non-aggregate query at all - `HAVING` with no `GROUP BY` and no
    aggregate call anywhere in the select list or `HAVING` itself is a
    `BindError` there, matching `sqlite3`'s own "HAVING clause on a
    non-aggregate query" rejection - so `plan()` never legitimately
    sees a bound `having` with `calls` empty and `stmt.group_by`
    empty. The `elif having is not None` branch below only exists
    for a `BoundSelectStatement` built by hand (as some planner unit
    tests do, bypassing `bind()`); it treats that shape as an
    ordinary predicate over the `Scan`/`Filter(WHERE)` row rather than
    routing it through `Aggregate`, since there is nothing to compute
    or group - never reached for any statement `bind()` actually
    produced. `sql/binder.py` (issue #61) similarly refuses to bind an
    `ORDER BY` expression with a bare aggregate call unless the query
    already aggregates, so `plan()` never legitimately sees `calls`
    grow past what `stmt.group_by`/the select list already required.
    """
    source = tables[stmt.from_table](repo)
    tree: Operator = Scan(source)
    if stmt.where is not None:
        tree = Filter(tree, stmt.where)

    calls: list[AggregateCall] = []
    select_list = _split_select_list(stmt.select_list, calls, stmt.group_by)
    having = _split_expr(stmt.having, calls, stmt.group_by) if stmt.having is not None else None
    order_keys = _split_order_by(stmt.order_by, calls, stmt.group_by)

    if calls or stmt.group_by:
        tree = Aggregate(tree, calls, group_by=stmt.group_by)
        if having is not None:
            tree = Filter(tree, having)
    elif having is not None:
        # See the docstring above: unreachable via bind(), kept only
        # so a hand-built BoundSelectStatement still gets a sane tree
        # rather than plan() crashing on it.
        tree = Filter(tree, having)

    if order_keys:
        tree = Sort(tree, order_keys)

    tree = Project(tree, select_list)

    if stmt.limit is not None:
        offset = stmt.offset if stmt.offset is not None else 0
        tree = Limit(tree, limit=stmt.limit, offset=offset)

    return tree
