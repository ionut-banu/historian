"""Name resolution: AST -> bound AST, or a structured error.

The fourth stage of the pipeline in `_docs/spec.md` §3
("binder     AST + resolved columns, errors for unknown names").
Consumes the `SelectStatement` `sql/parser.py` builds and the table
catalog this module owns, resolves every table and column reference
against them, and produces a new tree in which every column reference
is a zero-based integer offset into a `Row` - never a name
`exec/expression.py` (#12) would need to look up per row, per §3's
"column references resolve to integer offsets at bind time rather
than by name at runtime."

Issue #9. Implements the binder half of §3's pipeline plus the
table-catalog half of §2 that `schema.py` and `tables/blame.py` both
leave to this module (see both modules' docstrings).

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
reject_aggregates` is set (`bind()` turns this on for `WHERE`, the
one clause this issue's grammar can put an aggregate call in), is
rejected outright - `WHERE count(*) > 1` is `BindError`, matching
`sqlite3`'s own "misuse of aggregate function" rejection, though not
its wording (§3's Errors section does not require that).

A second, separate check lives in `bind()` itself, after the whole
select list is bound: when any select-list item's expression contains
an aggregate call anywhere, every item is walked for a bare column
reference that sits outside every aggregate call's own arguments
(`_split_for_aggregate_check`) - `SELECT path, count(*) FROM blame`
raises, naming `path`, because there is no `GROUP BY` (not built until
#69) for a bare, non-aggregated column to be grouped by. This is a
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
Python. `_ascii_fold` below implements SQLite's rule directly: only
the 26 ASCII letters move, nothing else is consulted.

Resolution and error order
----------------------------

Confirmed against `sqlite3` by constructing queries with more than one
thing wrong at once: the `FROM` table is resolved first, before
anything else (`select authr_name from ghost` reports the missing
table). Within a clause, the leftmost unresolved name wins (`select
ghost1, ghost2 from blame` reports `ghost1`). The select list resolves
before `WHERE` (`select ghost_select from blame where ghost_where = 1`
reports `ghost_select`). This is *not* `SelectStatement`'s own field
order (`select_list`, `from_table`, `where`) - `bind()` below checks
`from_table` first regardless, which a naive walk of the dataclass's
fields would get backward.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass

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
    Or,
    SelectItem,
    SelectStatement,
    Star,
    UnaryOp,
)
from historian.sql.lexer import Position
from historian.tables.blame import BLAME_SCHEMA

__all__ = [
    "BindError",
    "BoundColumnRef",
    "BoundSelectItem",
    "BoundSelectStatement",
    "TABLES",
    "bind",
]

#: The table catalog: FROM-clause name -> `Schema`. Phase 1 has exactly
#: one table. `BLAME_SCHEMA` is imported from `historian.tables.blame`
#: rather than redefined here, per that module's own docstring and the
#: #9/#11 grooming coordination comment. Note: importing it transitively
#: imports `tables/blame.py`, which imports `subprocess` at module
#: level - the module is merely imported, never invoked, so no git
#: repository or subprocess call is needed to exercise this module, but
#: this file's own import graph is not literally subprocess-free. That
#: trade-off was made by the grooming decision this catalog implements,
#: not revisited here.
TABLES: dict[str, Schema] = {"blame": BLAME_SCHEMA}

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
# new node type: every other `Expr` in a bound tree is one of
# `sql/ast.py`'s own types, reused unchanged as a type and rebuilt
# (via `dataclasses.replace`) only where a descendant changed.


@dataclass(frozen=True)
class BoundColumnRef(Expr):
    """A resolved column reference: everywhere a `ColumnRef` used to be.

    `offset` is the column's zero-based position in the FROM table's
    schema, computed once via `Schema.index_of` - the mechanism spec
    §3 describes for keeping row access by offset rather than by name.
    `name` is the column's declared schema spelling (used for an
    unaliased select-list item's output name; see `BoundSelectItem`).
    `position` is inherited from the original `ColumnRef` (or, for a
    `Star`-expansion item, from the `Star` itself), so an error found
    later can still point at source text.
    """

    offset: int
    name: str
    position: Position


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
class BoundSelectStatement:
    """A `SelectStatement` with every table and column reference
    resolved. `from_table` is the catalog's own key for the FROM
    table (its declared spelling), not necessarily the casing the
    query used."""

    select_list: tuple[BoundSelectItem, ...]
    from_table: str
    where: Expr | None
    position: Position


# --- ASCII-only folding --------------------------------------------------


def _ascii_fold(text: str) -> str:
    """Fold only the ASCII letters `A`-`Z` to `a`-`z`; leave every other
    character - including every character outside ASCII - untouched.

    This is SQLite's own identifier-matching rule, not Python's
    Unicode-aware `str.lower()`. See the module docstring for the
    `straße`/`STRASSE`/`STRAßE` evidence this must agree with.
    """
    return "".join(chr(ord(ch) + 32) if "A" <= ch <= "Z" else ch for ch in text)


def _same_name(a: str, b: str) -> bool:
    """ASCII case-insensitive identifier equality."""
    return _ascii_fold(a) == _ascii_fold(b)


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


# --- FROM-table resolution -------------------------------------------------


def _resolve_table(stmt: SelectStatement, catalog: dict[str, Schema]) -> _Context:
    """Resolve `stmt.from_table` against `catalog`, case-insensitively
    and ASCII-only. Checked first, before any select-list or WHERE
    name, per the module docstring's resolution-order evidence.

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
        return first
    if second is not None:
        return second

    raise BindError(f"no such column: {ref.name}", ref.position, ctx.schema.names)


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

    Order matters: an unrecognised name is rejected before
    `ctx.reject_aggregates` is even consulted, so `WHERE foo(x) > 1`
    (an unknown function, not a real aggregate) reports "no such
    function", never "aggregate functions are not allowed in WHERE" -
    the latter message would be actively misleading about what is
    actually wrong.
    """
    name = _ascii_fold(call.name)
    if name not in _AGGREGATE_NAMES:
        raise BindError(
            f"no such function: {call.name}", call.position, tuple(sorted(_AGGREGATE_NAMES))
        )
    if ctx.reject_aggregates:
        raise BindError(
            f"misuse of aggregate function {call.name}(): aggregate calls are not allowed in WHERE",
            call.position,
            (),
        )
    if name == "count":
        # count() and count(*) are both zero-column forms (`*` is one
        # AST node, not zero); count(<expr>) is the one-argument form.
        # Never more than one - `count(path, line_no)` is exactly
        # sqlite3's own arity error, differently worded (§3's Errors
        # section does not require matching text).
        if len(call.args) > 1:
            raise BindError(
                f"wrong number of arguments to function {call.name}()", call.position, ()
            )
        return
    # sum/avg/min/max: exactly one argument, and never `*` - `sum(*)`
    # is not `sum(<every column>)`; SQLite itself rejects it, and this
    # grammar has no meaning to give it either.
    if len(call.args) != 1:
        raise BindError(f"wrong number of arguments to function {call.name}()", call.position, ())
    if isinstance(call.args[0], Star):
        raise BindError(
            f"{call.name}(*) is not valid: {call.name} takes a single expression, not *",
            call.position,
            (),
        )


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
    builds for `WHERE`. `ctx` carries the setting through every
    recursive call below unchanged, so the fallback applies to a
    `ColumnRef` at any depth in the tree, not only at the top.
    """
    if isinstance(expr, Literal):
        return expr
    if isinstance(expr, ColumnRef):
        if ctx.alias_fallback:
            return _resolve_name(expr, ctx)
        return _bind_column_ref(expr, ctx)
    if isinstance(expr, Star):
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
            expr.position,
            (),
        )
    if isinstance(expr, FunctionCall):
        # Issue #60: name/arity/WHERE-rejection, before anything else -
        # see _validate_function_call and the module docstring's
        # "Aggregate calls" section. Every FunctionCall past this point
        # is a real, correctly-arity aggregate call.
        _validate_function_call(expr, ctx)
        if len(expr.args) == 1 and isinstance(expr.args[0], Star) and expr.args[0].table is None:
            # count(*): passed through unexpanded. `*` here means "no
            # columns", not "all columns" - see the module docstring.
            # A *qualified* sole argument (count(blame.*)) does not
            # take this path and falls through to the general Star
            # rejection above.
            return expr
        return dataclasses.replace(expr, args=tuple(_bind_expr(arg, ctx) for arg in expr.args))
    if isinstance(expr, UnaryOp):
        return dataclasses.replace(expr, operand=_bind_expr(expr.operand, ctx))
    if isinstance(expr, Not):
        return dataclasses.replace(expr, operand=_bind_expr(expr.operand, ctx))
    if isinstance(expr, BinaryOp):
        return dataclasses.replace(expr, left=_bind_expr(expr.left, ctx), right=_bind_expr(expr.right, ctx))
    if isinstance(expr, And):
        return dataclasses.replace(expr, left=_bind_expr(expr.left, ctx), right=_bind_expr(expr.right, ctx))
    if isinstance(expr, Or):
        return dataclasses.replace(expr, left=_bind_expr(expr.left, ctx), right=_bind_expr(expr.right, ctx))
    if isinstance(expr, Is):
        return dataclasses.replace(expr, left=_bind_expr(expr.left, ctx), right=_bind_expr(expr.right, ctx))
    if isinstance(expr, Like):
        return dataclasses.replace(expr, left=_bind_expr(expr.left, ctx), pattern=_bind_expr(expr.pattern, ctx))
    if isinstance(expr, In):
        return dataclasses.replace(
            expr, left=_bind_expr(expr.left, ctx), values=tuple(_bind_expr(v, ctx) for v in expr.values)
        )
    if isinstance(expr, Between):
        return dataclasses.replace(
            expr,
            operand=_bind_expr(expr.operand, ctx),
            low=_bind_expr(expr.low, ctx),
            high=_bind_expr(expr.high, ctx),
        )
    raise AssertionError(f"sql/binder.py: unhandled expression node type {type(expr).__name__}")


