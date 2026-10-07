"""Name resolution and single-expression binding.

Split out of `sql/binder.py` (issue #151). Resolves the FROM table, a
column reference or a `*`, validates aggregate calls, and binds one
expression tree at a time against a `_Context`; per-clause binding is
`sql/bind_clauses.py`, the order the clauses run in is `sql/binder.py`.
Imports `sql/bound.py` and below.

Which error is reported (issue #144)
-------------------------------------

When one expression holds several errors, the binder reports the one
SQLite reports, by walking the tree the way SQLite's name resolution
does (`resolveExprStep`): a node first, then its children left to
right, with one error slot that each new error overwrites - the error
raised is the last one recorded. What the walk does after an error
depends on the node it meets (`_visit`, one branch per rule):

1. A column reference that resolves: the walk goes on, whatever is
   recorded. One that does not records `no such column` and ABORTs.
2. A function call records at most one error of its own before its
   arguments (`_call_error`): aggregate misuse if it is an aggregate -
   known name, right argument count - where none is allowed, else `no
   such function`, else `wrong number of arguments`. Then it walks its
   arguments left to right, stopping at the first ABORT, and returns
   normally. Inside the arguments of an aggregate accepted where it
   stands, no aggregate is allowed (`_argument_permission`).
3. `x LIKE y [ESCAPE z]` is SQLite's call `like(y, x, z)`, with no
   error of its own (`sql/walk.py`'s `resolution_children`); `x NOT
   LIKE y` is a `NOT` (rule 6) around it.
4. `x IS NULL` / `x IS NOT NULL` walks `x` whatever is recorded, and
   returns normally.
5. `x IS y` / `x IS NOT y` with `y` a bare column name resolves `y`
   first and ABORTs if it fails; then the node trips like rule 6; then
   `x` is walked, and the resolved `y` met again trips too if `x`
   recorded an error.
6. Every other node ABORTs at once if an error is already recorded;
   otherwise it walks its children and passes an ABORT up.
7. `x [NOT] IN (e)`, one element and that element constant (no column
   and no function call in it, `sql/walk.py`'s `is_constant`), is
   SQLite's `x = +e` (`NOT (x = +e)`): the node trips like rule 6,
   `x` is walked, then the `+` trips if anything is recorded, then `e`
   is walked. The `+` is what tells it apart from an `IN` list: `e`
   itself may be a node that never trips (`IS NULL`, `LIKE`).

An ABORT unwinds to the nearest enclosing call, `LIKE` or `IS NULL`,
which returns normally, or ends the root. A root is one select-list
item, one `WHERE`/`HAVING`/`GROUP BY`/`ORDER BY` term, or `LIMIT` and
`OFFSET` together (`_bind_exprs` takes several roots for that). The
walk is an explicit stack (#107) and an ABORT is a loop popping it
down to a call's frame marker - no recursion, and no exception used
for control flow: the recorded error is raised once, when the walk
ends. The late aggregate misuse (`_Context.late_misuse`) and
historian's own rejections are never recorded here; `sql/binder.py`
raises them after every clause, and only if nothing else was raised.
See `_docs/decisions.md`, 2026-10-07.

Aggregate calls (issue #60)
-----------------------------

v1's grammar has no scalar functions at all (`_docs/spec.md` §1:
"Scalar functions: a deliberately small set, chosen when the queries
need them rather than up front" - none chosen yet), so `_AGGREGATE_NAMES`
below (`count`/`sum`/`avg`/`min`/`max`) is not a partial registry
alongside some other kind of function - it is every `FunctionCall`
name this grammar can ever legally bind. `_call_error` checks a
call's name (ASCII-fold, same rule as every other identifier in this
module) against that set first: an unrecognised name is "no such
function: ...", closing the gap #45 complained about (`SELECT
nonexistent_fn(path) FROM blame` used to bind successfully and only
fail later, generically, in `exec/expression.py`). Until #183 that
includes every one of SQLite's own built-in scalar functions - `abs`,
`length` - which SQLite accepts: an accepted difference. A recognised
name still gets its arity checked (`count` takes zero or one
argument, `*` counts as one; `sum`/`avg`/`min`/`max` take exactly one,
and never `*`) and, where the walk's permission forbids an aggregate
(`WHERE` of a non-aggregate query, `LIMIT`/`OFFSET`, or another
aggregate's arguments), is a misuse - `WHERE count(*) > 1` matches
`sqlite3`'s own "misuse of aggregate function" rejection, though not
its wording (§3's Errors section does not require that); the message
names the clause. In `WHERE` of an aggregate query and `ORDER BY` of
a non-aggregate one the misuse is collected instead, and reported
late: see `sql/binder.py`'s "Resolution order".

Two more aggregate-misuse shapes, closed by issue #102, follow the
same "reject at bind time, unconditionally" rule rather than waiting
to see whether any row would actually reach the trouble:

- **Nesting.** An aggregate call cannot be another aggregate call's
  argument (`count(count(*))`) - `sqlite3` calls this "misuse of
  aggregate function count()". The inner call records it where it
  stands, naming itself, because the outer call's arguments are walked
  with aggregates forbidden (rule 2).
- **An alias to an aggregate, reached other than directly.** Every
  clause that lets a select-list alias stand in for a real column
  (`_resolve_column`, below) allows a bare reference to an aliased
  aggregate to be used exactly where a real aggregate call could be
  used directly (`HAVING c > 1`, `ORDER BY c`) - but never anywhere
  else, most importantly never as *another* aggregate call's own
  argument (`HAVING count(c) > 0`, where `c` aliases `count(*)`) and
  never in `WHERE` at all, however it is reached (`WHERE c > 1`).
  `_aliased_aggregate_error` checks the resolved candidate's bound
  tree with `contains_aggregate`: inside an aggregate's arguments it
  is `sqlite3`'s "misuse of aliased aggregate c", recorded where the
  reference stands; in `WHERE` of an aggregate query it is collected
  as a late misuse, like a call written there.


`WHERE` resolving a select-list alias
--------------------------------------

Issue #32. SQLite falls back to a select-list alias for any name no
real column claims, in every clause except the select list itself
(`select path as p, line_no from blame where p = 'a.py'` succeeds via
the alias) - with the real column always winning when a name is both,
*except* in `ORDER BY`, where the alias wins instead. `_resolve_column`
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
qualifier to match against, so a qualified `ColumnRef` is looked up
among the real columns only.


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
    In,
    Is,
    Like,
    Literal,
    SelectStatement,
    Star,
)
from historian.sql.bound import BindError, BoundSelectItem
from historian.sql.walk import (
    BoundColumnRef,
    children,
    contains_aggregate,
    is_constant,
    resolution_children,
    with_resolution_children,
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
    #: legal, regardless of name or arity: `WHERE`, `ORDER BY` of a
    #: non-aggregate query, and `LIMIT`/`OFFSET`. `False` (the default)
    #: for the select list, `GROUP BY` and `HAVING`.
    reject_aggregates: bool = False
    #: Issue #115: where a rejected aggregate goes. `None` records it
    #: in the walk, where it stands (WHERE of a non-aggregate query,
    #: LIMIT/OFFSET). A list collects it instead, and the walk goes on
    #: as if the call were legal: `bind()` raises the first collected
    #: error only after GROUP BY, and only if nothing else was raised,
    #: because that is when SQLite reports an aggregate call in the
    #: WHERE of an aggregate query, or in the ORDER BY of a
    #: non-aggregate one. See `sql/binder.py`'s "Resolution order".
    late_misuse: list[BindError] | None = None
    #: Issue #144: the clause `reject_aggregates` is set for, named in
    #: the misuse message (`"WHERE"`, `"ORDER BY"`), so the message
    #: names the clause the call was found in. Empty for LIMIT/OFFSET,
    #: whose message names no clause.
    clause: str = ""
    #: Issue #144: `False` for LIMIT/OFFSET, which SQLite resolves
    #: against no table at all - every column reference there, a real
    #: column or an alias, is "no such column".
    columns_visible: bool = True


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
#
# Every lookup here returns the error it finds rather than raising it:
# the walk below decides whether an error is the one reported (#144).


def _column_error(ref: ColumnRef, available: tuple[str, ...]) -> BindError:
    """`no such column`, naming the reference as written - `name`, or
    the whole dotted `table.name`."""
    display = f"{ref.table}.{ref.name}" if ref.table is not None else ref.name
    return BindError(f"no such column: {display}", ref.position, available)


def _lookup_column(ref: ColumnRef, ctx: _Context) -> BoundColumnRef | None:
    """Look up `ref.name` against `ctx.schema` alone; `None` if no real
    column matches by name. The qualifier is the caller's to check."""
    for offset, column in enumerate(ctx.schema.columns):
        if _same_name(column.name, ref.name):
            return BoundColumnRef(offset=offset, name=column.name, position=ref.position)
    return None


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


