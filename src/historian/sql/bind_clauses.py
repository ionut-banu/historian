"""Per-clause binding: ordinals, LIMIT/OFFSET, GROUP BY, ORDER BY and
the select list.

Split out of `sql/binder.py` (issue #151). Each function binds one
clause (or one item of one) against a `_Context` built by `bind()`;
the order the clauses run in, which is what decides the error a
statement with several errors reports, stays in `sql/binder.py`.
Imports `sql/bind_expr.py` and below.
"""

from __future__ import annotations

import dataclasses

from historian.sql.ast import (
    Expr,
    Literal,
    OrderByItem,
    SelectItem,
    Star,
    UnaryOp,
    UnaryOperator,
)
from historian.sql.bind_expr import (
    _Context,
    _bind_expr,
    _bind_exprs,
    _bind_star,
)
from historian.sql.bound import BindError, BoundOrderByItem, BoundSelectItem
from historian.sql.walk import BoundColumnRef, contains_aggregate

# --- GROUP BY / HAVING (issue #69) ------------------------------------------
#
# An aggregate call cannot be a grouping key, however it is named -
# direct, via a select-list alias, or by ordinal (`select b, count(*)
# from t group by 2` raises the identical "aggregate functions are not allowed in the
# GROUP BY clause" sqlite3 gives for the direct and alias forms, not
# "ludicrous but legal"). `contains_aggregate` is the one predicate
# every one of those three routes checks against, after binding.


# --- Ordinal detection, shared by GROUP BY and ORDER BY (issue #61) --------
#
# SQLite treats *any* nesting of unary `+`/`-`
# and parentheses around an integer literal as an ordinal, in both
# clauses - not just a bare `Literal` (`GROUP BY 2`) or one level of
# unary (`ORDER BY -1`). Confirmed live against sqlite3 3.51.0:
#
#     order by +(+1) / -(-1) / -(-(1)) / (-(-1)) / - -1   -> ordinal 1
#     order by 1+0                                         -> constant, no sort
#     group by +1 / -(-1) / +(+1) / (1)                    -> ordinal 1
#     group by 1+0                                         -> constant expression
#
# A binary operator anywhere in the tree is never an ordinal - `1+0`
# falls straight through to the ordinary expression-binding path in
# both clauses, unaffected by this section: for `GROUP BY` that is
# exactly what keeps `GROUP BY 1+0` a `BindError` (a constant key, so a
# bare non-key select-list column stays ungrouped - the narrowing this
# issue's own `_docs/decisions.md` follow-on records must stay), and
# for `ORDER BY` it is what makes `1+0` sort by a same-valued constant
# for every row, which a stable sort leaves in original order.
#
# Parentheses never reach this module as their own node - `sql/
# parser.py`'s `_parse_primary` strips them at parse time - so only
# `UnaryOp` nesting needs unwrapping here.


def _ordinal_value(expr: Expr) -> int | None:
    """The integer value of *expr* if it is a `GROUP BY`/`ORDER BY`
    ordinal - `None` for anything else, including any expression
    containing a binary operator. Unwraps arbitrarily many layers of
    unary `+`/`-` down to a bare integer `Literal`, applying each
    layer's sign to the inner result - `-(-1)` unwraps as `-(-(1))` =
    `-(-1)` = `1`, matching sqlite3's own ordinal reading, not the
    arithmetic value of a doubly-negated *expression* (which would also
    be `1` here, coincidentally; the point is this function never
    evaluates arithmetic, it only walks node shapes).

    A loop, not recursion (issue #107): the unary chain is walked down
    once, counting the minus signs, and the sign is applied at the
    bottom - `- - ... 1` with 999 operators is ordinal -1 whatever the
    caller's stack depth."""
    negate = False
    node = expr
    while isinstance(node, UnaryOp):
        if node.op is UnaryOperator.NEG:
            negate = not negate
        node = node.operand
    if isinstance(node, Literal) and isinstance(node.value, int) and not isinstance(node.value, bool):
        return -node.value if negate else node.value
    return None


