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
    if isinstance(expr, UnaryOp):
        return _eval_unary(expr, row, schema)
    if isinstance(expr, Is):
        return _eval_is(expr, row, schema)
    if isinstance(expr, And):
        return values.and3(evaluate(expr.left, row, schema), evaluate(expr.right, row, schema))
    if isinstance(expr, Or):
        return values.or3(evaluate(expr.left, row, schema), evaluate(expr.right, row, schema))
    if isinstance(expr, Not):
        return values.not3(evaluate(expr.operand, row, schema))
    if isinstance(expr, Like):
        return _eval_like(expr, row, schema)
    if isinstance(expr, In):
        return _eval_in(expr, row, schema)
    if isinstance(expr, Between):
        return _eval_between(expr, row, schema)
    raise AssertionError(f"exec/expression.py: unhandled expression node type {type(expr).__name__}")


def _eval_between(expr: Between, row: Row, schema: Schema) -> Bool3:
    """`x BETWEEN low AND high` is `values.and3(values.ge(x, low),
    values.le(x, high))`, per this issue's own criteria - not bespoke
    logic. Confirmed against `sqlite3`: `20 BETWEEN 30 AND NULL` is
    `FALSE`, not `NULL` - the first comparison alone already makes it
    `FALSE`, and `and3(FALSE, NULL)` is `FALSE`. Affinity is applied
    to each bound independently: `x`'s own affinity can interact
    differently with `low` and with `high`.
    """
    operand_low_left, operand_low_right = _evaluate_affinity_pair(expr.operand, expr.low, row, schema)
    operand_high_left, operand_high_right = _evaluate_affinity_pair(expr.operand, expr.high, row, schema)
    result = values.and3(
        values.ge(operand_low_left, operand_low_right),
        values.le(operand_high_left, operand_high_right),
    )
    return values.not3(result) if expr.negated else result


def _eval_in(expr: In, row: Row, schema: Schema) -> Bool3:
    """`x IN (v1, ..., vn)` is `values.or3` folded over each
    `values.eq(x, vi)`, per this issue's own criteria - not bespoke
    NULL-handling logic. Confirmed against `sqlite3`: `5 IN (5,
    NULL)` is `TRUE` (`or3` short-circuits on the first match before
    the `NULL` element matters), `6 IN (5, NULL)` is `NULL` (no
    element matches, but a `NULL` element means "maybe", not "no").
    `IN ()` folds over zero elements, leaving the `False` starting
    accumulator untouched - matching `sql/ast.py`'s own docstring that
    `IN ()` is always `FALSE`. Affinity is applied to each `(x, vi)`
    pair independently, exactly as for `=`: `x`'s own affinity can
    interact differently with each element's.
    """
    result: Bool3 = False
    for element in expr.values:
        left, right = _evaluate_affinity_pair(expr.left, element, row, schema)
        result = values.or3(result, values.eq(left, right))
    return values.not3(result) if expr.negated else result


# --- LIKE: unconditional text coercion, no affinity, ASCII-only fold ----


def _ascii_fold(text: str) -> str:
    """Fold only the ASCII letters `A`-`Z` to `a`-`z`; leave every
    other character - including everything outside ASCII - untouched.
    SQLite's own identifier- and `LIKE`-matching rule, not Python's
    Unicode-aware `str.lower()` (confirmed against `sqlite3`: `'café'
    LIKE 'CAFÉ'` is `FALSE`, the é/É pair is not folded).

    Deliberately a second copy of `sql/binder.py`'s own `_ascii_fold`
    rather than an import of it: importing `sql/binder.py` transitively
    imports `historian.tables.blame` (for `BLAME_SCHEMA`), which
    imports `subprocess` at module level - `sql/binder.py`'s own
    docstring accepts that trade-off for itself, but this module's own
    constraints are explicit (`AGENTS.md`'s "only scan operators touch
    git"; this issue's own "no import of anything under tables/") and
    that trade-off is not this module's to inherit. Three lines,
    identical behaviour, kept in sync by inspection rather than a
    shared dependency neither module already has a reason to need.
    """
    return "".join(chr(ord(ch) + 32) if "A" <= ch <= "Z" else ch for ch in text)