def _resolve_column(ref: ColumnRef, ctx: _Context) -> tuple[Expr | None, BindError | None]:
    """Resolve `ref` to a bound column or, with `ctx.alias_fallback`, a
    select-list alias's bound expression - see the module docstring's
    "`WHERE` resolving a select-list alias" section (issue #32).
    Returns `(bound, None)`, or `(None, error)` when nothing matches.

    A qualifier that does not match the FROM table - whether a real,
    unrelated table or an unknown name - is "no such column: <qualifier>.
    <name>", the whole dotted reference verbatim, never "no such
    table". This is the opposite of `_bind_star`'s qualifier check
    below; both are separately confirmed against `sqlite3` and must not
    be unified. A qualified reference never falls back to an alias -
    confirmed against `sqlite3` (`select b as x from t where t.x = 10`
    still raises "no such column: t.x"): aliases have no table
    qualifier to match against.

    For an unqualified name with the fallback on, both a real-column
    match and an alias match are looked up, and `ctx.alias_first`
    decides which one wins when both exist: `False` for `WHERE`/`GROUP
    BY`/`HAVING` (the real column wins), `True` for `ORDER BY` (the
    alias wins instead - confirmed against `sqlite3`, the one clause
    where the four are not uniform).

    With `ctx.columns_visible` off (LIMIT/OFFSET) nothing resolves.
    """
    if not ctx.columns_visible:
        return None, _column_error(ref, ())
    if ref.table is not None and not _same_name(ref.table, ctx.table_name):
        return None, _column_error(ref, ctx.schema.names)
    column_match = _lookup_column(ref, ctx)
    alias_match = None
    if ref.table is None and ctx.alias_fallback:
        alias_match = _find_alias_expr(ref.name, ctx)
    if ctx.alias_first:
        first, second = alias_match, column_match
    else:
        first, second = column_match, alias_match
    if first is not None:
        return first, None
    if second is not None:
        return second, None
    return None, _column_error(ref, ctx.schema.names)


