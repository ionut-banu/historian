"""Name resolution and single-expression binding.

Split out of `sql/binder.py` (issue #151). Resolves the FROM table, a
column reference or a `*`, validates aggregate calls, and binds one
expression tree at a time against a `_Context`; per-clause binding is
`sql/bind_clauses.py`, the order the clauses run in is `sql/binder.py`.
Imports `sql/bound.py` and below.

Aggregate calls (issue #60)
-----------------------------

v1's grammar has no scalar functions at all (`_docs/spec.md` §1:
"Scalar functions: a deliberately small set, chosen when the queries
need them rather than up front" - none chosen yet), so `_AGGREGATE_NAMES`
below (`count`/`sum`/`avg`/`min`/`max`) is not a partial registry
alongside some other kind of function - it is every `FunctionCall`
name this grammar can ever legally bind. `_validate_function_call`
checks a call's name (ASCII-fold, same rule as every other identifier
in this module) against that set before anything else in the
`FunctionCall` branch of `_bind_expr` runs: an unrecognised name is
`BindError("no such function: ...")` immediately, closing the gap
#45 complained about (`SELECT nonexistent_fn(path) FROM blame` used
to bind successfully and only fail later, generically, in
`exec/expression.py`). A recognised name still gets its arity checked
(`count` takes zero or one argument, `*` counts as one; `sum`/`avg`/
`min`/`max` take exactly one, and never `*`) and, when `ctx.
reject_aggregates` is set (`bind()` turns this on for `WHERE`, and
for `ORDER BY` in a non-aggregate query), is rejected - `WHERE
count(*) > 1` is `BindError`, matching `sqlite3`'s own "misuse of
aggregate function" rejection, though not its wording (§3's Errors
section does not require that). Rejected does not always mean raised
at once: see `sql/binder.py`'s "Resolution order" for when it is reported.

Two more aggregate-misuse shapes, closed by issue #102, follow the
same "reject at bind time, unconditionally" rule rather than waiting
to see whether any row would actually reach the trouble:

- **Nesting.** An aggregate call cannot be another aggregate call's
  argument (`count(count(*))`) - `sqlite3` calls this "misuse of
  aggregate function count()". `_bind_expr`'s `FunctionCall` branch
  checks every bound argument, after binding it, for an aggregate call
  anywhere in its tree (`contains_aggregate`, `sql/walk.py`) and raises immediately
  if one is found - this is why the check has to run *after* the
  argument is bound rather than on the raw AST: an argument that is a
  `ColumnRef` to a select-list alias only reveals whether it is
  secretly an aggregate call once `_resolve_name` has spliced the
  alias's own bound expression in.
- **An alias to an aggregate, reached other than directly.** Every
  clause that lets a select-list alias stand in for a real column
  (`_resolve_name`, above) allows a bare reference to an aliased
  aggregate to be used exactly where a real aggregate call could be
  used directly (`HAVING c > 1`, `ORDER BY c`) - but never anywhere
  else, most importantly never as *another* aggregate call's own
  argument (`HAVING count(c) > 0`, where `c` aliases `count(*)`) and
  never in `WHERE` at all, however it is reached (`WHERE c > 1`). The
  nesting case above already catches the former once the alias is
  spliced in, since the substituted subtree is exactly a `FunctionCall`
  now. The latter - `WHERE`, or `ORDER BY` before the query is known to
  aggregate - is caught in `_resolve_name` itself: once the winning
  candidate is chosen (real column or alias), a `ctx.reject_aggregates`
  clause raises if that candidate's tree contains an aggregate call,
  the same rejection `_validate_function_call` already gives a
  *literal* aggregate call written directly in such a clause. Both
  checks read only `contains_aggregate` over an already-bound
  subtree - no new walk, no change to what nesting itself means.


`WHERE` resolving a select-list alias
--------------------------------------

Issue #32. SQLite falls back to a select-list alias for any name no
real column claims, in every clause except the select list itself
(`select path as p, line_no from blame where p = 'a.py'` succeeds via
the alias) - with the real column always winning when a name is both,
*except* in `ORDER BY`, where the alias wins instead. `_resolve_name`
below implements this as one function taking a precedence-direction
flag (`alias_first`), rather than a `WHERE`-specific helper, because
`GROUP BY`, `HAVING` and `ORDER BY` need the same rule with their own
direction: `bind()` passes `alias_first=False` for `WHERE`, `GROUP BY`
and `HAVING`, and `alias_first=True` for `ORDER BY`.

The match is a substitution, not a value lookup: a `ColumnRef` that
resolves to an alias is replaced by a reference to that select-list
item's own already-bound expression (`BoundSelectItem.expr`), the same
`dataclasses.replace`-based tree it already went through - never a
computed value. Confirmed why this must be a substitution and not a
cached value: `select random() as r from t where r = r` returns zero
rows in `sqlite3`, meaning `r` is evaluated fresh at each reference: a
cached value would make `r = r` trivially true for every row.
historian has no non-deterministic scalar function yet to make this a
differential case, but the design carries the same property - the
substituted subtree is evaluated by `exec/expression.py` per reference,
with no memoization by this module.

A table-qualified reference (`t.x`) is never a candidate for the
fallback - confirmed against `sqlite3` (`select b as x from t where
t.x = 10` still raises "no such column: t.x") - aliases have no table
qualifier to match against, so a qualified `ColumnRef` goes straight to
`_bind_column_ref` exactly as before.


ASCII-only case folding
-------------------------

SQLite folds only `A`-`Z`/`a`-`z` when matching an identifier, not
Python's Unicode-aware `str.lower()`/`str.casefold()`. Confirmed
against `sqlite3` 3.51.0: `select STRASSE from t` (table `t(straße
text)`) fails with "no such column: STRASSE", while `select STRAßE
from t` succeeds - `ß` is left alone rather than folded to `SS`, which
is exactly what `'straße'.upper() == 'STRASSE'` would wrongly do in
Python. `historian.ascii.ascii_fold` implements SQLite's rule
directly: only the 26 ASCII letters move, nothing else is consulted.

"""

