"""`BoundSelectStatement` -> operator tree: the planner stage of the
pipeline (`_docs/spec.md` §3 - "planner    AST -> operator tree").

`sql/binder.py` produces a `BoundSelectStatement` with every column
reference resolved to an integer offset, and `exec/operators.py`
implements the operators. This module's whole job is assembly -
deciding which of the eight operator classes to build, and in what
order, from one bound statement plus a repository path.

One plan representation, not two
----------------------------------

Per §3's own section of that name: v1 has no logical/physical split,
because every logical operation here has exactly one implementation,
so a second tree type plus a translation pass between them would be
ceremony with no decision behind it. `plan()` therefore builds
`exec/operators.py`'s actual `Operator` instances directly - up to
`Scan`, `Filter`, `Filter`, `ConstantGuard`, `Aggregate`, `Filter`,
`Sort`, `Project`, `Distinct` and `Limit` (see `plan()`) - and returns that tree as-is.
The one rewrite step after it is `plan/optimizer.py`'s `optimize()`
(#121, pushdown negotiation), which `cli.py` calls on this
tree before iterating it; it records pushed terms on the tree's
`Scan` and changes nothing else. Two rewrites SQLite makes before it
runs a query change which errors are raised, so they are part of
building the tree, here, and `--no-pushdown` (which skips
`optimize()`) still gets them: `HAVING` terms that move below the
aggregate (#141) and constant propagation in `WHERE` (#142). So is the
`ConstantGuard` that decides the column-free terms before any row
(#171).

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

from historian.exec.expression import apply_column_affinity, evaluate
from historian.exec.operators import (
    Aggregate,
    AggregateCall,
    ConstantGuard,
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
from historian.schema import Schema
from historian.sql.ast import And, BinaryOp, Expr, FunctionCall, In, Like, Literal, Operator as BinaryOperator, Or
from historian.sql.ast import OrderDirection, Star, UnaryOp
from historian.sql.binder import BoundColumnRef, BoundOrderByItem, BoundSelectItem, BoundSelectStatement
from historian.sql.walk import (
    children,
    contains_aggregate,
    expr_shape_equal,
    fix_columns,
    is_aggregate_query,
    is_constant_term,
    join_conjuncts,
    references_only_keys,
    replace_conjuncts,
    split_conjuncts,
    with_children,
)
from historian.values import Value

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
    """Whether one `HAVING` term that is not always false (below) moves:
    it has no aggregate call, and every column in it lies inside a
    `GROUP BY` key subexpression (or it has no column)."""
    if contains_aggregate(term):
        return False
    return references_only_keys(term, group_by)


# The `HAVING` that is planned - split, moved, kept and evaluated - is
# the one SQLite's parser leaves, not the one written (#171, QA round
# 1; measured on 3.50.4, `_docs/decisions.md` 2026-10-10):
#
# - The parser builds every `AND` with `sqlite3ExprAnd`, which replaces
#   `x AND y` by the integer `0` when one side is always false and
#   neither side contains a function call. Always false is an integer
#   literal `0` (`0`, `00`, `(0)`; not `-0`, `+0`, `0.0`, `'0'`, `NOT
#   0`), `x IN ()` with no call in `x`, or an `AND` already folded. A
#   function call is a `FunctionCall` (aggregates included) or a `LIKE`.
# - `x IN ()` with no call in `x` is replaced by `FALSE` (the integer
#   `0`), `x NOT IN ()` by `TRUE` (`1`). With a call in `x` they are
#   `FALSE AND x` and `TRUE OR x`, built without the fold.
# - A select-list alias is a plain name when the `HAVING` is parsed:
#   no call, not a literal. Its own expression was parsed, and folded,
#   with the select list, and is spliced in after.
# - `havingToWhere` then walks the `AND`s of the result: an always-
#   false term stays (`ExprAlwaysFalse`) - an integer `0`, the
#   `FALSE` of `x IN ()` over a call included, whose `x` is a term of
#   its own - and every other term moves by `_moves_below_aggregate`.
#
# A fold only drops parts that call nothing outside an alias, so the
# rewrite changes no row; it changes which terms move, which are
# constant, and which operands a condition skips, as it does in SQLite.
# An alias is recognised as the very select-list expression object the
# binder splices in (`sql/bind_expr.py`, `_find_alias_expr`).


def _is_alias(expr: Expr, aliases: Sequence[Expr]) -> bool:
    for alias in aliases:
        if expr is alias:
            return True
    return False


def _is_zero(expr: Expr) -> bool:
    return isinstance(expr, Literal) and type(expr.value) is int and expr.value == 0


def _as_parsed(having: Expr, aliases: Sequence[Expr]) -> Expr:
    """*having* as SQLite's parser leaves it: every `AND` it folds
    replaced by `Literal(0)`, every `x IN ()`/`x NOT IN ()` by
    `Literal(0)`/`Literal(1)` when `x` calls nothing and by `0 AND x`/
    `1 OR x` when it does; *having* itself when nothing changes.
    Bottom-up over an explicit stack (#107): `work` holds `(node,
    in_alias, expanded)`, `results` one `(expr, always_false,
    calls_function)` per finished node."""
    results: list[tuple[Expr, bool, bool]] = []
    work: list[tuple[Expr, bool, bool]] = [(having, False, False)]
    while work:
        node, in_alias, expanded = work.pop()
        if not in_alias and _is_alias(node, aliases):
            if not expanded:
                work.append((node, False, True))
                work.append((node, True, False))  # its own expression, its own parse
                continue
            body = results.pop()[0]
            results.append((body, False, False))  # to the fold, a plain name
            continue
        kids = children(node)
        if not expanded:
            work.append((node, in_alias, True))
            for kid in reversed(kids):
                work.append((kid, in_alias, False))
            continue
        done = results[len(results) - len(kids) :]
        del results[len(results) - len(kids) :]
        calls = isinstance(node, (FunctionCall, Like))
        for _expr, _false, kid_calls in done:
            calls = calls or kid_calls
        if isinstance(node, And) and (done[0][1] or done[1][1]) and not calls:
            results.append((Literal(0, node.position), True, False))
            continue
        if isinstance(node, In) and len(node.values) == 0:
            literal = Literal(1 if node.negated else 0, node.position)
            if not calls:
                results.append((literal, not node.negated, False))
                continue
            # Over a call: `FALSE AND x` / `TRUE OR x`, never folded.
            left = done[0][0]
            if node.negated:
                results.append((Or(literal, left, node.position), False, True))
            else:
                results.append((And(literal, left, node.position), False, True))
            continue
        new_kids = [entry[0] for entry in done]
        changed = False
        for old, new in zip(kids, new_kids):
            if old is not new:
                changed = True
        rebuilt = with_children(node, new_kids) if changed else node
        results.append((rebuilt, _is_zero(node), calls))
    return results[0][0]


def _move_having_terms(stmt: BoundSelectStatement) -> tuple[Expr | None, Expr | None]:
    """`(moved, kept)`: the terms of the parsed `HAVING` (`_as_parsed`)
    that move below the aggregate, and those that stay, each joined
    into one left-deep `And` in `HAVING` order, or `None` when there
    are none. With no `GROUP BY`, or when no term moves, `kept` is the
    parsed `HAVING` - `stmt.having` itself when the parser changes
    nothing, so the tree is exactly the one built before #141. *stmt*
    is not changed."""
    if stmt.having is None:
        return None, None
    aliases = [item.expr for item in stmt.select_list if item.alias is not None]
    having = _as_parsed(stmt.having, aliases)
    if len(stmt.group_by) == 0:
        return None, having
    moved: list[Expr] = []
    kept: list[Expr] = []
    pending: list[Expr] = [having]
    while pending:
        term = pending.pop()
        if isinstance(term, And):
            pending.append(term.right)
            pending.append(term.left)
        elif _is_zero(term) or not _moves_below_aggregate(term, stmt.group_by):
            kept.append(term)
        else:
            moved.append(term)
    if len(moved) == 0:
        return None, having
    return join_conjuncts(moved), (join_conjuncts(kept) if len(kept) > 0 else None)


# --- Constant propagation in WHERE (#142) ----------------------------------
#
# `_docs/spec.md` §3, "Constant propagation in `WHERE`". SQLite
# (`propagateConstants` in select.c) finds every top-level `WHERE`
# conjunct of the form `column = constant` - a source - and replaces
# every other occurrence of that column in the `WHERE` with the
# constant, which keeps the column's affinity. The rows that pass never
# change (each one already has that value in that column), but which
# sub-expressions run does, and so which queries raise.
#
# A source is `X = K`, `K = X` or `X IN (K)` (one element, not negated,
# which SQLite's parser turns into `X = K`), with `X` a bound column
# and `K` a literal or a chain of unary `+`/`-` over a numeric literal.
# A constant that is any other expression is #179. Of two sources for
# one column SQLite keeps the last (`findConstInWhere` walks the `AND`
# tree right side first and ignores a column it already has), and the
# earlier one is rewritten like any other term. The source used keeps
# its own column; nothing else in the `WHERE` does, at any depth.
#
# The replacement is a `FixedColumnRef` holding the constant converted
# by the column's affinity (`apply_column_affinity`) - the value
# SQLite's `OP_Affinity` gives it. Only the `WHERE` is rewritten: the
# select list, `GROUP BY`, `HAVING` (moved or not, #141), `ORDER BY`
# and aggregate arguments keep the column, and a `HAVING` term is never
# a source. v1 has no `COLLATE` and no non-`BINARY` column, the one
# case SQLite does not propagate.


def _constant_of(expr: Expr) -> Literal | UnaryOp | None:
    """*expr* if it is a source's constant - a `Literal`, or a chain of
    unary `+`/`-` over a numeric `Literal` - and `None` otherwise."""
    node = expr
    while isinstance(node, UnaryOp):
        node = node.operand
    if not isinstance(node, Literal):
        return None
    if node is not expr and not isinstance(node.value, (int, float)):
        return None
    assert isinstance(expr, (Literal, UnaryOp))
    return expr


def _source_of(term: Expr) -> tuple[BoundColumnRef, Literal | UnaryOp] | None:
    """`(column, constant)` when one top-level `WHERE` term is a source,
    `None` otherwise."""
    if isinstance(term, BinaryOp) and term.op is BinaryOperator.EQ:
        right_constant = _constant_of(term.right)
        if isinstance(term.left, BoundColumnRef) and right_constant is not None:
            return term.left, right_constant
        left_constant = _constant_of(term.left)
        if isinstance(term.right, BoundColumnRef) and left_constant is not None:
            return term.right, left_constant
        return None
    if isinstance(term, In) and not term.negated and len(term.values) == 1:
        constant = _constant_of(term.values[0])
        if isinstance(term.left, BoundColumnRef) and constant is not None:
            return term.left, constant
    return None


def _propagate_constants(where: Expr, schema: Schema) -> Expr:
    """*where* with SQLite's constant propagation applied - see the
    section comment above. *where* itself when it has no source, so a
    `WHERE` with none plans exactly as before #142; otherwise the same
    `AND` shape with every term but the sources used rewritten, and a
    term with nothing to replace kept as the same object. *schema* is
    the scan's, which the `WHERE` `Filter` reads.

    Loops only (#107): `split_conjuncts`, a backwards scan for the
    sources, and the iterative `fix_columns` and `replace_conjuncts`."""
    terms = split_conjuncts(where)
    fixed: dict[int, Value] = {}
    source_index: dict[int, int] = {}
    for index in range(len(terms) - 1, -1, -1):
        found = _source_of(terms[index])
        if found is None:
            continue
        column, constant = found
        if column.offset in fixed:
            continue
        value = evaluate(constant, (), schema)
        assert not isinstance(value, bool), "plan/planner.py: a source constant is a value, not a condition"
        fixed[column.offset] = apply_column_affinity(value, schema.columns[column.offset].type)
        source_index[column.offset] = index
    if len(fixed) == 0:
        return where
    kept = set(source_index.values())
    rewritten: list[Expr] = []
    for index, term in enumerate(terms):
        if index in kept:
            rewritten.append(term)
        else:
            rewritten.append(fix_columns(term, fixed))
    return replace_conjuncts(where, rewritten)


# --- WHERE terms with no column, decided before any row (#171) -------------
#
# `_docs/spec.md` §3, "`WHERE` terms with no column reference". SQLite
# evaluates every `WHERE` term that reads no column once, before the
# first row, in `WHERE` order (`sqlite3WhereBegin` codes each term with
# no table dependency ahead of the loop): the first that is not `TRUE`
# ends the query over zero rows, and one that raises raises on empty
# input too. The terms are the `WHERE` after constant propagation
# (#142), split on its top-level `AND`s as the `Filter` splits it, so a
# column replaced by a `FixedColumnRef` no longer counts; the moved
# `HAVING` terms (#141) that have no column follow, in `HAVING` order.
#
# They go in one `ConstantGuard` directly above the topmost of the two
# `Filter`s - below `Aggregate`, so a whole-table aggregate over a false
# constant still emits its one row. Both `Filter`s keep every term,
# constants included: by the time a row reaches them every constant is
# `TRUE` and cannot raise, so evaluating it again is harmless, and
# pushdown negotiation sees the same terms it did before. Built here,
# not in the optimizer, so `--no-pushdown` gets it too.


def _constant_terms(where: Expr | None, moved_having: Expr | None) -> list[Expr]:
    """The column-free terms of *where* (already propagated) in order,
    then those of *moved_having* in order - the very term objects the
    two `Filter`s split their predicates into."""
    terms: list[Expr] = []
    for predicate in (where, moved_having):
        if predicate is None:
            continue
        for term in split_conjuncts(predicate):
            if is_constant_term(term):
                terms.append(term)
    return terms


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

    The `WHERE` `Filter` holds the `WHERE` after constant propagation
    (`_propagate_constants`, #142): `stmt.where` itself when it has no
    `column = constant` term.

    Tree shape, per `_docs/spec.md` §3: `Scan -> Filter (WHERE) ->
    Filter (HAVING terms moved below the aggregate, not negotiable) ->
    ConstantGuard -> Aggregate (grouped or whole-table) -> Filter
    (HAVING) -> Sort -> Project -> Distinct -> Limit`. `ConstantGuard`
    exists only when a term of either `Filter` below it has no column
    (`_constant_terms`, #171). The moved-terms `Filter` exists only
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
    where = _propagate_constants(stmt.where, tree.schema) if stmt.where is not None else None
    if where is not None:
        tree = Filter(tree, where)
    moved_having, kept_having = _move_having_terms(stmt)
    if moved_having is not None:
        tree = Filter(tree, moved_having, negotiable=False)
    constant_terms = _constant_terms(where, moved_having)
    if len(constant_terms) > 0:
        tree = ConstantGuard(tree, constant_terms)

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

    # HAVING is one condition (`split_terms=False`): SQLite evaluates it
    # whole, so an always-false literal simplifies its top-level AND
    # (#189). The WHERE and moved-terms Filters above split theirs.
    if aggregate_query:
        tree = Aggregate(tree, calls, group_by=stmt.group_by)
        if having is not None:
            tree = Filter(tree, having, split_terms=False)
    elif having is not None:
        # See the docstring above: unreachable via bind(), kept only
        # so a hand-built BoundSelectStatement still gets a sane tree
        # rather than plan() crashing on it.
        tree = Filter(tree, having, split_terms=False)

    if order_keys:
        tree = Sort(tree, order_keys)

    tree = Project(tree, select_list)

    if stmt.distinct:
        tree = Distinct(tree)

    if stmt.limit is not None:
        offset = stmt.offset if stmt.offset is not None else 0
        tree = Limit(tree, limit=stmt.limit, offset=offset)

    return tree
