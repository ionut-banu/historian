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

from historian.exec.operators import Aggregate, AggregateCall, Filter, Operator, Project, Scan, ScanSource
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
    Or,
    Star,
    UnaryOp,
)
from historian.sql.binder import BoundColumnRef, BoundSelectItem, BoundSelectStatement
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


# --- The aggregate/scalar split (issue #60) ---------------------------------
#
# `_docs/spec.md` §3's "Expression evaluation": "The planner splits
# each SELECT and HAVING expression into aggregate calls, computed by
# the Aggregate operator, and the surrounding scalar expression,
# computed here [exec/expression.py] over the aggregate's output row."
# HAVING is #69's; this issue only needs the SELECT half.
#
# `_split_expr` walks one already-bound select-list expression left to
# right. Every `FunctionCall` it finds - by `sql/binder.py`'s own
# guarantee, always a real, correctly-arity aggregate call, since
# nothing else can survive binding - is replaced with a
# `BoundColumnRef` into `Aggregate`'s own output row, at the offset its
# `AggregateCall` occupies once appended to `calls`; every other node
# type is walked and rebuilt via `dataclasses.replace`, mirroring
# `sql/binder.py`'s own `_bind_expr` structure exactly (one boring,
# explicit isinstance branch per `sql/ast.py` node type - AGENTS.md: no
# dynamic dispatch). `calls` accumulates across the *entire* select
# list, in one flat, ordered, undeduplicated list - `count(*)` written
# twice gets two slots, not one shared one; `Aggregate` computing the
# same thing twice is cheap for one output row, and correctness needs
# no identity/equality bookkeeping to get that just as right.
#
# `evaluate()` needs no change for this: a `BoundColumnRef` it reads
# `row[offset]` from works identically whether `row` came from a table
# scan or from `Aggregate`'s own output - `exec/expression.py` has no
# reason to know which.


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


def _split_expr(expr: Expr, calls: list[AggregateCall]) -> Expr:
    if isinstance(expr, FunctionCall):
        slot = len(calls)
        calls.append(_build_aggregate_call(expr))
        return BoundColumnRef(offset=slot, name=expr.name, position=expr.position)
    if isinstance(expr, Literal):
        return expr
    if isinstance(expr, BoundColumnRef):
        return expr
    if isinstance(expr, UnaryOp):
        return dataclasses.replace(expr, operand=_split_expr(expr.operand, calls))
    if isinstance(expr, Not):
        return dataclasses.replace(expr, operand=_split_expr(expr.operand, calls))
    if isinstance(expr, BinaryOp):
        return dataclasses.replace(
            expr, left=_split_expr(expr.left, calls), right=_split_expr(expr.right, calls)
        )
    if isinstance(expr, And):
        return dataclasses.replace(
            expr, left=_split_expr(expr.left, calls), right=_split_expr(expr.right, calls)
        )
    if isinstance(expr, Or):
        return dataclasses.replace(
            expr, left=_split_expr(expr.left, calls), right=_split_expr(expr.right, calls)
        )
    if isinstance(expr, Is):
        return dataclasses.replace(
            expr, left=_split_expr(expr.left, calls), right=_split_expr(expr.right, calls)
        )
    if isinstance(expr, Like):
        return dataclasses.replace(
            expr, left=_split_expr(expr.left, calls), pattern=_split_expr(expr.pattern, calls)
        )
    if isinstance(expr, In):
        return dataclasses.replace(
            expr,
            left=_split_expr(expr.left, calls),
            values=tuple(_split_expr(v, calls) for v in expr.values),
        )
    if isinstance(expr, Between):
        return dataclasses.replace(
            expr,
            operand=_split_expr(expr.operand, calls),
            low=_split_expr(expr.low, calls),
            high=_split_expr(expr.high, calls),
        )
    raise AssertionError(f"plan/planner.py: unhandled expression node type {type(expr).__name__}")


def _split_select_list(
    select_list: tuple[BoundSelectItem, ...],
) -> tuple[tuple[BoundSelectItem, ...], list[AggregateCall]]:
    """Split every item in *select_list*, in order, into a rewritten
    select list (every aggregate call replaced by a reference into
    `Aggregate`'s output row) and the flat, ordered list of
    `AggregateCall`s that reference points at."""
    calls: list[AggregateCall] = []
    split_items = tuple(
        dataclasses.replace(item, expr=_split_expr(item.expr, calls)) for item in select_list
    )
    return split_items, calls


def plan(stmt: BoundSelectStatement, repo: Path, tables: dict[str, ScanFactory] = TABLES) -> Operator:
    """Build the operator tree for *stmt*, a repository at *repo*.

    `tables` maps `stmt.from_table` (already resolved against
    `sql/binder.py`'s own catalog, so the lookup here cannot fail for
    any statement `bind()` actually produced) to the factory that
    builds this query's `ScanSource`. Defaults to this module's own
    `TABLES`, but is a parameter - never hardcoded - so it can be
    swapped for a fake in tests with no repository and no git
    subprocess, per this module's own docstring.

    Returns `Project(Filter(Scan(source), stmt.where), stmt.select_list)`
    when `stmt.where` is present, or `Project(Scan(source),
    stmt.select_list)` when it is `None`, exactly as issue #13 built it
    - and, new in #60, `Aggregate` inserted directly below `Project`
    (`Project(Aggregate(<Filter or Scan>, calls), split_select_list)`)
    whenever the select list contains at least one aggregate call.
    Every other query - and every query as issue #13 already built it -
    gets neither `Aggregate` nor a rewritten select list:
    `_split_select_list` returns each item's expression unchanged in
    shape whenever it contains no `FunctionCall`, and `calls` comes
    back empty, so the `if calls:` check below never inserts
    `Aggregate` where issue #13's two original shapes still apply.
    """
    source = tables[stmt.from_table](repo)
    tree: Operator = Scan(source)
    if stmt.where is not None:
        tree = Filter(tree, stmt.where)
    select_list, calls = _split_select_list(stmt.select_list)
    if calls:
        tree = Aggregate(tree, calls)
    return Project(tree, select_list)