def _like_pattern_to_regex(pattern: str) -> re.Pattern[str]:
    """Compile a `LIKE` pattern (`%` any sequence including empty, `_`
    exactly one character) to a `re.fullmatch`-ready pattern. Every
    other character is escaped literally via `re.escape`, so the
    pattern text can never be interpreted as a regex metacharacter by
    accident. `re.DOTALL` so `_`/`%` match a newline too - `LIKE` has
    no notion of "line"."""
    pieces = []
    for ch in pattern:
        if ch == "%":
            pieces.append(".*")
        elif ch == "_":
            pieces.append(".")
        else:
            pieces.append(re.escape(ch))
    return re.compile("".join(pieces), re.DOTALL)


def _eval_like(expr: Like, row: Row, schema: Schema) -> Bool3:
    """`LIKE` / `NOT LIKE`. Confirmed against `sqlite3`: unlike every
    comparison above, `LIKE` never applies column affinity - both
    operands are cast to their SQLite text representation
    unconditionally (`n LIKE '5'` is `TRUE` for the INTEGER column
    `n=5`; `5 LIKE 5`, two integer literals, is also `TRUE`). `NULL`
    on either side makes the whole expression `NULL`
    (`NULL LIKE anything`, `anything LIKE NULL`). `NOT LIKE` is
    `values.not3` applied to the plain (un-negated) result - never a
    separately reasoned-out negation - which is what keeps `NULL`
    propagation correct through the negation for free.
    """
    left = evaluate(expr.left, row, schema)
    pattern = evaluate(expr.pattern, row, schema)
    if left is None or pattern is None:
        result: Bool3 = None
    else:
        left_text = _ascii_fold(_coerce_to_text(left))
        pattern_text = _ascii_fold(_coerce_to_text(pattern))
        result = bool(_like_pattern_to_regex(pattern_text).fullmatch(left_text))
    return values.not3(result) if expr.negated else result


# --- Column affinity -----------------------------------------------------
#
# Per this issue's own grooming (revising the 2026-08-27 decision entry's
# "convert the literal" phrasing, which was too narrow): affinity is
# decided per operand, independently, by a purely structural question -
# is this operand a *bare* BoundColumnRef? - never by "which side is a
# literal". `(n + 0) = '5'` has no affinity on its left side even though
# `n` is INTEGER, because the left operand there is a BinaryOp, not a
# bare BoundColumnRef.

_NUMERIC_AFFINITIES = (ColumnType.INTEGER, ColumnType.REAL)


def _affinity_of(expr: Expr, schema: Schema) -> ColumnType | None:
    """The affinity *expr* itself contributes to a comparison: the
    declared type of a bare `BoundColumnRef`, `None` for anything
    else - a literal, arithmetic, concatenation, or any other computed
    expression, even one that merely mentions a column."""
    if isinstance(expr, BoundColumnRef):
        return schema.columns[expr.offset].type
    return None


def _apply_affinity(
    left: Value, left_affinity: ColumnType | None, right: Value, right_affinity: ColumnType | None
) -> tuple[Value, Value]:
    """SQLite's own two-rule algorithm, run on one already-evaluated
    operand pair: numeric affinity wins whenever either operand has it
    (confirmed against `sqlite3`: this applies numeric conversion to
    *both* operands, including one that is itself `TEXT`-affinity, as
    in `n = s` above - not just the "other" side); otherwise, text
    affinity applies to a no-affinity operand whenever the other side
    has it. Neither rule ever fires when both operands carry no
    affinity at all - two literals compare with no coercion, matching
    `values.py`'s own class-rank comparison."""
    if left_affinity in _NUMERIC_AFFINITIES or right_affinity in _NUMERIC_AFFINITIES:
        return _try_numeric_affinity(left), _try_numeric_affinity(right)
    if left_affinity is ColumnType.TEXT or right_affinity is ColumnType.TEXT:
        return _coerce_to_text(left), _coerce_to_text(right)
    return left, right


def _strip_numeric_whitespace(text: str) -> str:
    """`text` with `_NUMERIC_WHITESPACE` characters trimmed from both
    ends - not Python's `str.strip()`, which trims a broader,
    Unicode-aware set this module has no evidence SQLite's own
    whole-string numeric-affinity check agrees with (only plain ASCII
    space is exercised by this issue's own criteria)."""
    return text.strip(_NUMERIC_WHITESPACE)