def _bind_star(star: Star, ctx: _Context) -> list[BoundColumnRef]:
    """Expand `*` / `table.*` into one `BoundColumnRef` per column of
    the FROM table's schema, in declared order.

    A qualifier that does not match the FROM table raises "no such
    table: <qualifier>", never "no such column" - confirmed against
    `sqlite3` for both an unrelated real table and an unknown name,
    and the opposite of `_resolve_column`'s qualifier check above.
    """
    if star.table is not None and not _same_name(star.table, ctx.table_name):
        raise BindError(f"no such table: {star.table}", star.position, ctx.catalog_names)
    return [
        BoundColumnRef(offset=offset, name=column.name, position=star.position)
        for offset, column in enumerate(ctx.schema.columns)
    ]


# --- Where an aggregate call is allowed (issues #60, #102, #115, #144) -------
#
# SQLite's `NC_AllowAgg`, plus where a rejected call is reported. One
# value per point of the walk, carried on each pending entry:
#
# - `_AGG_ALLOWED`: the select list, `GROUP BY`, `HAVING`, `ORDER BY` of
#   an aggregate query. (An aggregate key in `GROUP BY` is rejected after
#   the walk, by `sql/bind_clauses.py`.)
# - `_AGG_LATE`: allowed in the walk, but the call is collected in
#   `ctx.late_misuse` and reported after every clause, if nothing else
#   is: `WHERE` of an aggregate query, `ORDER BY` of a non-aggregate one.
# - `_AGG_CLAUSE`: the clause never allows one, and a call records its
#   misuse where it stands: `WHERE` of a non-aggregate query,
#   `LIMIT`/`OFFSET`.
# - `_AGG_NESTED`: inside an allowed aggregate call's arguments, where
#   a nested aggregate records its misuse where it stands, in any clause.

