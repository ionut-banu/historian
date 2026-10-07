"""Name resolution: AST -> bound AST, or a structured error.

The fourth stage of the pipeline in `_docs/spec.md` §3
("binder     AST + resolved columns, errors for unknown names").
Consumes the `SelectStatement` `sql/parser.py` builds and a table
catalog passed in by the caller (`bind()`'s required `catalog`
parameter), resolves every table and column reference against them,
and produces a new tree in which every column reference is a
zero-based integer offset into a `Row` - never a name
`exec/expression.py` (#12) would need to look up per row, per §3's
"column references resolve to integer offsets at bind time rather
than by name at runtime."

Issue #9. Implements the binder half of §3's pipeline. This module
does not build the real table catalog itself (issue #35) -
`historian/catalog.py` does, from direct imports of each table
module, and only `cli.py` and tests that want the real catalog import
it. `bind()`'s `catalog` parameter is what lets this module resolve
names against `blame` (or any fake schema, in tests) without ever
importing `historian.tables.blame` or `subprocess` itself.

Not in this module
-------------------

**Type/affinity checking** - `WHERE line_no = '5'` binds successfully
here; whether `'5'` needs coercing to compare against an `INTEGER`
column is `exec/expression.py`'s job (#12), per spec §3's explicit
split. **The rendered `error: ...` / caret / "blame has: ..."
box from spec §5** - `cli.py` (#41); `BindError` here carries
structured fields (message, position, available names), not text to
print.


Layout (issue #151)
--------------------

This module holds `bind()` and one function per step of the order
below. The rest is layered under it, each module importing only those
before it: `sql/bound.py` (`BindError`, the bound tree),
`sql/bind_expr.py` (name resolution, single-expression binding),
`sql/bind_clauses.py` (per-clause binding), `sql/grouped.py` (the
grouped and DISTINCT narrowing checks). The notes on aggregate calls,
alias fallback, the bound tree and ASCII folding live with the code
they describe.

Resolution order
------------------

When a statement has more than one error, `bind()` reports the one
SQLite reports (issue #115, spec §3 "Errors"). Measured against the
oracle - Python's `sqlite3` module (the version `tests/conftest.py` pins) - by splicing one,
two and three erroring fragments from different clauses into a base
query (`tests/differential/test_error_order.py`), the order is:

1. The FROM table, then the qualifier of any `x.*` select-list item
   (`SELECT ghost_s, ghost.* FROM blame` reports `no such table:
   ghost`).
2. LIMIT and OFFSET, walked as one expression with LIMIT on the
   left, as SQLite builds it (#144) - only what SQLite rejects there:
   a column reference (any, even a real column or an alias: LIMIT
   sees no columns), an unknown function, a wrong argument count or
   an aggregate call. Which of several is reported follows the walk
   below (`_resolve_limit_offset`).
3. The select list, items left to right.
4. `HAVING` on a non-aggregate query.
5. `HAVING`.
6. `WHERE`. In a non-aggregate query an aggregate call here is
   reported in place, in left-to-right order with the clause's names.
7. `ORDER BY`, terms left to right: every name error, and an
   integer term below 1 or above 65535 at its own turn (#144); then an
   ordinal past the end of the select list.
8. `GROUP BY`, terms left to right: every name error, and an integer
   term below 1 or above 65535 at its own turn; then an ordinal past
   the end of the select list, then an aggregate key.
9. Late: an aggregate call in the `WHERE` of an aggregate query, or in
   the `ORDER BY` of a non-aggregate one (`_Context.late_misuse`).
   "Aggregate query" means `GROUP BY` is written or the select list
   contains an aggregate call.
10. historian's own rejections of queries SQLite accepts, in this
    order: the bare column with an aggregate or not a `GROUP BY` key
    in the select list, in `HAVING`, in `ORDER BY`; the `SELECT
    DISTINCT ... ORDER BY` key; a `LIMIT`/`OFFSET` that is not a
    literal integer. They come after every error SQLite raises, so
    when a query has both, SQLite's wins.

Each select-list item and each `ORDER BY`/`GROUP BY` term is its
own root, and the first root with an error ends resolution, so
`select ghost1, ghost2 from blame` reports `ghost1`. Inside one root,
the error reported is the one SQLite's resolution walk records last:
a function call records its own error (unknown name, then arity,
then aggregate misuse) before walking its arguments, a column that
does not resolve records and stops the walk up to the nearest call,
and most other nodes stop at once when an error is recorded - see
`sql/bind_expr.py`'s "Which error is reported" (#144) for the six
rules. A nested aggregate is reported where it is found, in any
clause. None of this is `SelectStatement`'s own field order
(`select_list`, `from_table`, `where`, ...), which a naive walk of the
dataclass's fields would follow instead.
"""