def _try_numeric_affinity(value: Value) -> Value:
    """Column affinity's text-to-number conversion: the *entire*
    (whitespace-trimmed) string must be a well-formed number, or the
    value is left as text, unconverted - a stricter rule than
    arithmetic's leading-prefix parse above (`n = '5abc'` is `FALSE`;
    `'5abc' + 1` is `6`). A non-`str` value passes through unchanged -
    it is already numeric, or `NULL`, and affinity never touches
    either."""
    if not isinstance(value, str):
        return value
    stripped = _strip_numeric_whitespace(value)
    if not stripped:
        return value
    scanned = _scan_number(stripped, 0)
    if scanned is None:
        return value
    number, end = scanned
    if end != len(stripped):
        return value
    return number


#: `2**63`, exactly representable as a double. The one value a bare
#: `Literal` can hold that came from `sql/parser.py` overflowing an
#: unsigned INTEGER-token digit sequence to REAL, per
#: `_docs/decisions.md` (2026-09-01, "An INTEGER literal past int64 max
#: becomes a float").
_INT64_MIN_MAGNITUDE_AS_FLOAT = 9223372036854775808.0


def _eval_unary(expr: UnaryOp, row: Row, schema: Schema) -> Value:
    """Unary `+`/`-`.

    `-` performs real numeric negation, going through the same
    leading-prefix text coercion arithmetic uses (`-'5'` is `-5`,
    `-'abc'` is `0`) and the same int64-overflow-to-REAL rule.

    `+` is a true no-op in SQLite - confirmed directly against
    `sqlite3` 3.51.0, and contradicting this issue's own body, which
    claims unary `+` "goes through the identical leading-prefix
    parse" as unary `-` while only actually verifying `-`'s examples:
    `+'5abc'` stays the TEXT `'5abc'`, unconverted, and `+5` stays the
    exact `INTEGER` `5` - `+` never touches its operand's storage
    class or value at all. Per `AGENTS.md`, SQLite is right; see
    `tests/test_expression.py`'s
    `test_unary_plus_is_a_true_no_op` docstring for the exact queries
    run, and this issue's closing comment for the report.

    A `UnaryOp(NEG, Literal(...))` whose literal is exactly
    `2**63` (`9223372036854775808.0`) is `-9223372036854775808`
    written directly in source, per `sql/parser.py`'s int64-overflow
    rule - and real SQLite keeps that spelling as the exact `int64`
    minimum rather than negating a `REAL`
    (`_docs/decisions.md`, 2026-09-01). Special-cased structurally
    here since it cannot be computed by the general path below without
    losing precision once arithmetic is involved - see
    `tests/test_expression.py`'s
    `test_negated_int64_min_literal_arithmetic_stays_exact` for why
    the general path is actually wrong, not just imprecise, and that
    test group's own docstring for why this fix cannot be complete
    (`sql/ast.py`'s `Literal` cannot distinguish this from an
    explicitly `.0`-spelled REAL literal of the same value, and
    `sql/ast.py` is out of scope for this issue).
    """
    if expr.op is UnaryOperator.POS:
        return evaluate(expr.operand, row, schema)
    if (
        isinstance(expr.operand, Literal)
        and isinstance(expr.operand.value, float)
        and expr.operand.value == _INT64_MIN_MAGNITUDE_AS_FLOAT
    ):
        return _INT64_MIN
    operand = evaluate(expr.operand, row, schema)
    if operand is None:
        return None
    numeric = _arithmetic_operand(operand)
    if isinstance(numeric, int):
        return _int64_bounded(-numeric)
    return _squash_nan(-numeric)


# --- BinaryOp: arithmetic, concatenation, comparison --------------------

_ARITHMETIC_OPS = frozenset({Operator.ADD, Operator.SUB, Operator.MUL, Operator.DIV})

#: One `values.py` comparison function per comparison `Operator`. All
#: six take affinity-adjusted operands - see `_eval_binary` below.
_COMPARISON_FNS = {
    Operator.EQ: values.eq,
    Operator.NE: values.ne,
    Operator.LT: values.lt,
    Operator.LE: values.le,
    Operator.GT: values.gt,
    Operator.GE: values.ge,
}