_AGG_ALLOWED = 0
_AGG_LATE = 1
_AGG_CLAUSE = 2
_AGG_NESTED = 3


def _initial_permission(ctx: _Context) -> int:
    """The aggregate permission at the root of a clause's expression."""
    if not ctx.reject_aggregates:
        return _AGG_ALLOWED
    if ctx.late_misuse is not None:
        return _AGG_LATE
    return _AGG_CLAUSE


def _collect_late(error: BindError, ctx: _Context) -> None:
    """Keep *error* in `ctx.late_misuse` if it is the first one there
    (issue #115)."""
    assert ctx.late_misuse is not None
    if not ctx.late_misuse:
        ctx.late_misuse.append(error)


def _clause_misuse(call: FunctionCall, ctx: _Context) -> BindError:
    """The misuse of an aggregate call in a clause that never allows
    one, naming that clause (`ctx.clause`) when it has a name."""
    message = f"misuse of aggregate function {call.name}()"
    if ctx.clause:
        message += f": aggregate calls are not allowed in {ctx.clause}"
    return BindError(message, call.position, ())


def _aliased_aggregate_error(
    ref: ColumnRef, resolved: Expr, ctx: _Context, permission: int
) -> BindError | None:
    """Issue #102: a reference that resolved to a select-list alias
    naming an aggregate call, reached where an aggregate is not
    allowed. A real column is never an aggregate call, so only the
    alias branch of `_resolve_column` can get here.

    Inside an aggregate's arguments (`HAVING count(c) > 0`, `c`
    aliasing `count(*)`) it is `sqlite3`'s "misuse of aliased aggregate
    c", recorded where it stands; in a late clause (`WHERE c > 1` of an
    aggregate query) it is collected, as a call written there would
    be. `None` when there is nothing to record."""
    if permission == _AGG_ALLOWED or not contains_aggregate(resolved):
        return None
    if permission == _AGG_NESTED:
        return BindError(f"misuse of aliased aggregate {ref.name}", ref.position, ())
    error = BindError(
        f"misuse of aggregate: aliased column {ref.name} refers to an aggregate call, "
        "which is not allowed here",
        ref.position,
        (),
    )
    if permission == _AGG_LATE:
        _collect_late(error, ctx)
        return None
    return error


def _bind_column(ref: ColumnRef, ctx: _Context, permission: int) -> tuple[Expr | None, BindError | None]:
    """Rule 1 of the walk: `ref` resolved, or the error it records."""
    resolved, error = _resolve_column(ref, ctx)
    if error is not None:
        return None, error
    assert resolved is not None
    error = _aliased_aggregate_error(ref, resolved, ctx, permission)
    if error is not None:
        return None, error
    return resolved, None


# --- Aggregate calls (issue #60) ----------------------------------------------


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


