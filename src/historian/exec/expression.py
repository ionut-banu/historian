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

#: SQLite's `int64` bounds. This module's own constants - not imported
#: from `sql/parser.py`'s private `_INT64_MAX`, which is off-limits for
#: this issue and isn't exported anyway. Unlike the parser (which only
#: ever needs the positive bound, to detect an overflowing literal),
#: arithmetic here needs both: subtraction and negation can overflow
#: toward either end.
_INT64_MIN = -9223372036854775808
_INT64_MAX = 9223372036854775807

#: ASCII whitespace this module's own numeric-text scanner skips before
#: a number, in both the arithmetic (leading-prefix) and affinity
#: (whole-string) conversions below. Deliberately its own small
#: constant rather than importing `sql/lexer.py`'s `_WHITESPACE`: that
#: set encodes which bytes are whitespace *between SQL tokens*
#: (`\v` is pointedly excluded there, per `_docs/decisions.md`
#: 2026-09-01, because SQLite's tokenizer rejects it) - a completely
#: different question from what SQLite's C `atof`-equivalent skips
#: before a numeric string. Only ordinary space is ever exercised by
#: this issue's criteria; the rest is a conservative, unverified
#: default rather than a claim about SQLite's exact behaviour there.
_NUMERIC_WHITESPACE = " \t\n\r\f"


def _is_ascii_digit(ch: str) -> bool:
    """True for `'0'`-`'9'` only. Never `str.isdigit()`, which is
    `True` for non-ASCII digit-shaped characters - the exact bug fixed
    in `sql/lexer.py` per `_docs/decisions.md` 2026-09-01, which this
    module's own numeric-text scanner must not reintroduce."""
    return "0" <= ch <= "9"


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
    if isinstance(expr, BinaryOp):
        return _eval_binary(expr, row, schema)
    raise AssertionError(f"exec/expression.py: unhandled expression node type {type(expr).__name__}")


# --- BinaryOp: arithmetic (comparison and concat join this later) ------

_ARITHMETIC_OPS = frozenset({Operator.ADD, Operator.SUB, Operator.MUL, Operator.DIV})


def _eval_binary(expr: BinaryOp, row: Row, schema: Schema) -> Value | Bool3:
    if expr.op in _ARITHMETIC_OPS:
        left = evaluate(expr.left, row, schema)
        right = evaluate(expr.right, row, schema)
        return _arithmetic(expr.op, left, right)
    raise AssertionError(f"exec/expression.py: BinaryOp operator not yet handled: {expr.op}")


# --- Numeric text scanning: shared by arithmetic and affinity -----------


def _scan_number(text: str, start: int) -> tuple[int | float, int] | None:
    """The longest well-formed number beginning at `text[start:]`,
    after skipping leading whitespace - the primitive both arithmetic's
    leading-prefix coercion and (via a "consumed the whole string"
    check layered on top, in the affinity section below) column
    affinity's whole-string coercion are built from. Returns
    `(value, end)`, `end` being the index in `text` just past the
    number, or `None` if no digit appears anywhere in the mantissa.

    Deliberately not `float(x)`/`int(x)` on arbitrary input: those
    accept forms SQLite's text-to-number conversion does not recognise
    as numeric at all - `float("0x10")` raises, `float("inf")`
    succeeds - both wrong here (`'0x10'+1` is `1`, hex not recognised;
    `'inf'+1` is `1`, the word not recognised; confirmed against
    `sqlite3`). So this scans SQLite's own narrower grammar directly:
    optional sign, digits, optional `.` and more digits (at least one
    digit somewhere in the mantissa), optional exponent (`e`/`E`,
    optional sign, digits - backed off entirely, not just the invalid
    tail, if not followed by at least one digit: `'5e'`/`'5e+'` both
    fall back to the mantissa alone).
    """
    n = len(text)
    i = start
    while i < n and text[i] in _NUMERIC_WHITESPACE:
        i += 1
    mantissa_start = i
    if i < n and text[i] in "+-":
        i += 1
    has_digits = False
    while i < n and _is_ascii_digit(text[i]):
        i += 1
        has_digits = True
    is_float = False
    if i < n and text[i] == ".":
        i += 1
        is_float = True
        while i < n and _is_ascii_digit(text[i]):
            i += 1
            has_digits = True
    if not has_digits:
        return None
    mantissa_end = i
    exponent_end = mantissa_end
    if i < n and text[i] in "eE":
        j = i + 1
        if j < n and text[j] in "+-":
            j += 1
        exponent_digits_start = j
        while j < n and _is_ascii_digit(text[j]):
            j += 1
        if j > exponent_digits_start:
            exponent_end = j
            is_float = True
    end = exponent_end
    number_text = text[mantissa_start:end]
    # Constructing a new Value from source text, per this module's own
    # float()-call test - not a lossy comparison cast, the thing the
    # 2026-08-27 decision actually forbids. See that test's docstring.
    value: int | float = float(number_text) if is_float else int(number_text)
    return value, end