from __future__ import annotations

import dataclasses

from historian.schema import Schema
from historian.sql.ast import Expr, FunctionCall, SelectStatement, Star
from historian.sql.bind_clauses import (
    _bind_group_by,
    _bind_limit_offset,
    _bind_order_by,
    _bind_select_item,
    _resolve_limit_offset,
)
from historian.sql.bind_expr import _bind_expr, _bind_star, _Context, _resolve_table
from historian.sql.bound import (
    BindError,
    BoundOrderByItem,
    BoundSelectItem,
    BoundSelectStatement,
)
from historian.sql.grouped import _check_grouped_select_list, _split_for_grouped_check
from historian.sql.walk import BoundColumnRef, is_aggregate_query

__all__ = [
    "BindError",
    "BoundColumnRef",
    "BoundOrderByItem",
    "BoundSelectItem",
    "BoundSelectStatement",
    "bind",
]


# --- The phases of `bind()` ---------------------------------------------------
#
# One plain function per step of the module docstring's "Resolution
# order" (issue #151). Each takes what it needs and returns what a
# later step needs; the only state shared between steps is the
# `late_misuse` list, passed explicitly to steps 6, 7 and 9.


def _step1_from_table(stmt: SelectStatement, catalog: dict[str, Schema]) -> _Context:
    """Step 1: the FROM table, then the qualifier of any `x.*`
    select-list item - SQLite expands stars before resolving any name,
    so `SELECT ghost_s, ghost.* FROM blame` reports `ghost`."""
    ctx = _resolve_table(stmt, catalog)
    for item in stmt.select_list:
        if isinstance(item.expr, Star) and item.alias is None:
            _bind_star(item.expr, ctx)
    return ctx


def _step2_limit_offset_names(stmt: SelectStatement, ctx: _Context) -> None:
    """Step 2: LIMIT, then OFFSET, walked as one tree: only what SQLite
    rejects there (a column reference, an unknown function, an
    aggregate call). The literal-only rule (#77) is historian's own and
    waits for step 10."""
    limit_offset = tuple(expr for expr in (stmt.limit, stmt.offset) if expr is not None)
    if limit_offset:
        _resolve_limit_offset(limit_offset, ctx)


def _step3_select_list(stmt: SelectStatement, ctx: _Context) -> tuple[BoundSelectItem, ...]:
    """Step 3: the select list, left to right. Items bind against the
    plain `ctx`, with no alias fallback, so aliases stay invisible to
    each other (#32)."""
    bound_items: list[BoundSelectItem] = []
    for item in stmt.select_list:
        bound_items.extend(_bind_select_item(item, ctx))
    return tuple(bound_items)


def _step4_having_on_plain_query(stmt: SelectStatement, aggregate_query: bool) -> None:
    """Step 4: HAVING on a non-aggregate query, before HAVING's own
    names."""
    if stmt.having is not None and not aggregate_query:
        # A `HAVING` clause only makes sense against an aggregate
        # query - confirmed live against `sqlite3 3.51.0`:
        # `select path from t having path = 'x'` (no GROUP BY, no
        # aggregate anywhere) -> "HAVING clause on a non-aggregate
        # query". Whether the query *is* an aggregate query is decided
        # by `GROUP BY`'s presence or an aggregate call in the select
        # list alone - `select count(*) from t having 1` succeeds
        # (the select list's own `count(*)` is enough, even though
        # HAVING's own predicate has no aggregate call in it at all).
        # An aggregate call written in HAVING itself does *not* by
        # itself make the query aggregate, also confirmed live:
        # `select path from t having count(*) > 1` still raises the
        # identical "HAVING clause on a non-aggregate query" error -
        # only the select list (or GROUP BY) decides that question.
        raise BindError(
            "HAVING requires an aggregate query - add GROUP BY or an "
            "aggregate function to the select list",
            stmt.having.position,
            (),
        )


