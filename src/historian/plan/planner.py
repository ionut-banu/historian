"""`BoundSelectStatement` -> operator tree: the planner stage of the
pipeline (`_docs/spec.md` §3 - "planner    AST -> operator tree").

`sql/binder.py` produces a `BoundSelectStatement` with every column
reference resolved to an integer offset, and `exec/operators.py`
implements the operators. This module's whole job is assembly -
deciding which of the seven operator classes to build, and in what
order, from one bound statement plus a repository path.

One plan representation, not two
----------------------------------

Per §3's own section of that name: v1 has no logical/physical split,
because every logical operation here has exactly one implementation,
so a second tree type plus a translation pass between them would be
ceremony with no decision behind it. `plan()` therefore builds
`exec/operators.py`'s actual `Operator` instances directly - up to
`Scan`, `Filter`, `Aggregate`, `Filter`, `Sort`, `Project`,
`Distinct` and `Limit` (see `plan()`) - and returns that tree as-is.
The one rewrite step after it is `plan/optimizer.py`'s `optimize()`
(#121, pushdown negotiation), which `cli.py` calls on this
tree before iterating it; it records pushed terms on the tree's
`Scan` and changes nothing else.

The table -> scan-factory mapping
------------------------------------

This is the one real design decision here, because phase 2
(`_docs/spec.md` §2: `commits`, `commit_files`, `refs`, `tree`)
repeats it five more times. `ScanFactory` (`Callable[[Path],
ScanSource]`) is what a catalog table name maps to - a factory rather
than a pre-constructed scan, because a scan needs the repository path
and that path is not known until `plan()` is called. `plan()`'s
`tables: dict[str, ScanFactory]` parameter is required, not defaulted
(#35): `tests/test_planner.py` passes a fake `ScanSource`
factory and exercises no git subprocess at all, while `cli.py` passes
`historian.catalog.SCAN_FACTORIES`, the real `{"blame": BlameScan}`
mapping. This module itself never imports `tables/blame.py` or
`historian.catalog` - `ScanFactory`'s own definition only names the
`ScanSource` protocol from `exec/operators.py`, never a concrete table
module, so it carries no import cost. `historian/catalog.py` is the
one place a table's scan factory is built from a real import; only
`cli.py` and tests that want the real catalog import it.

`AGENTS.md`'s "no git and no subprocess" promise for the planner
holds for both the *behaviour* (this module never calls a scan's
`.scan()` itself, never shells out, and is fully testable with a fake
source and no repository - see `tests/test_planner.py`) and the
*import graph* (this module imports neither `historian.tables.blame`
nor `subprocess`, directly or indirectly).

`Scan` is built with nothing pushed
------------------------------------

Every `Scan` this module builds pushes nothing (`pushed=()`), whatever
`source.capabilities()` reports - this module never calls
`capabilities()` or `accepts()`. Splitting `WHERE` into terms and
negotiating them with the scan is `plan/optimizer.py` (#121), a
separate step over the finished tree, so a caller that skips it
(`--no-pushdown`, #43) gets exactly this module's tree.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from pathlib import Path

from collections.abc import Sequence

from historian.exec.operators import (
    Aggregate,
    AggregateCall,
    Distinct,
    Filter,
    Limit,
    Operator,
    Project,
    Scan,
    ScanSource,
    Sort,
    SortKey,
)
from historian.sql.ast import Expr, FunctionCall, Literal, OrderDirection, Star
from historian.sql.binder import BoundColumnRef, BoundOrderByItem, BoundSelectItem, BoundSelectStatement
from historian.sql.walk import (
    children,
    contains_aggregate,
    expr_shape_equal,
    is_aggregate_query,
    join_conjuncts,
    references_only_keys,
    split_conjuncts,
    with_children,
)

__all__ = ["ScanFactory", "plan"]

#: A table's entry in the catalog: given the repository path, produce
#: the `ScanSource` `Scan` will adapt. A factory rather than a
#: ready-made instance, because the repository path is only known at
#: `plan()` time. Names only the `ScanSource` protocol from
#: `exec/operators.py` - never a concrete table module - so this type
#: alias carries no import cost; the real catalog lives in
#: `historian/catalog.py`, not here.
ScanFactory = Callable[[Path], ScanSource]


# --- The aggregate/scalar split ---------------------------------------------
#
# `_docs/spec.md` §3's "Expression evaluation": "The planner splits
# each SELECT and HAVING expression into aggregate calls, computed by
# the Aggregate operator, and the surrounding scalar expression,
# computed here [exec/expression.py] over the aggregate's output row."
# The same split runs over HAVING and ORDER BY (see `plan()`). A
# select-list, HAVING or ORDER BY subexpression that matches a GROUP
# BY key by shape is also rewritten into a reference into
# `Aggregate`'s output row, at that key's own column offset - the
# group-key columns come first, ahead of every aggregate call's
# column (`exec/operators.py`'s `Aggregate.__init__`).
#
# `_split_expr` walks one already-bound expression left to right.
# Any subtree matching a `group_by` key by shape (`sql/walk.py`'s
# `expr_shape_equal`, ignoring position - the same function
# `sql/binder.py` used to decide the expression was legal at bind
# time) is replaced wholesale
# with a `BoundColumnRef` into that key's own slot in `Aggregate`'s
# output row - shape equality, not `==` on the raw AST node, because
# the two occurrences (SELECT/HAVING vs. GROUP BY) were bound
# independently and never share `position`.
# Every `FunctionCall` still found after that check - by
# `sql/binder.py`'s own guarantee, always a real, correctly-arity
# aggregate call - is replaced with a `BoundColumnRef` into `Aggregate`'s
# own output row, at `len(group_by) + len(calls)` (group-key columns
# come first), the offset its `AggregateCall` occupies once appended
# to `calls`; every other node type is walked and rebuilt through
# `sql/walk.py`'s `children`/`with_children`, the same tables
# `sql/binder.py`'s own `_bind_expr` walks (one boring, explicit
# isinstance branch per node type - AGENTS.md: no dynamic dispatch). `calls`
# accumulates across the select list, then HAVING, then ORDER BY, in
# one flat, ordered, undeduplicated list, shared between them so
# their offsets never collide - `count(*)` written twice gets two
# slots, not one shared one; `Aggregate` computing the same thing twice is cheap,
# and correctness needs no identity/equality bookkeeping to get that
# just as right.
#
# `evaluate()` needs no change for this: a `BoundColumnRef` it reads
# `row[offset]` from works identically whether `row` came from a table
# scan or from `Aggregate`'s own output - `exec/expression.py` has no
# reason to know which.


def _group_key_index(expr: Expr, group_by: Sequence[Expr]) -> int | None:
    """The offset of *expr* among `Aggregate`'s group-key columns, if
    it matches one of `group_by`'s keys by shape - `None` otherwise.
    The first match wins, matching `group_by`'s own declared order
    (which is also `Aggregate`'s own output column order)."""
    for index, key in enumerate(group_by):
        if expr_shape_equal(expr, key):
            return index
    return None


def _build_aggregate_call(call: FunctionCall) -> AggregateCall:
    """`sql/binder.py` has already validated `call.name` (ASCII-folded)
    against its own aggregate registry and checked its arity - nothing
    else can reach this far - so a plain `.lower()` is safe here rather
    than a third copy of that module's own ASCII-only fold: the two can
    only disagree on a non-ASCII character, and a name that ASCII-folds
    to `count`/`sum`/`avg`/`min`/`max` is, by construction, already
    pure ASCII letters differing from the target only in case.

    `distinct` (#84) is threaded straight through from the bound
    `FunctionCall` unchanged - no per-kind logic here, since accepting
    or ignoring it is `_Accumulator`'s concern (`exec/operators.py`),
    not the planner's."""
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
    return AggregateCall(kind=kind, arg=arg, position=call.position, distinct=call.distinct)


def _split_expr(expr: Expr, calls: list[AggregateCall], group_by: Sequence[Expr]) -> Expr:
    """*expr* with every `GROUP BY`-key match and every aggregate call
    replaced by a reference into `Aggregate`'s output row, appending
    each call's `AggregateCall` to *calls* - see the section comment
    above.

    Not recursive (#107): *pending* holds `(node, operands_done)`
    pairs and *results* the rewritten subtrees finished so far, the
    same shape as `sql/binder.py`'s `_bind_expr`. A node is first seen
    with `operands_done=False`: a key match, an aggregate call or a
    leaf is rewritten on the spot; any other node is pushed back with
    `operands_done=True`, then its operands in reverse, so they are
    visited left to right - which is what keeps *calls* in left-to-
    right order, each call's slot the one the recursive version gave
    it."""
    pending: list[tuple[Expr, bool]] = [(expr, False)]
    results: list[Expr] = []
    while pending:
        node, operands_done = pending.pop()
        if operands_done:
            first = len(results) - len(children(node))
            rewritten = results[first:]
            del results[first:]
            results.append(with_children(node, rewritten))
            continue
        key_index = _group_key_index(node, group_by)
        if key_index is not None:
            results.append(
                BoundColumnRef(offset=key_index, name=f"group_{key_index + 1}", position=node.position)
            )
            continue
        if isinstance(node, FunctionCall):
            slot = len(group_by) + len(calls)
            calls.append(_build_aggregate_call(node))
            results.append(BoundColumnRef(offset=slot, name=node.name, position=node.position))
            continue
        if isinstance(node, (Literal, BoundColumnRef)):
            results.append(node)
            continue
        pending.append((node, True))
        for operand in reversed(children(node)):
            pending.append((operand, False))
    (result,) = results
    return result


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
    the same shared *calls* list: an `ORDER BY` expression containing
    an aggregate call (legal even when that call is absent from the
    select list, per `sql/binder.py`'s aggregate-legality rule) must
    not collide with a select-list or `HAVING` aggregate's own slot.
    Must run
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


# --- HAVING terms that move below the aggregate (#141) ----------------------
#
# `_docs/spec.md` §3, "`HAVING` terms that move below the aggregate".
# SQLite (`havingToWhere`) moves each `AND`-term of `HAVING` that no
# group can disagree on into `WHERE`, where it runs once per input row,
# after the query's own `WHERE` terms. Which terms is observable - an
# error raised per row that the per-group evaluation never reached,
# and the rows of a group that differ in a way the key hides - so the
# planner moves the same ones. It is part of building the tree, not
# the optimizer's: `--no-pushdown` skips `optimize()` and must still
# move them.
#
# The moved terms are the bound terms as bound, with column offsets
# into the scan row, joined back into one left-deep `And` in `HAVING`
# order and put in one `Filter` between the `WHERE` `Filter` (or the
# `Scan`) and the `Aggregate`. That `Filter` is not negotiable: a
# moved term is never offered to the scan (#172). The kept terms are
# joined the same way and go through `_split_expr` as `HAVING` always
# did; a moved term has no aggregate call, so no slot changes.


def _moves_below_aggregate(term: Expr, group_by: Sequence[Expr]) -> bool:
    """Whether one `AND`-term of `HAVING` moves: it has no aggregate
    call, every column in it lies inside a `GROUP BY` key subexpression
    (or it has no column), and it is not an integer literal `0` - the
    always-false term SQLite 3.50.4 leaves in `HAVING`
    (`ExprAlwaysFalse` in `havingToWhereExprCb`; measured, #141)."""
    if contains_aggregate(term):
        return False
    if not references_only_keys(term, group_by):
        return False
    if isinstance(term, Literal) and type(term.value) is int and term.value == 0:
        return False
    return True


def _move_having_terms(stmt: BoundSelectStatement) -> tuple[Expr | None, Expr | None]:
    """`(moved, kept)`: the `HAVING` terms that move below the
    aggregate, and those that stay, each joined into one left-deep
    `And` in `HAVING` order, or `None` when there are none. With no
    `GROUP BY`, or when no term moves, `kept` is `stmt.having` itself,
    so the tree is exactly the one built before #141. *stmt* is not
    changed."""
    if stmt.having is None or len(stmt.group_by) == 0:
        return None, stmt.having
    moved: list[Expr] = []
    kept: list[Expr] = []
    for term in split_conjuncts(stmt.having):
        if _moves_below_aggregate(term, stmt.group_by):
            moved.append(term)
        else:
            kept.append(term)
    if len(moved) == 0:
        return None, stmt.having
    return join_conjuncts(moved), (join_conjuncts(kept) if len(kept) > 0 else None)


def plan(stmt: BoundSelectStatement, repo: Path, tables: dict[str, ScanFactory]) -> Operator:
    """Build the operator tree for *stmt*, a repository at *repo*.

    `tables` maps `stmt.from_table` (already resolved against
    `sql/binder.py`'s own catalog, so the lookup here cannot fail for
    any statement `bind()` actually produced) to the factory that
    builds this query's `ScanSource`. Required, not defaulted (#35): this
    module has no real catalog of its own to fall back to, since it
    never imports `historian.tables.blame` or `historian.
    catalog`. Tests pass a fake factory with no repository and no git
    subprocess, per this module's own docstring; `cli.py` passes
    `historian.catalog.SCAN_FACTORIES`.

    Tree shape, per `_docs/spec.md` §3: `Scan -> Filter (WHERE) ->
    Filter (HAVING terms moved below the aggregate, not negotiable) ->
    Aggregate (grouped or whole-table) -> Filter (HAVING) -> Sort ->
    Project -> Distinct -> Limit`. The moved-terms `Filter` exists only
    with a `GROUP BY` and at least one term that moves
    (`_move_having_terms`, #141); the `HAVING` `Filter` holds the terms
    that stay, and is left out when every term moved. `Limit` is outermost, present only
    when `stmt.limit is not None`; `stmt.offset` defaults to 0 when
    absent (`OFFSET` cannot appear without `LIMIT` per §1's grammar).
    `Distinct` sits directly above `Project`, present whenever
    `stmt.distinct` is `True`. `Sort` sits directly below `Project`,
    present only when `stmt.order_by` is non-empty: `ORDER BY` may
    legally reference a column or aggregate absent from the final
    select list (`select k from g group by k order by count(*) desc`
    - confirmed against sqlite3), so `Sort` needs the wider
    pre-`Project` row, whether or not the query aggregates.
    `_docs/decisions.md` records why sorting the wider row set and
    only then projecting and deduplicating in a streaming,
    order-preserving pass gives the same answer as sorting the
    narrower, deduplicated set, for every `ORDER BY` shape `sql/
    binder.py`'s DISTINCT narrowing still allows to bind.
    `Aggregate` (and, above it, `HAVING`'s `Filter`) is inserted only
    when the query needs it - `stmt.group_by` is non-empty, or at
    least one aggregate call appears anywhere in the select list,
    `HAVING` or `ORDER BY` (`sql/walk.py`'s `is_aggregate_query`).
    The aggregate/scalar split (`_split_select_list`/`_split_expr`,
    run over the select list, then `HAVING`, then `ORDER BY`) shares
    one flat `calls` list so their offsets never collide. A `GROUP
    BY`-free, aggregate-free query gets neither `Aggregate` nor
    `HAVING`'s `Filter`.

    All three splits - select list, `HAVING`, `ORDER BY` - must run
    *before* `Aggregate` is constructed: `Aggregate.__init__` snapshots
    `calls` (and builds its own output schema from that snapshot)
    immediately, so any split run afterwards would silently hand a
    downstream operator an offset `Aggregate` never built a column for.

    `sql/binder.py` refuses to bind a `HAVING` clause on a
    non-aggregate query at all - `HAVING` with no `GROUP BY` and no
    aggregate call in the select list is a `BindError` there (an
    aggregate call written in `HAVING` itself does not make the query
    aggregate), matching `sqlite3`'s "HAVING clause on a non-aggregate
    query" rejection - so `plan()` never legitimately sees a bound
    `having` in a query that does not aggregate. The `elif having is
    not None` branch below only exists for a `BoundSelectStatement`
    built by hand (as some planner unit tests do, bypassing `bind()`); it treats that shape as an
    ordinary predicate over the `Scan`/`Filter(WHERE)` row rather than
    routing it through `Aggregate`, since there is nothing to compute
    or group - never reached for any statement `bind()` actually
    produced. `sql/binder.py` similarly refuses to bind an
    `ORDER BY` expression with a bare aggregate call unless the query
    already aggregates, so `plan()` never legitimately sees `calls`
    grow past what `stmt.group_by`/the select list already required.
    """
    source = tables[stmt.from_table](repo)
    tree: Operator = Scan(source)
    if stmt.where is not None:
        tree = Filter(tree, stmt.where)
    moved_having, kept_having = _move_having_terms(stmt)
    if moved_having is not None:
        tree = Filter(tree, moved_having, negotiable=False)

    # Aggregate when GROUP BY is written or an aggregate call appears
    # in the select list, HAVING or ORDER BY - for a bound statement the
    # same answer the binder's select-list-only call gives.
    aggregate_exprs = [item.expr for item in stmt.select_list]
    if stmt.having is not None:
        aggregate_exprs.append(stmt.having)
    aggregate_exprs.extend(item.expr for item in stmt.order_by)
    aggregate_query = is_aggregate_query(stmt.group_by, aggregate_exprs)
    calls: list[AggregateCall] = []
    select_list = _split_select_list(stmt.select_list, calls, stmt.group_by)
    having = _split_expr(kept_having, calls, stmt.group_by) if kept_having is not None else None
    order_keys = _split_order_by(stmt.order_by, calls, stmt.group_by)

    if aggregate_query:
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

    if stmt.distinct:
        tree = Distinct(tree)

    if stmt.limit is not None:
        offset = stmt.offset if stmt.offset is not None else 0
        tree = Limit(tree, limit=stmt.limit, offset=offset)

    return tree