def _eval_binary(expr: BinaryOp, row: Row, schema: Schema) -> Value | Bool3:
    if expr.op in _ARITHMETIC_OPS:
        left = evaluate(expr.left, row, schema)
        right = evaluate(expr.right, row, schema)
        return _arithmetic(expr.op, left, right)
    if expr.op is Operator.CONCAT:
        left = evaluate(expr.left, row, schema)
        right = evaluate(expr.right, row, schema)
        if left is None or right is None:
            return None
        return _coerce_to_text(left) + _coerce_to_text(right)
    if expr.op in _COMPARISON_FNS:
        left, right = _evaluate_affinity_pair(expr.left, expr.right, row, schema)
        return _COMPARISON_FNS[expr.op](left, right)
    raise AssertionError(f"exec/expression.py: BinaryOp operator not yet handled: {expr.op}")


def _evaluate_affinity_pair(
    left_expr: Expr, right_expr: Expr, row: Row, schema: Schema
) -> tuple[Value, Value]:
    """Evaluate both sides of a comparison-shaped pair of operands
    (`=`/`<>`/.../`IS`/`IS NOT`, and - later - each element of `IN`
    and each bound of `BETWEEN`) and apply column affinity to the
    result. Shared by every predicate that this issue's own criteria
    says goes through "the identical affinity algorithm" as `=`."""
    left = evaluate(left_expr, row, schema)
    right = evaluate(right_expr, row, schema)
    return _apply_affinity(left, _affinity_of(left_expr, schema), right, _affinity_of(right_expr, schema))


def _eval_is(expr: Is, row: Row, schema: Schema) -> bool:
    """`IS` / `IS NOT`, including the `IS NULL` / `IS NOT NULL`
    spelling (`sql/ast.py`'s own docstring: `IS NULL` is `IS` against
    a `NULL` literal, not a separate node). Goes through the identical
    affinity algorithm as `=`/`<>` before calling `values.is_`/
    `values.is_not` - confirmed against `sqlite3`: `5 IS '5'` is
    `FALSE` like `5 = '5'`, but `n IS '5'` (INTEGER column) is `TRUE`."""
    left, right = _evaluate_affinity_pair(expr.left, expr.right, row, schema)
    return values.is_not(left, right) if expr.negated else values.is_(left, right)


# --- SQLite's number-to-text conversion, shared by ||, text affinity, --
# --- and LIKE's unconditional text coercion -----------------------------


def _format_float(value: float) -> str:
    """SQLite's `REAL -> TEXT` algorithm: `%.15g` (15 significant
    digits, C's own rounding), then guarantee the result contains a
    `.` or an `e` so it can never be mistaken for an `INTEGER`'s text
    form - appending `.0` when neither is present, or inserting it
    immediately before the `e` when an exponent is present but bare
    (`"1e+15"` -> `"1.0e+15"`). Deliberately not Python's
    `str()`/`repr()`, which disagree in ways confirmed against
    `sqlite3` 3.51.0 and pinned by
    `test_real_to_text_disagrees_with_python_str_to_prove_the_point` in
    `tests/test_expression.py`: `str(1e15)` is `'1000000000000000.0'`
    (wrong shape), `str(1234567890123456.0)` keeps all 16 digits
    unrounded (wrong precision), `str(1/3)` keeps 16 digits too,
    `str(1e20)` is `'1e+20'` (missing the `.0`).

    Infinity is not NaN - it is an ordinary, legal `REAL`
    (`values.py`'s own docstring) - and gets its own SQLite-specific
    spelling, confirmed against `sqlite3`: `1e400||''` is `'Inf'`,
    `-1e400||''` is `'-Inf'`, neither of which `%.15g` would produce
    unaided (Python's own `"%.15g" % float("inf")` is `'inf'`,
    lowercase, with no trailing `.0` to insert sensibly).
    """
    if math.isinf(value):
        return "-Inf" if value < 0 else "Inf"
    text = "%.15g" % value
    if "e" in text:
        mantissa, _, exponent = text.partition("e")
        if "." not in mantissa:
            mantissa += ".0"
        return f"{mantissa}e{exponent}"
    if "." not in text:
        text += ".0"
    return text


def _coerce_to_text(value: Value) -> str:
    """A `Value` as SQLite would render it as `TEXT` - used by `||`
    unconditionally on both operands (after the NULL check, which
    happens in the caller) and, later in this module, by column
    affinity's numeric-to-text conversion and `LIKE`'s unconditional
    text coercion. Never called with `None`."""
    if isinstance(value, bool):  # pragma: no cover - defensive; Value excludes bool
        raise TypeError(f"bool is not a SQL Value, got {value!r}")
    if isinstance(value, float):
        return _format_float(value)
    if isinstance(value, int):
        return str(value)
    return value


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
