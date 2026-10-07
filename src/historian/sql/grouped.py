"""The grouped and DISTINCT narrowing checks: historian's own rejections.

Split out of `sql/binder.py` (issue #151). Walks bound expressions to
find a bare column outside every aggregate call and every key, for
`bind()`'s step 10. Imports `sql/bound.py` and below.

A second, separate check lives in `bind()` itself, after the whole
select list is bound: when any select-list item's expression contains
an aggregate call anywhere, every item is walked for a bare column
reference that sits outside every aggregate call's own arguments
(`_split_for_grouped_check`, extended by issue #69 for `GROUP BY` -
see that section below) - `SELECT path, count(*) FROM blame` raises,
naming `path`, when there is no `GROUP BY` for a bare, non-aggregated
column to be grouped by. This is a
deliberate narrowing of what `sqlite3` itself accepts (it silently
picks a value from an arbitrary row) - see `_docs/decisions.md`,
2026-09-19, for the full reasoning; §1's "SQLite is right" rule does
not apply here because SQLite has no principled answer to copy, only
an unspecified internal choice.

Splitting an aggregate call out of its surrounding scalar expression
(`count(*) + 1`) and building the `Aggregate` operator itself are not
this module's job - `plan/planner.py` and `exec/operators.py` own
those, per `_docs/spec.md` §3's "Expression evaluation" split. This
module only decides whether the query is legal to run at all.

"""

from __future__ import annotations

from historian.sql.ast import (
    ColumnRef,
    Expr,
    FunctionCall,
)
from historian.sql.bound import BindError, BoundSelectItem
from historian.sql.walk import BoundColumnRef, children, expr_shape_equal, is_aggregate_query

# --- The grouped narrowing (issue #60, extended by #69) ---------------------
#
# #60's own rule ("a bare column mixed with an aggregate, no GROUP BY,
# is a BindError") extends here to grouped queries: a select-list
# expression must be an aggregate call, a GROUP BY key (exactly, or
# built purely from GROUP BY keys - `_docs/decisions.md`'s follow-on
# note), or a BindError. `_split_for_grouped_check` walks the
# expression and, at every node, first checks whether the whole
# subtree matches a GROUP BY key by shape (`expr_shape_equal`) - if
# so, that subtree is covered and is never walked into for a bad bare
# column, whatever it contains.
#
# DISTINCT (issue #78) reuses this exact walk for a different question,
# in `bind()`'s step 10 (`_step10b_distinct_order_by`) rather than a dedicated function here: when
# `stmt.distinct` is set, every bare column an ORDER BY key touches
# must match a select-list item by shape (exactly, or be built purely
# from select-list items) - `_split_for_grouped_check(order_item.expr,
# select_exprs)`, passing the bound select-list expressions in place
# of `group_by`'s keys. An ordinal ORDER BY key needs no extra check:
# it already resolves to the referenced select-list item's own bound
# expression (`_bind_order_by`, above), which trivially
# shape-matches itself as the first `group_keys` entry checked. A
# select-list alias reference is the same story: `_resolve_column`
# (`ctx.alias_first=True` for ORDER BY) already splices in that item's
# own bound expression in its place.
#
# Issue #103: an aggregate call is a bare "key touch" too, and the
# DISTINCT narrowing above needs it held to the same "must itself
# shape-match" rule a bare column already gets - unlike the three
# GROUP BY-keyed callers above, where an aggregate call is never
# required to equal a particular key (it is what makes a bare column
# under it exempt in the first place). `_split_for_grouped_check`'s
# `strict_function_calls` parameter, default `False`, switches that
# one branch: `False` (every existing caller) keeps today's "any
# aggregate call is fine wherever it sits" behaviour; `True` (the
# DISTINCT caller alone) treats an aggregate call that does not itself
# shape-match one of the given keys as the "bad" node the walk
# reports back, exactly as a bare column would be - so `bad` can now
# be the offending `FunctionCall` itself, not only a `BoundColumnRef`,
# and callers that pass `True` branch on its type to phrase the
# `BindError`.
#
# Confirmed against sqlite3 3.51.0 that this narrows what SQLite
# itself accepts - `create table u2(p,n); insert into u2
# values('x',2),('x',1),('y',1); select distinct p from u2 order by
# n;` returns `y` then `x` in real SQLite, and this shape is a
# `BindError` here instead. Unlike the GROUP BY-narrowing precedent
# above, the justification is **not** "SQLite's own answer is an
# unspecified internal choice" (2026-09-19's entry) restated for a new
# clause - it is the oracle, not historian's own determinism:
# historian's own pipeline (stable `Sort`, then a streaming
# first-seen `Distinct`) is already fully deterministic for this shape
# even without the narrowing, but SQLite's answer for it is not
# reproducible from any documented rule, so matching it would mean
# reverse-engineering an undocumented, version-fragile SQLite internal
# and getting it wrong invisibly until the oracle disagrees. See
# `_docs/decisions.md` for the discriminating arithmetic in full.
#
# DISTINCT (`_step10b_distinct_order_by`) runs unconditionally whenever
# `stmt.distinct` is set, independent of whether the query aggregates
# at all - the two narrowings ask genuinely different questions and
# neither replaces the other. It passes `strict_function_calls=True`
# (#103), and only it does: the one caller matched against the select
# list rather than `GROUP BY`'s keys, so the one place an aggregate
# call is not automatically exempt.
#
# HAVING (step 10 of `bind()`, `_step10a_grouped_narrowing`):
#
# A bare column reference in HAVING
# that is neither a GROUP BY key (matched by shape, exactly
# `_check_grouped_select_list`'s own rule for the select list)
# nor inside an aggregate call's own arguments is a BindError,
# whether or not GROUP BY is present - with no GROUP BY there
# are no keys, so every bare column outside an aggregate is
# rejected. sqlite3 instead evaluates it against an arbitrary
# row of the (possibly single, implicit) group - confirmed
# live: `select count(*) from t having path = 'x'` -> `3`;
# `select a, count(*) from t group by a having path = 'z'` ->
# `2|1`. Reusing `_split_for_grouped_check`'s own walk is the
# same "grouped but not a key" reasoning `_docs/decisions.md`'s
# follow-on note already gives for the select list - a
# non-key, non-aggregate column's value is still whichever row
# sqlite3 happened to visit last, which breaks AGENTS.md's
# determinism rule and would feed the oracle unfixable false
# mismatches, exactly as it would in the select list.
#
# ORDER BY of an aggregate query (same function):
#
# The same "grouped but not a key" narrowing HAVING already
# gets (2026-09-24's decisions.md entry), extended here: once
# the query aggregates, every ORDER BY expression must be an
# aggregate call, a GROUP BY key (matched by shape), or built
# purely from GROUP BY keys - reusing `_split_for_grouped_check`
# rather than a fresh walk. This deliberately diverges from
# sqlite3, which accepts a bare non-key, non-aggregate ORDER BY
# column and sorts by an arbitrary row's value per group
# (confirmed live: `select k, count(*) from g group by k order
# by v` succeeds in sqlite3) - see `_docs/decisions.md` for the
# dated follow-on note recording this.