def _coerce_arithmetic_text(text: str) -> int | float:
    """SQLite's leading-prefix text-to-number coercion for arithmetic
    (distinct from affinity's whole-string rule below, per this
    issue's own grooming): the longest valid numeric prefix, or `0`
    (an `int`) if the text contains no digit at all - confirmed
    against `sqlite3`: `'abc'+1` is `1`, `'   '+1` is `1`."""
    scanned = _scan_number(text, 0)
    return scanned[0] if scanned is not None else 0


def _arithmetic_operand(value: Value) -> int | float:
    """A `Value` as an arithmetic operand: a number passes through
    unchanged, text goes through the leading-prefix coercion above.
    Never called with `None` - the caller checks for NULL first, since
    NULL's propagation through arithmetic is "the whole expression is
    NULL", not "NULL contributes 0"."""
    if isinstance(value, str):
        return _coerce_arithmetic_text(value)
    return value


def _int64_bounded(exact: int) -> int | float:
    """`exact`, computed with Python's native unbounded `int`
    arithmetic so nothing silently wraps, narrowed to SQLite's `int64`
    range or promoted to `float` if it does not fit
    (`_docs/decisions.md`, 2026-09-01, "int64 arithmetic overflows to
    REAL": `9223372036854775807 + 1` is `9.22337203685478e+18`,
    `REAL`, not a Python arbitrary-precision `int`).

    The `float()` call here is arithmetic *result production*, a
    different path from comparison and never confused with it - see
    the module docstring and this module's own
    `test_no_stray_float_calls_outside_the_named_exceptions`, which
    names this function as the third legitimate exception beyond the
    issue's own two.
    """
    if _INT64_MIN <= exact <= _INT64_MAX:
        return exact
    return float(exact)


def _squash_nan(result: float) -> float | None:
    """NaN can only ever be a computed *result* here, never a stored
    value (`values.py` rejects one outright) - catches `Inf-Inf`,
    `Inf*0`, `Inf/Inf`, not only `0.0/0.0`
    (`_docs/decisions.md`, 2026-08-31)."""
    if math.isnan(result):
        return None
    return result


def _truncating_int_div(left: int, right: int) -> int:
    """`left / right`, truncated toward zero - C/SQLite semantics, not
    Python's `//`, which floors toward negative infinity and disagrees
    for a negative operand (confirmed against `sqlite3`: `-5/2` is
    `-2`, `5/-2` is `-2`; Python's `-5 // 2` and `5 // -2` are both
    `-3`). Stays in `int` throughout: `math.trunc(left / right)` would
    give the right answer for small inputs by routing through a
    `float`, which both loses precision past 2^53 and is exactly the
    "obvious fix" the 2026-08-27 decision forbids on the arithmetic
    path."""
    quotient = abs(left) // abs(right)
    if (left < 0) != (right < 0):
        quotient = -quotient
    return quotient


def _arithmetic(op: Operator, left: Value, right: Value) -> Value:
    """`+ - * /`. NULL propagates through every operator (`NULL + 1`
    is `NULL`). Division by zero - integer or float - is `None`
    directly, checked before any division is attempted: Python's `/`
    raises `ZeroDivisionError` for a zero denominator in both the
    `int/int` and `float/float` cases - it does not produce
    `inf`/`nan` the way SQLite's underlying arithmetic does - so this
    cannot be discovered by computing first and inspecting the result
    afterward the way the `Inf - Inf` family can (`_docs/spec.md`
    §3's own NaN note; `_docs/decisions.md`, 2026-08-31).
    """
    if left is None or right is None:
        return None
    left_num = _arithmetic_operand(left)
    right_num = _arithmetic_operand(right)
    if op is Operator.DIV:
        if right_num == 0:
            return None
        if isinstance(left_num, int) and isinstance(right_num, int):
            return _int64_bounded(_truncating_int_div(left_num, right_num))
        return _squash_nan(left_num / right_num)
    if isinstance(left_num, int) and isinstance(right_num, int):
        if op is Operator.ADD:
            exact = left_num + right_num
        elif op is Operator.SUB:
            exact = left_num - right_num
        elif op is Operator.MUL:
            exact = left_num * right_num
        else:
            raise AssertionError(f"exec/expression.py: not an arithmetic operator: {op}")
        return _int64_bounded(exact)
    if op is Operator.ADD:
        result = left_num + right_num
    elif op is Operator.SUB:
        result = left_num - right_num
    elif op is Operator.MUL:
        result = left_num * right_num
    else:
        raise AssertionError(f"exec/expression.py: not an arithmetic operator: {op}")
    return _squash_nan(result)