# --- The bare-column-mixed-with-aggregate narrowing (issue #60) ------------
#
# `_docs/decisions.md`, 2026-09-19: `SELECT path, count(*) FROM blame`
# (no GROUP BY) is a BindError in historian, where sqlite3 silently
# picks a value from an arbitrary row. Implemented as one walk per
# select-list item, over the already-bound tree (so every remaining
# ColumnRef is a BoundColumnRef and every remaining FunctionCall is a
# real, validated aggregate call - _validate_function_call above
# guarantees the latter): does this item's expression contain an
# aggregate call anywhere, and what is the first bare column reference
# in it that sits outside every aggregate call's own arguments (a
# FunctionCall subtree is never walked into for this purpose - its
# arguments are exactly the columns this rule exists to leave alone,
# `count(path)` is fine, only a *bare* `path` is not). `bind()` below
# only raises when the *query* has an aggregate call somewhere in its
# select list; an ordinary, aggregate-free query is entirely unaffected
# regardless of what this function reports for it.
#
# One isinstance branch per sql/ast.py node type, mirroring
# `_bind_expr`'s own structure exactly (AGENTS.md: no dynamic dispatch)
# rather than a generic "walk children" abstraction - kept as separate,
# boring branches even where two node types share an identical body,
# matching this module's existing convention (`_bind_expr`'s own
# BinaryOp/And/Or/Is branches are equally identical and equally
# separate). `left_bad or right_bad` is safe because a `BoundColumnRef`
# is a dataclass instance, always truthy - this is "the first non-None
# of the two", not a boolean test of either column's contents.