# --- LIMIT / OFFSET (issue #77) ---------------------------------------------
#
# `<n>` narrows to exactly what `_ordinal_value` above recognises for
# `GROUP BY`/`ORDER BY` ordinals - reused unchanged rather than a third
# copy of the same recursive unwrap (issue #77's own design, justified
# in `_docs/decisions.md` the way the 2026-09-18 entry justified
# keeping `AS` mandatory: a deliberate syntactic narrowing of what
# sqlite3 itself accepts here, not a semantic disagreement with it).
# Unlike an ordinal, there is no range check: 0 and any negative
# resolved value are legal, carrying their own runtime meaning
# (`exec/operators.py`'s `Limit`) rather than being rejected or
# reinterpreted here.


def _bind_limit_offset(expr: Expr, clause: str) -> int:
    """Resolve one `LIMIT`/`OFFSET` expression (`clause` is `"LIMIT"`
    or `"OFFSET"`, used only for the error message) to its literal
    integer value via `_ordinal_value` - `BindError` for every other
    shape sqlite3 itself accepts here (arithmetic, `CASE`, a
    predicate, any scalar or aggregate function call, a TEXT/REAL/
    `NULL` literal, a column reference, a select-list alias, a
    subquery).

    Called last, after every error SQLite itself raises (issue #115):
    a column reference, an unknown function or an aggregate call here
    has already been reported by `_resolve_limit_offset`, at LIMIT's
    own turn, so what reaches this rejection is a shape SQLite accepts
    (or, until #183, a call to one of SQLite's built-in functions,
    which historian reports as unknown)."""
    value = _ordinal_value(expr)
    if value is None:
        raise BindError(
            f"{clause} must be a literal integer, optionally wrapped in "
            "unary +/- and parentheses",
            expr.position,
            (),
        )
    return value


def _resolve_limit_offset(exprs: tuple[Expr, ...], ctx: _Context) -> None:
    """The errors SQLite raises for `LIMIT` and `OFFSET` (*exprs*, in
    that order), at their turn in the resolution order: right after the
    FROM table, before the select list (issue #115). The same walk as
    every other expression (issue #144), over one tree with `LIMIT` on
    the left and `OFFSET` on the right, as SQLite builds it - so an
    ABORT in `LIMIT` means `OFFSET` is never reached - in a context
    where no column resolves (a real column or an alias is "no such
    column" too), no alias is looked up and no aggregate is allowed.
    Nothing is bound: `_bind_limit_offset`'s literal-only rule runs
    last, at step 10."""
    limit_ctx = dataclasses.replace(
        ctx,
        select_items=(),
        alias_fallback=False,
        alias_first=False,
        reject_aggregates=True,
        late_misuse=None,
        clause="",
        columns_visible=False,
    )
    _bind_exprs(exprs, limit_ctx)


# --- Ordinals out of range, shared by GROUP BY and ORDER BY ------------------


def _ordinal_suffix(number: int) -> str:
    """`1st`, `2nd`, `3rd`, `4th`, ..., `11th`, `12th`, `13th`, `21st`
    - the spelling SQLite's "Nth ... term out of range" uses (checked
    against the oracle for the 2nd, 3rd, 11th and 21st term)."""
    if 11 <= number % 100 <= 13:
        return f"{number}th"
    last = number % 10
    if last == 1:
        return f"{number}st"
    if last == 2:
        return f"{number}nd"
    if last == 3:
        return f"{number}rd"
    return f"{number}th"


def _rejected_at_its_turn(ordinal: int) -> bool:
    """Whether SQLite rejects ordinal *ordinal* when it reaches the term,
    before any later term's names, rather than after every term's names
    (issue #144, measured): an integer that fits a 32-bit int - SQLite's
    `sqlite3ExprIsInteger` - and is below 1 or above 65535. Any other
    ordinal is range-checked after the names, as before (#115)."""
    if ordinal < -2147483647 or ordinal > 2147483647:
        return False
    return ordinal < 1 or ordinal > 65535