from __future__ import annotations

from dataclasses import dataclass

from historian.ascii import ascii_fold
from historian.schema import Schema
from historian.sql.ast import (
    ColumnRef,
    Expr,
    FunctionCall,
    Literal,
    SelectStatement,
    Star,
)
from historian.sql.bound import BindError, BoundSelectItem
from historian.sql.walk import (
    BoundColumnRef,
    children,
    contains_aggregate,
    with_children,
)


#: The v1 aggregate registry (issue #60): every `FunctionCall` name
#: this grammar can legally bind, ASCII-folded. v1 has no scalar
#: functions (`_docs/spec.md` §1), so this is not a partial list
#: alongside some other kind of function - anything not in it is
#: unconditionally unknown. See the module docstring's "Aggregate
#: calls" section.
_AGGREGATE_NAMES = frozenset({"count", "sum", "avg", "min", "max"})


# --- ASCII-only folding --------------------------------------------------
#
# `ascii_fold` lives in `historian.ascii` (issue #53) - see this module's
# own docstring's "ASCII-only case folding" section for the SQLite
# evidence it implements.


def _same_name(a: str, b: str) -> bool:
    """ASCII case-insensitive identifier equality."""
    return ascii_fold(a) == ascii_fold(b)


# --- Binding context -------------------------------------------------------
#
# A plain, immutable bundle of what every resolution needs: the FROM
# table's own schema and declared name (v1 has exactly one), the
# catalog's full set of table names for a "no such table" error's
# `available` data, and (issue #32) the select-list alias fallback
# settings a particular clause binds with. Not global state and not a
# class with behaviour - just the parameters every helper below would
# otherwise need threaded through separately. `select_items`/
# `alias_fallback`/`alias_first` default to "no fallback", which is
# what `_bind_select_item` binds every select-list item with (so
# aliases stay invisible to each other, #32); `bind()` builds further
# `_Context`s, via `dataclasses.replace`, with the fallback turned on
# for `WHERE`, `GROUP BY`, `HAVING` and `ORDER BY`.