def _split_for_grouped_check(
    expr: Expr, group_keys: tuple[Expr, ...], *, strict_function_calls: bool = False
) -> tuple[bool, Expr | None]:
    """`(does expr contain an aggregate call anywhere outside a
    covered GROUP BY key, the first bad node found outside both every
    aggregate call's own arguments and every covered GROUP BY key - or
    None)`.

    The "bad" node is a bare `BoundColumnRef` for every caller. When
    `strict_function_calls` is `True` (the DISTINCT/ORDER BY caller in
    `bind()` alone - issue #103), a `FunctionCall` that does not itself
    shape-match one of `group_keys` is bad too, reported as that
    `FunctionCall` itself rather than `None` - the same "must equal a
    given key" rule a bare column already gets, extended to an
    aggregate call for the one caller where an aggregate call is
    required to match a select-list item rather than being exempt by
    virtue of being an aggregate at all.

    A loop over an explicit stack (issue #107), visiting nodes in
    pre-order, left to right - operands pushed in reverse - so "first"
    means what it always has: the leftmost bad node. At every node the
    whole subtree is first matched against every key; a match, an
    aggregate call, a column and a literal are never walked into.
    """
    has_aggregate = False
    bad: Expr | None = None
    pending: list[Expr] = [expr]
    while pending:
        node = pending.pop()
        if _matches_any_key(node, group_keys):
            continue
        if isinstance(node, FunctionCall):
            has_aggregate = True
            if strict_function_calls and bad is None:
                bad = node
            continue
        if isinstance(node, BoundColumnRef):
            if bad is None:
                bad = node
            continue
        if isinstance(node, ColumnRef):
            raise AssertionError("sql/binder.py: _split_for_grouped_check needs a bound tree")
        for operand in reversed(children(node)):
            pending.append(operand)
    return has_aggregate, bad


def _matches_any_key(expr: Expr, group_keys: tuple[Expr, ...]) -> bool:
    for key in group_keys:
        if expr_shape_equal(expr, key):
            return True
    return False


def _check_grouped_select_list(
    bound_items: tuple[BoundSelectItem, ...], group_by: tuple[Expr, ...]
) -> None:
    """Raise `BindError` for the first select-list item that is
    neither an aggregate call, a `GROUP BY` key, nor built purely from
    `GROUP BY` keys - but only when the query is grouped at all
    (`group_by` is non-empty) or some select-list item already has an
    aggregate call somewhere (#60's original trigger, unchanged for a
    plain aggregate-free, GROUP BY-free query)."""
    if not is_aggregate_query(group_by, [item.expr for item in bound_items]):
        return
    for item in bound_items:
        _has_aggregate, bad_column = _split_for_grouped_check(item.expr, group_by)
        if bad_column is not None:
            reason = (
                "must appear in the GROUP BY clause or be used in an aggregate function"
                if group_by
                else "must appear in an aggregate function since this query has no GROUP BY"
            )
            raise BindError(
                f"column {bad_column.name} {reason}",
                bad_column.position,
                (),
            )