def _check_ordinal(raw_expr: Expr, index: int, clause: str, item_count: int) -> int:
    """The ordinal value of *raw_expr*, the *index*-th (zero-based)
    term of *clause* (`"GROUP BY"`/`"ORDER BY"`), checked against the
    select list's length after `Star` expansion; `BindError` naming
    the term, as SQLite does, when it is out of range."""
    ordinal = _ordinal_value(raw_expr)
    assert ordinal is not None
    if ordinal < 1 or ordinal > item_count:
        raise BindError(
            f"{_ordinal_suffix(index + 1)} {clause} term out of range - should be between 1 and {item_count}",
            raw_expr.position,
            (),
        )
    return ordinal


# --- GROUP BY (issue #69) ----------------------------------------------------


def _reject_aggregate_keys(group_by: tuple[Expr, ...], keys: list[Expr | None]) -> tuple[Expr, ...]:
    """Pass 3 of `_bind_group_by`: reject a key that is, or reaches, an
    aggregate call, and return the keys, all bound, in clause order."""
    bound_keys: list[Expr] = []
    for raw_expr, key in zip(group_by, keys):
        assert key is not None
        if contains_aggregate(key):
            raise BindError(
                "aggregate functions are not allowed in the GROUP BY clause",
                raw_expr.position,
                (),
            )
        bound_keys.append(key)
    return tuple(bound_keys)


def _bind_group_by(
    group_by: tuple[Expr, ...], ctx: _Context, bound_items: tuple[BoundSelectItem, ...]
) -> tuple[Expr, ...]:
    """Resolve the `GROUP BY` terms, in three passes over them - the
    order SQLite reports a clause's errors in (issue #115, measured):

    1. Every term that is not an ordinal binds as an ordinary
       expression, left to right, with select-list alias fallback
       (`alias_first=False`, per #32/#60's precedent for `GROUP BY`/
       `HAVING`). Name errors (no such column or function, arity, a
       nested aggregate) raise here, so `GROUP BY 99, ghost` reports
       `ghost`.
       An ordinal below 1 or above 65535 is rejected here, at its
       own turn (`_rejected_at_its_turn`, #144).
    2. Every ordinal (`_ordinal_value`, shared with `ORDER BY`) is
       checked against `bound_items` (the select list *after* `Star`
       expansion, matching `sqlite3`'s own "1st GROUP BY term"
       counting) and resolved to that item's own bound expression -
       purely by position, never through `_resolve_column`, since an
       ordinal is not a name.
    3. A term that is, or reaches, an aggregate call - written
       directly, an alias of one, or an ordinal pointing at one - is
       rejected identically, whichever route reaches it: `GROUP BY
       count(*), 99` reports the ordinal.

    The keys come back in clause order, exactly as the one-pass
    version built them.
    """
    group_ctx = dataclasses.replace(
        ctx,
        select_items=bound_items,
        alias_fallback=True,
        alias_first=False,
        reject_aggregates=False,
        late_misuse=None,
    )
    keys: list[Expr | None] = []
    for index, raw_expr in enumerate(group_by):
        ordinal = _ordinal_value(raw_expr)
        if ordinal is None:
            keys.append(_bind_expr(raw_expr, group_ctx))
            continue
        if _rejected_at_its_turn(ordinal):
            _check_ordinal(raw_expr, index, "GROUP BY", len(bound_items))
        keys.append(None)
    for index, raw_expr in enumerate(group_by):
        if keys[index] is None:
            ordinal = _check_ordinal(raw_expr, index, "GROUP BY", len(bound_items))
            keys[index] = bound_items[ordinal - 1].expr
    return _reject_aggregate_keys(group_by, keys)