def _step5_having(
    stmt: SelectStatement, ctx: _Context, items: tuple[BoundSelectItem, ...]
) -> Expr | None:
    """Step 5: HAVING (issue #69): the one clause where an aggregate
    call *is* legal, referenced directly or by select-list alias.
    Alias fallback on, column-first. Splitting an aggregate call out
    into an `Aggregate` slot is `plan/planner.py`'s job (spec §3's
    "Expression evaluation")."""
    having_ctx = dataclasses.replace(
        ctx,
        select_items=items,
        alias_fallback=True,
        alias_first=False,
        reject_aggregates=False,
    )
    return _bind_expr(stmt.having, having_ctx) if stmt.having is not None else None


def _step6_where(
    stmt: SelectStatement,
    ctx: _Context,
    items: tuple[BoundSelectItem, ...],
    aggregate_query: bool,
    late_misuse: list[BindError],
) -> Expr | None:
    """Step 6: WHERE: alias fallback on (#32), column-first, aggregate
    calls rejected (#60) - on the spot in a non-aggregate query, but
    late (step 9) in an aggregate one, which is when SQLite reports
    them."""
    where_ctx = dataclasses.replace(
        ctx,
        select_items=items,
        alias_fallback=True,
        alias_first=False,
        reject_aggregates=True,
        late_misuse=late_misuse if aggregate_query else None,
        clause="WHERE",
    )
    return _bind_expr(stmt.where, where_ctx) if stmt.where is not None else None


def _step7_order_by(
    stmt: SelectStatement,
    ctx: _Context,
    items: tuple[BoundSelectItem, ...],
    aggregate_query: bool,
    late_misuse: list[BindError],
) -> tuple[BoundOrderByItem, ...]:
    """Step 7: ORDER BY (#61): alias-first. A bare aggregate call is
    legal only in an aggregate query; in any other it is rejected late
    (step 9). See the "ORDER BY" section comment above
    `_bind_order_by`."""
    order_ctx = dataclasses.replace(
        ctx,
        select_items=items,
        alias_fallback=True,
        alias_first=True,
        reject_aggregates=not aggregate_query,
        late_misuse=late_misuse,
        clause="ORDER BY",
    )
    return _bind_order_by(stmt.order_by, order_ctx, items)


def _step9_late_misuse(late_misuse: list[BindError]) -> None:
    """Step 9: the late aggregate misuse: an aggregate call in the
    WHERE of an aggregate query, or in the ORDER BY of a non-aggregate
    one."""
    if late_misuse:
        raise late_misuse[0]


# Step 10: historian's own rejections of queries SQLite accepts, after
# every error SQLite raises (#115), in the order they had before. Three
# functions, run in this order: the grouped narrowing (select list,
# HAVING, ORDER BY), the DISTINCT narrowing, the literal LIMIT/OFFSET.


def _step10a_grouped_narrowing(
    bound_items: tuple[BoundSelectItem, ...],
    bound_having: Expr | None,
    bound_order_by: tuple[BoundOrderByItem, ...],
    bound_group_by: tuple[Expr, ...],
    aggregate_query: bool,
) -> None:
    """Step 10, first part: the select list - #60's
    bare-column-mixed-with-aggregate narrowing, extended by #69 to
    GROUP BY keys - then HAVING, then ORDER BY of an aggregate query.
    Why each is narrowed is in `sql/grouped.py`'s comments."""
    _check_grouped_select_list(bound_items, bound_group_by)
    if bound_having is not None:
        _has_aggregate, bad_column = _split_for_grouped_check(bound_having, bound_group_by)
        if bad_column is not None:
            raise BindError(
                f"column {bad_column.name} must appear in the GROUP BY "
                "clause or be used in an aggregate function",
                bad_column.position,
                (),
            )
    if aggregate_query:
        for order_item in bound_order_by:
            _has_aggregate, bad_column = _split_for_grouped_check(order_item.expr, bound_group_by)
            if bad_column is not None:
                raise BindError(
                    f"column {bad_column.name} must appear in the GROUP BY "
                    "clause or be used in an aggregate function",
                    bad_column.position,
                    (),
                )


