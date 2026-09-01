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
split. **Function name/arity validation** - no registry of built-ins
exists yet; `SELECT nonexistent_fn(path) FROM blame` binds
successfully. **`WHERE` resolving a select-list alias** - real,
verified SQLite behaviour (`select path as p, line_no from blame
where p = 'a.py'` succeeds via the alias) that needs expression
substitution, not plain offset resolution, because `WHERE` runs before
`Project` computes any alias. Deferred to #32; until it lands, `WHERE
<alias>` conservatively raises "no such column" - a query SQLite
accepts gets wrongly rejected, which is the safe direction. **The
rendered `error: ...` / caret / "blame has: ..." box from spec §5** -
milestone item 18; `BindError` here carries structured fields
(message, position, available names), not text to print.

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
# table's own schema and declared name (v1 has exactly one), and the
# catalog's full set of table names for a "no such table" error's
# `available` data. Not global state and not a class with behaviour -
# just the three things every helper below would otherwise need as
# separate parameters.


@dataclass(frozen=True)
class _Context:
    schema: Schema
    table_name: str
    catalog_names: tuple[str, ...]


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


def _bind_column_ref(ref: ColumnRef, ctx: _Context) -> BoundColumnRef:
    """Resolve a bare or table-qualified `ColumnRef`.

    A qualifier that does not match the FROM table - whether a real,
    unrelated table or an unknown name - raises "no such column:
    <qualifier>.<name>", the whole dotted reference verbatim, never
    "no such table". This is the opposite of `_bind_star`'s qualifier
    check below; both are separately confirmed against `sqlite3` and
    must not be unified.
    """
    if ref.table is not None and not _same_name(ref.table, ctx.table_name):
        raise BindError(f"no such column: {ref.table}.{ref.name}", ref.position, ctx.schema.names)
    for offset, column in enumerate(ctx.schema.columns):
        if _same_name(column.name, ref.name):
            return BoundColumnRef(offset=offset, name=column.name, position=ref.position)
    display = f"{ref.table}.{ref.name}" if ref.table is not None else ref.name
    raise BindError(f"no such column: {display}", ref.position, ctx.schema.names)


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


# --- General expression binding --------------------------------------------
#
# One case per `sql/ast.py` node type. Every type other than
# `ColumnRef` and `Star` is reused unchanged and rebuilt via
# `dataclasses.replace` with its children bound - no new type, no
# dynamic dispatch, just an explicit `isinstance` chain matching the
# style already established by the parser's own precedence methods.


def _bind_expr(expr: Expr, ctx: _Context) -> Expr:
    if isinstance(expr, Literal):
        return expr
    if isinstance(expr, ColumnRef):
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
        if len(expr.args) == 1 and isinstance(expr.args[0], Star) and expr.args[0].table is None:
            # count(*): passed through unexpanded and unvalidated. `*`
            # here means "no columns", not "all columns" - see the
            # module docstring. A *qualified* sole argument
            # (count(blame.*)) does not take this path and falls
            # through to the general Star rejection above.
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
    bound_where = _bind_expr(stmt.where, ctx) if stmt.where is not None else None
    return BoundSelectStatement(
        select_list=tuple(bound_items),
        from_table=ctx.table_name,
        where=bound_where,
        position=stmt.position,
    )