def _is_aggregate_call(call: FunctionCall) -> bool:
    """A known aggregate name with an argument count it accepts."""
    return ascii_fold(call.name) in _AGGREGATE_NAMES and _arity_error(call) is None


def _call_error(call: FunctionCall, ctx: _Context, permission: int) -> BindError | None:
    """Rule 2 of the walk: the one error *call* records before its
    arguments are walked, or `None`. An unrecognised name first, then
    the arity, then aggregate misuse - SQLite's order within one call
    (issue #115): `WHERE foo(x) > 1` is "no such function", `WHERE
    avg() = 1` is "wrong number of arguments to function avg()", never
    the misuse. A misuse in a late clause is collected in
    `ctx.late_misuse` instead of recorded."""
    if ascii_fold(call.name) not in _AGGREGATE_NAMES:
        return BindError(f"no such function: {call.name}", call.position, tuple(sorted(_AGGREGATE_NAMES)))
    arity_error = _arity_error(call)
    if arity_error is not None:
        return arity_error
    if permission == _AGG_CLAUSE:
        return _clause_misuse(call, ctx)
    if permission == _AGG_NESTED:
        return BindError(
            f"misuse of aggregate function {call.name}(): aggregate function calls cannot be nested",
            call.position,
            (),
        )
    if permission == _AGG_LATE:
        _collect_late(_clause_misuse(call, ctx), ctx)
    return None


def _argument_permission(call: FunctionCall, permission: int) -> int:
    """The aggregate permission inside *call*'s arguments: an aggregate
    call accepted where it stands (allowed, or collected late) allows
    none in its arguments; any other call - unknown, wrong arity, or
    itself a misuse - leaves the permission as it was, as SQLite does."""
    if permission in (_AGG_ALLOWED, _AGG_LATE) and _is_aggregate_call(call):
        return _AGG_NESTED
    return permission


def _is_star_call(call: FunctionCall) -> bool:
    """`f(*)`: SQLite's tree has no argument there at all, so there is
    nothing to walk - `count(*)` binds as it is, passed through
    unexpanded (`*` here means "no columns", not "all columns", see
    `sql/bound.py`'s docstring). A *qualified* sole argument
    (`count(blame.*)`) is not this, and reaches the `Star` backstop."""
    return len(call.args) == 1 and isinstance(call.args[0], Star) and call.args[0].table is None


# --- The walk (issues #107, #144) ---------------------------------------------
#
# One explicit stack of `(node, step, permission)` entries, no
# recursion. `_VISIT` is a node's first visit. `_REBUILD` comes back to
# a node whose operands are all bound and rebuilds it around them.
# `_RETURN` does the same for a function call, `LIKE` or `IS NULL`, and
# is also the frame marker an ABORT unwinds to. `_IS_RIGHT` is rule 5's
# right-hand column, already resolved, met again after the left operand.
# `_IN_PLUS` is rule 7's `+`, between a one-element `IN`'s left operand
# and its element.

_VISIT = 0
_REBUILD = 1
_RETURN = 2
_IS_RIGHT = 3
_IN_PLUS = 4

#: The walk's explicit stack of `(node, step, permission)` entries.
_Pending = list[tuple[Expr, int, int]]


def _star_backstop(star: Star) -> BindError:
    # A whole, alias-less select-list item and a call's sole
    # unqualified `*` are handled before the walk ever reaches one -
    # see `_bind_select_item` and `_is_star_call`. Any other position is
    # exactly the parser-permissiveness backstop: `* AS alias`, `*`
    # inside a general expression, and `count(blame.*)` (a *qualified*
    # star as a function argument, a syntax error in SQLite) all reach
    # here and are rejected at once rather than crashing or silently
    # mis-expanding.
    return BindError(
        "* is only allowed as a whole select-list item or the sole argument to a function call",
        star.position,
        (),
    )