@dataclass(frozen=True)
class _Context:
    schema: Schema
    table_name: str
    catalog_names: tuple[str, ...]
    select_items: tuple[BoundSelectItem, ...] = ()
    alias_fallback: bool = False
    alias_first: bool = False
    #: `True` while binding a clause where an aggregate call is never
    #: legal, regardless of name or arity: `WHERE`, and `ORDER BY` of
    #: a non-aggregate query. `False` (the default) for the select
    #: list, `GROUP BY` and `HAVING`.
    reject_aggregates: bool = False
    #: Issue #115: where a rejected aggregate goes. `None` raises it on
    #: the spot (WHERE of a non-aggregate query). A list collects it
    #: instead, and binding goes on as if the call were legal: `bind()`
    #: raises the first collected error only after GROUP BY, because
    #: that is when SQLite reports an aggregate call in the WHERE of an
    #: aggregate query, or in the ORDER BY of a non-aggregate one. See
    #: `sql/binder.py`'s "Resolution order".
    late_misuse: list[BindError] | None = None
    #: Issue #144: the clause `reject_aggregates` is set for, named in
    #: the misuse message (`"WHERE"`, `"ORDER BY"`), so the message
    #: names the clause the call was found in.
    clause: str = ""


# --- FROM-table resolution -------------------------------------------------


def _resolve_table(stmt: SelectStatement, catalog: dict[str, Schema]) -> _Context:
    """Resolve `stmt.from_table` against `catalog`, case-insensitively
    and ASCII-only. Checked first, before anything else in the
    statement - step 1 of `sql/binder.py`'s "Resolution order".

    The error's position is `stmt.position` (the `SELECT` keyword):
    `from_table` is a bare string on the AST with no position of its
    own to point a caret at. Confirmed this is not a gap to route
    around: `sqlite3`'s own CLI likewise prints no caret for "no such
    table" (only for "no such column"), so the renderer (#41) is
    not expected to place one here either.
    """
    for catalog_name, schema in catalog.items():
        if _same_name(catalog_name, stmt.from_table):
            return _Context(schema=schema, table_name=catalog_name, catalog_names=tuple(catalog.keys()))
    raise BindError(f"no such table: {stmt.from_table}", stmt.position, tuple(catalog.keys()))


# --- Column and Star resolution --------------------------------------------


def _lookup_column(ref: ColumnRef, ctx: _Context) -> BoundColumnRef | None:
    """Look up `ref` against `ctx.schema` alone; `None` if no real
    column matches by name.

    A qualifier that does not match the FROM table - whether a real,
    unrelated table or an unknown name - still raises immediately
    rather than returning `None`: "no such column: <qualifier>.<name>",
    the whole dotted reference verbatim, never "no such table". This is
    the opposite of `_bind_star`'s qualifier check below; both are
    separately confirmed against `sqlite3` and must not be unified.
    Factored out of `_bind_column_ref` so `_resolve_name` can try the
    real-column candidate without a raise-and-catch dance.
    """
    if ref.table is not None and not _same_name(ref.table, ctx.table_name):
        raise BindError(f"no such column: {ref.table}.{ref.name}", ref.position, ctx.schema.names)
    for offset, column in enumerate(ctx.schema.columns):
        if _same_name(column.name, ref.name):
            return BoundColumnRef(offset=offset, name=column.name, position=ref.position)
    return None


def _bind_column_ref(ref: ColumnRef, ctx: _Context) -> BoundColumnRef:
    """Resolve a bare or table-qualified `ColumnRef` against the FROM
    table's schema only - no select-list alias fallback. Used directly
    wherever alias fallback does not apply (select-list items, a
    qualified reference anywhere) and as the schema-only half of
    `_resolve_name`'s fallback below."""
    bound = _lookup_column(ref, ctx)
    if bound is not None:
        return bound
    display = f"{ref.table}.{ref.name}" if ref.table is not None else ref.name
    raise BindError(f"no such column: {display}", ref.position, ctx.schema.names)


def _find_alias_expr(name: str, ctx: _Context) -> Expr | None:
    """The first item in `ctx.select_items` (declaration order) whose
    explicit alias matches `name`, ASCII case-insensitively via
    `_same_name` - `select b as x, c as x from t where x > ...`
    resolves `x` to `b`, the first occurrence, confirmed against
    `sqlite3` (#32). An item with no explicit `alias`
    is never a candidate: only names written with `AS` participate in
    the fallback, per the issue's design recommendation ("select-list's
    aliases").

    Returns the item's own bound expression (`BoundSelectItem.expr`),
    to be spliced into the caller's tree in place of the reference -
    not a copy, since these are frozen, side-effect-free AST nodes and
    sharing one instance across two positions in a tree carries no
    caching risk: each occurrence is walked and evaluated independently
    by `exec/expression.py`, never memoized by node identity.
    """
    for item in ctx.select_items:
        if item.alias is not None and _same_name(item.alias, name):
            return item.expr
    return None