# --- ORDER BY (issue #61) ----------------------------------------------------
#
# The one clause that reverses two rules every other clause here holds:
# the select-list alias wins over a same-named real column
# (`_resolve_column`'s `alias_first=True` - #32's own reserved-but-unused
# direction, confirmed against sqlite3:
# `select a as real_a, b as a from t order by a` sorts by the alias
# `b`, not the real column `a`), and an aggregate call is legal even
# when it resolves through an ordinal - `GROUP BY`'s ordinal rejects
# one outright, `ORDER BY`'s does not (confirmed: `select k, count(*)
# from g group by k order by 2 desc` succeeds in sqlite3, while the
# identically-shaped `GROUP BY 2` pointing at an aggregate is rejected).
#
# A bare aggregate call is legal in `ORDER BY` only once the query is
# already aggregating (`GROUP BY` present, or an aggregate call in the
# select list) - confirmed live against sqlite3: `select p from u
# order by count(*)` (no GROUP BY, no select-list aggregate) is
# "misuse of aggregate: count()", the same rejection WHERE gets, while
# `select count(*) from u order by count(*)` (select list already
# aggregates) succeeds. `reject_aggregates` below is exactly this
# question, inverted - the existing WHERE-rejection mechanism (#60),
# reused rather than reimplemented.


def _bind_order_by(
    order_by: tuple[OrderByItem, ...], ctx: _Context, bound_items: tuple[BoundSelectItem, ...]
) -> tuple[BoundOrderByItem, ...]:
    """Resolve the `ORDER BY` terms in two passes, the order SQLite
    reports a clause's errors in (issue #115): first every term that is
    not an ordinal binds as an ordinary expression through `ctx`, left
    to right - the caller supplies `alias_first=True`, and whichever
    `reject_aggregates`/`late_misuse` the query's aggregate status
    calls for; this function decides neither - so `ORDER BY 99, ghost`
    reports `ghost` - except an ordinal below 1 or above 65535, which
    is rejected at its own turn (`_rejected_at_its_turn`, #144), so
    `ORDER BY 0, ghost` reports the ordinal; then every ordinal
    (`_ordinal_value`) is checked,
    1-based, against `bound_items`, out-of-range (0, negative, or past
    the end) raising the same "Nth ... term out of range" shape
    `GROUP BY` uses, and resolves to the referenced item's own bound
    expression. Unlike `GROUP BY`, an ordinal resolving to an aggregate
    call is never rejected (see the section comment above).
    """
    exprs: list[Expr | None] = []
    for index, item in enumerate(order_by):
        ordinal = _ordinal_value(item.expr)
        if ordinal is None:
            exprs.append(_bind_expr(item.expr, ctx))
            continue
        if _rejected_at_its_turn(ordinal):
            _check_ordinal(item.expr, index, "ORDER BY", len(bound_items))
        exprs.append(None)
    bound: list[BoundOrderByItem] = []
    for index, item in enumerate(order_by):
        expr = exprs[index]
        if expr is None:
            ordinal = _check_ordinal(item.expr, index, "ORDER BY", len(bound_items))
            expr = bound_items[ordinal - 1].expr
        bound.append(BoundOrderByItem(expr=expr, direction=item.direction, position=item.position))
    return tuple(bound)



# --- Select-list binding ----------------------------------------------------


def _bind_select_item(item: SelectItem, ctx: _Context) -> list[BoundSelectItem]:
    """Bind one select-list item, expanding a `Star` into several
    `BoundSelectItem`s or resolving an ordinary expression into one.

    Aliases do not enter a namespace visible to other select-list
    items: each item is resolved against `ctx.schema` alone, with no
    reference to any other item's alias. Confirmed against `sqlite3`
    (`select path as p, p as p2 from blame` fails on the second item)
    - and nothing extra needs implementing here for that, since this
    function never looks past its own `item`.
    """
    if isinstance(item.expr, Star):
        if item.alias is not None:
            # `* AS alias` - parses today (#31, a parser bug) but is a
            # syntax error in `sqlite3`. Same defensive backstop as
            # the general Star case in `_bind_expr`.
            raise BindError(
                "* is only allowed as a whole select-list item or the sole argument to a function call",
                item.position,
                (),
            )
        return [
            BoundSelectItem(expr=bound, alias=None, output_name=bound.name, position=item.position)
            for bound in _bind_star(item.expr, ctx)
        ]
    bound_expr = _bind_expr(item.expr, ctx)
    if item.alias is not None:
        output_name = item.alias
    elif isinstance(bound_expr, BoundColumnRef):
        output_name = bound_expr.name
    else:
        output_name = None
    return [BoundSelectItem(expr=bound_expr, alias=item.alias, output_name=output_name, position=item.position)]