def _is_null_test(node: Is) -> bool:
    """`x IS NULL` / `x IS NOT NULL` (SQLite's `TK_ISNULL`/`TK_NOTNULL`):
    the parser builds both as `Is` with a `NULL` literal on the right."""
    return isinstance(node.right, Literal) and node.right.value is None


def _push_operands(node: Expr, step: int, permission: int, operands: tuple[Expr, ...], pending: _Pending) -> None:
    """Come back to *node* with *step* once *operands* are walked, left
    to right."""
    pending.append((node, step, permission))
    for operand in reversed(operands):
        pending.append((operand, _VISIT, permission))


def _visit_column(ref: ColumnRef, permission: int, ctx: _Context, results: list[Expr]) -> tuple[BindError | None, bool]:
    """Rule 1: a column that resolves never trips, whatever is recorded
    already; one that does not records its error and ABORTs."""
    bound, error = _bind_column(ref, ctx, permission)
    if error is not None:
        return error, True
    assert bound is not None
    results.append(bound)
    return None, False


def _visit_call(
    call: FunctionCall, permission: int, ctx: _Context, pending: _Pending, results: list[Expr]
) -> tuple[BindError | None, bool]:
    """Rule 2: the call's own error first; then its arguments, in a
    list an ABORT ends without ending anything around the call."""
    error = _call_error(call, ctx, permission)
    if _is_star_call(call):
        results.append(call)
    else:
        _push_operands(call, _RETURN, _argument_permission(call, permission), call.args, pending)
    return error, False


def _visit_is_column(
    node: Is, right: ColumnRef, permission: int, tripped: bool, ctx: _Context, pending: _Pending
) -> tuple[BindError | None, bool]:
    """Rule 5: the bare column on the right is resolved before anything
    else, then the node trips like any other, then the left operand is
    walked and the resolved right is met again (`_IS_RIGHT`), where it
    trips too, as SQLite's already-resolved column does."""
    bound, error = _bind_column(right, ctx, permission)
    if error is not None:
        return error, True
    if tripped:
        return None, True
    assert bound is not None
    pending.append((node, _REBUILD, permission))
    pending.append((bound, _IS_RIGHT, permission))
    pending.append((node.left, _VISIT, permission))
    return None, False


def _visit_one_element_in(node: In, element: Expr, permission: int, pending: _Pending) -> None:
    """Rule 7: the left operand, then SQLite's `+` (`_IN_PLUS`), then
    the element. Whether the element is constant is asked only when the
    `+` is reached with an error recorded - the only time the answer
    matters - so a chain of nested one-element lists is not checked once
    per level: an `IN` met with an error recorded trips at its own visit
    and never reaches its `+`."""
    pending.append((node, _REBUILD, permission))
    pending.append((element, _VISIT, permission))
    pending.append((element, _IN_PLUS, permission))
    pending.append((node.left, _VISIT, permission))


def _visit(
    node: Expr, permission: int, tripped: bool, ctx: _Context, pending: _Pending, results: list[Expr]
) -> tuple[BindError | None, bool]:
    """The first visit to *node*: SQLite's `resolveExprStep`, one
    branch per rule of the module docstring's "Which error is
    reported". *tripped* is whether an error is already recorded.
    Returns the error *node* records, if any, and whether the walk
    ABORTs here. Pushes what is left to walk onto *pending* and a bound
    leaf onto *results*."""
    if isinstance(node, ColumnRef):
        return _visit_column(node, permission, ctx, results)
    if isinstance(node, FunctionCall):
        return _visit_call(node, permission, ctx, pending, results)
    if isinstance(node, Like):
        # Rule 3: a call over (pattern, left, escape) with no error of
        # its own. `x NOT LIKE y` is a NOT - rule 6 - around the call.
        if node.negated and tripped:
            return None, True
        _push_operands(node, _RETURN, permission, resolution_children(node), pending)
        return None, False
    if isinstance(node, Is) and _is_null_test(node):
        # Rule 4: the operand is walked whatever is recorded, and an
        # ABORT in it stops here.
        _push_operands(node, _RETURN, permission, children(node), pending)
        return None, False
    if isinstance(node, Is) and isinstance(node.right, ColumnRef) and node.right.table is None:
        return _visit_is_column(node, node.right, permission, tripped, ctx, pending)
    if isinstance(node, Star):
        raise _star_backstop(node)
    if isinstance(node, BoundColumnRef):
        raise AssertionError("sql/binder.py: a BoundColumnRef reached _bind_expr; it is already bound")
    # Rule 6: every other node trips on a recorded error.
    if tripped:
        return None, True
    if isinstance(node, In) and len(node.values) == 1:
        _visit_one_element_in(node, node.values[0], permission, pending)
        return None, False
    operands = resolution_children(node)
    if len(operands) == 0:
        results.append(node)
    else:
        _push_operands(node, _REBUILD, permission, operands, pending)
    return None, False