def _reject_aliased_aggregate(ref: ColumnRef, resolved: Expr, ctx: _Context) -> None:
    # Issue #102: a real column is never an aggregate call, so this
    # only ever fires for the alias branch - a bare reference to a
    # select-list alias that turns out to name an aggregate call,
    # reached in a clause where an aggregate is never legal at all
    # (`ctx.reject_aggregates`, the same flag `_validate_function_call`
    # already checks for a *literal* aggregate call written directly
    # here). `WHERE c > 1` (c aliasing `count(*)`) is exactly this -
    # `sqlite3`'s own "misuse of aggregate: count()", confirmed against
    # the oracle - and it must be rejected unconditionally, before any
    # row is ever considered, the same way the literal-call case
    # already is.
    if ctx.reject_aggregates and contains_aggregate(resolved):
        _reject_aggregate(
            BindError(
                f"misuse of aggregate: aliased column {ref.name} refers to an aggregate call, "
                "which is not allowed here",
                ref.position,
                (),
            ),
            ctx,
        )


def _resolve_name(ref: ColumnRef, ctx: _Context) -> Expr:
    """Resolve `ref`, falling back to a select-list alias of the same
    name when no real column claims it - see the module docstring's
    "`WHERE` resolving a select-list alias" section for the full
    rationale (issue #32). Only called when `ctx.alias_fallback` is
    set; `_bind_expr` calls `_bind_column_ref` directly otherwise.

    A table-qualified `ref` skips the fallback entirely and resolves
    exactly as `_bind_column_ref` always has. For an unqualified name,
    both a real-column match and an alias match are looked up, and
    `ctx.alias_first` decides which one wins when both exist: `False`
    for `WHERE`/`GROUP BY`/`HAVING` (the real column wins), `True` for
    `ORDER BY` (the alias wins instead - confirmed against `sqlite3`,
    the one clause where the four are not uniform). `bind()` calls it
    with both values.
    """
    if ref.table is not None:
        return _bind_column_ref(ref, ctx)

    column_match = _lookup_column(ref, ctx)
    alias_match = _find_alias_expr(ref.name, ctx)

    if ctx.alias_first:
        first, second = alias_match, column_match
    else:
        first, second = column_match, alias_match
    if first is not None:
        resolved = first
    elif second is not None:
        resolved = second
    else:
        raise BindError(f"no such column: {ref.name}", ref.position, ctx.schema.names)

    _reject_aliased_aggregate(ref, resolved, ctx)
    return resolved


def _bind_star(star: Star, ctx: _Context) -> list[BoundColumnRef]:
    """Expand `*` / `table.*` into one `BoundColumnRef` per column of
    the FROM table's schema, in declared order.

    A qualifier that does not match the FROM table raises "no such
    table: <qualifier>", never "no such column" - confirmed against
    `sqlite3` for both an unrelated real table and an unknown name,
    and the opposite of `_bind_column_ref`'s qualifier check above.
    """
    if star.table is not None and not _same_name(star.table, ctx.table_name):
        raise BindError(f"no such table: {star.table}", star.position, ctx.catalog_names)
    return [
        BoundColumnRef(offset=offset, name=column.name, position=star.position)
        for offset, column in enumerate(ctx.schema.columns)
    ]


# --- Aggregate-call validation (issue #60) ----------------------------------


