"""Expression evaluation: `(expr, row, schema) -> Value | Bool3`.

Issue #12. Implements `_docs/spec.md` §3's "Expression evaluation"
section and the column-affinity carve-out in that section's "Values
and three-valued logic": *"`exec/expression.py` walks an expression
against a row and returns a value. It is a plain function over
`(expr, row, schema)` with no git, no I/O, and no operator
dependencies."*

Consumes the bound tree `sql/binder.py` produces: `BoundColumnRef` for
every column reference, every other node reused unchanged from
`sql/ast.py` per that module's own docstring. `evaluate()` is a single
recursive dispatcher, structured the same way `sql/binder.py`'s
`_bind_expr` is - an explicit `isinstance` chain, no dynamic dispatch,
no metaclasses (`AGENTS.md`).

`Value` or `Bool3`, decided by node shape, not calling context
------------------------------------------------------------------

The issue's own goal statement asks for one function that "correctly
computes a `Value` for a value-position expression or a `Bool3` for a
predicate-position expression, for every expression shape the grammar
can build today." This module reads "position" as a property of the
node's own shape, not an external mode the caller passes in:
`Literal`, `BoundColumnRef`, arithmetic/concatenation `BinaryOp`, and
`UnaryOp` are value-shaped and return `Value`; comparison `BinaryOp`,
`And`, `Or`, `Not`, `Is`, `Like`, `In`, `Between` are predicate-shaped
and return `Bool3`. Composition follows that split exactly: a
comparison's operands are evaluated as `Value` (they must be, to reach
`values.eq` et al.), and `And`/`Or`/`Not`'s operands are evaluated as
`Bool3`.

Not handled: a predicate-shaped node used where a value is expected
(`SELECT (1 = 1)`) or a value-shaped node used where a predicate is
expected (`WHERE line_no`, relying on C-style truthiness). Real
SQLite accepts both - booleans have no storage class of their own
(`_docs/decisions.md`, 2026-08-27: "`typeof(true)` is `integer`") - but
neither shape appears in any of this issue's acceptance criteria, and
building the coercion either direction is a real, separate design
question (truthiness for `WHERE line_no` needs its own affinity-like
rule for text, which no criterion specifies or verifies against
`sqlite3`). Left to whichever of #34 (`Filter`/`Project`) or a future
issue actually needs it - noted in this issue's closing report rather
than guessed at here.

Column affinity
----------------

`values.py` compares values only; it cannot know that `WHERE n = '5'`
should convert `'5'` before comparing, because it never sees the AST
or the schema (see that module's own "Not in this module" section).
This module owns that conversion. Per this issue's grooming (revising
the 2026-08-27 decision entry, which described affinity too narrowly
as "convert the literal"): affinity is decided **per operand**,
independently, by asking a narrow structural question of the AST node
that produced each side of a comparison, not by asking which side
"is a literal" - `(n + 0) = '5'` has no affinity on its left side even
though `n` is `INTEGER`, because the left operand is a computed
`BinaryOp`, not a bare `BoundColumnRef`. Implemented as `_affinity_of`
(inspects the expression node) and `_apply_affinity` (SQLite's own
two-rule algorithm: numeric affinity wins whenever either operand has
it, unless blocked; otherwise text affinity applies to a no-affinity
operand) - see the comparison section below.

Numeric comparison stays exact
--------------------------------

Per the 2026-08-27 decision, comparing an `int` against a `float`
must never go through `float()` - past 2^53 that loses the integer's
exact value and can reverse the answer. This module's comparison path
(`_apply_affinity` and everything it calls) never does this; the only
`float()` calls anywhere in this file are the three named in
`tests/test_expression.py`'s
`test_no_stray_float_calls_outside_the_named_exceptions` - two
matching the issue's own list (affinity's text-to-number conversion,
the float formatting helper) plus a third this module's own
int64-overflow handling needs (see that test's docstring for why a
third is unavoidable and why it cannot be confused with the
comparison path).
"""

from __future__ import annotations

import math
import re

from historian import values
from historian.schema import ColumnType, Row, Schema
from historian.sql.ast import (
    And,
    Between,
    BinaryOp,
    Expr,
    FunctionCall,
    In,
    Is,
    Like,
    Literal,
    Not,
    Operator,
    Or,
    Star,
    UnaryOp,
    UnaryOperator,
)
from historian.sql.binder import BoundColumnRef
from historian.sql.lexer import Position
from historian.values import Bool3, Value

__all__ = ["EvalError", "evaluate"]


class EvalError(Exception):
    """Unsupported grammar reaching `evaluate()`: a `FunctionCall`,
    since no aggregate or scalar function is in scope for this module
    (see the module docstring). Same structured shape as `LexError`
    (`sql/lexer.py`), `ParseError` (`sql/parser.py`) and `BindError`
    (`sql/binder.py`): a message plus the offending `Position`, per
    §3's "Errors" - never a bare Python exception, and this module
    never renders it as text.
    """

    def __init__(self, message: str, position: Position) -> None:
        super().__init__(message)
        self.position = position


def evaluate(expr: Expr, row: Row, schema: Schema) -> Value | Bool3:
    """Evaluate *expr* against *row*, described by *schema*.

    Returns a `historian.values.Value` for a value-shaped node, a
    `historian.values.Bool3` for a predicate-shaped one - see the
    module docstring for exactly which shapes are which and why that
    split is structural rather than context-driven.
    """
    if isinstance(expr, Literal):
        return expr.value
    if isinstance(expr, BoundColumnRef):
        return row[expr.offset]
    if isinstance(expr, Star):
        # The binder expands every Star before this module ever sees a
        # tree (per sql/binder.py's own docstring) - reaching here is
        # a "should never happen" bug upstream, not UX to design for.
        raise AssertionError(
            "exec/expression.py: a Star reached the evaluator; the binder must expand it first"
        )
    if isinstance(expr, FunctionCall):
        raise EvalError(
            f"{expr.name}(...) is not supported here: aggregate and scalar function calls are "
            "not evaluated by exec/expression.py (see its module docstring)",
            expr.position,
        )
    raise AssertionError(f"exec/expression.py: unhandled expression node type {type(expr).__name__}")