def _rebuild(node: Expr, results: list[Expr]) -> Expr:
    """Take *node*'s bound operands off the end of *results*, in
    `resolution_children` order, and rebuild *node* around them."""
    first = len(results) - len(resolution_children(node))
    bound_operands = results[first:]
    del results[first:]
    return with_resolution_children(node, bound_operands)


def _unwind_to_enclosing_call(pending: _Pending) -> None:
    """An ABORT: drop every pending entry down to the nearest enclosing
    function call, `LIKE` or `IS NULL` (`_RETURN`), which then returns
    normally, so the walk goes on with whatever follows it - or, with
    none, drop everything: the root is over."""
    while len(pending) > 0 and pending[-1][1] != _RETURN:
        pending.pop()


def _bind_exprs(roots: tuple[Expr, ...], ctx: _Context) -> tuple[Expr, ...]:
    """Bind *roots*, walked as one tree whose root has them as its
    children left to right (an ABORT in one skips the rest), and raise
    the error recorded last, if any - see the module docstring's "Which
    error is reported". Every other caller binds one root, through
    `_bind_expr`; LIMIT and OFFSET are one tree in SQLite and are
    walked together.

    Not recursive (issue #107): *pending* is an explicit stack of
    `(node, step, permission)` entries (see the comment above
    `_VISIT`) and *results* the bound subtrees finished so far. Once an
    error is recorded no tree is built any more - only which error is
    reported is still open.
    """
    permission = _initial_permission(ctx)
    pending: _Pending = []
    for root in reversed(roots):
        pending.append((root, _VISIT, permission))
    results: list[Expr] = []
    recorded: BindError | None = None
    while len(pending) > 0:
        node, step, permission = pending.pop()
        abort = False
        if step == _REBUILD or step == _RETURN:
            if recorded is None:
                results.append(_rebuild(node, results))
        elif step == _IS_RIGHT:
            if recorded is not None:
                abort = True
            else:
                results.append(node)
        elif step == _IN_PLUS:
            abort = recorded is not None and is_constant(node)
        else:
            error, abort = _visit(node, permission, recorded is not None, ctx, pending, results)
            if error is not None:
                recorded = error
        if abort:
            _unwind_to_enclosing_call(pending)
    if recorded is not None:
        raise recorded
    assert len(results) == len(roots)
    return tuple(results)


def _bind_expr(expr: Expr, ctx: _Context) -> Expr:
    """Bind every `ColumnRef` in `expr`'s tree against `ctx.schema`,
    with select-list alias fallback (issue #32) when `ctx.alias_fallback`
    is set - off by default on the `_Context` every select-list item
    binds with (`_bind_select_item`), which is how aliases stay
    invisible to each other; on for the `_Context`s `bind()` builds
    for the other clauses. `ctx` is the same for every node of the tree,
    so the fallback applies to a `ColumnRef` at any depth in the tree,
    not only at the top. One root of the walk: `_bind_exprs`."""
    (bound,) = _bind_exprs((expr,), ctx)
    return bound
