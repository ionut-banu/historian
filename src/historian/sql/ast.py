"""The SQL abstract syntax tree: statement nodes and expression nodes.

The second stage of the pipeline in `_docs/spec.md` §3 (`sql/parser.py`
is the third - `parser.py` builds these, `ast.py` only defines their
shape). Two separate hierarchies, per §3's "AST" - a `SelectStatement`
is not an expression, and code that walks expressions never has to
consider it.

Every node is a frozen dataclass (`dataclasses.FrozenInstanceError` on
mutation) and every node carries a `position` - the `Position` of the
first token that forms it, so an error found later (the binder, the
evaluator) can point at the offending text without re-deriving where it
came from. For a node built by combining smaller ones (`BinaryOp`,
`And`, ...) that is the position of its leftmost operand, i.e. of the
whole (sub)expression's first token - not the operator's own position.

Schema-blind by design
-----------------------

Nothing here knows what tables or columns exist. `ColumnRef` carries an
unresolved `table`/`name` pair and `SelectStatement.from_table` is a
bare string; neither is checked against a catalog. That is
`sql/binder.py`'s job (issue #9), which has the `Schema` this module
deliberately never imports.

What v1's grammar does not need yet
------------------------------------

`DISTINCT`, `GROUP BY`, `HAVING`, `ORDER BY`, `LIMIT`, `OFFSET` and any
`JOIN` have no node here - see issue #8's grooming. Adding a field to a
frozen dataclass later is additive, not a rewrite, so there is nothing
to pre-declare. `CASE` is deferred the same way, for a different
reason: it is an independent keyword-delimited primary expression form
that does not interact with precedence, so building it earlier than it
is needed would buy nothing.

`IS NULL` / `IS NOT NULL` are not their own node types
--------------------------------------------------------

`x IS NULL` is `x IS <the NULL literal>` - SQLite's own grammar treats
`NULL` as an ordinary expression in `IS`'s right-hand operand position,
not a distinct production. `Is` represents `IS`, `IS NOT`, `IS NULL`
and `IS NOT NULL` uniformly: the parser builds `IS NULL` as
`Is(left, Literal(None, ...), negated=False, ...)`. This costs nothing
extra in the grammar and means the future evaluator calls
`values.is_`/`values.is_not` directly with no special-casing for the
NULL spelling.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto

from historian.sql.lexer import Position
from historian.values import Value

__all__ = [
    "And",
    "Between",
    "BinaryOp",
    "ColumnRef",
    "Expr",
    "FunctionCall",
    "In",
    "Is",
    "Like",
    "Literal",
    "Not",
    "Operator",
    "Or",
    "SelectItem",
    "SelectStatement",
    "Star",
    "Stmt",
    "UnaryOp",
    "UnaryOperator",
]


# --- The two hierarchies --------------------------------------------------
#
# Plain marker base classes, not dataclasses themselves - each concrete
# node declares its own fields (including its own `position`) rather than
# inheriting them, so a node's field order reads naturally at the call
# site (`Literal(value, position)`, not `Literal(position, value)`).
# Nothing here uses a metaclass or dynamic dispatch: `isinstance` against
# these two classes is the only thing they are for.


class Expr:
    """Base of the expression-node hierarchy."""


class Stmt:
    """Base of the statement-node hierarchy. Kept separate from `Expr`
    per `_docs/spec.md` §3: a `SelectStatement` is not an expression."""


# --- Operators -------------------------------------------------------------
#
# One `Operator`/`UnaryOperator` member per operator, the same shape as
# the lexer's "one `TokenType` per keyword" (`_docs/decisions.md`,
# 2026-08-27) applied to the AST: a typo in a match site is an
# `AttributeError` at import, not a silently-never-matching string.
# `UnaryOperator` is a separate enum from `Operator` rather than reusing
# `ADD`/`SUB` for the unary case - a `UnaryOp` built with `Operator.MUL`
# would be nonsense, and a distinct enum makes it a type error rather
# than a runtime surprise.


class Operator(Enum):
    """A binary operator: arithmetic, string concatenation, or
    comparison. One `BinaryOp` node type covers all of these - they
    share the same two-operand shape, and SQLite's own precedence table
    (issue #8's grooming) treats them as one family of binary operators
    at four different precedence tiers, not four different grammars."""

    ADD = auto()  # +
    SUB = auto()  # -
    MUL = auto()  # *
    DIV = auto()  # /
    CONCAT = auto()  # ||
    EQ = auto()  # =
    NE = auto()  # <> or !=
    LT = auto()  # <
    LE = auto()  # <=
    GT = auto()  # >
    GE = auto()  # >=


class UnaryOperator(Enum):
    """A prefix unary operator: `+x` or `-x`."""

    POS = auto()  # +x
    NEG = auto()  # -x


# --- Expression nodes --------------------------------------------------


@dataclass(frozen=True)
class Literal(Expr):
    """A constant value: a number, a string, or `NULL`.

    `value` is a `historian.values.Value` - already the Python type
    (`int`, `float`, `str`, or `None`) that the rest of the engine
    consumes, not the token's raw text. See `_read_int_literal` in
    `parser.py` for the `INTEGER` overflow-to-`float` rule
    (`_docs/decisions.md`, 2026-09-01).
    """

    value: Value
    position: Position


@dataclass(frozen=True)
class ColumnRef(Expr):
    """A column reference, bare (`path`) or table-qualified
    (`blame.path`). `table` is `None` for a bare reference - kept as a
    separate field rather than folded into `name`, so the binder can
    resolve a qualified reference against a specific table without
    re-splitting a string."""

    table: str | None
    name: str
    position: Position


@dataclass(frozen=True)
class Star(Expr):
    """`*` or `table.*` in a select list, or as a bare argument to a
    function call (`count(*)`). Deliberately not a `ColumnRef`: `*`
    names no single column, and giving it one would make every
    `ColumnRef` consumer special-case a fake name meaning "all of
    them" instead of checking a distinct type."""

    table: str | None
    position: Position


@dataclass(frozen=True)
class FunctionCall(Expr):
    """`name(args, ...)` - `count(*)`, `sum(x)`, `foo(a, b)`.

    Unevaluated: nothing here knows whether `name` is a real aggregate,
    a real scalar function, or a typo. That is a binder/executor
    concern once a registry of built-ins exists (`_docs/spec.md` §3),
    not a parser one - `foo(x)` is syntactically identical whether
    `foo` is `count` or an invented name.
    """

    name: str
    args: tuple[Expr, ...]
    position: Position


@dataclass(frozen=True)
class UnaryOp(Expr):
    """A prefix unary `+` or `-` applied to `operand`.

    Never folded into a negative `Literal`, even when `operand` is
    itself a numeric literal (`-5` is `UnaryOp(NEG, Literal(5, ...))`,
    not `Literal(-5, ...)`). Constant folding is a v2 optimisation, and
    building it now against `INTEGER` overflow would tempt an int64-min
    special case the grammar has nothing to observe by yet - see
    `_docs/decisions.md`, 2026-09-01.
    """

    op: UnaryOperator
    operand: Expr
    position: Position


@dataclass(frozen=True)
class BinaryOp(Expr):
    """A binary arithmetic, concatenation, or comparison expression:
    `left <op> right`."""

    op: Operator
    left: Expr
    right: Expr
    position: Position


@dataclass(frozen=True)
class And(Expr):
    """`left AND right`."""

    left: Expr
    right: Expr
    position: Position


@dataclass(frozen=True)
class Or(Expr):
    """`left OR right`."""

    left: Expr
    right: Expr
    position: Position


@dataclass(frozen=True)
class Not(Expr):
    """`NOT operand` - logical negation, not `NOT LIKE`/`NOT IN`/
    `NOT BETWEEN`, which are the `negated` flag on `Like`/`In`/
    `Between` instead (see those classes)."""

    operand: Expr
    position: Position


@dataclass(frozen=True)
class Is(Expr):
    """`left IS right` / `left IS NOT right`, including the `IS NULL`
    / `IS NOT NULL` spelling - see the module docstring for why those
    are not separate node types."""

    left: Expr
    right: Expr
    negated: bool
    position: Position


@dataclass(frozen=True)
class Like(Expr):
    """`left LIKE pattern` / `left NOT LIKE pattern`."""

    left: Expr
    pattern: Expr
    negated: bool
    position: Position


@dataclass(frozen=True)
class In(Expr):
    """`left IN (values, ...)` / `left NOT IN (values, ...)`.

    `values` may be empty (`IN ()` is valid SQL, always false -
    confirmed against `sqlite3`) and holds full expressions, not just
    literals: the grammar parses whatever a comma-separated expression
    list yields here, which is also what lets #24 (Part B) detect a
    subquery by peeking at the first token before parsing it as an
    expression - restricting this to literals would take that check
    away.
    """

    left: Expr
    values: tuple[Expr, ...]
    negated: bool
    position: Position


@dataclass(frozen=True)
class Between(Expr):
    """`operand BETWEEN low AND high` /
    `operand NOT BETWEEN low AND high`.

    The `AND` here is the operator's own syntax, not the general
    `And` node - it is consumed directly by the parser and never
    re-enters expression precedence, which is what stops it from
    swallowing a trailing `AND <predicate>` that follows the whole
    `BETWEEN` (issue #8's grooming: `x BETWEEN 1 AND 10 AND y = 1`
    must parse as `(x BETWEEN 1 AND 10) AND (y = 1)`).
    """

    operand: Expr
    low: Expr
    high: Expr
    negated: bool
    position: Position


# --- Statement nodes -------------------------------------------------------


@dataclass(frozen=True)
class SelectItem:
    """One entry in a `SELECT` list: an expression and its optional
    `AS alias`. Not part of the `Expr` hierarchy itself - it pairs an
    expression with something that is not an expression (the alias
    name), the same way a dict entry is not itself a key or a value.
    """

    expr: Expr
    alias: str | None
    position: Position


@dataclass(frozen=True)
class SelectStatement(Stmt):
    """`SELECT <select_list> FROM <from_table> [WHERE <where>]`.

    `from_table` is a bare, unresolved table name - not a node of its
    own - and `where` is `None` when the clause is absent. Neither
    `from_table` nor any `ColumnRef` inside this tree is checked
    against a catalog; see the module docstring.
    """

    select_list: tuple[SelectItem, ...]
    from_table: str
    where: Expr | None
    position: Position