def _validate_function_call(call: FunctionCall, ctx: _Context) -> None:
    """Name and arity for one `FunctionCall`, plus the WHERE-rejects-
    aggregates rule - see the module docstring's "Aggregate calls"
    section. Raises `BindError`; never returns a value, mirroring
    `_bind_column_ref`'s own "raise or fall through" shape.

    Order matters, and is SQLite's (issue #115): an unrecognised name
    first, then the arity, then `ctx.reject_aggregates` - so `WHERE
    foo(x) > 1` reports "no such function" and `WHERE avg() = 1`
    reports "wrong number of arguments to function avg()", never the
    aggregate misuse. A rejected aggregate goes through
    `_reject_aggregate`, which raises it or, when `ctx.late_misuse` is
    a list, collects it for `bind()` to raise later.
    """
    name = ascii_fold(call.name)
    if name not in _AGGREGATE_NAMES:
        raise BindError(
            f"no such function: {call.name}", call.position, tuple(sorted(_AGGREGATE_NAMES))
        )
    arity_error = _arity_error(call)
    if arity_error is not None:
        raise arity_error
    if ctx.reject_aggregates:
        _reject_aggregate(
            BindError(
                f"misuse of aggregate function {call.name}(): aggregate calls are not allowed in {ctx.clause}",
                call.position,
                (),
            ),
            ctx,
        )


def _arity_error(call: FunctionCall) -> BindError | None:
    """The arity error for a call to one of `_AGGREGATE_NAMES`, or
    `None` when the argument count is right. `count` takes zero
    arguments, `*` or one expression; `sum`/`avg`/`min`/`max` take
    exactly one expression and never `*` - `sum(*)` is not `sum(<every
    column>)`, and SQLite reports it as the same arity error (checked
    against the oracle)."""
    message = f"wrong number of arguments to function {call.name}()"
    if ascii_fold(call.name) == "count":
        # count() and count(*) are both zero-column forms (`*` is one
        # AST node, not zero); count(<expr>) is the one-argument form.
        if len(call.args) > 1:
            return BindError(message, call.position, ())
        return None
    if len(call.args) != 1 or isinstance(call.args[0], Star):
        return BindError(message, call.position, ())
    return None


def _reject_aggregate(error: BindError, ctx: _Context) -> None:
    """Raise *error* now, or - when `ctx.late_misuse` is a list -
    keep the first such error there and return, so binding goes on as
    if the aggregate were legal (issue #115)."""
    if ctx.late_misuse is None:
        raise error
    if not ctx.late_misuse:
        ctx.late_misuse.append(error)


# --- General expression binding --------------------------------------------
#
# One case per `sql/ast.py` node type. Every type other than
# `ColumnRef` and `Star` is reused unchanged and rebuilt via
# `dataclasses.replace` with its children bound - no new type, no
# dynamic dispatch, just an explicit `isinstance` chain matching the
# style already established by the parser's own precedence methods.


def _rebuild_with_bound_operands(node: Expr, results: list[Expr], ctx: _Context) -> Expr:
    """Take *node*'s bound operands off the end of *results* and
    rebuild *node* around them; a `FunctionCall` also gets the nested-
    aggregate check, after all of its arguments are bound."""
    first = len(results) - len(children(node))
    bound_operands = results[first:]
    del results[first:]
    if isinstance(node, FunctionCall):
        _check_no_nested_aggregate(node, bound_operands, ctx)
    return with_children(node, bound_operands)


def _bind_leaf(node: Expr, ctx: _Context) -> Expr | None:
    """Bind *node* on the spot when it has no operands to wait for, or
    return `None` when it has some. A `Star` and a `BoundColumnRef` here
    always raise."""
    if isinstance(node, Literal):
        return node
    if isinstance(node, ColumnRef):
        if ctx.alias_fallback:
            return _resolve_name(node, ctx)
        return _bind_column_ref(node, ctx)
    if isinstance(node, Star):
        # A whole, alias-less select-list item and count(*)'s sole
        # unqualified argument are handled by their own callers before
        # ever reaching here - see `_bind_select_item` and the
        # `FunctionCall` case in `_start_function_call`. Any other
        # position is exactly the parser-permissiveness backstop: `* AS
        # alias`, `*` inside a general expression, and `count(blame.*)`
        # (a *qualified* star as a function argument) all reach this
        # branch and are rejected here rather than crashing or
        # silently mis-expanding.
        raise BindError(
            "* is only allowed as a whole select-list item or the sole argument to a function call",
            node.position,
            (),
        )
    if isinstance(node, BoundColumnRef):
        raise AssertionError("sql/binder.py: a BoundColumnRef reached _bind_expr; it is already bound")
    return None