def _split_for_aggregate_check(expr: Expr) -> tuple[bool, BoundColumnRef | None]:
    """`(does expr contain an aggregate call anywhere, the first bare
    BoundColumnRef found outside every aggregate call's own arguments -
    or None)`."""
    if isinstance(expr, FunctionCall):
        return True, None
    if isinstance(expr, BoundColumnRef):
        return False, expr
    if isinstance(expr, Literal):
        return False, None
    if isinstance(expr, Star):
        # Unreachable in practice: the only Star that survives binding
        # is count(*)'s own sole argument, already consumed by the
        # FunctionCall branch above before this function ever sees it.
        # Kept for the same defensive reason evaluate() keeps its own
        # Star guard.
        return False, None
    if isinstance(expr, UnaryOp):
        return _split_for_aggregate_check(expr.operand)
    if isinstance(expr, Not):
        return _split_for_aggregate_check(expr.operand)
    if isinstance(expr, BinaryOp):
        left_has, left_bad = _split_for_aggregate_check(expr.left)
        right_has, right_bad = _split_for_aggregate_check(expr.right)
        return left_has or right_has, left_bad or right_bad
    if isinstance(expr, And):
        left_has, left_bad = _split_for_aggregate_check(expr.left)
        right_has, right_bad = _split_for_aggregate_check(expr.right)
        return left_has or right_has, left_bad or right_bad
    if isinstance(expr, Or):
        left_has, left_bad = _split_for_aggregate_check(expr.left)
        right_has, right_bad = _split_for_aggregate_check(expr.right)
        return left_has or right_has, left_bad or right_bad
    if isinstance(expr, Is):
        left_has, left_bad = _split_for_aggregate_check(expr.left)
        right_has, right_bad = _split_for_aggregate_check(expr.right)
        return left_has or right_has, left_bad or right_bad
    if isinstance(expr, Like):
        left_has, left_bad = _split_for_aggregate_check(expr.left)
        pattern_has, pattern_bad = _split_for_aggregate_check(expr.pattern)
        return left_has or pattern_has, left_bad or pattern_bad
    if isinstance(expr, In):
        has, bad = _split_for_aggregate_check(expr.left)
        for value in expr.values:
            value_has, value_bad = _split_for_aggregate_check(value)
            has = has or value_has
            bad = bad or value_bad
        return has, bad
    if isinstance(expr, Between):
        op_has, op_bad = _split_for_aggregate_check(expr.operand)
        low_has, low_bad = _split_for_aggregate_check(expr.low)
        high_has, high_bad = _split_for_aggregate_check(expr.high)
        return op_has or low_has or high_has, op_bad or low_bad or high_bad
    raise AssertionError(f"sql/binder.py: unhandled expression node type {type(expr).__name__}")


