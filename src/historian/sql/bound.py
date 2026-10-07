"""The binder's output types: `BindError` and the bound statement.

Split out of `sql/binder.py` (issue #151) so the modules that bind
expressions, bind clauses and run the grouped checks can share them
without importing each other. First in the layering: this module
imports only `sql/ast.py`, `sql/lexer.py` and `sql/walk.py`.

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
columns"; `_call_error` checks the function name.

Because v1 has exactly one FROM table and no `JOIN`, a `BoundColumnRef`
does not track which table it came from - the offset alone is
unambiguous. Building multi-table bookkeeping now, before there is a
`JOIN` to need it, is exactly the kind of speculative generality
`AGENTS.md` asks to redesign rather than pre-build.

"""

from __future__ import annotations

from dataclasses import dataclass

from historian.sql.ast import Expr, OrderDirection
from historian.sql.lexer import Position
from historian.sql.walk import BoundColumnRef

__all__ = [
    "BindError",
    "BoundColumnRef",
    "BoundOrderByItem",
    "BoundSelectItem",
    "BoundSelectStatement",
]


# --- Errors ------------------------------------------------------------
#
# Same shape as `LexError` (`sql/lexer.py`) and `ParseError`
# (`sql/parser.py`): message via `Exception.__init__`, plus a
# `.position` attribute, so a caller can report it per spec §5 without
# a traceback ever reaching the user. `.available` is this module's own
# addition - the names that *would* have resolved, for the eventual
# "blame has: ..." rendering (#41), never used by this module itself.


class BindError(Exception):
    """A table or column reference in the statement does not resolve
    against the catalog, or a `Star` appears somewhere it cannot mean
    anything (see `sql/binder.py`'s docstring).

    Structured, not rendered: `message` is plain text naming the
    problem, `position` points at the offending token, and `available`
    lists what the caller could have referred to instead - table names
    for an unresolved table, column names in schema order for an
    unresolved column. Rendering this per spec §5 (the caret, "blame
    has: ...") is `cli.py`'s job (#41), not this module.
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
    expression's header is not pinned down; `Project` falls back to a
    positional placeholder (`exec/operators.py`).
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
    whether to insert a `Distinct` operator. See `sql/grouped.py`'s
    "DISTINCT" comment, for the one thing `distinct` *does*
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