def _step10b_distinct_order_by(
    stmt: SelectStatement,
    bound_items: tuple[BoundSelectItem, ...],
    bound_order_by: tuple[BoundOrderByItem, ...],
) -> None:
    """Step 10, second part: DISTINCT (issue #78, #103): a narrowing on
    ORDER BY, matched against the select list. Why, and the sqlite3
    evidence, is in `sql/grouped.py`'s "DISTINCT" comment."""
    if stmt.distinct:
        select_exprs = tuple(item.expr for item in bound_items)
        for order_item in bound_order_by:
            _has_select_match, bad = _split_for_grouped_check(
                order_item.expr, select_exprs, strict_function_calls=True
            )
            if isinstance(bad, FunctionCall):
                raise BindError(
                    f"aggregate {bad.name}(...) must appear in the select list "
                    "to be used in ORDER BY together with SELECT DISTINCT",
                    bad.position,
                    (),
                )
            if bad is not None:
                raise BindError(
                    f"column {bad.name} must appear in the select list "
                    "to be used in ORDER BY together with SELECT DISTINCT",
                    bad.position,
                    (),
                )


def _step10c_limit_offset_literals(stmt: SelectStatement) -> tuple[int | None, int | None]:
    """Step 10, third part: LIMIT / OFFSET (issue #77): a literal
    integer, resolved to a plain Python `int` - see
    `_bind_limit_offset`. `stmt.offset` is never set while `stmt.limit`
    is `None` (the parser's own guarantee, `sql/ast.py`'s docstring),
    so the bound offset is correspondingly `None` in that case too."""
    bound_limit = _bind_limit_offset(stmt.limit, "LIMIT") if stmt.limit is not None else None
    bound_offset = _bind_limit_offset(stmt.offset, "OFFSET") if stmt.offset is not None else None
    return bound_limit, bound_offset


# --- Entry point -------------------------------------------------------------


def bind(stmt: SelectStatement, catalog: dict[str, Schema]) -> BoundSelectStatement:
    """Resolve every table and column reference in `stmt` against
    `catalog`, and expand `SELECT *` / `table.*`.

    Raises `BindError` - never returns `None`/`False` - for the first
    error in the order the module docstring's "Resolution order"
    section states, which is SQLite's (issue #115). The body below is
    that order, one `_stepN_...` call after another. `catalog` is
    required, not defaulted: this module never imports
    `historian.tables.blame` or `historian.catalog` itself (issue #35
    - AGENTS.md's "no git and no subprocess imports" for everything
    above the scan operators), so it has no real catalog of its own
    to fall back to. Callers that want the real `blame` table pass
    `historian.catalog.SCHEMAS` explicitly - `cli.py` is the one
    production call site that does.
    """
    ctx = _step1_from_table(stmt, catalog)
    _step2_limit_offset_names(stmt, ctx)
    items = _step3_select_list(stmt, ctx)
    # Whether the query aggregates at all - GROUP BY written, or an
    # aggregate call anywhere in the select list. An aggregate call in
    # HAVING or ORDER BY does not count (confirmed against sqlite3:
    # `select path from t having count(*) > 1` is still "HAVING clause
    # on a non-aggregate query"). Decides steps 4, 6 and 7.
    aggregate_query = is_aggregate_query(stmt.group_by, [item.expr for item in items])
    _step4_having_on_plain_query(stmt, aggregate_query)
    # Step 9's collection: filled while WHERE and ORDER BY bind.
    late_misuse: list[BindError] = []
    bound_having = _step5_having(stmt, ctx, items)
    bound_where = _step6_where(stmt, ctx, items, aggregate_query, late_misuse)
    bound_order_by = _step7_order_by(stmt, ctx, items, aggregate_query, late_misuse)
    # 8. GROUP BY (#69): last of the clauses, ordinal or named, alias
    # fallback on, column-first.
    bound_group_by = _bind_group_by(stmt.group_by, ctx, items)
    _step9_late_misuse(late_misuse)
    _step10a_grouped_narrowing(items, bound_having, bound_order_by, bound_group_by, aggregate_query)
    _step10b_distinct_order_by(stmt, items, bound_order_by)
    bound_limit, bound_offset = _step10c_limit_offset_literals(stmt)
    return BoundSelectStatement(
        select_list=items,
        from_table=ctx.table_name,
        where=bound_where,
        group_by=bound_group_by,
        having=bound_having,
        order_by=bound_order_by,
        limit=bound_limit,
        offset=bound_offset,
        position=stmt.position,
        distinct=stmt.distinct,
    )