def _check_bare_columns_against_aggregates(bound_items: list[BoundSelectItem]) -> None:
    """Raise `BindError` for the first bare, non-aggregated column
    found in any select-list item, but only when the select list has
    an aggregate call *somewhere* - an aggregate-free query is not
    this rule's business at all, per the module docstring's "Aggregate
    calls" section."""
    splits = [(item, *_split_for_aggregate_check(item.expr)) for item in bound_items]
    if not any(has_aggregate for _item, has_aggregate, _bad in splits):
        return
    for _item, _has_aggregate, bad_column in splits:
        if bad_column is not None:
            raise BindError(
                f"column {bad_column.name} must appear in an aggregate function since "
                "this query has no GROUP BY",
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


def bind(stmt: SelectStatement, catalog: dict[str, Schema] = TABLES) -> BoundSelectStatement:
    """Resolve every table and column reference in `stmt` against
    `catalog`, and expand `SELECT *` / `table.*`.

    Raises `BindError` - never returns `None`/`False` - on the first
    name that does not resolve, in the order documented in the module
    docstring: the FROM table, then the select list left to right,
    then WHERE. `catalog` defaults to `TABLES`, the module's own
    table catalog, but is a parameter (not hardcoded) so tests can
    bind against a synthetic schema with no dependency on `blame`.
    """
    ctx = _resolve_table(stmt, catalog)
    bound_items: list[BoundSelectItem] = []
    for item in stmt.select_list:
        bound_items.extend(_bind_select_item(item, ctx))
    # Issue #60: the bare-column-mixed-with-aggregate narrowing, after
    # the whole select list is bound and before WHERE - the select list
    # resolves before WHERE per the module docstring's resolution-order
    # section, and this check is squarely part of resolving it.
    _check_bare_columns_against_aggregates(bound_items)
    # WHERE binds with the select-list alias fallback on (issue #32),
    # column-first (`alias_first=False`), and aggregate calls rejected
    # outright (issue #60) - a fresh `_Context` rather than mutating
    # `ctx`, since `_Context` is frozen and select-list items must keep
    # binding against the plain `ctx` above, with no fallback, so
    # aliases stay invisible to each other (finding 3).
    where_ctx = dataclasses.replace(
        ctx,
        select_items=tuple(bound_items),
        alias_fallback=True,
        alias_first=False,
        reject_aggregates=True,
    )
    bound_where = _bind_expr(stmt.where, where_ctx) if stmt.where is not None else None
    return BoundSelectStatement(
        select_list=tuple(bound_items),
        from_table=ctx.table_name,
        where=bound_where,
        position=stmt.position,
    )
