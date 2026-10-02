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
box from spec §5** - milestone item 18; `BindError` here carries
structured fields (message, position, available names), not text to
print.

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
at once: see "Resolution order" below for when it is reported.

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

`WHERE` resolving a select-list alias
--------------------------------------

Issue #32. SQLite falls back to a select-list alias for any name no
real column claims, in every clause except the select list itself
(`select path as p, line_no from blame where p = 'a.py'` succeeds via
the alias) - with the real column always winning when a name is both,
*except* in `ORDER BY`, where the alias wins instead. `_resolve_name`
below implements this as one function taking a precedence-direction
flag (`alias_first`), rather than a `WHERE`-specific helper, because
`GROUP BY`/`HAVING` (#60) and `ORDER BY` (#61) need the same rule with
their own direction - `alias_first=False` for the former two,
`alias_first=True` for `ORDER BY`. Only `WHERE` has a live caller
today (`bind()` passes `alias_first=False`); `GROUP BY`, `HAVING` and
`ORDER BY` have no grammar yet (#60, #61 add it) and are expected to
call `_resolve_name` rather than reinvent it.

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

Bound tree shape
-----------------

`sql/ast.py`'s node types are frozen, so this module cannot annotate
them in place - it builds a new tree instead. Every `Expr` node type
that carries no name to resolve (`Literal`, `BinaryOp`, `And`, `Or`,
`Not`, `Is`, `Like`, `In`, `Between`, `UnaryOp`, `FunctionCall`) is
reused unchanged as a *type*: this module walks into its children and
rebuilds the node via `dataclasses.replace` with the bound children in
place of the originals. `ColumnRef` is the one node type that does
carry a name, and every occurrence of it is replaced by `BoundColumnRef`,
a new leaf carrying the resolved integer offset plus enough to render
an error later (the resolved name, the position). `Star` is expanded
away entirely at the select-list level, into one `BoundColumnRef` per
column of the FROM table's schema in declared order - except as the
sole, unqualified argument of a `FunctionCall` (`count(*)`), where it
is passed through untouched: `*` there means "no columns", not "all
columns", and this module does not validate that the function name is
real.

Because v1 has exactly one FROM table and no `JOIN`, a `BoundColumnRef`
does not track which table it came from - the offset alone is
unambiguous. Building multi-table bookkeeping now, before there is a
`JOIN` to need it, is exactly the kind of speculative generality
`AGENTS.md` asks to redesign rather than pre-build.

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

Resolution order
------------------

When a statement has more than one error, `bind()` reports the one
SQLite reports (issue #115, spec §3 "Errors"). Measured against the
oracle - Python's `sqlite3` module, SQLite 3.45.1 - by splicing one,
two and three erroring fragments from different clauses into a base
query (`tests/differential/test_error_order.py`), the order is:

1. The FROM table, then the qualifier of any `x.*` select-list item
   (`SELECT ghost_s, ghost.* FROM blame` reports `no such table:
   ghost`).
2. LIMIT, then OFFSET - only what SQLite rejects there: a column
   reference (any, even a real column or an alias: LIMIT sees no
   columns) or an aggregate call. A column reference outside every
   aggregate call is reported at once; an error inside an aggregate
   call only once both clauses have no such column reference, the
   last one found winning (`_check_limit_offset_names`).
3. The select list, items left to right.
4. `HAVING` on a non-aggregate query.
5. `HAVING`.
6. `WHERE`. In a non-aggregate query an aggregate call here is
   reported in place, in left-to-right order with the clause's names.
7. `ORDER BY`, terms left to right: every name error first, then an
   out-of-range ordinal.
8. `GROUP BY`, terms left to right: every name error first, then an
   out-of-range ordinal, then an aggregate key.
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

Within one clause expression, operands are visited left to right, so
the leftmost unresolved name wins (`select ghost1, ghost2 from blame`
reports `ghost1`); within one call the name is checked first, then
the arity, then aggregate misuse (`WHERE avg() = 1` reports the
arity), and a nested aggregate is reported where it is found, in any
clause. The order of different error kinds inside one expression tree
is #144. None of this is `SelectStatement`'s own field order
(`select_list`, `from_table`, `where`, ...), which a naive walk of the
dataclass's fields would follow instead.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass

from historian.ascii import ascii_fold
from historian.schema import Schema
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
    OrderByItem,
    OrderDirection,
    Or,
    SelectItem,
    SelectStatement,
    Star,
    UnaryOp,
    UnaryOperator,
)
from historian.sql.lexer import Position
from historian.sql.walk import (
    BoundColumnRef,
    children,
    contains_aggregate,
    expr_shape_equal,
    is_aggregate_query,
    with_children,
)

__all__ = [
    "BindError",
    "BoundColumnRef",
    "BoundOrderByItem",
    "BoundSelectItem",
    "BoundSelectStatement",
    "bind",
]

#: The v1 aggregate registry (issue #60): every `FunctionCall` name
#: this grammar can legally bind, ASCII-folded. v1 has no scalar
#: functions (`_docs/spec.md` §1), so this is not a partial list
#: alongside some other kind of function - anything not in it is
#: unconditionally unknown. See the module docstring's "Aggregate
#: calls" section.
_AGGREGATE_NAMES = frozenset({"count", "sum", "avg", "min", "max"})


# --- Errors ------------------------------------------------------------
#
# Same shape as `LexError` (`sql/lexer.py`) and `ParseError`
# (`sql/parser.py`): message via `Exception.__init__`, plus a
# `.position` attribute, so a caller can report it per spec §5 without
# a traceback ever reaching the user. `.available` is this module's own
# addition - the names that *would* have resolved, for the eventual
# "blame has: ..." rendering (#18), never used by this module itself.


class BindError(Exception):
    """A table or column reference in the statement does not resolve
    against the catalog, or a `Star` appears somewhere it cannot mean
    anything (see the module docstring).

    Structured, not rendered: `message` is plain text naming the
    problem, `position` points at the offending token, and `available`
    lists what the caller could have referred to instead - table names
    for an unresolved table, column names in schema order for an
    unresolved column. Rendering this per spec §5 (the caret, "blame
    has: ...") is milestone item 18, not this module.
    """

    def __init__(self, message: str, position: Position, available: tuple[str, ...]) -> None:
        super().__init__(message)
        self.position = position
        self.available = available


# --- Bound tree ----------------------------------------------------------
#
# Frozen dataclasses, matching `sql/ast.py`'s own convention. Only one
# new node type, `BoundColumnRef`: every other `Expr` in a bound tree is
# one of `sql/ast.py`'s own types, reused unchanged as a type and
# rebuilt (via `dataclasses.replace`) only where a descendant changed.
# `BoundColumnRef` is defined in `sql/walk.py` (issue #112), next to the
# shared walks that must know it, and imported here, so `from
# historian.sql.binder import BoundColumnRef` names the same class.


@dataclass(frozen=True)
class BoundSelectItem:
    """One resolved entry in a `SELECT` list.

    `output_name` is the header this item produces: the explicit
    `alias`, used verbatim with no folding, when one was written;
    otherwise the declared schema spelling when `expr` is a
    `BoundColumnRef`; otherwise `None` - an unaliased, non-column
    expression's header is not something this issue's criteria pin
    down, and nothing downstream yet consumes it.
    """

    expr: Expr
    alias: str | None
    output_name: str | None
    position: Position


@dataclass(frozen=True)
class BoundOrderByItem:
    """One resolved entry in an `ORDER BY` list (issue #61): a bound
    key expression and its direction - an ordinal is already resolved
    to the referenced select-list item's own bound expression, not
    carried as a `Literal` any more, exactly like `group_by`'s own
    ordinal handling. Still may contain a real `FunctionCall` aggregate
    node - splitting it out into an `Aggregate` slot is `plan/
    planner.py`'s job, the same split already applied to `select_list`
    and `having`.
    """

    expr: Expr
    direction: OrderDirection
    position: Position


@dataclass(frozen=True)
class BoundSelectStatement:
    """A `SelectStatement` with every table and column reference
    resolved. `from_table` is the catalog's own key for the FROM
    table (its declared spelling), not necessarily the casing the
    query used.

    `group_by` (issue #69) is `()` when the query has no `GROUP BY`,
    else the resolved key expressions in clause order - an ordinal is
    already resolved to the referenced select-list item's own bound
    expression, not carried as a `Literal` any more. `having` is
    `None` when absent, else the bound predicate - still containing
    real `FunctionCall` aggregate nodes, since splitting those out
    into `Aggregate` slots is `plan/planner.py`'s job (the same split
    it already applies to `select_list`, per `_docs/spec.md` §3's
    "Expression evaluation"). `order_by` (issue #61) is `()` when the
    query has no `ORDER BY`, else the resolved `BoundOrderByItem`s in
    clause order - see that class's own docstring, and the module
    docstring's "`WHERE` resolving a select-list alias" section for
    why `ORDER BY`'s own resolution is alias-first, the reverse of
    every other clause here. `limit`/`offset` (issue #77) are `int |
    None`, already resolved to a plain Python `int` - never an `Expr`
    - by `_bind_limit_offset` below, reusing `_ordinal_value`'s
    literal-integer recognition (arbitrary unary +/- nesting) with no
    range restriction: 0 and any negative value resolve successfully,
    carrying the runtime meaning `exec/operators.py`'s `Limit` gives
    them. `None` when the corresponding clause is absent - `offset` is
    never set while `limit` is `None`, since §1's grammar has no bare
    `OFFSET`. `distinct` (issue #78) is carried straight through from
    `SelectStatement.distinct` unchanged - `DISTINCT` names no table or
    column, so there is nothing for this module to resolve about it;
    it exists on the bound tree only so `plan/planner.py` knows
    whether to insert a `Distinct` operator. See this module's own
    "DISTINCT" section, below, for the one thing `distinct` *does*
    affect here: a narrowing on `order_by`.
    """

    select_list: tuple[BoundSelectItem, ...]
    from_table: str
    where: Expr | None
    group_by: tuple[Expr, ...]
    having: Expr | None
    order_by: tuple[BoundOrderByItem, ...]
    limit: int | None
    offset: int | None
    position: Position
    distinct: bool = False


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
# aliases stay invisible to each other - finding 3); `bind()` builds a
# second `_Context`, via `dataclasses.replace`, with the fallback
# turned on for `WHERE`.


@dataclass(frozen=True)
class _Context:
    schema: Schema
    table_name: str
    catalog_names: tuple[str, ...]
    select_items: tuple[BoundSelectItem, ...] = ()
    alias_fallback: bool = False
    alias_first: bool = False
    #: Issue #60: `True` while binding `WHERE` - the one clause a
    #: `FunctionCall` can appear in today where an aggregate call is
    #: never legal, regardless of name or arity. `False` (the default)
    #: for the select list, where an aggregate call is exactly what
    #: this issue exists to allow.
    reject_aggregates: bool = False
    #: Issue #115: where a rejected aggregate goes. `None` raises it on
    #: the spot (WHERE of a non-aggregate query). A list collects it
    #: instead, and binding goes on as if the call were legal: `bind()`
    #: raises the first collected error only after GROUP BY, because
    #: that is when SQLite reports an aggregate call in the WHERE of an
    #: aggregate query, or in the ORDER BY of a non-aggregate one. See
    #: the module docstring's "Resolution order".
    late_misuse: list[BindError] | None = None


# --- FROM-table resolution -------------------------------------------------


def _resolve_table(stmt: SelectStatement, catalog: dict[str, Schema]) -> _Context:
    """Resolve `stmt.from_table` against `catalog`, case-insensitively
    and ASCII-only. Checked first, before anything else in the
    statement - step 1 of the module docstring's "Resolution order".

    The error's position is `stmt.position` (the `SELECT` keyword):
    `from_table` is a bare string on the AST with no position of its
    own to point a caret at. Confirmed this is not a gap to route
    around: `sqlite3`'s own CLI likewise prints no caret for "no such
    table" (only for "no such column"), so a future renderer (#18) is
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
    `sqlite3` (issue #32 finding 4). An item with no explicit `alias`
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
    `ORDER BY` (#61; the alias wins instead - confirmed against
    `sqlite3`, the one clause where the four are not uniform). Only
    `alias_first=False` has a reachable caller today, from `bind()`'s
    `WHERE` handling.
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
                f"misuse of aggregate function {call.name}(): aggregate calls are not allowed in WHERE",
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
    against the oracle, 3.45.1)."""
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


def _bind_expr(expr: Expr, ctx: _Context) -> Expr:
    """Bind every `ColumnRef` in `expr`'s tree against `ctx.schema`,
    with select-list alias fallback (issue #32) when `ctx.alias_fallback`
    is set - off by default on the `_Context` every select-list item
    binds with (`_bind_select_item`), which is how aliases stay
    invisible to each other (finding 3); on for the `_Context` `bind()`
    builds for `WHERE`. `ctx` is the same for every node of the tree,
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
            first = len(results) - len(children(node))
            bound_operands = results[first:]
            del results[first:]
            if isinstance(node, FunctionCall):
                _check_no_nested_aggregate(node, bound_operands, ctx)
            results.append(with_children(node, bound_operands))
            continue
        if isinstance(node, Literal):
            results.append(node)
            continue
        if isinstance(node, ColumnRef):
            if ctx.alias_fallback:
                results.append(_resolve_name(node, ctx))
            else:
                results.append(_bind_column_ref(node, ctx))
            continue
        if isinstance(node, Star):
            # A whole, alias-less select-list item and count(*)'s sole
            # unqualified argument are handled by their own callers before
            # ever reaching here - see `_bind_select_item` and the
            # `FunctionCall` case below. Any other position is exactly the
            # parser-permissiveness backstop the grooming asked for: `* AS
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
        if isinstance(node, FunctionCall):
            # Issue #60: name/arity/WHERE-rejection, before anything else -
            # see _validate_function_call and the module docstring's
            # "Aggregate calls" section. Every FunctionCall past this point
            # is a real, correctly-arity aggregate call.
            _validate_function_call(node, ctx)
            if len(node.args) == 1 and isinstance(node.args[0], Star) and node.args[0].table is None:
                # count(*): passed through unexpanded. `*` here means "no
                # columns", not "all columns" - see the module docstring.
                # A *qualified* sole argument (count(blame.*)) does not
                # take this path and falls through to the general Star
                # rejection above.
                results.append(node)
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


# --- GROUP BY / HAVING (issue #69) ------------------------------------------
#
# An aggregate call cannot be a grouping key, however it is named -
# direct, via a select-list alias, or by ordinal (orchestrator
# correction on this issue: `select b, count(*) from t group by 2`
# raises the identical "aggregate functions are not allowed in the
# GROUP BY clause" sqlite3 gives for the direct and alias forms, not
# "ludicrous but legal"). `contains_aggregate` is the one predicate
# every one of those three routes checks against, after binding.


# --- Ordinal detection, shared by GROUP BY and ORDER BY (issue #61) --------
#
# Orchestrator correction: SQLite treats *any* nesting of unary `+`/`-`
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
    a column reference or aggregate call here has already been
    reported by `_check_limit_offset_names`, at LIMIT's own turn, so
    what reaches this rejection is a shape SQLite accepts."""
    value = _ordinal_value(expr)
    if value is None:
        raise BindError(
            f"{clause} must be a literal integer, optionally wrapped in "
            "unary +/- and parentheses",
            expr.position,
            (),
        )
    return value


def _check_limit_offset_names(exprs: tuple[Expr, ...]) -> None:
    """The errors SQLite raises for `LIMIT` and `OFFSET` (*exprs*, in
    that order), at their turn in the resolution order: right after the
    FROM table, before the select list (issue #115). SQLite resolves
    them against no columns at all and with no select-list alias, so
    every column reference is "no such column", real column or not.

    Measured against the oracle (3.45.1), two strengths of error:

    - A column reference outside every aggregate call is reported at
      once (`LIMIT ghost_l OFFSET ghost_f` reports `ghost_l`).
    - An aggregate call is always an error here, but a soft one: the
      call's arity error or, failing that, its misuse, and then any
      column reference inside its arguments, each replacing the one
      before - and a later hard error, or a later soft one, in either
      clause replaces it again. Only if both clauses are walked
      without a hard error is the last soft error raised (`LIMIT
      avg(1) OFFSET ghost_f` reports `ghost_f`; `LIMIT count(*) OFFSET
      sum(1)` reports `sum`; `LIMIT count(ghost_x)` reports
      `ghost_x`).

    A call to a function that is not an aggregate is left alone, along
    with its arguments: SQLite rejects it here too, but the order of
    that against the rest is #144, so it reaches `_bind_limit_offset`'s
    literal-only rejection instead. Pre-order, left to right, over an
    explicit stack of `(node, inside an aggregate call)` (issue #107).
    """
    soft: BindError | None = None
    for expr in exprs:
        pending: list[tuple[Expr, bool]] = [(expr, False)]
        while pending:
            node, inside_aggregate = pending.pop()
            if isinstance(node, ColumnRef):
                display = f"{node.table}.{node.name}" if node.table is not None else node.name
                error = BindError(f"no such column: {display}", node.position, ())
                if not inside_aggregate:
                    raise error
                soft = error
                continue
            if isinstance(node, FunctionCall):
                if ascii_fold(node.name) not in _AGGREGATE_NAMES:
                    continue
                arity_error = _arity_error(node)
                if arity_error is not None:
                    soft = arity_error
                else:
                    soft = BindError(f"misuse of aggregate function {node.name}()", node.position, ())
                inside_aggregate = True
            for operand in reversed(children(node)):
                pending.append((operand, inside_aggregate))
    if soft is not None:
        raise soft


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
    2. Every ordinal (`_ordinal_value`, shared with `ORDER BY`) is
       checked against `bound_items` (the select list *after* `Star`
       expansion, matching `sqlite3`'s own "1st GROUP BY term"
       counting) and resolved to that item's own bound expression -
       purely by position, never through `_resolve_name`, since an
       ordinal is not a name.
    3. A term that is, or reaches, an aggregate call - written
       directly, an alias of one, or an ordinal pointing at one - is
       rejected identically, the uniform rule the orchestrator's
       correction on #69 states: `GROUP BY count(*), 99` reports the
       ordinal.

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
    for raw_expr in group_by:
        if _ordinal_value(raw_expr) is None:
            keys.append(_bind_expr(raw_expr, group_ctx))
        else:
            keys.append(None)
    for index, raw_expr in enumerate(group_by):
        if keys[index] is None:
            ordinal = _check_ordinal(raw_expr, index, "GROUP BY", len(bound_items))
            keys[index] = bound_items[ordinal - 1].expr
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


# --- ORDER BY (issue #61) ----------------------------------------------------
#
# The one clause that reverses two rules every other clause here holds:
# the select-list alias wins over a same-named real column
# (`_resolve_name`'s `alias_first=True` - #32's own reserved-but-unused
# direction, confirmed against sqlite3 during this issue's grooming:
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
    reports `ghost`; then every ordinal (`_ordinal_value`) is checked,
    1-based, against `bound_items`, out-of-range (0, negative, or past
    the end) raising the same "Nth ... term out of range" shape
    `GROUP BY` uses, and resolves to the referenced item's own bound
    expression. Unlike `GROUP BY`, an ordinal resolving to an aggregate
    call is never rejected (see the section comment above).
    """
    exprs: list[Expr | None] = []
    for item in order_by:
        if _ordinal_value(item.expr) is None:
            exprs.append(_bind_expr(item.expr, ctx))
        else:
            exprs.append(None)
    bound: list[BoundOrderByItem] = []
    for index, item in enumerate(order_by):
        expr = exprs[index]
        if expr is None:
            ordinal = _check_ordinal(item.expr, index, "ORDER BY", len(bound_items))
            expr = bound_items[ordinal - 1].expr
        bound.append(BoundOrderByItem(expr=expr, direction=item.direction, position=item.position))
    return tuple(bound)


# --- The grouped narrowing (issue #60, extended by #69) ---------------------
#
# #60's own rule ("a bare column mixed with an aggregate, no GROUP BY,
# is a BindError") extends here to grouped queries: a select-list
# expression must be an aggregate call, a GROUP BY key (exactly, or
# built purely from GROUP BY keys - `_docs/decisions.md`'s follow-on
# note), or a BindError. `_split_for_grouped_check` is
# `_split_for_aggregate_check`'s own walk with one addition: at every
# node, first check whether the whole subtree matches a GROUP BY key
# by shape (`expr_shape_equal`) - if so, that subtree is covered and
# is never walked into for a bad bare column, whatever it contains.
#
# DISTINCT (issue #78) reuses this exact walk for a different question,
# in `bind()` itself rather than a dedicated function here: when
# `stmt.distinct` is set, every bare column an ORDER BY key touches
# must match a select-list item by shape (exactly, or be built purely
# from select-list items) - `_split_for_grouped_check(order_item.expr,
# select_exprs)`, passing the bound select-list expressions in place
# of `group_by`'s keys. An ordinal ORDER BY key needs no extra check:
# it already resolves to the referenced select-list item's own bound
# expression (`_bind_order_by`, above), which trivially
# shape-matches itself as the first `group_keys` entry checked. A
# select-list alias reference is the same story: `_resolve_name`
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
    bound_items: list[BoundSelectItem], group_by: tuple[Expr, ...]
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


# --- Entry point -------------------------------------------------------------


def bind(stmt: SelectStatement, catalog: dict[str, Schema]) -> BoundSelectStatement:
    """Resolve every table and column reference in `stmt` against
    `catalog`, and expand `SELECT *` / `table.*`.

    Raises `BindError` - never returns `None`/`False` - for the first
    error in the order the module docstring's "Resolution order"
    section states, which is SQLite's (issue #115): the FROM table and
    any unknown `x.*` qualifier, LIMIT then OFFSET, the select list,
    HAVING on a non-aggregate query, HAVING, WHERE, ORDER BY, GROUP BY,
    the late aggregate misuse, and last historian's own rejections of
    queries SQLite accepts. The body below is that order, one step
    after another. `catalog` is required, not defaulted: this module
    never imports `historian.tables.blame` or `historian.catalog`
    itself (issue #35 - AGENTS.md's "no git and no subprocess
    imports" for everything above the scan operators), so it has no
    real catalog of its own to fall back to. Callers that want the
    real `blame` table pass `historian.catalog.SCHEMAS` explicitly -
    `cli.py` is the one production call site that does.
    """
    # 1. The FROM table, then the qualifier of any `x.*` select-list
    # item - SQLite expands stars before resolving any name, so
    # `SELECT ghost_s, ghost.* FROM blame` reports `ghost`.
    ctx = _resolve_table(stmt, catalog)
    for item in stmt.select_list:
        if isinstance(item.expr, Star) and item.alias is None:
            _bind_star(item.expr, ctx)

    # 2. LIMIT, then OFFSET: only what SQLite rejects there (a column
    # reference, an aggregate call). The literal-only rule (#77) is
    # historian's own and waits for step 10.
    limit_offset = tuple(expr for expr in (stmt.limit, stmt.offset) if expr is not None)
    _check_limit_offset_names(limit_offset)

    # 3. The select list, left to right. Items bind against the plain
    # `ctx`, with no alias fallback, so aliases stay invisible to each
    # other (#32 finding 3).
    bound_items: list[BoundSelectItem] = []
    for item in stmt.select_list:
        bound_items.extend(_bind_select_item(item, ctx))
    items = tuple(bound_items)

    # Whether the query aggregates at all - GROUP BY written, or an
    # aggregate call anywhere in the select list. An aggregate call in
    # HAVING or ORDER BY does not count (confirmed against sqlite3:
    # `select path from t having count(*) > 1` is still "HAVING clause
    # on a non-aggregate query"). Decides steps 4, 6 and 7.
    aggregate_query = is_aggregate_query(stmt.group_by, [item.expr for item in items])

    # 4. HAVING on a non-aggregate query, before HAVING's own names.
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

    # The late aggregate misuse (step 9) is collected here while WHERE
    # and ORDER BY bind, and raised only after GROUP BY.
    late_misuse: list[BindError] = []

    # 5. HAVING (issue #69): the one clause where an aggregate call
    # *is* legal, referenced directly or by select-list alias. Alias
    # fallback on, column-first. Splitting an aggregate call out into
    # an `Aggregate` slot is `plan/planner.py`'s job (spec §3's
    # "Expression evaluation").
    having_ctx = dataclasses.replace(
        ctx,
        select_items=items,
        alias_fallback=True,
        alias_first=False,
        reject_aggregates=False,
    )
    bound_having = _bind_expr(stmt.having, having_ctx) if stmt.having is not None else None

    # 6. WHERE: alias fallback on (#32), column-first, aggregate calls
    # rejected (#60) - on the spot in a non-aggregate query, but late
    # (step 9) in an aggregate one, which is when SQLite reports them.
    where_ctx = dataclasses.replace(
        ctx,
        select_items=items,
        alias_fallback=True,
        alias_first=False,
        reject_aggregates=True,
        late_misuse=late_misuse if aggregate_query else None,
    )
    bound_where = _bind_expr(stmt.where, where_ctx) if stmt.where is not None else None

    # 7. ORDER BY (#61): alias-first. A bare aggregate call is legal
    # only in an aggregate query; in any other it is rejected late
    # (step 9). See the "ORDER BY" section comment above
    # `_bind_order_by`.
    order_ctx = dataclasses.replace(
        ctx,
        select_items=items,
        alias_fallback=True,
        alias_first=True,
        reject_aggregates=not aggregate_query,
        late_misuse=late_misuse,
    )
    bound_order_by = _bind_order_by(stmt.order_by, order_ctx, items)

    # 8. GROUP BY (#69): last of the clauses, ordinal or named, alias
    # fallback on, column-first.
    bound_group_by = _bind_group_by(stmt.group_by, ctx, items)

    # 9. The late aggregate misuse: an aggregate call in the WHERE of
    # an aggregate query, or in the ORDER BY of a non-aggregate one.
    if late_misuse:
        raise late_misuse[0]

    # 10. historian's own rejections of queries SQLite accepts, after
    # every error SQLite raises (#115), in the order they had before.
    #
    # The select list: #60's bare-column-mixed-with-aggregate
    # narrowing, extended by #69 to GROUP BY keys.
    _check_grouped_select_list(bound_items, bound_group_by)
    if bound_having is not None:
        # Orchestrator correction: a bare column reference in HAVING
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
        _has_aggregate, bad_column = _split_for_grouped_check(bound_having, bound_group_by)
        if bad_column is not None:
            raise BindError(
                f"column {bad_column.name} must appear in the GROUP BY "
                "clause or be used in an aggregate function",
                bad_column.position,
                (),
            )
    if aggregate_query:
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
        for order_item in bound_order_by:
            _has_aggregate, bad_column = _split_for_grouped_check(order_item.expr, bound_group_by)
            if bad_column is not None:
                raise BindError(
                    f"column {bad_column.name} must appear in the GROUP BY "
                    "clause or be used in an aggregate function",
                    bad_column.position,
                    (),
                )
    # DISTINCT (issue #78): a narrowing on ORDER BY, symmetric to the
    # grouped narrowing just above but matched against the *select
    # list* instead of GROUP BY's keys - see the "DISTINCT" section
    # comment above `_split_for_grouped_check` for the sqlite3
    # evidence and the reasoning (oracle reliability, not historian's
    # own determinism - `_docs/decisions.md`). Runs unconditionally
    # whenever `stmt.distinct` is set, independent of whether the
    # query aggregates at all - the two narrowings ask genuinely
    # different questions and neither replaces the other.
    #
    # Issue #103: `strict_function_calls=True` here (and only here) -
    # an aggregate call touched by the ORDER BY key must itself
    # shape-match a select-list item the same way a bare column must,
    # so a `FunctionCall` that does not match comes back as `bad` too,
    # not only a `BoundColumnRef`. This is the one caller of
    # `_split_for_grouped_check` matched against the select list
    # rather than `GROUP BY`'s keys, so it is also the one place an
    # aggregate call is not automatically exempt.
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
    # LIMIT / OFFSET (issue #77): a literal integer, resolved to a
    # plain Python `int` - see `_bind_limit_offset`. `stmt.offset` is
    # never set while `stmt.limit` is `None` (the parser's own
    # guarantee, `sql/ast.py`'s docstring), so `bound_offset` is
    # correspondingly `None` in that case too.
    bound_limit = _bind_limit_offset(stmt.limit, "LIMIT") if stmt.limit is not None else None
    bound_offset = _bind_limit_offset(stmt.offset, "OFFSET") if stmt.offset is not None else None
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