def _start_function_call(call: FunctionCall, ctx: _Context) -> Expr | None:
    """The first visit to a `FunctionCall`: run its checks before any
    argument is bound, and return the call itself for `count(*)`, or
    `None` when its arguments still have to be bound."""
    # Issue #60: name/arity/WHERE-rejection, before anything else -
    # see _validate_function_call and the module docstring's
    # "Aggregate calls" section. Every FunctionCall past this point
    # is a real, correctly-arity aggregate call.
    _validate_function_call(call, ctx)
    if len(call.args) == 1 and isinstance(call.args[0], Star) and call.args[0].table is None:
        # count(*): passed through unexpanded. `*` here means "no
        # columns", not "all columns" - see `sql/bound.py`'s docstring.
        # A *qualified* sole argument (count(blame.*)) does not
        # take this path and falls through to the general Star
        # rejection in `_bind_leaf`.
        return call
    return None


def _bind_expr(expr: Expr, ctx: _Context) -> Expr:
    """Bind every `ColumnRef` in `expr`'s tree against `ctx.schema`,
    with select-list alias fallback (issue #32) when `ctx.alias_fallback`
    is set - off by default on the `_Context` every select-list item
    binds with (`_bind_select_item`), which is how aliases stay
    invisible to each other; on for the `_Context`s `bind()` builds
    for the other clauses. `ctx` is the same for every node of the tree,
    so the fallback applies to a `ColumnRef` at any depth in the tree,
    not only at the top.

    Not recursive (issue #107; the children table it walks is
    `sql/walk.py`'s `children`, shared with the planner and the
    parser): *pending* holds `(node, operands_done)` pairs and
    *results* the bound subtrees finished so far. A node is first seen
    with `operands_done=False` - a leaf is bound on the spot; any other
    node is pushed back with `operands_done=True`, then its operands in
    reverse so they are bound left to right; seen again, it takes its
    bound operands off *results* and is rebuilt around them. That is
    the recursive version's order exactly: a `FunctionCall`'s name and
    arity are checked before any argument is bound, and the nested-
    aggregate check runs after all of them are.
    """
    pending: list[tuple[Expr, bool]] = [(expr, False)]
    results: list[Expr] = []
    while pending:
        node, operands_done = pending.pop()
        if operands_done:
            results.append(_rebuild_with_bound_operands(node, results, ctx))
            continue
        leaf = _bind_leaf(node, ctx)
        if leaf is not None:
            results.append(leaf)
            continue
        if isinstance(node, FunctionCall):
            passed_through = _start_function_call(node, ctx)
            if passed_through is not None:
                results.append(passed_through)
                continue
        # #51: a LIKE's escape is an ordinary operand expression, bound
        # the same way left/pattern already are - `children` leaves it
        # out when there is no ESCAPE clause, so there is nothing to bind.
        pending.append((node, True))
        for operand in reversed(children(node)):
            pending.append((operand, False))
    (result,) = results
    return result


def _check_no_nested_aggregate(call: FunctionCall, bound_args: list[Expr], ctx: _Context) -> None:
    """Issue #102: an aggregate call can never be another aggregate
    call's own argument - `count(count(*))` is `sqlite3`'s own "misuse
    of aggregate function count()". This has to run after binding each
    argument, not on the raw AST, because a `ColumnRef` argument only
    reveals whether it secretly names an aggregate call once alias
    resolution (`_resolve_name`) has spliced that alias's own bound
    expression in - a bound argument that is itself a `FunctionCall`,
    whether written directly or reached through a select-list alias, is
    exactly what `contains_aggregate` was already built to detect."""
    for raw_arg, bound_arg in zip(call.args, bound_args):
        if not contains_aggregate(bound_arg):
            continue
        if isinstance(raw_arg, ColumnRef) and _find_alias_expr(raw_arg.name, ctx) is not None:
            # Reached through a select-list alias - `sqlite3`'s own
            # message names the alias, not the outer function:
            # "misuse of aliased aggregate c".
            raise BindError(f"misuse of aliased aggregate {raw_arg.name}", raw_arg.position, ())
        raise BindError(
            f"misuse of aggregate function {call.name}(): aggregate function calls "
            "cannot be nested",
            raw_arg.position,
            (),
        )
