"""Tests for historian.exec.expression.

Issue #12. Unit-style per spec §4's test-architecture table: hand-built
`Expr`/`Row`/`Schema` values, no repository, no git, no SQLite process
at test time (`AGENTS.md`'s scan-only-touches-git rule). Every expected
value below was independently checked against the `sqlite3` command-line
tool (3.51.0) during this issue's own work - the query used is quoted
above each group, matching `tests/test_binder.py`'s convention.

No differential suite exists yet for this issue (it arrives with #10's
harness landing plus the "8b" operators issue) - see the issue body's
own "Conformance" section - so correctness here rests entirely on these
sqlite3-verified values, not on comparing against a live SQLite process.

Two synthetic schemas are used throughout, matching the exact tables
built in sqlite3 to derive expected values:

    CREATE TABLE t(n INTEGER, s TEXT, r REAL); INSERT INTO t VALUES (5, '5', 5.0);

`_ROW`/`_SCHEMA` below mirror this exactly, so a test's own sqlite3
annotation can be run verbatim against the same shape.
"""

import math

import pytest

from historian.schema import Column, ColumnType, Row, Schema
from historian.sql.ast import (
    And,
    Between,
    BinaryOp,
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

_POS = Position(line=1, column=1, offset=0)

#: `t(n INTEGER, s TEXT, r REAL)`, one row: `(5, '5', 5.0)`.
_SCHEMA = Schema(
    columns=(
        Column("n", ColumnType.INTEGER),
        Column("s", ColumnType.TEXT),
        Column("r", ColumnType.REAL),
    )
)
_ROW: Row = (5, "5", 5.0)


def _lit(value) -> Literal:
    return Literal(value, _POS)


def _col(name: str) -> BoundColumnRef:
    offset = _SCHEMA.index_of(name)
    return BoundColumnRef(offset=offset, name=name, position=_POS)


def _bin(op: Operator, left, right) -> BinaryOp:
    return BinaryOp(op=op, left=left, right=right, position=_POS)


def _unary(op: UnaryOperator, operand) -> UnaryOp:
    return UnaryOp(op=op, operand=operand, position=_POS)


# --- Literals and bound column references -----------------------------


def test_literal_returns_its_value_unchanged():
    """`evaluate(Literal(5), ...)` is `5` - no computation to check
    against sqlite3, this is the base case of the recursion."""
    from historian.exec.expression import evaluate

    assert evaluate(_lit(5), _ROW, _SCHEMA) == 5


def test_literal_null_returns_none():
    from historian.exec.expression import evaluate

    assert evaluate(_lit(None), _ROW, _SCHEMA) is None


def test_bound_column_ref_resolves_by_offset_not_name():
    """`BoundColumnRef` carries a resolved offset (per §3, "column
    references resolve to integer offsets at bind time rather than by
    name at runtime") - this reads `row[ref.offset]` directly, never
    consulting `schema` to re-derive the offset from `ref.name`."""
    from historian.exec.expression import evaluate

    assert evaluate(_col("n"), _ROW, _SCHEMA) == 5
    assert evaluate(_col("s"), _ROW, _SCHEMA) == "5"
    assert evaluate(_col("r"), _ROW, _SCHEMA) == 5.0


def test_bound_column_ref_never_rederives_offset_from_schema_by_name():
    """A `BoundColumnRef` whose `name` field disagrees with what schema
    actually has at `offset` - constructed by hand, unreachable via a
    real binder - still resolves by `offset` alone. This is what makes
    the offset the load-bearing field, not `name` (kept only for
    rendering)."""
    from historian.exec.expression import evaluate

    mismatched = BoundColumnRef(offset=0, name="totally_not_n", position=_POS)
    assert evaluate(mismatched, _ROW, _SCHEMA) == 5


# --- Defensive guards: Star and FunctionCall ---------------------------


def test_star_reaching_evaluator_is_an_assertion_error():
    """The binder expands every `Star` before this module ever sees a
    tree (per its own docstring) - a `Star` here is a defensive
    "should never happen" guard, not UX to design, per the issue body."""
    from historian.exec.expression import evaluate

    with pytest.raises(AssertionError):
        evaluate(Star(table=None, position=_POS), _ROW, _SCHEMA)


def test_function_call_raises_structured_eval_error_not_bare_exception():
    """No aggregate or scalar function is in scope for this module (the
    planner splits aggregates out before this ever runs; no scalar
    function is chosen yet per §1) - a `FunctionCall` reaching here is
    unsupported grammar, per §3's "Errors" taxonomy, not a runtime type
    error and not a bare Python exception."""
    from historian.exec.expression import EvalError, evaluate

    call = FunctionCall(name="frobnicate", args=(), position=_POS)
    with pytest.raises(EvalError) as excinfo:
        evaluate(call, _ROW, _SCHEMA)
    assert excinfo.value.position is _POS


# --- Arithmetic: basic ops, NULL propagation ---------------------------
#
# sqlite3: `select 5+3, 5-3, 5*3, 5/3;` -> 8|2|15|1


def test_ordinary_arithmetic_on_two_integers():
    from historian.exec.expression import evaluate

    assert evaluate(_bin(Operator.ADD, _lit(5), _lit(3)), _ROW, _SCHEMA) == 8
    assert evaluate(_bin(Operator.SUB, _lit(5), _lit(3)), _ROW, _SCHEMA) == 2
    assert evaluate(_bin(Operator.MUL, _lit(5), _lit(3)), _ROW, _SCHEMA) == 15
    assert evaluate(_bin(Operator.DIV, _lit(5), _lit(3)), _ROW, _SCHEMA) == 1


@pytest.mark.parametrize(
    "op", [Operator.ADD, Operator.SUB, Operator.MUL, Operator.DIV, Operator.MOD]
)
def test_null_propagates_through_every_arithmetic_operator(op):
    """sqlite3: `select NULL+1, NULL-1, NULL*1, NULL/1, NULL%1;` -> all
    NULL."""
    from historian.exec.expression import evaluate

    assert evaluate(_bin(op, _lit(None), _lit(1)), _ROW, _SCHEMA) is None
    assert evaluate(_bin(op, _lit(1), _lit(None)), _ROW, _SCHEMA) is None


# --- Arithmetic: leading-prefix text-to-number coercion -----------------
#
# sqlite3: `select '5'+1, 'abc'+1, '5abc'+1, '  5  '+1, '0x10'+1, '5e'+1,
# '+5'+1, '-5'+1, '1e2'+1, typeof('1e2'+1), '.5'+1, '5.'+1, '5.5.5'+1,
# '5e+'+1, '   '+1, 'inf'+1;`
# -> 6|1|6|6|1|6|6|-4|101.0|real|1.5|6.0|6.5|6|1|1 (the word "inf" is not
# recognised as numeric either, same as "0x10" - contributes 0)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("5", 6),
        ("abc", 1),
        ("5abc", 6),
        ("  5  ", 6),
        ("0x10", 1),
        ("5e", 6),
        ("+5", 6),
        ("-5", -4),
        (".5", 1.5),
        ("5.", 6.0),
        ("5.5.5", 6.5),
        ("5e+", 6),
        ("   ", 1),
        ("inf", 1),
    ],
)
def test_arithmetic_text_coercion_is_a_leading_prefix_parse(text, expected):
    from historian.exec.expression import evaluate

    result = evaluate(_bin(Operator.ADD, _lit(text), _lit(1)), _ROW, _SCHEMA)
    assert result == expected
    assert type(result) is type(expected)


def test_arithmetic_text_coercion_exponent_form_is_always_real():
    """sqlite3: `select '1e2'+1, typeof('1e2'+1);` -> 101.0|real - REAL
    even though 100 is a whole number, because exponent notation was
    used."""
    from historian.exec.expression import evaluate

    result = evaluate(_bin(Operator.ADD, _lit("1e2"), _lit(1)), _ROW, _SCHEMA)
    assert result == 101.0
    assert isinstance(result, float)


# --- Arithmetic: division by zero is NULL, never ZeroDivisionError -----
#
# sqlite3: `select 1/0, 1.0/0, -1.0/0, typeof(1/0);` -> NULL|NULL|NULL|null


@pytest.mark.parametrize(
    "left,right",
    [(1, 0), (1.0, 0), (-1.0, 0), (1, 0.0)],
)
def test_division_by_zero_is_null_not_an_exception(left, right):
    from historian.exec.expression import evaluate

    assert evaluate(_bin(Operator.DIV, _lit(left), _lit(right)), _ROW, _SCHEMA) is None


# --- Arithmetic: NaN is squashed to NULL, not just 0.0/0.0 --------------
#
# sqlite3: `select 1e400-1e400, 1e400*0, 1e400/1e400;` -> NULL|NULL|NULL
# (1e400 parses as Infinity in sqlite3; typeof(1e400) is real, an
# ordinary legal value - values.py's own docstring confirms this).


@pytest.mark.parametrize(
    "op,left,right",
    [
        (Operator.SUB, math.inf, math.inf),
        (Operator.MUL, math.inf, 0),
        (Operator.DIV, math.inf, math.inf),
    ],
)
def test_nan_producing_arithmetic_yields_null(op, left, right):
    from historian.exec.expression import evaluate

    assert evaluate(_bin(op, _lit(left), _lit(right)), _ROW, _SCHEMA) is None


# --- Arithmetic: truncating integer division/toward zero, not floor -----
#
# sqlite3: `select 5/2, -5/2, 5/-2;` -> 2|-2|-2 (Python's -5//2 is -3 and
# 5//-2 is -3 - both wrong; this is the truncating-toward-zero rule).


@pytest.mark.parametrize(
    "left,right,expected",
    [(5, 2, 2), (-5, 2, -2), (5, -2, -2), (-5, -2, 2)],
)
def test_integer_division_truncates_toward_zero(left, right, expected):
    from historian.exec.expression import evaluate

    result = evaluate(_bin(Operator.DIV, _lit(left), _lit(right)), _ROW, _SCHEMA)
    assert result == expected
    assert isinstance(result, int)


def test_truncating_division_never_routes_through_float():
    """The 2^53 boundary: an exact truncating division of two large
    int64-range integers must not lose precision by going through
    `a / b`. `9223372036854775807 // 1` computed via `math.trunc(a/b)`
    would round in float and silently corrupt this value; the direct
    computation must not.
    """
    from historian.exec.expression import evaluate

    result = evaluate(_bin(Operator.DIV, _lit(9223372036854775807), _lit(1)), _ROW, _SCHEMA)
    assert result == 9223372036854775807
    assert isinstance(result, int)


# --- Arithmetic: int64 overflow to REAL ---------------------------------
#
# sqlite3: `select 9223372036854775807+1, typeof(9223372036854775807+1);`
# -> 9.22337203685478e+18|real
# sqlite3: `select 9223372036854775807*2, typeof(9223372036854775807*2);`
# -> 1.84467440737096e+19|real
# sqlite3: `select -9223372036854775808/-1, typeof(-9223372036854775808/-1);`
# -> 9.22337203685478e+18|real


def test_add_overflowing_int64_becomes_real():
    from historian.exec.expression import evaluate

    result = evaluate(_bin(Operator.ADD, _lit(9223372036854775807), _lit(1)), _ROW, _SCHEMA)
    assert isinstance(result, float)
    assert result == float(9223372036854775807 + 1)


def test_multiply_overflowing_int64_becomes_real():
    from historian.exec.expression import evaluate

    result = evaluate(_bin(Operator.MUL, _lit(9223372036854775807), _lit(2)), _ROW, _SCHEMA)
    assert isinstance(result, float)
    assert result == float(9223372036854775807 * 2)


def test_subtract_overflowing_int64_becomes_real():
    """sqlite3: `select -9223372036854775807-2,
    typeof(-9223372036854775807-2);` -> -9.22337203685478e+18|real."""
    from historian.exec.expression import evaluate

    result = evaluate(_bin(Operator.SUB, _lit(-9223372036854775807), _lit(2)), _ROW, _SCHEMA)
    assert isinstance(result, float)
    assert result == float(-9223372036854775807 - 2)


def test_division_result_overflowing_int64_becomes_real():
    """`Literal` is hand-built directly with the raw Python int here
    rather than routed through `UnaryOp(NEG, ...)` - constructing this
    shape is `sql/parser.py`'s concern (and out of scope for this
    issue), not this module's; the evaluator's own overflow rule is
    what this test is isolating."""
    from historian.exec.expression import evaluate

    result = evaluate(
        _bin(Operator.DIV, _lit(-9223372036854775808), _lit(-1)),
        _ROW,
        _SCHEMA,
    )
    assert isinstance(result, float)
    assert result == float(9223372036854775808)


def test_add_within_int64_bounds_stays_int():
    """The boundary itself does not overflow: `9223372036854775806+1`
    still fits."""
    from historian.exec.expression import evaluate

    result = evaluate(_bin(Operator.ADD, _lit(9223372036854775806), _lit(1)), _ROW, _SCHEMA)
    assert result == 9223372036854775807
    assert isinstance(result, int)


# --- Arithmetic: % (issue #75), C-style truncating remainder ------------
#
# Mirrors the DIV tests above: `_truncating_int_div` is reused directly
# (`remainder = left - _truncating_int_div(left, right) * right`), so
# `%`'s sign rule is the DIV sign rule with the quotient discarded.
#
# sqlite3: `select 7%2, -7%2, 7%-2, -7%-2;` -> 1|-1|1|-1


@pytest.mark.parametrize(
    "left,right,expected",
    [(7, 2, 1), (-7, 2, -1), (7, -2, 1), (-7, -2, -1)],
)
def test_modulo_sign_follows_the_dividend(left, right, expected):
    from historian.exec.expression import evaluate

    result = evaluate(_bin(Operator.MOD, _lit(left), _lit(right)), _ROW, _SCHEMA)
    assert result == expected
    assert isinstance(result, int)


# sqlite3: `select 7%0, 7%0.0, 7.5%0;` -> NULL|NULL|NULL


@pytest.mark.parametrize("left,right", [(7, 0), (7, 0.0), (7.5, 0)])
def test_modulo_by_zero_is_null_not_an_exception(left, right):
    from historian.exec.expression import evaluate

    assert evaluate(_bin(Operator.MOD, _lit(left), _lit(right)), _ROW, _SCHEMA) is None


def test_modulo_zero_check_is_against_the_truncated_divisor():
    """sqlite3: `select 7%0.5;` -> NULL. `0.5` truncates to integer `0`
    before the zero check, so this is a zero-divisor case even though
    the literal written is not zero - the truncation must happen
    before the check, not after."""
    from historian.exec.expression import evaluate

    assert evaluate(_bin(Operator.MOD, _lit(7), _lit(0.5)), _ROW, _SCHEMA) is None


# sqlite3: `select 7.5%2, -7.5%2, 7%2.5, typeof(7.5%2);` -> 1.0|-1.0|1.0|real


@pytest.mark.parametrize(
    "left,right,expected",
    [(7.5, 2, 1.0), (-7.5, 2, -1.0), (7, 2.5, 1.0)],
)
def test_modulo_real_operand_truncates_toward_zero_before_the_remainder(left, right, expected):
    from historian.exec.expression import evaluate

    result = evaluate(_bin(Operator.MOD, _lit(left), _lit(right)), _ROW, _SCHEMA)
    assert result == expected
    assert isinstance(result, float)


def test_modulo_result_storage_class_is_real_even_for_an_integral_valued_real():
    """sqlite3: `select 4.0%3, typeof(4.0%3);` -> 1.0|real - REAL-ness
    is about storage class, not value: `4.0` is integral-valued but was
    stored as REAL, so the result is REAL too."""
    from historian.exec.expression import evaluate

    result = evaluate(_bin(Operator.MOD, _lit(4.0), _lit(3)), _ROW, _SCHEMA)
    assert result == 1.0
    assert isinstance(result, float)


# sqlite3: `select 1e19%7, -1e300%7;` -> 0.0|-1.0 - large-magnitude REAL
# operands clamp to the int64 range the way `CAST(x AS INTEGER)` does,
# rather than converting with Python's unbounded `int()`:
# `CAST(1e19 AS INTEGER)` = 9223372036854775807 (int64 max, clamped),
# and `9223372036854775807 % 7` = 0; `CAST(-1e300 AS INTEGER)` clamps to
# int64 min, and `-9223372036854775808 % 7` = -1.


@pytest.mark.parametrize("left,expected", [(1e19, 0.0), (-1e300, -1.0)])
def test_modulo_large_magnitude_real_operand_clamps_to_int64_range(left, expected):
    from historian.exec.expression import evaluate

    result = evaluate(_bin(Operator.MOD, _lit(left), _lit(7)), _ROW, _SCHEMA)
    assert result == expected
    assert isinstance(result, float)


def test_modulo_int64_min_by_negative_one_does_not_trap():
    """sqlite3: `select -9223372036854775808%-1,
    typeof(-9223372036854775808%-1);` -> 0|integer. Unlike `/`, `%`'s
    result magnitude can never exceed `abs(right)`, so it can never
    overflow int64 when both operands already fit int64 - no
    `_int64_bounded` call is needed on the remainder itself."""
    from historian.exec.expression import evaluate

    result = evaluate(
        _bin(Operator.MOD, _lit(-9223372036854775808), _lit(-1)), _ROW, _SCHEMA
    )
    assert result == 0
    assert isinstance(result, int)


# sqlite3: `select '7abc'%2, 'abc'%2, ' 7 '%2, '-7abc'%2, '7.5abc'%2,
# typeof('7.5abc'%2);` -> 1|0|1|-1|1.0|real


@pytest.mark.parametrize(
    "text,expected",
    [
        ("7abc", 1),
        ("abc", 0),
        (" 7 ", 1),
        ("-7abc", -1),
        ("7.5abc", 1.0),
    ],
)
def test_modulo_text_operand_goes_through_leading_prefix_coercion(text, expected):
    from historian.exec.expression import evaluate

    result = evaluate(_bin(Operator.MOD, _lit(text), _lit(2)), _ROW, _SCHEMA)
    assert result == expected
    assert type(result) is type(expected)


# Issue #106: `%` reads a TEXT operand's integer value with its own
# digit-stop scan (SQLite's `sqlite3Atoi64`, reached from
# `OP_Remainder` via `sqlite3VdbeIntValue`), never accepting `.` or an
# exponent, clamped to int64 - while the REAL-vs-INTEGER class still
# comes from the general conversion (`numericType`). tests/oracle.py,
# module the oracle: `'1e3' % 7` -> 1.0, `'1.5e2' % 7` -> 1.0,
# `'-1e2' % 7` -> -1.0, `'2E1' % 7` -> 2.0, `'1e2abc' % 7` -> 1.0,
# `'1e400' % 3` -> 1.0, `'99999999999999999999e0' % 7` -> 0.0 (int64
# max % 7), `'-9223372036854775808e0' % 7` -> -1.0, `'5e' % 3` -> 2
# INTEGER, `'abc' % 5` -> 0 INTEGER.


@pytest.mark.parametrize(
    "text,expected_int,expected_is_real",
    [
        ("1e3", 1, True),
        ("1.5e2", 1, True),
        ("-1e2", -1, True),
        ("2E1", 2, True),
        ("1e2abc", 1, True),
        ("1e400", 1, True),
        ("99999999999999999999e0", 9223372036854775807, True),
        ("-9223372036854775808e0", -9223372036854775808, True),
        ("-99999999999999999999.5", -9223372036854775808, True),
        ("  +12e1", 12, True),
        (".5", 0, True),
        ("5e", 5, False),
        ("7abc", 7, False),
        ("abc", 0, False),
        ("", 0, False),
        ("- 12", 0, False),
        ("9223372036854775807", 9223372036854775807, False),
        ("9223372036854775808", 9223372036854775807, True),
        ("-9223372036854775809", -9223372036854775808, True),
        pytest.param("9" * 320, 9223372036854775807, True, id="320-nines"),
        pytest.param("0" * 5000 + "12", 12, False, id="5000-zeros-then-12"),
    ],
)
def test_modulo_text_operand_scan_stops_at_the_first_non_digit(
    text, expected_int, expected_is_real
):
    from historian.exec.expression import _modulo_text_operand

    value, is_real = _modulo_text_operand(text)
    assert value == expected_int
    assert type(value) is int
    assert is_real is expected_is_real


@pytest.mark.parametrize(
    "text,expected",
    [
        ("1e3", 1.0),
        ("1.5e2", 1.0),
        ("-1e2", -1.0),
        ("2E1", 2.0),
        ("1e2abc", 1.0),
        ("1e400", 1.0),
    ],
)
def test_modulo_text_operand_with_an_exponent_through_evaluate(text, expected):
    """`text % 7` - the digit-stop value, reported REAL. Values from
    tests/oracle.py (`'1e400' % 7` -> 1.0 likewise)."""
    from historian.exec.expression import evaluate

    result = evaluate(_bin(Operator.MOD, _lit(text), _lit(7)), _ROW, _SCHEMA)
    assert result == expected
    assert type(result) is float


def test_int64_truncated_clamps_positive_infinity_to_int64_max():
    from historian.exec.expression import _int64_truncated
    from historian.values import INT64_MAX

    assert _int64_truncated(float("inf")) == INT64_MAX


def test_int64_truncated_clamps_negative_infinity_to_int64_min():
    from historian.exec.expression import _int64_truncated
    from historian.values import INT64_MIN

    assert _int64_truncated(float("-inf")) == INT64_MIN


@pytest.mark.parametrize(
    "left,right,expected",
    [
        (float("inf"), 3, 1.0),
        (5, float("inf"), 5.0),
        (5, float("-inf"), 5.0),
        (float("inf"), float("inf"), 0.0),
    ],
)
def test_modulo_infinite_real_operand_clamps_instead_of_crashing(left, right, expected):
    """tests/oracle.py: `('1e400'+0) % 3` -> 1.0, `5 %
    ('1e400'+0)` -> 5.0, `5 % (-('1e400'+0))` -> 5.0, `('1e400'+0) %
    ('1e400'+0)` -> 0.0."""
    from historian.exec.expression import evaluate

    result = evaluate(_bin(Operator.MOD, _lit(left), _lit(right)), _ROW, _SCHEMA)
    assert result == expected
    assert type(result) is float


def test_modulo_comparison_result_as_operand():
    """sqlite3: `select (1=1)%2;` -> 1. The same `coerce_to_value` path
    issue #63 added for the other arithmetic operators, routed through
    `_eval_binary`'s `_ARITHMETIC_OPS` branch - not a new call site."""
    from historian.exec.expression import evaluate

    result = evaluate(_bin(Operator.MOD, _TRUE, _lit(2)), _ROW, _SCHEMA)
    assert result == 1
    assert isinstance(result, int)


def test_modulo_operand_raw_bool_raises_via_arithmetic_operand_guard():
    """Same defensive guard as the other arithmetic operators
    (`test_arithmetic_operand_raises_on_a_raw_bool_true`) - a raw
    Python `bool` must never reach `arithmetic_operand`."""
    from historian.exec.expression import arithmetic_operand

    with pytest.raises(TypeError):
        arithmetic_operand(True)


# --- Concatenation (||) --------------------------------------------------
#
# sqlite3: `select 'a'||1, typeof('a'||1);` -> a1|text
# sqlite3: `select 1||NULL, NULL||1;` -> (NULL)|(NULL)


def test_concat_of_text_and_integer():
    from historian.exec.expression import evaluate

    result = evaluate(_bin(Operator.CONCAT, _lit("a"), _lit(1)), _ROW, _SCHEMA)
    assert result == "a1"
    assert isinstance(result, str)


@pytest.mark.parametrize("left,right", [(1, None), (None, 1), (None, None)])
def test_concat_with_null_operand_is_null(left, right):
    from historian.exec.expression import evaluate

    assert evaluate(_bin(Operator.CONCAT, _lit(left), _lit(right)), _ROW, _SCHEMA) is None


# --- Concatenation: SQLite's float-to-text formatting, not Python's ------
#
# sqlite3: `select 5.0||'', 100.0||'', 1e15||'', 1234567890123456.0||'',
# (1.0/3.0)||'', 1e20||'', (0.1+0.2)||'';`
# -> 5.0|100.0|1.0e+15|1.23456789012346e+15|0.333333333333333|1.0e+20|0.3
# Python's str() disagrees with every non-trivial one of these:
# str(1e15) == '1000000000000000.0', str(1234567890123456.0) keeps all
# 16 digits unrounded, str(1/3) == '0.3333333333333333' (16 digits),
# str(1e20) == '1e+20' (no '.0'), str(0.1+0.2) == '0.30000000000000004'.


@pytest.mark.parametrize(
    "value,expected",
    [
        (5.0, "5.0"),
        (100.0, "100.0"),
        (1e15, "1.0e+15"),
        (1234567890123456.0, "1.23456789012346e+15"),
        (1.0 / 3.0, "0.333333333333333"),
        (1e20, "1.0e+20"),
        (0.1 + 0.2, "0.3"),
    ],
)
def test_real_to_text_uses_sqlites_15_significant_digit_format(value, expected):
    from historian.exec.expression import evaluate

    result = evaluate(_bin(Operator.CONCAT, _lit(value), _lit("")), _ROW, _SCHEMA)
    assert result == expected


def test_real_to_text_disagrees_with_python_str_to_prove_the_point():
    """Not a claim about this module - a check that the fixture values
    above are actually adversarial, so this test file cannot pass by
    accident against a naive `str(value)` implementation."""
    adversarial = [1e15, 1234567890123456.0, 1.0 / 3.0, 1e20, 0.1 + 0.2]
    expected = ["1.0e+15", "1.23456789012346e+15", "0.333333333333333", "1.0e+20", "0.3"]
    for value, sqlite_text in zip(adversarial, expected):
        assert str(value) != sqlite_text


def test_infinity_formats_as_inf_not_a_python_float_string():
    """sqlite3: `select 1e400||'', -1e400||'', typeof(1e400);`
    -> Inf|-Inf|real - Infinity is an ordinary, legal REAL (values.py's
    own docstring), unlike NaN, so it needs a real text form too."""
    from historian.exec.expression import evaluate

    assert evaluate(_bin(Operator.CONCAT, _lit(math.inf), _lit("")), _ROW, _SCHEMA) == "Inf"
    assert evaluate(_bin(Operator.CONCAT, _lit(-math.inf), _lit("")), _ROW, _SCHEMA) == "-Inf"


# --- Float-to-text formatting: negative zero is normalised, like SQLite --
#
# Issue #12, QA round 1 FAIL. sqlite3: `select (-0.0)||'', (0.0*-1)||'',
# (-1*0.0)||'', (0.0/-1)||'', (-(1.0-1.0))||'';` -> 0.0|0.0|0.0|0.0|0.0 -
# SQLite renders every one of these as positive zero, never `-0.0`, even
# though Python's own `-0.0`, `0.0 * -1`, and `"%.15g" % -0.0` all
# preserve the sign. Comparison is unaffected either way (`-0.0 = 0.0` is
# TRUE under IEEE 754, in both engines) - this is purely a TEXT-rendering
# mismatch, caught through `||` exactly as QA found it.


def test_format_float_normalises_negative_zero_directly():
    """sqlite3: `select (-0.0)||'';` -> 0.0. Calls the formatter
    directly, not just through the evaluator, per the issue's
    instruction to test the formatter itself."""
    from historian.exec.expression import _format_float

    assert _format_float(-0.0) == "0.0"


@pytest.mark.parametrize(
    "value",
    [-0.0, 0.0 * -1, -1 * 0.0, 0.0 / -1, -(1.0 - 1.0)],
    ids=["literal_neg_zero", "zero_times_neg_one", "neg_one_times_zero", "zero_div_neg_one", "negated_computed_zero"],
)
def test_negative_zero_renders_as_positive_through_concat(value):
    """sqlite3: `select (-0.0)||'', (0.0*-1)||'', (-1*0.0)||'',
    (0.0/-1)||'', (-(1.0-1.0))||'';` -> 0.0|0.0|0.0|0.0|0.0 in every
    case, regardless of how the negative zero was produced - a literal,
    multiplication, division, or unary minus on a computed zero."""
    from historian.exec.expression import evaluate

    assert evaluate(_bin(Operator.CONCAT, _lit(value), _lit("")), _ROW, _SCHEMA) == "0.0"


def test_negative_zero_via_unary_minus_on_computed_zero_through_evaluator():
    """sqlite3: `select (-(1.0-1.0))||'';` -> 0.0. Reached through
    UnaryOp(NEG, ...) rather than a Python-level negative-zero literal,
    to confirm the arithmetic path's own output gets normalised too,
    not only a value that was already -0.0 going in."""
    from historian.exec.expression import evaluate

    computed_zero = _bin(Operator.SUB, _lit(1.0), _lit(1.0))
    negated = _unary(UnaryOperator.NEG, computed_zero)
    assert evaluate(_bin(Operator.CONCAT, negated, _lit("")), _ROW, _SCHEMA) == "0.0"


@pytest.mark.parametrize(
    "value,expected",
    [(-0.5, "-0.5"), (-1.0, "-1.0"), (-1e15, "-1.0e+15")],
)
def test_genuinely_negative_values_keep_their_sign(value, expected):
    """sqlite3: `select (-0.5)||'', (-1.0)||'', (-1e15)||'';` ->
    -0.5|-1.0|-1.0e+15. The negative-zero fix must not touch any value
    that is actually negative, not just zero-valued."""
    from historian.exec.expression import evaluate

    assert evaluate(_bin(Operator.CONCAT, _lit(value), _lit("")), _ROW, _SCHEMA) == expected


# --- Unary minus: leading-prefix text coercion, then negate -------------
#
# sqlite3: `select -'5', typeof(-'5'), -'abc', typeof(-'abc'), -'5.5',
# typeof(-'5.5');` -> -5|integer|0|integer|-5.5|real


@pytest.mark.parametrize(
    "text,expected",
    [("5", -5), ("abc", 0), ("5.5", -5.5)],
)
def test_unary_minus_on_text_goes_through_leading_prefix_parse(text, expected):
    from historian.exec.expression import evaluate

    result = evaluate(_unary(UnaryOperator.NEG, _lit(text)), _ROW, _SCHEMA)
    assert result == expected
    assert type(result) is type(expected)


def test_unary_minus_on_ordinary_numbers():
    from historian.exec.expression import evaluate

    assert evaluate(_unary(UnaryOperator.NEG, _lit(5)), _ROW, _SCHEMA) == -5
    assert evaluate(_unary(UnaryOperator.NEG, _lit(5.5)), _ROW, _SCHEMA) == -5.5


def test_unary_minus_on_null_is_null():
    """sqlite3: `select -NULL, typeof(-NULL);` -> NULL|null."""
    from historian.exec.expression import evaluate

    assert evaluate(_unary(UnaryOperator.NEG, _lit(None)), _ROW, _SCHEMA) is None


def test_unary_minus_overflowing_int64_becomes_real():
    """Negating a plain `int` (not the int64-min-literal special case
    below) past the negative boundary must still promote to `float`,
    reusing the same overflow rule as binary arithmetic. This
    `Literal` holds the raw Python int `-9223372036854775808` directly
    - not built via a negated literal - so it does not hit the
    special-case path in the next test; it isolates the general
    overflow-on-negate rule instead."""
    from historian.exec.expression import evaluate

    result = evaluate(_unary(UnaryOperator.NEG, _lit(-9223372036854775808)), _ROW, _SCHEMA)
    assert isinstance(result, float)
    assert result == float(9223372036854775808)


# --- Unary minus on the int64-min literal: a documented, partial fix ----
#
# sqlite3: `select -9223372036854775808, typeof(-9223372036854775808);`
# -> -9223372036854775808|integer
# sqlite3: `select -9223372036854775808+1, typeof(-9223372036854775808+1);`
# -> -9223372036854775807|integer
#
# `sql/parser.py` (out of scope for #12) parses the digit sequence
# "9223372036854775808" alone as the float 9223372036854775808.0,
# since it overflows int64 as a plain INTEGER literal
# (`_docs/decisions.md`, 2026-09-01) - so `-9223372036854775808` in
# source text becomes `UnaryOp(NEG, Literal(9223372036854775808.0))`
# once parsed. Naively negating that float loses precision once
# arithmetic is involved (the double's ULP at 2**63 is 2048, so
# `-9223372036854775808.0 + 1.0` rounds right back to
# `-9223372036854775808.0` - the wrong answer). This module special-
# cases exactly this AST shape to recover the correct exact int64-min
# integer - see `_docs/decisions.md`, 2026-09-01 (int64 literal
# overflow) for why the fix is necessarily incomplete: `sql/ast.py`'s
# `Literal` has no field distinguishing this from an explicitly
# `.0`-spelled REAL literal of the same value, and `sql/ast.py` is out
# of scope for this issue.


def test_negated_int64_min_literal_is_the_exact_integer():
    from historian.exec.expression import evaluate

    overflowed_literal = _lit(9223372036854775808.0)
    result = evaluate(_unary(UnaryOperator.NEG, overflowed_literal), _ROW, _SCHEMA)
    assert result == -9223372036854775808
    assert isinstance(result, int)


def test_negated_int64_min_literal_arithmetic_stays_exact():
    """The case that actually distinguishes the fix from doing
    nothing: naive float negation followed by + 1 silently loses the
    +1 entirely (rounds back to the same float), where SQLite (and
    this module, with the special case above) gives the exact
    integer."""
    from historian.exec.expression import evaluate

    negated = _unary(UnaryOperator.NEG, _lit(9223372036854775808.0))
    result = evaluate(_bin(Operator.ADD, negated, _lit(1)), _ROW, _SCHEMA)
    assert result == -9223372036854775807
    assert isinstance(result, int)


# --- Unary minus and the sign of zero (issue #110) ----------------------
#
# SQLite negates a computed value as `0 - x`, not by flipping the sign
# bit, so `-(0.0 * 1)` is `+0.0`; only a REAL literal directly under
# `-` (parentheses are not a node) is folded to a negative literal, so
# `-(0.0)` is `-0.0`. Checked with `tests/oracle.py` (the oracle),
# `SELECT <expr> FROM blame` over one row with `line_no = 3`,
# REALs by `float.hex()`. Here `n` (5) stands in for `line_no`; any
# non-negative integer times 0.0 is `+0.0`.


def _neg(operand):
    return _unary(UnaryOperator.NEG, operand)


@pytest.mark.parametrize(
    "expr,expected_hex",
    [
        pytest.param(_neg(_bin(Operator.MUL, _col("n"), _lit(0.0))), "0x0.0p+0", id="-(n * 0.0)"),
        pytest.param(_neg(_bin(Operator.MUL, _col("r"), _lit(0.0))), "0x0.0p+0", id="-(r * 0.0)"),
        pytest.param(_neg(_bin(Operator.MUL, _lit(0.0), _lit(1))), "0x0.0p+0", id="-(0.0 * 1)"),
        pytest.param(_neg(_bin(Operator.DIV, _lit(0.0), _lit(1))), "0x0.0p+0", id="-(0.0 / 1)"),
        pytest.param(_neg(_bin(Operator.MOD, _lit(0.0), _lit(5))), "0x0.0p+0", id="-(0.0 % 5)"),
        pytest.param(_neg(_bin(Operator.SUB, _lit(1), _lit(1.0))), "0x0.0p+0", id="-(1 - 1.0)"),
        pytest.param(
            _neg(_bin(Operator.ADD, _neg(_lit(0.0)), _lit(0))), "0x0.0p+0", id="-(-0.0 + 0)"
        ),
        # Unary plus does not stop the literal fold on the pinned oracle
        # (`-(+0.0)` is `-0.0`); `0.0` was a SQLite 3.45.1 artefact (#117).
        pytest.param(_neg(_unary(UnaryOperator.POS, _lit(0.0))), "-0x0.0p+0", id="-(+0.0)"),
        pytest.param(_neg(_lit("0.0")), "0x0.0p+0", id="-'0.0'"),
        pytest.param(_neg(_lit("0.0abc")), "0x0.0p+0", id="-'0.0abc'"),
        pytest.param(_neg(_lit("1e-400")), "0x0.0p+0", id="-'1e-400'"),
        pytest.param(_neg(_lit("-0.0")), "0x0.0p+0", id="-'-0.0'"),
        pytest.param(_neg(_lit(0.0)), "-0x0.0p+0", id="-(0.0) literal"),
        pytest.param(_neg(_neg(_lit(0.0))), "0x0.0p+0", id="-(-0.0)"),
        pytest.param(_neg(_neg(_neg(_lit(0.0)))), "0x0.0p+0", id="-(-(-0.0))"),
        pytest.param(
            _neg(_bin(Operator.MUL, _lit(1.5), _lit(1))), "-0x1.8000000000000p+0", id="-(1.5 * 1)"
        ),
        pytest.param(_neg(_lit(1.5)), "-0x1.8000000000000p+0", id="-1.5 literal"),
        pytest.param(_neg(_lit("1e400")), "-inf", id="-'1e400'"),
        pytest.param(_neg(_bin(Operator.ADD, _lit("1e400"), _lit(0))), "-inf", id="-('1e400'+0)"),
        pytest.param(
            _neg(_neg(_bin(Operator.ADD, _lit("1e400"), _lit(0)))), "inf", id="-(-('1e400'+0))"
        ),
    ],
)
def test_unary_minus_zero_sign_matches_sqlite(expr, expected_hex):
    from historian.exec.expression import evaluate

    result = evaluate(expr, _ROW, _SCHEMA)
    assert type(result) is float
    assert result.hex() == expected_hex


def test_unary_minus_over_a_text_integer_zero_stays_integer():
    """`-'0'` and `-(0)` are INTEGER `0` (oracle: `int 0`)."""
    from historian.exec.expression import evaluate

    for expr in (_neg(_lit("0")), _neg(_lit(0))):
        result = evaluate(expr, _ROW, _SCHEMA)
        assert type(result) is int
        assert result == 0


def test_unary_minus_over_a_computed_nan_is_still_null():
    """A NaN operand (never a stored value, but one the evaluator must
    not let out) negates to NULL, as before."""
    from historian.exec.expression import evaluate

    nan_row: Row = (5, "5", math.nan)
    assert evaluate(_neg(_bin(Operator.MUL, _col("r"), _lit(1))), nan_row, _SCHEMA) is None


# --- Unary plus: SQLite's real behaviour is a no-op, not a coercion -----
#
# This issue's own body claims "unary -/+ on a text operand goes
# through the identical leading-prefix parse", citing only unary
# minus's own sqlite3-verified examples as evidence for both. Directly
# checked against sqlite3 3.51.0 and this half of the claim is false:
# `select +'5', typeof(+'5'), +'abc', typeof(+'abc'), +'5.5',
# typeof(+'5.5');` -> 5|text|abc|text|5.5|text - unary + does not touch
# its operand's storage class or value at all, confirmed further with
# `select +n, typeof(+n) from u;` (u.n INTEGER 5) -> 5|integer, i.e. a
# real no-op/identity, not "coerce then pass through as-is because it
# was already a number". Per AGENTS.md, SQLite is right; implemented
# as an identity here rather than as the criterion's claimed leading-
# prefix parse. Reported on the issue per the software-engineer role's
# instructions for a criterion that contradicts SQLite.


@pytest.mark.parametrize("value", ["5abc", "  5  ", "abc", 5, 5.5, None])
def test_unary_plus_is_a_true_no_op(value):
    from historian.exec.expression import evaluate

    result = evaluate(_unary(UnaryOperator.POS, _lit(value)), _ROW, _SCHEMA)
    assert result == value
    assert type(result) is type(value)


# --- Column affinity ------------------------------------------------------
#
# sqlite3, `t(n INTEGER, s TEXT, r REAL)`, row `(5, '5', 5.0)`:
#
#   select n = s, n = 'hello', s = 5.0, n = r;      -> 1|0|0|1
#   select s = (5+0);                               -> 1
#   select n = ('5' || '');                          -> 1
#   select (n+0) = '5';                               -> 0
#
# `_ROW_HELLO` is the same schema with `s = 'hello'` instead of `'5'`,
# for the "conversion fails, compares by class rank" half of `n = s`.

_ROW_HELLO: Row = (5, "hello", 5.0)


def test_two_literals_no_affinity_applied():
    """sqlite3: `select 5 = '5';` -> 0. No column is involved on
    either side, so `values.eq` runs with no coercion at all."""
    from historian.exec.expression import evaluate

    assert evaluate(_bin(Operator.EQ, _lit(5), _lit("5")), _ROW, _SCHEMA) is False


def test_bare_integer_column_converts_text_literal():
    """sqlite3 (`n INT`, `n=5`): `n = '5'` -> 1."""
    from historian.exec.expression import evaluate

    assert evaluate(_bin(Operator.EQ, _col("n"), _lit("5")), _ROW, _SCHEMA) is True


def test_bare_text_column_converts_integer_literal():
    """sqlite3 (`s TEXT`, `s='5'`): `s = 5` -> 1."""
    from historian.exec.expression import evaluate

    assert evaluate(_bin(Operator.EQ, _col("s"), _lit(5)), _ROW, _SCHEMA) is True


def test_computed_expression_has_no_affinity_even_though_it_contains_a_column():
    """sqlite3: `(n + 0) = '5'` -> 0, even though `n` is `INTEGER` -
    the left operand is a `BinaryOp`, not a bare `BoundColumnRef`, so
    it contributes no affinity at all. This is the exact correction
    this issue's grooming made to the 2026-08-27 decision entry's
    "convert the literal" phrasing."""
    from historian.exec.expression import evaluate

    computed = _bin(Operator.ADD, _col("n"), _lit(0))
    assert evaluate(_bin(Operator.EQ, computed, _lit("5")), _ROW, _SCHEMA) is False


def test_column_versus_column_applies_numeric_affinity_to_the_text_side():
    """sqlite3: `n = s` -> 1 for `n=5, s='5'`, -> 0 for `n=5,
    s='hello'` (conversion of 'hello' fails, no crash, compares by
    class rank)."""
    from historian.exec.expression import evaluate

    cmp = _bin(Operator.EQ, _col("n"), _col("s"))
    assert evaluate(cmp, _ROW, _SCHEMA) is True
    assert evaluate(cmp, _ROW_HELLO, _SCHEMA) is False


def test_text_affinity_applied_to_a_no_affinity_numeric_operand():
    """sqlite3: `s = (5+0)` -> 1 - `s` is `TEXT` affinity, `(5+0)` is
    a computed, no-affinity operand holding the int `5`; text affinity
    converts it to `'5'` before comparing."""
    from historian.exec.expression import evaluate

    computed = _bin(Operator.ADD, _lit(5), _lit(0))
    assert evaluate(_bin(Operator.EQ, _col("s"), computed), _ROW, _SCHEMA) is True


def test_numeric_affinity_applied_to_a_no_affinity_text_operand():
    """sqlite3: `n = ('5' || '')` -> 1 - `n` is numeric affinity, the
    concatenation result is a no-affinity `'5'`; numeric affinity
    converts it to `5` before comparing."""
    from historian.exec.expression import evaluate

    computed = _bin(Operator.CONCAT, _lit("5"), _lit(""))
    assert evaluate(_bin(Operator.EQ, _col("n"), computed), _ROW, _SCHEMA) is True


@pytest.mark.parametrize(
    "text,matches",
    [
        ("  5  ", True),
        ("+5", True),
        ("5e0", True),
        ("5.0", True),
        ("5 5", False),
        ("abc", False),
        ("5abc", False),
    ],
)
def test_affinity_text_to_number_requires_the_whole_trimmed_string(text, matches):
    """sqlite3 (`n INT`, `n=5`): `n = '  5  '` -> 1 (surrounding
    whitespace only), `n = '+5'` -> 1, `n = '5e0'` -> 1 (exponent
    form, becomes 5.0, numerically equal to 5), `n = '5 5'` -> 0,
    `n = 'abc'` / `n = '5abc'` -> 0 (no conversion at all - stays
    TEXT, never equal). This is a *whole-string* rule, distinct from
    arithmetic's leading-prefix rule tested above - '5abc' converts
    for arithmetic but not for affinity."""
    from historian.exec.expression import evaluate

    result = evaluate(_bin(Operator.EQ, _col("n"), _lit(text)), _ROW, _SCHEMA)
    assert result is matches


def test_numeric_affinity_preserves_int_versus_float_distinction():
    """sqlite3: `n = '5'` -> 1 (becomes the exact int 5), `n = '5.0'`
    -> 1 (becomes 5.0, numerically equal to 5 via values.py's
    exact numeric comparison) - both true, but for different reasons,
    which only matters once a value goes on to be compared again."""
    from historian.exec.expression import evaluate

    assert evaluate(_bin(Operator.EQ, _col("n"), _lit("5")), _ROW, _SCHEMA) is True
    assert evaluate(_bin(Operator.EQ, _col("n"), _lit("5.0")), _ROW, _SCHEMA) is True


def test_text_affinity_converts_numeric_to_sqlite_text_not_python_str():
    """sqlite3: `s = 5.0` -> 0 even though `s='5'` - the REAL `5.0`
    becomes the text `'5.0'` (via this module's own SQLite float
    formatter), not `'5'`, so it does not match."""
    from historian.exec.expression import evaluate

    assert evaluate(_bin(Operator.EQ, _col("s"), _lit(5.0)), _ROW, _SCHEMA) is False


def test_affinity_never_turns_null_into_anything_else():
    """sqlite3 (`n INT`, `s TEXT`, `n=5, s='5'`): `s = NULL` and
    `n = NULL` are both `NULL`. A `TEXT`-affinity column compared
    against a bare `NULL` literal reaches `_apply_affinity`'s text
    branch with a `None` operand (no affinity of its own) - it must
    pass straight through, not be coerced into `'None'`-shaped text
    or anything else, leaving `values.eq`'s own NULL handling to
    decide the outcome."""
    from historian.exec.expression import evaluate

    assert evaluate(_bin(Operator.EQ, _col("s"), _lit(None)), _ROW, _SCHEMA) is None
    assert evaluate(_bin(Operator.EQ, _col("n"), _lit(None)), _ROW, _SCHEMA) is None


def test_real_column_affinity_behaves_identically_to_integer_column():
    """A synthetic `REAL` column, since none of phase 1's real tables
    have one (`blame.line_no` is the only non-TEXT column,
    `_docs/decisions.md` 2026-08-27) - this module must not special-
    case `INTEGER` over `REAL`. sqlite3 (`r REAL`, `r=5.0`): `r = '5'`
    -> 1, same whole-string numeric conversion as the INTEGER case."""
    from historian.exec.expression import evaluate

    assert evaluate(_bin(Operator.EQ, _col("r"), _lit("5")), _ROW, _SCHEMA) is True
    assert evaluate(_bin(Operator.EQ, _col("r"), _lit("5abc")), _ROW, _SCHEMA) is False


#: Issue #99: an `Aggregate`-output-shaped schema, where a bare
#: `BoundColumnRef` points at a column declared `None` - no affinity,
#: the declared type of an aggregate result or computed `GROUP BY` key.
#: One row: `(21, 12, 2.5)`.
_NO_AFFINITY_SCHEMA = Schema(
    columns=(
        Column("sum_1", None),
        Column("count_2", None),
        Column("avg_3", None),
    )
)
_NO_AFFINITY_ROW: Row = (21, 12, 2.5)


def _no_affinity_col(name: str) -> BoundColumnRef:
    return BoundColumnRef(offset=_NO_AFFINITY_SCHEMA.index_of(name), name=name, position=_POS)


def test_bare_column_ref_to_a_no_affinity_column_is_not_coerced_to_text():
    """sqlite3 (awkward fixture, issue #99): `sum(line_no) > 3` is true
    for `café.py`'s 21. A `None`-declared column contributes no
    affinity, and neither does the literal, so `21 > 3` compares as
    plain integers - not `'21' > '3'` as the old `TEXT` placeholder
    made it."""
    from historian.exec.expression import evaluate

    expr = _bin(Operator.GT, _no_affinity_col("sum_1"), _lit(3))
    assert evaluate(expr, _NO_AFFINITY_ROW, _NO_AFFINITY_SCHEMA) is True


def test_bare_column_ref_to_a_no_affinity_column_does_not_convert_a_text_literal():
    """sqlite3: `count(*) = '12'` -> 0 and `avg(line_no) = '2.5'` -> 0
    (issue #99) - with no affinity on either side, `'12'` stays text
    and a number never equals a text value."""
    from historian.exec.expression import evaluate

    count_eq = _bin(Operator.EQ, _no_affinity_col("count_2"), _lit("12"))
    avg_eq = _bin(Operator.EQ, _no_affinity_col("avg_3"), _lit("2.5"))
    assert evaluate(count_eq, _NO_AFFINITY_ROW, _NO_AFFINITY_SCHEMA) is False
    assert evaluate(avg_eq, _NO_AFFINITY_ROW, _NO_AFFINITY_SCHEMA) is False


def test_no_affinity_column_still_takes_the_other_operands_affinity():
    """A no-affinity operand is still subject to the *other* side's
    affinity, exactly like a literal or computed expression. sqlite3
    (`t(s TEXT)`, one row `'5'`): `SELECT s, count(*) FROM t GROUP BY
    s HAVING s = count(*) + 4` -> `('5', 1)` - `TEXT`-declared `s`
    against the no-affinity integer `5` compares as text `'5' = '5'`.
    Uses a mixed schema so one side is a real `TEXT` column and the
    other is `None`."""
    from historian.exec.expression import evaluate

    schema = Schema(columns=(Column("s", ColumnType.TEXT), Column("k", None)))
    s_ref = BoundColumnRef(offset=0, name="s", position=_POS)
    k_ref = BoundColumnRef(offset=1, name="k", position=_POS)
    assert evaluate(_bin(Operator.EQ, s_ref, k_ref), ("5", 5), schema) is True


# --- Comparisons: all six operators wire through affinity too -----------
#
# sqlite3 (`n INT`, `n=5`): `n < '10'` -> 1, `n > '3'` -> 1


def test_less_than_and_greater_than_apply_affinity_too():
    from historian.exec.expression import evaluate

    assert evaluate(_bin(Operator.LT, _col("n"), _lit("10")), _ROW, _SCHEMA) is True
    assert evaluate(_bin(Operator.GT, _col("n"), _lit("3")), _ROW, _SCHEMA) is True


@pytest.mark.parametrize(
    "op,expected",
    [
        (Operator.EQ, True),
        (Operator.NE, False),
        (Operator.LT, False),
        (Operator.LE, True),
        (Operator.GT, False),
        (Operator.GE, True),
    ],
)
def test_every_comparison_operator_is_wired(op, expected):
    """Sanity sweep over all six, `5 <op> 5` - each one's own
    three-valued semantics are `values.py`'s job and already tested
    there; this only proves this module's dispatch reaches all six."""
    from historian.exec.expression import evaluate

    assert evaluate(_bin(op, _lit(5), _lit(5)), _ROW, _SCHEMA) is expected


# --- Numeric comparison stays exact through this module too -------------
#
# tests/test_values.py's own 2^53 pin: 9007199254740993 = 9007199254740992.0
# is FALSE, 9007199254740993 > 9007199254740992.0 is TRUE. Confirmed the
# same way against sqlite3 directly.


def test_int64_overflow_conversion_never_applies_to_comparison():
    """The int64-overflow-to-REAL rule is about arithmetic *result
    production* only, per this issue's own acceptance criteria - never
    about comparison, and the two must never be confused by a future
    change. `9223372036854775807 = 9223372036854775807` involves no
    arithmetic at all, so both operands must stay the exact `int`s
    they are - confirmed against `sqlite3`: `9223372036854775807 =
    9223372036854775807` is `1`, `9223372036854775807 >
    9223372036854775806` is `1`."""
    from historian.exec.expression import evaluate

    eq = _bin(Operator.EQ, _lit(9223372036854775807), _lit(9223372036854775807))
    gt = _bin(Operator.GT, _lit(9223372036854775807), _lit(9223372036854775806))
    assert evaluate(eq, _ROW, _SCHEMA) is True
    assert evaluate(gt, _ROW, _SCHEMA) is True


def test_2_53_boundary_comparison_stays_exact_through_a_bound_column_ref():
    """The checkable form of the exactness rule: via `evaluate()` and
    a `BoundColumnRef`, not `values.eq` called directly."""
    from historian.exec.expression import evaluate

    big_row: Row = (9007199254740993, "5", 5.0)
    eq = _bin(Operator.EQ, _col("n"), _lit(9007199254740992.0))
    gt = _bin(Operator.GT, _col("n"), _lit(9007199254740992.0))
    assert evaluate(eq, big_row, _SCHEMA) is False
    assert evaluate(gt, big_row, _SCHEMA) is True


# --- IS / IS NOT: the same affinity algorithm, then values.is_/is_not ---
#
# sqlite3: `select 5 IS '5';` -> 0 (two literals, matches `5 = '5'`)
# sqlite3 (`n INT`, `n=5`): `n IS '5'` -> 1, `n IS NOT '5'` -> 0


def _is(left, right, negated=False) -> Is:
    return Is(left=left, right=right, negated=negated, position=_POS)


def test_is_two_literals_mirrors_eq_no_affinity():
    from historian.exec.expression import evaluate

    assert evaluate(_is(_lit(5), _lit("5")), _ROW, _SCHEMA) is False


def test_is_and_is_not_apply_affinity_like_eq():
    from historian.exec.expression import evaluate

    assert evaluate(_is(_col("n"), _lit("5")), _ROW, _SCHEMA) is True
    assert evaluate(_is(_col("n"), _lit("5"), negated=True), _ROW, _SCHEMA) is False


# --- AND / OR / NOT: three-valued logic, via values.and3/or3/not3 -------
#
# spec §3's own table: NULL AND FALSE -> FALSE; NULL AND TRUE, NULL AND
# NULL -> NULL; NULL OR TRUE -> TRUE; NULL OR FALSE, NULL OR NULL ->
# NULL; NOT NULL -> NULL. Already exhaustively tested for values.py
# itself in tests/test_values.py - this only proves evaluate() reaches
# and3/or3/not3 correctly for predicate-shaped operands built from real
# expression nodes (comparisons), not hand-fed bools.

_TRUE = _bin(Operator.EQ, _lit(1), _lit(1))
_FALSE = _bin(Operator.EQ, _lit(1), _lit(0))
_NULL = _bin(Operator.EQ, _lit(1), _lit(None))


def test_and_three_valued_logic():
    from historian.exec.expression import evaluate

    assert evaluate(And(_NULL, _FALSE, _POS), _ROW, _SCHEMA) is False
    assert evaluate(And(_NULL, _TRUE, _POS), _ROW, _SCHEMA) is None
    assert evaluate(And(_NULL, _NULL, _POS), _ROW, _SCHEMA) is None
    assert evaluate(And(_TRUE, _TRUE, _POS), _ROW, _SCHEMA) is True


def test_or_three_valued_logic():
    from historian.exec.expression import evaluate

    assert evaluate(Or(_NULL, _TRUE, _POS), _ROW, _SCHEMA) is True
    assert evaluate(Or(_NULL, _FALSE, _POS), _ROW, _SCHEMA) is None
    assert evaluate(Or(_NULL, _NULL, _POS), _ROW, _SCHEMA) is None
    assert evaluate(Or(_FALSE, _FALSE, _POS), _ROW, _SCHEMA) is False


def test_not_three_valued_logic():
    from historian.exec.expression import evaluate

    assert evaluate(Not(_NULL, _POS), _ROW, _SCHEMA) is None
    assert evaluate(Not(_TRUE, _POS), _ROW, _SCHEMA) is False
    assert evaluate(Not(_FALSE, _POS), _ROW, _SCHEMA) is True


# --- AND / OR / NOT: a value-shaped operand nested anywhere in the tree -
#
# Issue #38 round 2 (QA FAIL on the first round). `coerce_to_bool3` was
# only ever called at Filter's and Project's own root call sites, so a
# value-shaped operand *nested* under AND/OR/NOT - not sitting at the
# WHERE/select-list root - reached `values.and3`/`or3`/`not3` raw and
# raised `TypeError` instead of being coerced. Confirmed live:
# `historian "SELECT path FROM blame WHERE line_no - line_no AND path
# = 'AGENTS.md'"` raised `TypeError: not a Bool3: 0 of type int`.
#
# The orchestrator's correction comment on #38 settles the fix: AND,
# OR and NOT's operands are predicate positions *unconditionally* -
# that is a property of the node's own shape, exactly what
# `evaluate()`'s recursive dispatch already knows at every level, per
# this module's own "Value or Bool3, decided by node shape" design.
# So `evaluate()`'s own `And`/`Or`/`Not` branches now wrap each
# operand's result in `coerce_to_bool3` before handing it to
# `values.and3`/`or3`/`not3` - still no `position` parameter, still a
# caller-side coercion, just called one level deeper too.
#
# `n - n` (a computed value-shaped `BinaryOp`, never a bare column) is
# `0` for `_ROW`'s `n = 5` - falsy. Confirmed against `sqlite3`:
# `select (5-5) and 1;` -> `0`; `select 1 and (5-5);` -> `0`;
# `select 0 or 5;` -> `1`; `select not(5-5);` -> `1`; `select '0abc'
# or 0;` -> `0`.

_VALUE_FALSY = _bin(Operator.SUB, _col("n"), _col("n"))
_VALUE_TRUTHY = _col("n")


def test_and_coerces_a_value_shaped_left_operand_nested_in_the_tree():
    """`(n - n) AND (1 = 1)` - the left operand is value-shaped and
    falsy (`0`); pre-fix this raised `TypeError` reaching `and3`
    directly. `sqlite3`: `select (5-5) and 1;` -> `0`."""
    from historian.exec.expression import evaluate

    assert evaluate(And(_VALUE_FALSY, _TRUE, _POS), _ROW, _SCHEMA) is False


def test_and_coerces_a_value_shaped_right_operand_nested_in_the_tree():
    """`(1 = 1) AND (n - n)` - same coercion, right operand this time.
    `sqlite3`: `select 1 and (5-5);` -> `0`."""
    from historian.exec.expression import evaluate

    assert evaluate(And(_TRUE, _VALUE_FALSY, _POS), _ROW, _SCHEMA) is False


def test_or_coerces_a_value_shaped_operand_nested_in_the_tree():
    """`(1 = 2) OR n` - `n` is a bare, value-shaped column (`5`,
    truthy). `sqlite3`: `select 0 or 5;` -> `1`."""
    from historian.exec.expression import evaluate

    assert evaluate(Or(_FALSE, _VALUE_TRUTHY, _POS), _ROW, _SCHEMA) is True


def test_not_coerces_a_value_shaped_operand_nested_in_the_tree():
    """`NOT (n - n)` - `sqlite3`: `select not(5-5);` -> `1`."""
    from historian.exec.expression import evaluate

    assert evaluate(Not(_VALUE_FALSY, _POS), _ROW, _SCHEMA) is True


def test_or_discriminates_leading_prefix_truthiness_not_bare_python_truthiness_when_nested():
    """`'0abc' OR (1 = 2)` - a bare Python string `'0abc'` is truthy
    (nonempty), so a fix that fell back to `bool(evaluate(...))`
    instead of `coerce_to_bool3`'s leading-prefix numeric rule would
    wrongly keep this `TRUE`. `sqlite3`: `select '0abc' or 0;` ->
    `0`."""
    from historian.exec.expression import evaluate

    assert evaluate(Or(_lit("0abc"), _FALSE, _POS), _ROW, _SCHEMA) is False


def test_and_still_raises_on_a_genuinely_invalid_bool3_from_values_py():
    """`coerce_to_bool3` only ever produces a `bool`/`None` - it cannot
    itself manufacture an invalid `Bool3` - so `values.py`'s own
    `_check_bool3` guard stays the last line of defense, exactly as
    the issue's Constraints section requires (`values.py` untouched).
    Not a new behaviour; pinned here so a future change to
    `coerce_to_bool3` that started returning something else would be
    caught by `and3` the same way it always has been."""
    from historian import values
    from historian.exec.expression import coerce_to_bool3

    assert coerce_to_bool3(0) is False
    assert coerce_to_bool3(3) is True
    with pytest.raises(TypeError):
        values.and3(1, True)  # SQLite's own int spelling, never a Bool3


# --- LIKE: unconditional text coercion, no affinity, ASCII-only fold ----


def _like(left, pattern, negated=False, escape=None) -> Like:
    return Like(
        left=left, pattern=pattern, negated=negated, position=_POS, escape=escape
    )


def test_like_does_not_use_affinity_casts_both_sides_to_text_unconditionally():
    """sqlite3 (`n INT`, `n=5`): `n LIKE '5'` -> 1; `5 LIKE 5` -> 1
    (two integer literals, no column at all - LIKE always converts,
    affinity or not)."""
    from historian.exec.expression import evaluate

    assert evaluate(_like(_col("n"), _lit("5")), _ROW, _SCHEMA) is True
    assert evaluate(_like(_lit(5), _lit(5)), _ROW, _SCHEMA) is True


@pytest.mark.parametrize(
    "text,pattern,expected",
    [
        ("abc", "a%c", True),
        ("abc", "a_c", True),
        ("abc", "a__", True),
        ("ABC", "abc", True),
    ],
)
def test_like_wildcards_and_ascii_case_insensitivity(text, pattern, expected):
    """sqlite3: `'abc' like 'a%c'`, `'abc' like 'a_c'`, `'abc' like
    'a__'`, `'ABC' like 'abc'` -> all 1."""
    from historian.exec.expression import evaluate

    assert evaluate(_like(_lit(text), _lit(pattern)), _ROW, _SCHEMA) is expected


@pytest.mark.parametrize(
    "pattern",
    ["a_c", "a%c"],
)
def test_like_wildcards_match_a_newline(pattern):
    """sqlite3: `select ('a' || char(10) || 'c') like 'a_c', ('a' ||
    char(10) || 'c') like 'a%c';` -> `1|1`. `LIKE` has no notion of
    "line", so both `_` (exactly one character) and `%` (any sequence)
    must match a newline the same as any other character - pinned
    through `evaluate()`, not against `_like_pattern_to_regex`'s
    internals, so it survives #51's concurrent rewrite of that
    function."""
    from historian.exec.expression import evaluate

    assert evaluate(_like(_lit("a\nc"), _lit(pattern)), _ROW, _SCHEMA) is True


def test_like_case_folding_is_ascii_only_not_unicode():
    """sqlite3: `select 'café' like 'CAFÉ';` -> 0 - the é/É pair is not
    ASCII and is not folded, reusing the same rule
    `sql/binder.py`'s `_ascii_fold` implements (only A-Z/a-z move),
    matching the straße/STRASSE precedent already recorded in
    `_docs/decisions.md` (2026-09-01) for the same reason."""
    from historian.exec.expression import evaluate

    assert evaluate(_like(_lit("café"), _lit("CAFÉ")), _ROW, _SCHEMA) is False


@pytest.mark.parametrize("left,pattern", [(None, "x"), ("x", None)])
def test_like_with_null_operand_is_null(left, pattern):
    from historian.exec.expression import evaluate

    assert evaluate(_like(_lit(left), _lit(pattern)), _ROW, _SCHEMA) is None


def test_not_like_is_not3_of_the_unnegated_result():
    """sqlite3: `select 'abc' not like 'abc';` -> 0. Also proves NULL
    propagation survives the negation (`not3(None)` is `None`, not
    `True`) via `null not like 'x'` -> NULL."""
    from historian.exec.expression import evaluate

    assert evaluate(_like(_lit("abc"), _lit("abc"), negated=True), _ROW, _SCHEMA) is False
    assert evaluate(_like(_lit("abc"), _lit("xyz"), negated=True), _ROW, _SCHEMA) is True
    assert evaluate(_like(_lit(None), _lit("x"), negated=True), _ROW, _SCHEMA) is None


# --- LIKE ... ESCAPE (issue #51) -----------------------------------------
#
# Every value below checked live against sqlite3 3.51.0 during this
# issue's own work, not reasoned about from memory.


def test_like_escape_basic_percent_escaping():
    """sqlite3: `select '10%' like '10!%' escape '!';` -> 1 (the `!`
    before `%` makes it a literal percent, not a wildcard); `select
    '10x' like '10!%' escape '!';` -> 0 (the input has no literal
    `%`)."""
    from historian.exec.expression import evaluate

    assert evaluate(_like(_lit("10%"), _lit("10!%"), escape=_lit("!")), _ROW, _SCHEMA) is True
    assert evaluate(_like(_lit("10x"), _lit("10!%"), escape=_lit("!")), _ROW, _SCHEMA) is False


def test_like_escape_basic_underscore_escaping():
    """sqlite3: `select 'a_b' like 'a!_b' escape '!';` -> 1 (literal
    underscore); `select 'axb' like 'a!_b' escape '!';` -> 0 (`_` no
    longer means "any one character" once escaped)."""
    from historian.exec.expression import evaluate

    assert evaluate(_like(_lit("a_b"), _lit("a!_b"), escape=_lit("!")), _ROW, _SCHEMA) is True
    assert evaluate(_like(_lit("axb"), _lit("a!_b"), escape=_lit("!")), _ROW, _SCHEMA) is False


def test_like_escape_escaping_the_escape_character_itself():
    """sqlite3: `select 'a!b' like 'a!!b' escape '!';` -> 1 - pattern
    `a!!b` with escape `!` means literal `a`, literal `!`, literal
    `b`."""
    from historian.exec.expression import evaluate

    assert evaluate(_like(_lit("a!b"), _lit("a!!b"), escape=_lit("!")), _ROW, _SCHEMA) is True


def test_like_escape_at_end_of_pattern_is_unsatisfiable_not_a_literal():
    """An escape character with nothing after it makes the pattern
    unsatisfiable - not a no-op, not an error. sqlite3: `select 'a!'
    like 'a!' escape '!';` -> 0 even though the strings are identical,
    and `select 'aX' like 'a!' escape '!';` -> 0. A wrongly-lenient
    implementation that treated a trailing escape as a literal `!`
    would pass the first case."""
    from historian.exec.expression import evaluate

    assert evaluate(_like(_lit("a!"), _lit("a!"), escape=_lit("!")), _ROW, _SCHEMA) is False
    assert evaluate(_like(_lit("aX"), _lit("a!"), escape=_lit("!")), _ROW, _SCHEMA) is False


def test_like_escape_before_an_ordinary_character_is_a_no_op():
    """sqlite3: `select 'ab' like 'a!b' escape '!';` -> 1 - escaping a
    character that needed no escaping just matches it literally."""
    from historian.exec.expression import evaluate

    assert evaluate(_like(_lit("ab"), _lit("a!b"), escape=_lit("!")), _ROW, _SCHEMA) is True


@pytest.mark.parametrize(
    "text,pattern,escape,expected",
    [
        # sqlite3: `select 'a%b' like 'axb' escape 'X';` -> 0 - lowercase
        # `x` in the pattern does not match uppercase escape `X`, so it
        # stays an ordinary (ASCII-folded) letter and the input's `%`
        # has nothing to match it against.
        ("a%b", "axb", "X", False),
        # sqlite3: `select 'aXb' like 'axb' escape 'X';` -> 1 - same
        # reasoning: pattern's `x` is an ordinary, case-folded letter,
        # matching input's `X` via the normal ASCII fold.
        ("aXb", "axb", "X", True),
        # sqlite3: `select 'a%b' like 'aXb' escape 'x';` -> 0 - escape
        # recognition is exact in both directions.
        ("a%b", "aXb", "x", False),
        # sqlite3: `select 'a%b' like 'ax%b' escape 'X';` -> 0 -
        # lowercase `x` isn't recognised as the uppercase escape, so the
        # `%` right after it is untouched and remains a real wildcard
        # (which `a%b`'s literal `%` at that position does not satisfy).
        ("a%b", "ax%b", "X", False),
        # Two more direct probes, escape recognition case-sensitive:
        # sqlite3: `select 'a%' like 'aX%' escape 'x';` -> 0.
        ("a%", "aX%", "x", False),
        # sqlite3: `select 'a%' like 'ax%' escape 'x';` -> 1.
        ("a%", "ax%", "x", True),
    ],
)
def test_like_escape_recognition_is_case_sensitive_independent_of_ascii_fold(
    text, pattern, escape, expected
):
    """Escape-character *recognition* inside the pattern is
    case-sensitive / exact-codepoint, independent of `LIKE`'s own
    ASCII case-fold of the matched text. `_like_pattern_to_regex` must
    scan the *raw*, unfolded pattern text for escape occurrences - not
    the ASCII-folded text `_eval_like` used to hand it before this
    issue, which would have made escape recognition wrongly
    case-insensitive."""
    from historian.exec.expression import evaluate

    result = evaluate(_like(_lit(text), _lit(pattern), escape=_lit(escape)), _ROW, _SCHEMA)
    assert result is expected


def test_like_escape_preserves_dotall_for_a_newline_in_the_matched_text():
    """`_like_pattern_to_regex`'s `re.DOTALL` flag (untested before
    this issue, per #50's concurrent grooming) must survive the
    ESCAPE-aware rewrite: `_`/`%` still match a newline, including when
    an ESCAPE clause is present. sqlite3: `select ('a' || char(10) ||
    '%') like 'a_!%' escape '!';` -> 1 (`_` matches the newline, `!%`
    escapes the percent to a literal, so input `'a\\n%'` matches); and
    `select ('a' || char(10) || 'x') like 'a_!%' escape '!';` -> 0 (no
    trailing literal `%` in the input)."""
    from historian.exec.expression import evaluate

    assert (
        evaluate(_like(_lit("a\n%"), _lit("a_!%"), escape=_lit("!")), _ROW, _SCHEMA) is True
    )
    assert (
        evaluate(_like(_lit("a\nx"), _lit("a_!%"), escape=_lit("!")), _ROW, _SCHEMA) is False
    )


def test_like_escape_null_escape_operand_is_null():
    """sqlite3: `select typeof('10%' LIKE '10!%' ESCAPE NULL);` ->
    `null`, no error - even though the (missing) escape text could
    never satisfy the length check, the NULL check comes first. Also
    covers NULL left/NULL pattern combined with a NULL escape."""
    from historian.exec.expression import evaluate

    assert evaluate(_like(_lit("10%"), _lit("10!%"), escape=_lit(None)), _ROW, _SCHEMA) is None
    assert evaluate(_like(_lit(None), _lit("x"), escape=_lit(None)), _ROW, _SCHEMA) is None


def test_like_escape_length_check_runs_even_when_left_or_pattern_is_null():
    """sqlite3: `select null like 'x' escape 'ab';` and `select 'x'
    like null escape 'ab';` both raise `ESCAPE expression must be a
    single character` - the length check is not skipped just because
    another operand is NULL; only a NULL *escape* operand itself
    suppresses it (see the test above)."""
    from historian.exec.expression import EvalError, evaluate

    with pytest.raises(EvalError):
        evaluate(_like(_lit(None), _lit("x"), escape=_lit("ab")), _ROW, _SCHEMA)
    with pytest.raises(EvalError):
        evaluate(_like(_lit("x"), _lit(None), escape=_lit("ab")), _ROW, _SCHEMA)


@pytest.mark.parametrize("bad_escape", ["", "ab"])
def test_like_escape_invalid_length_raises_eval_error_not_bare_exception(bad_escape):
    """sqlite3's own wording, reused verbatim: `ESCAPE expression must
    be a single character` - identical for both an empty string and a
    2+ character escape, confirmed: `select '10%' like '10!%' escape
    '';` and `select '10%' like '10!%' escape '!!';` both raise that
    exact message. A structured `EvalError` (`sql/expression.py`'s own
    class, same shape as `LexError`/`ParseError`/`BindError`), never a
    bare Python exception."""
    from historian.exec.expression import EvalError, evaluate

    like = _like(_lit("10%"), _lit("10!%"), escape=_lit(bad_escape))
    with pytest.raises(EvalError) as excinfo:
        evaluate(like, _ROW, _SCHEMA)
    assert "ESCAPE expression must be a single character" in str(excinfo.value)
    assert excinfo.value.position is like.escape.position


def test_like_escape_invalid_length_is_a_runtime_error_only_reachable_when_evaluated():
    """The single-character check is deliberately raised from inside
    `_eval_like` at evaluate() time, not at parse or bind time -
    confirmed live: `EXPLAIN select '10%' LIKE '10!%' ESCAPE '!!';`
    compiles cleanly in sqlite3, the failure only appears once the
    statement is actually stepped. Building the `Like` node and parsing
    it (already covered in tests/test_parser.py) never raises; only
    calling `evaluate()` on it does."""
    from historian.exec.expression import EvalError, evaluate

    like = _like(_lit("10%"), _lit("10!%"), escape=_lit("!!"))
    # Construction alone (the parse-time shape) raises nothing.
    assert isinstance(like, Like)
    with pytest.raises(EvalError):
        evaluate(like, _ROW, _SCHEMA)


def test_not_like_escape_composes_with_negation():
    """sqlite3: `select '10%' not like '10!%' escape '!';` -> 0,
    `select '10x' not like '10!%' escape '!';` -> 1 - `NOT LIKE ...
    ESCAPE` feeds into the existing `values.not3` wrapping, no separate
    reasoning path."""
    from historian.exec.expression import evaluate

    assert (
        evaluate(_like(_lit("10%"), _lit("10!%"), negated=True, escape=_lit("!")), _ROW, _SCHEMA)
        is False
    )
    assert (
        evaluate(_like(_lit("10x"), _lit("10!%"), negated=True, escape=_lit("!")), _ROW, _SCHEMA)
        is True
    )


def test_not_like_escape_invalid_length_still_raises_before_negation():
    """An invalid escape length under `NOT LIKE` still raises
    `EvalError` - the length check happens before negation, the same
    as `NULL` propagation already does for plain `NOT LIKE`."""
    from historian.exec.expression import EvalError, evaluate

    like = _like(_lit("10%"), _lit("10!%"), negated=True, escape=_lit("!!"))
    with pytest.raises(EvalError):
        evaluate(like, _ROW, _SCHEMA)


def test_like_escape_non_ascii_multibyte_single_codepoint_escape():
    """"Single character" is counted the same way SQLite's own
    `length()` counts it: Unicode code points, not UTF-8 bytes.
    sqlite3: `select length('😀');` -> 1, even though `😀` is 4 bytes in
    UTF-8. Confirmed both directions: `select '10😀%' LIKE '10😀é%'
    ESCAPE '😀';` -> 0 (`😀` escapes `%` to... no: escapes the following
    `é` to a literal, so the pattern needs a literal `é` the input
    lacks) and `select '10%' LIKE '10😀%' ESCAPE '😀';` -> 1 (`😀`
    escapes `%` to a literal, so the pattern becomes the literal string
    `10%`)."""
    from historian.exec.expression import evaluate

    assert (
        evaluate(_like(_lit("10😀%"), _lit("10😀é%"), escape=_lit("😀")), _ROW, _SCHEMA) is False
    )
    assert evaluate(_like(_lit("10%"), _lit("10😀%"), escape=_lit("😀")), _ROW, _SCHEMA) is True


def test_like_escape_operand_is_an_arbitrary_expression_not_just_a_literal():
    """#63's coercion applies to the escape operand exactly as it
    already does for `left`/`pattern`: a comparison-result (`Bool3`)
    escape operand goes through `coerce_to_value` before anything else
    touches it, becoming SQLite's `1`/`0`/`NULL` rather than Python's
    `True`/`False`/`None`. sqlite3: `select '10%' LIKE '10!%' ESCAPE
    (1=1);` -> 0 (escape coerces to text `"1"`; pattern `10!%` with
    escape `1` reinterprets its own `1` digit as the escape character,
    escaping the `0`, leaving effective pattern `0!` + wildcard, which
    `10%` does not start with) and `select '10%' LIKE '101%' ESCAPE
    (1=1);` -> 0 (same escape `"1"`; pattern `101%` becomes fully
    literal `0%`, which does not equal input `10%`). Also: `select '1'
    like '11' escape (1=1);` -> 1."""
    from historian.exec.expression import evaluate

    comparison_true = _bin(Operator.EQ, _lit(1), _lit(1))
    assert (
        evaluate(_like(_lit("10%"), _lit("10!%"), escape=comparison_true), _ROW, _SCHEMA)
        is False
    )
    assert (
        evaluate(_like(_lit("10%"), _lit("101%"), escape=comparison_true), _ROW, _SCHEMA)
        is False
    )
    assert evaluate(_like(_lit("1"), _lit("11"), escape=comparison_true), _ROW, _SCHEMA) is True


# --- LIKE ... ESCAPE with a column-reference operand (issue #51 follow-up) -
#
# The coordinator's own repro on this branch: `SELECT count(*) FROM
# blame WHERE 'a' LIKE 'a' ESCAPE author_name` used to raise
# `AssertionError: exec/expression.py: unhandled expression node type
# ColumnRef`, because `sql/binder.py`'s `Like` branch bound `left`/
# `pattern` but silently left `expr.escape` as a raw, unbound
# `ColumnRef` - a real bug, not the accepted gap the original grooming
# claimed. `Like.escape` is now bound in `_bind_expr` exactly like
# `left`/`pattern`, so a column-reference escape resolves to a
# `BoundColumnRef` and reaches `evaluate()`'s normal `BoundColumnRef`
# branch (`row[expr.offset]`) instead of its "unhandled expression node
# type" defensive `AssertionError`. `blame` has no single-character
# column, so the "one character" case below uses `_SCHEMA`'s own `s`
# column (`t(n INTEGER, s TEXT, r REAL)`, `_ROW = (5, '5', 5.0)` - `s`
# is the text `'5'`, one character) rather than a synthetic schema.
# `tests/differential/test_blame.py`'s
# `test_like_escape_column_operand_reruns_the_coordinators_repro` pins
# the same fix end to end, through the real parser/binder/evaluator
# pipeline the bug actually lived in - this section stays at the
# `evaluate()` level, on a hand-built, already-bound tree.


def test_like_escape_column_operand_one_character_is_used_as_the_escape():
    """`s` (`_ROW`'s text column) is `'5'`, one character - confirmed
    against sqlite3: `select '10%' like '105%' escape '5';` -> 1 (the
    `5` before `%` makes it a literal percent, exactly like the `!`-
    escape cases elsewhere in this file, just spelled with `s`'s own
    row value instead of a literal)."""
    from historian.exec.expression import evaluate

    assert evaluate(_like(_lit("10%"), _lit("105%"), escape=_col("s")), _ROW, _SCHEMA) is True


def test_like_escape_column_operand_wrong_length_raises_eval_error():
    """`r` (`_ROW`'s real column) coerces to the text `'5.0'`, three
    characters - confirmed against sqlite3: `select '10%' like '105%'
    escape '5.0';` raises `ESCAPE expression must be a single
    character`. Same error here, from a column-valued escape rather
    than a literal one."""
    from historian.exec.expression import EvalError, evaluate

    with pytest.raises(EvalError):
        evaluate(_like(_lit("10%"), _lit("105%"), escape=_col("r")), _ROW, _SCHEMA)


def test_like_escape_column_operand_null_row_value_is_null():
    """sqlite3 (`t(x text, esc text)`, `esc=NULL`): `select typeof(x
    like 'a!!b' escape esc) from t;` -> `null`, no error - the same
    `NULL`-propagates rule as a literal `NULL` escape, now reached via
    a column's actual row value rather than a `NULL` literal. Built
    from a small synthetic schema/row (not `_SCHEMA`/`_ROW`, which has
    no `NULL` column) purely for this one case."""
    from historian.exec.expression import evaluate

    schema = Schema(columns=(Column("x", ColumnType.TEXT), Column("esc", ColumnType.TEXT)))
    row: Row = ("a!b", None)
    esc_col = BoundColumnRef(offset=1, name="esc", position=_POS)
    assert evaluate(_like(_lit("a!b"), _lit("a!!b"), escape=esc_col), row, schema) is None


# --- AND / OR: where evaluation stops (issue #51, corrected by #111) ---
#
# #51 made AND/OR stop after a decided left operand everywhere. #111
# measured SQLite (the oracle, an exhaustive sweep in
# tests/differential/test_evaluation_order.py) and found the rule
# depends on where the expression is used:
#
#   select count(*) from t where p = 'zzz' and p like 'a' escape 'ab';
#       -> 0      condition context: FALSE left side, ESCAPE never runs
#   select (p = 'zzz' and p like 'a' escape 'ab') from t;
#       -> Error  value context: both operands evaluated
#
# `evaluate()` is value context, `evaluate_condition()` is the root of
# WHERE/HAVING. The leaf-counting tests further down pin every row of
# the rule; these keep #51's own cases, now in the context they hold in.

_POISON_LIKE = _like(_lit("a"), _lit("a"), escape=_lit("ab"))  # invalid escape length


def test_and_with_a_false_left_operand_stops_in_condition_context():
    """`WHERE FALSE AND <raises>` is `False`, the right operand never
    evaluated."""
    from historian.exec.expression import evaluate_condition

    assert evaluate_condition(And(_FALSE, _POISON_LIKE, _POS), _ROW, _SCHEMA) is False


@pytest.mark.parametrize("left", [_TRUE, _FALSE, _NULL])
def test_and_evaluates_both_operands_in_value_context(left):
    """`SELECT (<left> AND <raises>)` raises whatever the left side is."""
    from historian.exec.expression import EvalError, evaluate

    with pytest.raises(EvalError):
        evaluate(And(left, _POISON_LIKE, _POS), _ROW, _SCHEMA)


def test_and_with_a_true_left_operand_continues_in_condition_context():
    from historian.exec.expression import EvalError, evaluate_condition

    with pytest.raises(EvalError):
        evaluate_condition(And(_TRUE, _POISON_LIKE, _POS), _ROW, _SCHEMA)


def test_and_with_a_null_left_operand_stops_at_the_root_of_a_condition():
    """At the root of WHERE only TRUE keeps a row, and `NULL AND x` is
    never TRUE: sqlite3, `... where line_no = NULL and <raises>` -> no
    rows. The result is NULL, which drops the row."""
    from historian.exec.expression import evaluate_condition

    assert evaluate_condition(And(_NULL, _POISON_LIKE, _POS), _ROW, _SCHEMA) is None


def test_or_with_a_true_left_operand_stops_in_condition_context():
    from historian.exec.expression import evaluate_condition

    assert evaluate_condition(Or(_TRUE, _POISON_LIKE, _POS), _ROW, _SCHEMA) is True


@pytest.mark.parametrize("left", [_TRUE, _FALSE, _NULL])
def test_or_evaluates_both_operands_in_value_context(left):
    from historian.exec.expression import EvalError, evaluate

    with pytest.raises(EvalError):
        evaluate(Or(left, _POISON_LIKE, _POS), _ROW, _SCHEMA)


@pytest.mark.parametrize("left", [_FALSE, _NULL])
def test_or_with_a_false_or_null_left_operand_continues_at_the_root_of_a_condition(left):
    from historian.exec.expression import EvalError, evaluate_condition

    with pytest.raises(EvalError):
        evaluate_condition(Or(left, _POISON_LIKE, _POS), _ROW, _SCHEMA)


@pytest.mark.parametrize("entry", ["evaluate", "evaluate_condition"])
def test_and_or_still_match_the_three_valued_truth_table_when_nothing_raises(entry):
    """Neither context changes and3/or3's own truth table
    (tests/test_values.py) where every operand is evaluated - guarding
    against stopping too eagerly, e.g. on a NULL left side of OR, where
    the right side still decides the answer."""
    from historian.exec import expression

    run = getattr(expression, entry)
    assert run(And(_TRUE, _NULL, _POS), _ROW, _SCHEMA) is None
    assert run(And(_TRUE, _TRUE, _POS), _ROW, _SCHEMA) is True
    assert run(Or(_NULL, _TRUE, _POS), _ROW, _SCHEMA) is True
    assert run(Or(_NULL, _FALSE, _POS), _ROW, _SCHEMA) is None
    assert run(Or(_NULL, _NULL, _POS), _ROW, _SCHEMA) is None
    assert run(Or(_FALSE, _FALSE, _POS), _ROW, _SCHEMA) is False
    assert run(Not(Or(_NULL, _FALSE, _POS), _POS), _ROW, _SCHEMA) is None


def test_a_null_left_side_that_stops_a_condition_leaves_null_not_false():
    """`NULL AND FALSE` is FALSE as a value, but at the root of a
    condition the AND stops on the NULL and the result is NULL: the
    two differ only in a way no WHERE can see, since neither is TRUE.
    Under NOT, where NULL counts as TRUE, the AND does not stop."""
    from historian.exec.expression import evaluate, evaluate_condition

    assert evaluate(And(_NULL, _FALSE, _POS), _ROW, _SCHEMA) is False
    assert evaluate_condition(And(_NULL, _FALSE, _POS), _ROW, _SCHEMA) is None
    assert evaluate_condition(Not(And(_NULL, _FALSE, _POS), _POS), _ROW, _SCHEMA) is True


# --- Which leaves are evaluated (issue #111) -----------------------------
#
# Only an error makes evaluation order observable in a query, so a
# change that evaluated one leaf too many where nothing raises would
# pass every differential test. These count reads instead: each leaf is
# a bare column, and the row records which offsets were read, in order.
# Leaf values are 1 (TRUE), 0 (FALSE) and None (NULL). Every expected
# read list follows from the rule measured against the oracle (see
# tests/differential/test_evaluation_order.py and _docs/decisions.md,
# 2026-10-01):
#
# - value context reads every leaf;
# - in condition context AND stops after FALSE, OR after TRUE, and a
#   NULL left side stops AND where NULL counts as FALSE (an even number
#   of NOTs above, the WHERE root included) and OR where it counts as
#   TRUE (an odd number);
# - BETWEEN in condition context is `x >= low AND x <= high`, and NOT
#   BETWEEN is NOT over that;
# - IN stops at the first equal element in both contexts; a NULL
#   element or left side never stops it.

_T, _F, _N = 1, 0, None


class _CountingRow(tuple):
    """A row that records the offset of every column read."""

    def __new__(cls, cells):
        row = super().__new__(cls, cells)
        row.reads = []
        return row

    def __getitem__(self, index):
        self.reads.append(index)
        return super().__getitem__(index)


def _leaf(offset: int) -> BoundColumnRef:
    return BoundColumnRef(offset=offset, name=f"c{offset}", position=_POS)


_A, _B, _C, _D = _leaf(0), _leaf(1), _leaf(2), _leaf(3)

_COUNTING_SCHEMA = Schema(columns=tuple(Column(f"c{i}", ColumnType.INTEGER) for i in range(4)))


def _reads(entry: str, expr, *cells):
    """`(result, offsets read)` for *expr* over a row of *cells*, run
    through `evaluate` or `evaluate_condition`."""
    from historian.exec import expression

    row = _CountingRow(cells)
    result = getattr(expression, entry)(expr, row, _COUNTING_SCHEMA)
    return result, row.reads


def _and(left, right):
    return And(left, right, _POS)


def _or(left, right):
    return Or(left, right, _POS)


def _not(operand):
    return Not(operand, _POS)


@pytest.mark.parametrize("left", [_T, _F, _N])
@pytest.mark.parametrize("right", [_T, _F, _N])
@pytest.mark.parametrize(
    "build",
    [_and, _or, lambda l, r: _not(_and(l, r)), lambda l, r: _not(_or(l, r))],
    ids=["AND", "OR", "NOT AND", "NOT OR"],
)
def test_value_context_reads_every_leaf(build, left, right):
    """`SELECT (a AND b)`, `SELECT NOT (a OR b)`, ...: both leaves are
    read whatever their values."""
    _, reads = _reads("evaluate", build(_A, _B), left, right)
    assert reads == [0, 1]


@pytest.mark.parametrize(
    "build, left, expected_reads",
    [
        # At the root, NULL counts as FALSE.
        (_and, _F, [0]),
        (_and, _N, [0]),
        (_and, _T, [0, 1]),
        (_or, _T, [0]),
        (_or, _N, [0, 1]),
        (_or, _F, [0, 1]),
        # Under one NOT, NULL counts as TRUE.
        (lambda l, r: _not(_and(l, r)), _F, [0]),
        (lambda l, r: _not(_and(l, r)), _N, [0, 1]),
        (lambda l, r: _not(_and(l, r)), _T, [0, 1]),
        (lambda l, r: _not(_or(l, r)), _T, [0]),
        (lambda l, r: _not(_or(l, r)), _N, [0]),
        (lambda l, r: _not(_or(l, r)), _F, [0, 1]),
        # Under two, back to FALSE.
        (lambda l, r: _not(_not(_and(l, r))), _N, [0]),
        (lambda l, r: _not(_not(_or(l, r))), _N, [0, 1]),
        # The polarity reaches through AND/OR to nested operands.
        (lambda l, r: _and(_D, _not(_and(l, r))), _N, [3, 0, 1]),
        (lambda l, r: _or(_not(_or(l, r)), _D), _N, [0, 3]),
    ],
)
def test_condition_context_stops_where_sqlite_does(build, left, expected_reads):
    _, reads = _reads("evaluate_condition", build(_A, _B), left, _T, _F, _T)
    assert reads == expected_reads


@pytest.mark.parametrize("left", [_T, _F, _N])
@pytest.mark.parametrize("right", [_T, _F, _N])
@pytest.mark.parametrize(
    "build",
    [_and, _or, lambda l, r: _not(_and(l, r)), lambda l, r: _not(_or(l, r))],
    ids=["AND", "OR", "NOT AND", "NOT OR"],
)
def test_condition_context_keeps_exactly_the_rows_value_context_keeps(build, left, right):
    """Stopping early never changes which rows a WHERE keeps: the
    condition result is TRUE exactly when the value result is."""
    from historian import values
    from historian.exec.expression import coerce_to_bool3

    condition, _ = _reads("evaluate_condition", build(_A, _B), left, right)
    value, _ = _reads("evaluate", build(_A, _B), left, right)
    assert values.is_true(coerce_to_bool3(condition)) == values.is_true(coerce_to_bool3(value))


@pytest.mark.parametrize(
    "wrap",
    [
        lambda e: _bin(Operator.EQ, e, _lit(0)),
        lambda e: _is(e, _lit(None), negated=True),
        lambda e: _bin(Operator.ADD, e, _lit(0)),
        lambda e: _bin(Operator.CONCAT, e, _lit("x")),
        lambda e: _like(e, _lit("0")),
        lambda e: _between(e, _lit(0), _lit(1)),
        lambda e: _in(_lit(7), (e,)),
        lambda e: _in(e, (_lit(7),)),
        lambda e: _unary(UnaryOperator.POS, e),
        lambda e: _unary(UnaryOperator.NEG, e),
    ],
    ids=["=", "IS NOT", "+", "||", "LIKE", "BETWEEN", "IN element", "IN left", "unary +", "unary -"],
)
def test_an_operand_of_any_other_operator_is_value_context_inside_a_condition(wrap):
    """`WHERE (a AND b) = 0`, `WHERE +(a AND b)`, ...: the AND is an
    operand, not the condition, so both leaves are read even though the
    left one is FALSE. (`BETWEEN` reads its operand twice, #137.)"""
    _, reads = _reads("evaluate_condition", wrap(_and(_A, _B)), _F, _T)
    assert reads[:2] == [0, 1]
    assert set(reads) == {0, 1}


@pytest.mark.parametrize("entry", ["evaluate", "evaluate_condition"])
@pytest.mark.parametrize("negated", [False, True])
@pytest.mark.parametrize(
    "cells, expected_reads",
    [
        # Left side, then each element in turn, the left side read again
        # per element (#137).
        ((1, 1, 2, 3), [0, 1]),
        ((2, 1, 2, 3), [0, 1, 0, 2]),
        ((3, 1, 2, 3), [0, 1, 0, 2, 0, 3]),
        ((4, 1, 2, 3), [0, 1, 0, 2, 0, 3]),
        # A NULL element does not stop it; the match after it does.
        ((2, None, 2, 3), [0, 1, 0, 2]),
        # A NULL left side matches nothing and stops nothing.
        ((None, 1, 2, 3), [0, 1, 0, 2, 0, 3]),
    ],
)
def test_in_stops_at_the_first_match_in_both_contexts(entry, negated, cells, expected_reads):
    _, reads = _reads(entry, _in(_A, (_B, _C, _D), negated=negated), *cells)
    assert reads == expected_reads


@pytest.mark.parametrize("entry", ["evaluate", "evaluate_condition"])
def test_in_results_are_unchanged_by_stopping(entry):
    """sqlite3: `5 IN (5, NULL)` -> 1, `6 IN (5, NULL)` -> NULL, `6 IN
    (5, 7)` -> 0, `NULL IN (5)` -> NULL, and NOT IN their negations."""
    expr = _in(_A, (_B, _C))
    assert _reads(entry, expr, 5, 5, None)[0] is True
    assert _reads(entry, expr, 6, 5, None)[0] is None
    assert _reads(entry, expr, 6, 5, 7)[0] is False
    assert _reads(entry, expr, None, 5, 7)[0] is None
    negated = _in(_A, (_B, _C), negated=True)
    assert _reads(entry, negated, 5, 5, None)[0] is False
    assert _reads(entry, negated, 6, 5, None)[0] is None
    assert _reads(entry, negated, 6, 5, 7)[0] is True


def test_empty_in_reads_nothing_in_condition_context():
    assert _reads("evaluate_condition", _in(_A, ()), 1) == (False, [])
    assert _reads("evaluate_condition", _in(_A, (), negated=True), 1) == (True, [])


def test_empty_in_reads_its_left_side_in_value_context():
    """On the pinned oracle a value-context `x IN ()` still evaluates
    `x` (so an error in it surfaces); 3.45.1 did not (#117)."""
    assert _reads("evaluate", _in(_A, ()), 1) == (False, [0])
    assert _reads("evaluate", _in(_A, (), negated=True), 1) == (True, [0])


@pytest.mark.parametrize("negated", [False, True])
@pytest.mark.parametrize("cells", [(5, 10, 20), (5, None, 20), (5, 0, 20), (None, 0, 20)])
def test_between_reads_every_operand_in_value_context(negated, cells):
    """The operand, the low bound, the operand again (#137), the high
    bound."""
    _, reads = _reads("evaluate", _between(_A, _B, _C, negated=negated), *cells)
    assert reads == [0, 1, 0, 2]


@pytest.mark.parametrize(
    "negated, outer_not, cells, expected_reads",
    [
        # `x >= low` FALSE stops it in every polarity.
        (False, False, (5, 10, 20), [0, 1]),
        (True, False, (5, 10, 20), [0, 1]),
        (False, True, (5, 10, 20), [0, 1]),
        # NULL stops it where NULL counts as FALSE: plain BETWEEN at the
        # root, or NOT BETWEEN under one NOT.
        (False, False, (5, None, 20), [0, 1]),
        (True, True, (5, None, 20), [0, 1]),
        (False, False, (None, 0, 20), [0, 1]),
        # ... and not where it counts as TRUE.
        (True, False, (5, None, 20), [0, 1, 0, 2]),
        (False, True, (5, None, 20), [0, 1, 0, 2]),
        # TRUE never stops it.
        (False, False, (5, 0, 20), [0, 1, 0, 2]),
        (True, False, (5, 0, 20), [0, 1, 0, 2]),
    ],
)
def test_between_stops_like_and_in_condition_context(negated, outer_not, cells, expected_reads):
    expr = _between(_A, _B, _C, negated=negated)
    if outer_not:
        expr = _not(expr)
    _, reads = _reads("evaluate_condition", expr, *cells)
    assert reads == expected_reads


@pytest.mark.parametrize("negated", [False, True])
@pytest.mark.parametrize("outer_not", [False, True])
@pytest.mark.parametrize("operand", [5, None])
@pytest.mark.parametrize("low", [0, 5, 10, None])
@pytest.mark.parametrize("high", [0, 5, 10, None])
def test_between_keeps_exactly_the_rows_value_context_keeps(negated, outer_not, operand, low, high):
    from historian import values
    from historian.exec.expression import coerce_to_bool3

    expr = _between(_A, _B, _C, negated=negated)
    if outer_not:
        expr = _not(expr)
    condition, _ = _reads("evaluate_condition", expr, operand, low, high)
    value, _ = _reads("evaluate", expr, operand, low, high)
    assert values.is_true(coerce_to_bool3(condition)) == values.is_true(coerce_to_bool3(value))


# --- IN: per-element affinity, or3-folded, values.not3 for NOT IN -------


def _in(left, values_, negated=False) -> In:
    return In(left=left, values=tuple(values_), negated=negated, position=_POS)


def test_in_applies_affinity_to_each_element_independently():
    """sqlite3 (`n INT`, `n=5`): `n IN ('5', '6')` -> 1, `n IN ('5',
    'abc')` -> 1 (the 'abc' element fails to convert and simply
    doesn't match - no error)."""
    from historian.exec.expression import evaluate

    assert evaluate(_in(_col("n"), [_lit("5"), _lit("6")]), _ROW, _SCHEMA) is True
    assert evaluate(_in(_col("n"), [_lit("5"), _lit("abc")]), _ROW, _SCHEMA) is True


def test_in_null_propagation_matches_or3_folding():
    """sqlite3: `5 IN (5, NULL)` -> 1 (short-circuits: the first
    element already matches); `6 IN (5, NULL)` -> NULL (no element
    matches, but a NULL element means "maybe", not "no")."""
    from historian.exec.expression import evaluate

    assert evaluate(_in(_lit(5), [_lit(5), _lit(None)]), _ROW, _SCHEMA) is True
    assert evaluate(_in(_lit(6), [_lit(5), _lit(None)]), _ROW, _SCHEMA) is None


def test_not_in_is_not3_of_the_unnegated_result():
    """sqlite3: `select 6 not in (5, NULL);` -> NULL, not TRUE -
    `NOT NULL` is `NULL`, confirming NOT IN is `values.not3` applied to
    the un-negated IN result, never a separately reasoned-out
    negation."""
    from historian.exec.expression import evaluate

    assert evaluate(_in(_lit(6), [_lit(5), _lit(None)], negated=True), _ROW, _SCHEMA) is None
    assert evaluate(_in(_lit(6), [_lit(5), _lit(7)], negated=True), _ROW, _SCHEMA) is True


def test_in_empty_list_is_always_false():
    """`sql/ast.py`'s own docstring: "`IN ()` is valid SQL, always
    false - confirmed against `sqlite3`."."""
    from historian.exec.expression import evaluate

    assert evaluate(_in(_lit(5), []), _ROW, _SCHEMA) is False


# --- IN: a list element has no affinity of its own, ever (issue #47) ----
#
# sqlite3, `t(n INTEGER, s TEXT, r REAL)`, row `(5, '5', 5.0)`:
#
#   select '5' in (n), '5' not in (n);             -> 0|1
#   select '5' in (n, 99), '5' in (99, n);          -> 0|0
#   select '5' in (r), '5.0' in (r);                -> 0|0
#   select 5 in (s), 5 in (s, 99);                  -> 0|0
#   select (n+0) in ('5');                          -> 0
#
# Independently re-verified during this issue's own work, matching the
# grooming comment's evidence table exactly.


def test_in_list_element_that_is_a_bare_column_has_no_affinity():
    """sqlite3 (`n INT`, `n=5`): `'5' IN (n)` -> 0, `'5' NOT IN (n)` ->
    1. This is the exact shape from the shipped CLI defect (`'1' IN
    (line_no)` returning 42 rows instead of 0): naively applying `=`'s
    per-operand affinity rule to the list element converts '5' to the
    column's own type and gets this backwards. The list element
    contributes no affinity of its own, regardless of being a bare
    `BoundColumnRef`."""
    from historian.exec.expression import evaluate

    assert evaluate(_in(_lit("5"), [_col("n")]), _ROW, _SCHEMA) is False
    assert evaluate(_in(_lit("5"), [_col("n")], negated=True), _ROW, _SCHEMA) is True


def test_in_list_element_affinity_is_order_independent():
    """sqlite3 (`n INT`, `n=5`): `'5' IN (n, 99)` -> 0, `'5' IN (99, n)`
    -> 0 - position within the list doesn't matter, and the numeric
    literal element 99 doesn't leak affinity onto n either."""
    from historian.exec.expression import evaluate

    assert evaluate(_in(_lit("5"), [_col("n"), _lit(99)]), _ROW, _SCHEMA) is False
    assert evaluate(_in(_lit("5"), [_lit(99), _col("n")]), _ROW, _SCHEMA) is False


def test_in_list_element_no_affinity_for_real_column():
    """sqlite3 (`r REAL`, `r=5.0`): `'5' IN (r)` -> 0, `'5.0' IN (r)` ->
    0 - REAL columns are affected identically to INTEGER, not a special
    case."""
    from historian.exec.expression import evaluate

    assert evaluate(_in(_lit("5"), [_col("r")]), _ROW, _SCHEMA) is False
    assert evaluate(_in(_lit("5.0"), [_col("r")]), _ROW, _SCHEMA) is False


def test_in_list_element_no_affinity_mirror_direction():
    """sqlite3 (`s TEXT`, `s='5'`): `5 IN (s)` -> 0, `5 IN (s, 99)` ->
    0 - the mirror direction, a numeric left operand with a TEXT
    column inside the list."""
    from historian.exec.expression import evaluate

    assert evaluate(_in(_lit(5), [_col("s")]), _ROW, _SCHEMA) is False
    assert evaluate(_in(_lit(5), [_col("s"), _lit(99)]), _ROW, _SCHEMA) is False


def test_in_applies_affinity_to_each_element_independently_still_passes():
    """Same shape as the pre-existing
    `test_in_applies_affinity_to_each_element_independently`
    (`n` on the *left* of IN), restated here to make explicit that this
    issue's fix does not touch that direction: sqlite3 (`n INT`, `n=5`):
    `n IN ('5', '6')` -> 1, `n IN ('5', 'abc')` -> 1."""
    from historian.exec.expression import evaluate

    assert evaluate(_in(_col("n"), [_lit("5"), _lit("6")]), _ROW, _SCHEMA) is True
    assert evaluate(_in(_col("n"), [_lit("5"), _lit("abc")]), _ROW, _SCHEMA) is True


def test_in_computed_left_operand_still_has_no_affinity():
    """sqlite3 (`n INT`, `n=5`): `(n + 0) IN ('5')` -> 0 - matches `=`'s
    existing behaviour for computed operands: the left side is a
    `BinaryOp`, not a bare `BoundColumnRef`, so it contributes no
    affinity and class-rank comparison never matches."""
    from historian.exec.expression import evaluate

    computed = _bin(Operator.ADD, _col("n"), _lit(0))
    assert evaluate(_in(computed, [_lit("5")]), _ROW, _SCHEMA) is False


def test_in_null_column_element_propagates_null_not_false():
    """sqlite3 (`n INTEGER`, `n=NULL`), `.nullvalue NULL`:
    `select '5' in (n);` -> NULL, `select '5' in (n, 99);` -> NULL,
    `select '5' not in (n);` -> NULL. A bare-column list element that
    is itself NULL at the row is not treated specially for being a
    column - it must still propagate NULL, not collapse to FALSE. Every
    existing NULL-in-IN test above (`test_in_null_propagation_matches_or3_folding`,
    `test_not_in_is_not3_of_the_unnegated_result`) uses `Literal`
    elements only, so none of them exercises a NULL element that also
    carries a declared column type - exactly the combination the bug
    lived in."""
    from historian.exec.expression import evaluate

    null_row: Row = (None, "5", 5.0)
    assert evaluate(_in(_lit("5"), [_col("n")]), null_row, _SCHEMA) is None
    assert evaluate(_in(_lit("5"), [_col("n"), _lit(99)]), null_row, _SCHEMA) is None
    assert evaluate(_in(_lit("5"), [_col("n")], negated=True), null_row, _SCHEMA) is None


def test_in_versus_between_diverge_on_the_same_operand_shape():
    """sqlite3 (`n INTEGER`, `n=1`): `select '1' between n and n;` -> 1
    (each bound independently carries its own affinity, symmetric with
    `=`), while `select '1' in (n);` -> 0 for the same row. The two
    operators look structurally identical in this module - both walk
    operand pairs through `_evaluate_affinity_pair` - but must not be
    unified: BETWEEN's bounds are not "a list" in SQLite's own terms,
    and this fix must not touch `_eval_between` or generalize the
    shared helper in a way that would also strip BETWEEN's per-bound
    affinity."""
    from historian.exec.expression import evaluate

    one_row: Row = (1, "5", 5.0)
    assert evaluate(_between(_lit("1"), _col("n"), _col("n")), one_row, _SCHEMA) is True
    assert evaluate(_in(_lit("1"), [_col("n")]), one_row, _SCHEMA) is False


# --- BETWEEN: and3(ge(x, low), le(x, high)), affinity per bound ---------


def _between(operand, low, high, negated=False) -> Between:
    return Between(operand=operand, low=low, high=high, negated=negated, position=_POS)


def test_between_applies_affinity_to_each_bound_independently():
    """sqlite3 (`n INT`, `n=5`): `n BETWEEN '1' AND '10'` -> 1."""
    from historian.exec.expression import evaluate

    result = evaluate(_between(_col("n"), _lit("1"), _lit("10")), _ROW, _SCHEMA)
    assert result is True


def test_between_null_propagation_matches_and3_short_circuit():
    """sqlite3: `select 20 between 30 and NULL;` -> FALSE, not NULL -
    the first comparison (`20 >= 30`) alone is already `FALSE`, and
    `and3(FALSE, NULL)` is `FALSE`, not `NULL`."""
    from historian.exec.expression import evaluate

    result = evaluate(_between(_lit(20), _lit(30), _lit(None)), _ROW, _SCHEMA)
    assert result is False


def test_not_between_is_not3_of_the_unnegated_result():
    from historian.exec.expression import evaluate

    assert evaluate(_between(_lit(5), _lit(1), _lit(10), negated=True), _ROW, _SCHEMA) is False
    assert evaluate(_between(_lit(50), _lit(1), _lit(10), negated=True), _ROW, _SCHEMA) is True


def test_not_between_with_satisfied_low_and_null_high_is_null():
    """sqlite3: `select 5 not between 1 and NULL is null;` -> `1`.

    The one shape that tells real `and3(...)`-then-`not3` apart from a
    `bool(a and b)`-then-`not3` mutant: the low bound (`5 >= 1`) is
    `TRUE`, so Python's `and` does not short-circuit on the left the
    way it does in `test_between_null_propagation_matches_and3_short_circuit`
    above, and instead evaluates the high bound (`5 <= NULL`), which is
    `None`. The real `and3(True, None)` is `None`, and
    `not3(None)` is `None`. The mutant instead collapses to
    `bool(True and None)` = `bool(None)` = `False`, and `not3(False)`
    is `True` - wrongly keeping the row instead of dropping it."""
    from historian.exec.expression import evaluate

    result = evaluate(_between(_lit(5), _lit(1), _lit(None), negated=True), _ROW, _SCHEMA)
    assert result is None


# --- coerce_to_value: Bool3 -> Value, Project's own coercion (#38) -------
#
# sqlite3: `select 1 = 1, typeof(1 = 1), 1 = 2, typeof(1 = 2), 1 = null,
# typeof(1 = null);` -> `1|integer|0|integer||null`.


def test_coerce_to_value_converts_true_to_the_int_one():
    from historian.exec.expression import coerce_to_value

    result = coerce_to_value(True)
    assert result == 1
    assert type(result) is int


def test_coerce_to_value_converts_false_to_the_int_zero():
    from historian.exec.expression import coerce_to_value

    result = coerce_to_value(False)
    assert result == 0
    assert type(result) is int


def test_coerce_to_value_passes_none_through_unchanged():
    """NULL means the same thing on both sides of this boundary -
    `values.py`'s own "Two representations, both using None" design -
    so it needs no direction-specific handling at all."""
    from historian.exec.expression import coerce_to_value

    assert coerce_to_value(None) is None


def test_coerce_to_value_passes_a_value_shaped_result_through_unchanged():
    """An `int`/`float`/`str` `evaluate()` result is already the right
    SQLite value and needs no conversion - only a `Bool3` does."""
    from historian.exec.expression import coerce_to_value

    assert coerce_to_value(5) == 5 and type(coerce_to_value(5)) is int
    assert coerce_to_value(5.0) == 5.0 and type(coerce_to_value(5.0)) is float
    assert coerce_to_value("x") == "x"


def test_coerce_to_value_applied_to_a_real_comparison_evaluate_result():
    """End to end through `evaluate()`: `1 = 1`, `1 = 2`, `1 = NULL` -
    `coerce_to_value(evaluate(...))` matches `sqlite3`'s own
    `1`/`0`/`NULL`, not Python's `True`/`False`/`None`. Checked with
    `type(...) is int`, not merely `== 1`/`== 0` - `True == 1` in
    Python, so a bare `==` assertion would be blind to this issue's
    own bug (per this issue's own acceptance criteria)."""
    from historian.exec.expression import coerce_to_value, evaluate

    eq_true = _bin(Operator.EQ, _lit(1), _lit(1))
    eq_false = _bin(Operator.EQ, _lit(1), _lit(2))
    eq_null = _bin(Operator.EQ, _lit(1), _lit(None))

    true_result = coerce_to_value(evaluate(eq_true, _ROW, _SCHEMA))
    false_result = coerce_to_value(evaluate(eq_false, _ROW, _SCHEMA))
    null_result = coerce_to_value(evaluate(eq_null, _ROW, _SCHEMA))

    assert (true_result, type(true_result)) == (1, int)
    assert (false_result, type(false_result)) == (0, int)
    assert null_result is None


# --- coerce_to_bool3: Value -> Bool3, Filter's own coercion (#38) --------
#
# Leading-prefix numeric coercion (the same rule `arithmetic_operand`
# already implements for arithmetic), then `!= 0` - confirmed case by
# case against `sqlite3` in issue #38's own body, not "nonempty string
# is truthy".


def test_coerce_to_bool3_passes_true_and_false_through_unchanged():
    from historian.exec.expression import coerce_to_bool3

    assert coerce_to_bool3(True) is True
    assert coerce_to_bool3(False) is False


def test_coerce_to_bool3_passes_none_through_unchanged():
    """NULL needs no direction-specific handling - see
    `test_coerce_to_value_passes_none_through_unchanged`'s docstring,
    same reasoning in the other direction."""
    from historian.exec.expression import coerce_to_bool3

    assert coerce_to_bool3(None) is None


def test_coerce_to_bool3_nonzero_and_zero_integer():
    from historian.exec.expression import coerce_to_bool3

    assert coerce_to_bool3(3) is True
    assert coerce_to_bool3(-3) is True
    assert coerce_to_bool3(0) is False


def test_coerce_to_bool3_real_zero_and_underflowed_real_are_falsy():
    """sqlite3: `create table t(r real); insert into t values(0.0);
    select 'kept' from t where r;` -> no rows. Same for `1e-400`,
    which underflows to `0.0` in a double before this function ever
    sees it - `typeof(r)` is still `real`, not `integer`."""
    from historian.exec.expression import coerce_to_bool3

    assert coerce_to_bool3(0.0) is False
    assert coerce_to_bool3(1e-400) is False


def test_coerce_to_bool3_nonzero_real_is_truthy():
    """sqlite3: `create table t(r real); insert into t values
    (0.4),(-0.4),(0.5); select r,'kept' from t where r;` -> all three
    kept, negative included."""
    from historian.exec.expression import coerce_to_bool3

    assert coerce_to_bool3(0.4) is True
    assert coerce_to_bool3(-0.4) is True
    assert coerce_to_bool3(0.5) is True


def test_coerce_to_bool3_text_uses_leading_prefix_rule_not_nonempty_string_rule():
    """sqlite3, case by case (issue #38's own body):
    `'0.0'` -> falsy (the whole string parses to numeric `0.0`);
    `'  1  '` -> truthy (whitespace-trimmed leading-prefix parse gives
    `1`); `'1abc'` -> truthy (leading-prefix parse gives `1` - proves
    this is arithmetic's leading-prefix rule, not affinity's stricter
    whole-string rule, which would leave `'1abc'` as unconverted text);
    `'0abc'` -> falsy, the case that actually distinguishes the
    leading-prefix rule from a wrong "any nonempty string is truthy"
    hypothesis, under which `'0abc'` would wrongly be kept; `''` and
    `'abc'` -> falsy, no digit anywhere, coerce to `0` (the same rule
    arithmetic uses: `'abc'+1` is `1`)."""
    from historian.exec.expression import coerce_to_bool3

    assert coerce_to_bool3("0.0") is False
    assert coerce_to_bool3("  1  ") is True
    assert coerce_to_bool3("1abc") is True
    assert coerce_to_bool3("0abc") is False
    assert coerce_to_bool3("") is False
    assert coerce_to_bool3("abc") is False


def test_coerce_to_bool3_applied_to_a_real_bare_column_evaluate_result():
    """End to end through `evaluate()`: a bare `BoundColumnRef` is
    value-shaped, so `evaluate()` returns the row's raw `n=5`, never a
    `Bool3` - `coerce_to_bool3` gives it truthiness (`5 != 0`,
    `True`), matching `sqlite3`'s `select x from t where x` keeping a
    nonzero numeric column."""
    from historian.exec.expression import coerce_to_bool3, evaluate

    assert coerce_to_bool3(evaluate(_col("n"), _ROW, _SCHEMA)) is True


def test_coerce_to_bool3_applied_to_a_computed_null_valued_expression():
    """A `NULL`-valued *value-shaped* expression, not a comparison:
    `NULL + n` is `NULL` for every row (arithmetic propagates NULL).
    `coerce_to_bool3` passes the `None` through unchanged, and
    `values.is_true` then drops the row exactly like any other `NULL`
    predicate - no crash, no special-casing needed here."""
    from historian.exec.expression import coerce_to_bool3, evaluate

    null_plus_n = _bin(Operator.ADD, _lit(None), _col("n"))

    assert coerce_to_bool3(evaluate(null_plus_n, _ROW, _SCHEMA)) is None


# --- Code-level enforcement: no stray float() outside named exceptions --


def test_no_stray_float_calls_outside_the_named_exceptions():
    """The 2026-08-27 decision: comparison must never route through
    `float()` - past 2^53 that loses an `int`'s exact value and can
    reverse the answer
    (`test_2_53_boundary_comparison_stays_exact_through_a_bound_column_ref`
    above is the *value* pin for this rule; this is the *code-level*
    pin, matching how `tests/test_values.py` pins the 2^53 case for
    `values.py`).

    This code-level walk only catches an explicit `float(` call site
    appearing somewhere it isn't allowed - it cannot see an *implicit*
    float route, such as `math.trunc(left / right)`, which reaches the
    same lossy division with no `float(` call anywhere in the source
    for this walk to find. That route is instead caught by the
    value-level `test_truncating_division_never_routes_through_float`,
    which is the actual guard against it (re-verified live, issue
    #50: replacing `_truncating_int_div`'s body with
    `math.trunc(left / right)` passes this AST walk unchanged and is
    only caught by that other test).

    Walks this module's own source with `ast`, and asserts every
    `float(` call site sits inside a function on an explicit allowlist
    of three. Text-to-number *conversion* is not on it: since issue
    #134, `_scan_number` converts text to a REAL with
    `historian.atof.text_to_real` (SQLite's own, not correctly rounded,
    algorithm), never `float()`, so a `float(` call there now fails
    this test too.

    - `_format_float` - the float-formatting helper for
      `||`/text-affinity, converting a number *to* text, never used to
      convert a number *for* comparison. Named here because the issue
      names it, even though this implementation's `_format_float`
      happens not to call `float()` itself (it only formats an
      already-`float` argument) - nothing about this test should
      depend on that being true if a future change makes it call
      `float()` too, e.g. to normalize an int argument.
    - `_int64_bounded` - an exception the int64-overflow criteria
      require: `9223372036854775807 + 1` must become
      `REAL`, which needs converting an already-overflowed *exact*
      Python `int` (computed first with unbounded `int` arithmetic) to
      `float`. This is arithmetic *result production*, a different
      path from comparison and never confused with it per this issue's
      own "applies only to arithmetic result production, never to
      comparison" criterion - kept true structurally by living in its
      own function, never called from `_apply_affinity`, `_eval_is`,
      `_eval_in`, `_eval_between`, or the comparison branch of
      `_eval_binary`.
    - `_mod_result` - the same "arithmetic result
      production" shape as `_int64_bounded`, added for `%` (issue
      #75): `%`'s remainder is always computed as an exact `int`, but
      its storage class follows the *original* operands (REAL if
      either was REAL) - reporting a REAL result needs converting that
      exact `int` remainder to `float`, kept in its own function for
      the same reason `_int64_bounded` is.

    Any `float(` call appearing anywhere else in this module - most
    plausibly, a future "normalize this before comparing" edit to the
    comparison path - fails this test.
    """
    import ast
    import inspect

    from historian.exec import expression

    allowed_functions = {"_format_float", "_int64_bounded", "_mod_result"}
    tree = ast.parse(inspect.getsource(expression))

    violations: list[tuple[str, int]] = []

    class _FloatCallVisitor(ast.NodeVisitor):
        def __init__(self):
            self._function_stack: list[str] = []

        def visit_FunctionDef(self, node: ast.FunctionDef):
            self._function_stack.append(node.name)
            self.generic_visit(node)
            self._function_stack.pop()

        def visit_Call(self, node: ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id == "float":
                enclosing = self._function_stack[-1] if self._function_stack else "<module>"
                if enclosing not in allowed_functions:
                    violations.append((enclosing, node.lineno))
            self.generic_visit(node)

    _FloatCallVisitor().visit(tree)
    assert violations == []


# --- Issue #63: Bool3 -> Value reverse coercion at a nested operand site -
#
# #38 fixed the two root boundaries (a select-list root, a WHERE/HAVING
# predicate root). This is one recursion level deeper: a predicate-shaped
# result (a comparison, IS, LIKE, IN, BETWEEN) reaching a *nested*
# Value-requiring operand of another node - one unit case per row of this
# issue's own audit table, each confirmed against sqlite3 3.51.0 in the
# issue body. `_TRUE`/`_FALSE`/`_NULL` (defined above, near the AND/OR/NOT
# section) are reused throughout: each is a real comparison `BinaryOp`
# node, not a hand-fed Python bool, so `evaluate()` genuinely dispatches
# through the comparison branch and produces a real `bool` the way a
# predicate-shaped subexpression actually would.


def test_arithmetic_operand_raises_on_a_raw_bool_true():
    """Defensive guard, mirroring the exact pattern `values.py`'s own
    `_rank` and this module's own `_coerce_to_text` already use: a raw
    Python `bool` must never reach arithmetic. Not reachable through
    `evaluate()` itself once the call-site coercion below is in place -
    this pins the guard directly, at the unit most likely to catch a
    future regression that removes the call-site coercion without also
    removing this defensive check."""
    from historian.exec.expression import arithmetic_operand

    with pytest.raises(TypeError):
        arithmetic_operand(True)


def test_arithmetic_operand_raises_on_a_raw_bool_false():
    from historian.exec.expression import arithmetic_operand

    with pytest.raises(TypeError):
        arithmetic_operand(False)


def test_comparison_nested_predicate_operand_left_side_converts_to_sqlite_int():
    """sqlite3: `select (1=1) = 1;` -> 1. `(1=1)` is itself a
    comparison `BinaryOp` nested as the left operand of another
    comparison - before this issue's fix, `_evaluate_affinity_pair`
    handed `values.eq` a raw Python `True`, which raised."""
    from historian.exec.expression import evaluate

    result = evaluate(_bin(Operator.EQ, _TRUE, _lit(1)), _ROW, _SCHEMA)
    assert result is True


def test_comparison_nested_predicate_operand_ne():
    """sqlite3: `select (1=1) <> 1;` -> 0."""
    from historian.exec.expression import evaluate

    result = evaluate(_bin(Operator.NE, _TRUE, _lit(1)), _ROW, _SCHEMA)
    assert result is False


def test_is_nested_predicate_operand_left_side():
    """sqlite3: `select (1=1) is 1;` -> 1."""
    from historian.exec.expression import evaluate

    assert evaluate(_is(_TRUE, _lit(1)), _ROW, _SCHEMA) is True


def test_is_nested_predicate_operand_right_side():
    """sqlite3: `select 1 is (1=1);` -> 1."""
    from historian.exec.expression import evaluate

    assert evaluate(_is(_lit(1), _TRUE), _ROW, _SCHEMA) is True


def test_concat_nested_predicate_operand():
    """sqlite3: `select (1=1) || 'x';` -> '1x'."""
    from historian.exec.expression import evaluate

    result = evaluate(_bin(Operator.CONCAT, _TRUE, _lit("x")), _ROW, _SCHEMA)
    assert result == "1x"


def test_like_nested_predicate_operand_left_side():
    """sqlite3: `select (1=1) like '1';` -> 1 - the left side of LIKE."""
    from historian.exec.expression import evaluate

    assert evaluate(_like(_TRUE, _lit("1")), _ROW, _SCHEMA) is True


def test_like_nested_predicate_operand_pattern_side():
    """sqlite3: `select '1' like (1=1);` -> 1 - the pattern side of
    LIKE."""
    from historian.exec.expression import evaluate

    assert evaluate(_like(_lit("1"), _TRUE), _ROW, _SCHEMA) is True


def test_like_nested_predicate_operand_discriminates_against_str_bool():
    """sqlite3: `select 'True' like (1=1);` -> 0. A conversion that
    went through `str(bool)` (`str(True)` = `'True'`) would wrongly
    match here - SQLite's own `'1'` text spelling of TRUE does not."""
    from historian.exec.expression import evaluate

    assert evaluate(_like(_lit("True"), _TRUE), _ROW, _SCHEMA) is False


def test_in_nested_predicate_operand_left_side():
    """sqlite3: `select (1=1) in (1,2);` -> 1."""
    from historian.exec.expression import evaluate

    assert evaluate(_in(_TRUE, [_lit(1), _lit(2)]), _ROW, _SCHEMA) is True


def test_in_nested_predicate_operand_list_element():
    """sqlite3: `select 1 in (1=1, 2);` -> 1 - the list element, not
    the left operand."""
    from historian.exec.expression import evaluate

    assert evaluate(_in(_lit(1), [_TRUE, _lit(2)]), _ROW, _SCHEMA) is True


def test_between_nested_predicate_operand_itself():
    """sqlite3: `select (1=1) between 0 and 2;` -> 1."""
    from historian.exec.expression import evaluate

    assert evaluate(_between(_TRUE, _lit(0), _lit(2)), _ROW, _SCHEMA) is True


def test_between_nested_predicate_low_bound():
    """sqlite3: `select 1 between (1=1) and 5;` -> 1 - the low bound,
    coerced to 1."""
    from historian.exec.expression import evaluate

    assert evaluate(_between(_lit(1), _TRUE, _lit(5)), _ROW, _SCHEMA) is True


def test_between_nested_predicate_high_bound():
    """sqlite3: `select 1 between 0 and (1=1);` -> 1 - the high bound,
    coerced to 1."""
    from historian.exec.expression import evaluate

    assert evaluate(_between(_lit(1), _lit(0), _TRUE), _ROW, _SCHEMA) is True


def test_arithmetic_on_a_nested_predicate_operand_still_returns_eleven():
    """sqlite3: `select (1=1)+10, typeof((1=1)+10);` -> 11|integer.
    `(1=1)+10` computed via an explicit `Bool3 -> Value` conversion
    first and computed by handing Python's raw `True` straight to `+`
    produce the identical object - `11`, an `int` - so this assertion
    alone cannot discriminate the deliberate fix from the accident
    (see this issue's own "Arithmetic" section). It only discriminates
    in combination with `test_arithmetic_operand_raises_on_a_raw_bool_true`
    above: with the defensive guard in `arithmetic_operand` in place,
    deleting the explicit `coerce_to_value()` call at this call site
    turns *this* test red (`TypeError` instead of `11`) while leaving
    the guard test alone unaffected - verified by hand as part of this
    issue's own verification, not merely asserted here."""
    from historian.exec.expression import evaluate

    result = evaluate(_bin(Operator.ADD, _TRUE, _lit(10)), _ROW, _SCHEMA)
    assert result == 11
    assert type(result) is int


def test_unary_minus_on_a_nested_predicate_operand():
    """sqlite3: `select -(1=1);` -> -1. Same discriminating relationship
    with the `arithmetic_operand` guard as the arithmetic test above,
    for `_eval_unary`'s own call site."""
    from historian.exec.expression import evaluate

    result = evaluate(_unary(UnaryOperator.NEG, _TRUE), _ROW, _SCHEMA)
    assert result == -1
    assert type(result) is int


def test_comparison_of_a_null_predicate_result_stays_null():
    """sqlite3: `select (1=NULL) = 1;` -> NULL. Not a defect - `Bool3`
    `NULL` and `Value` `NULL` are already the identical Python `None`
    on both sides of this boundary - pinned explicitly as a regression
    guard rather than left to accident."""
    from historian.exec.expression import evaluate

    assert evaluate(_bin(Operator.EQ, _NULL, _lit(1)), _ROW, _SCHEMA) is None


# --- TEXT past int64 is REAL at conversion time (issue #105) -----------
#
# `_scan_number` is the one place TEXT becomes a number, for arithmetic
# (`arithmetic_operand`), column affinity and `sum`'s classification
# (`try_numeric_affinity`) alike. A plain digit run - no `.`, no
# exponent - is an `int` only if it fits int64; otherwise it is the
# `float` of the digit *text* (never `float()` of a Python `int`, which
# raises `OverflowError` for a large enough one), `inf` if it overflows
# a double. Confirmed with `tests/oracle.py`:
# `select '9223372036854775808' + 0` -> 9.223372036854776e+18
# (`0x1.0000000000000p+63`), `select '999...9' + 0` (320 nines) -> inf.


def test_try_numeric_affinity_one_past_int64_max_is_real():
    from historian.exec.expression import try_numeric_affinity

    result = try_numeric_affinity("9223372036854775808")
    assert type(result) is float
    assert result == 9223372036854775808.0


def test_try_numeric_affinity_huge_digit_run_is_inf_not_a_crash():
    from historian.exec.expression import try_numeric_affinity

    result = try_numeric_affinity("9" * 320)
    assert type(result) is float
    assert result == float("inf")


def test_arithmetic_operand_one_past_int64_max_is_real():
    from historian.exec.expression import arithmetic_operand

    result = arithmetic_operand("9223372036854775808")
    assert type(result) is float
    assert result == 9223372036854775808.0


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("9223372036854775807", 9223372036854775807),
        ("-9223372036854775808", -9223372036854775808),
        ("+9223372036854775807", 9223372036854775807),
        ("0009223372036854775807", 9223372036854775807),
        ("0" * 5000 + "9223372036854775807", 9223372036854775807),
        ("0" * 5000, 0),
        ("-0", 0),
    ],
    ids=["max", "min", "plus_max", "zeros_max", "zeros_5000_max", "zeros_5000", "minus_zero"],
)
def test_arithmetic_operand_digit_run_inside_int64_stays_int(text, expected):
    """The boundary itself stays INTEGER, however it is spelled -
    including behind more leading zeros than Python's 4300-digit
    `int()` limit allows in one call."""
    from historian.exec.expression import arithmetic_operand

    result = arithmetic_operand(text)
    assert type(result) is int
    assert result == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("-9223372036854775809", -9223372036854775808.0),
        ("+9223372036854775808", 9223372036854775808.0),
        ("0009223372036854775808", 9223372036854775808.0),
        ("0" * 5000 + "9223372036854775808", 9223372036854775808.0),
        ("9" * 5000, float("inf")),
        ("-" + "9" * 5000, float("-inf")),
        ("18446744073709551616abc", 18446744073709551616.0),
    ],
    ids=["min_minus_one", "plus_max_plus_one", "zeros", "zeros_5000", "nines_5000", "neg_nines_5000", "prefix"],
)
def test_arithmetic_operand_digit_run_outside_int64_is_real(text, expected):
    """`'9'*5000` is past Python's 4300-digit `int()` limit, which
    raised `ValueError` before #105; `sqlite3` gives `inf`."""
    from historian.exec.expression import arithmetic_operand

    result = arithmetic_operand(text)
    assert type(result) is float
    assert result == expected


def test_text_past_int64_minus_one_is_real_not_an_exact_int():
    """sqlite3 (`tests/oracle.py`): `select '9223372036854775808' - 1,
    typeof(...)` -> 9.223372036854776e+18|real. The exact subtraction
    would land back inside int64 (`9223372036854775807`), which is what
    historian returned before #105."""
    from historian.exec.expression import evaluate

    result = evaluate(_bin(Operator.SUB, _lit("9223372036854775808"), _lit(1)), _ROW, _SCHEMA)
    assert type(result) is float
    assert result == 9223372036854775808.0


def test_unary_minus_of_text_past_int64_is_real():
    """sqlite3: `select -'9223372036854775808'` -> -9.223372036854776e+18,
    REAL - not the INTEGER int64 min it happens to equal."""
    from historian.exec.expression import evaluate

    result = evaluate(_unary(UnaryOperator.NEG, _lit("9223372036854775808")), _ROW, _SCHEMA)
    assert type(result) is float
    assert result == -9223372036854775808.0


def test_huge_digit_run_text_plus_zero_is_inf():
    """sqlite3: `select '999...9' + 0` (320 nines) -> inf. Raised
    `OverflowError` before #105."""
    from historian.exec.expression import evaluate

    result = evaluate(_bin(Operator.ADD, _lit("9" * 320), _lit(0)), _ROW, _SCHEMA)
    assert result == float("inf")


# --- Depth: evaluate() does not recurse per tree level (issue #107) -----
#
# The parser now rejects any tree taller than SQLite's 1000, but a bound
# tree can be taller than any one parsed expression (a select-list alias
# spliced into WHERE doubles it), and evaluate() is a public function in
# its own right. These trees are built by hand, far past 1000 levels,
# and evaluated at the interpreter's default recursion limit. Expected
# values are plain arithmetic/logic on the leaves, each checked against
# the oracle (tests/oracle.py) at a height SQLite accepts:
# `SELECT 1+1+...` (n terms) is n, a NULL anywhere makes it NULL, and
# `1 AND ... AND 0 AND ...` is 0.

_DEEP = 5000


def _left_chain(n: int, make_node, leaf):
    """A left-deep chain of *n* leaves joined by *make_node(left, right)*."""
    node = leaf(0)
    for i in range(1, n):
        node = make_node(node, leaf(i))
    return node


def _at_depth(extra_frames: int, fn):
    if extra_frames == 0:
        return fn()
    return _at_depth(extra_frames - 1, fn)


def test_deep_addition_chain_evaluates_without_recursion_error():
    from historian.exec.expression import evaluate

    expr = _left_chain(_DEEP, lambda l, r: _bin(Operator.ADD, l, r), lambda i: _col("n"))
    assert evaluate(expr, _ROW, _SCHEMA) == 5 * _DEEP


def test_deep_addition_chain_evaluates_from_a_deep_call_stack():
    """Nothing in evaluate() grows with the tree: it still works with
    700 frames of the default 1000 already used by the caller."""
    from historian.exec.expression import evaluate

    expr = _left_chain(_DEEP, lambda l, r: _bin(Operator.ADD, l, r), lambda i: _lit(1))
    assert _at_depth(700, lambda: evaluate(expr, _ROW, _SCHEMA)) == _DEEP


def test_deep_right_nested_chain_evaluates():
    from historian.exec.expression import evaluate

    expr = _lit(1)
    for _ in range(_DEEP - 1):
        expr = _bin(Operator.SUB, _lit(1), expr)
    # 1 - (1 - (1 - ... 1)): alternates, ending at 1 for an odd count.
    assert evaluate(expr, _ROW, _SCHEMA) == (1 if _DEEP % 2 == 1 else 0)


def test_deep_chain_with_a_null_is_null():
    from historian.exec.expression import evaluate

    expr = _left_chain(
        _DEEP, lambda l, r: _bin(Operator.ADD, l, r), lambda i: _lit(None if i == _DEEP // 2 else 1)
    )
    assert evaluate(expr, _ROW, _SCHEMA) is None


def test_deep_concat_chain():
    from historian.exec.expression import evaluate

    expr = _left_chain(_DEEP, lambda l, r: _bin(Operator.CONCAT, l, r), lambda i: _col("n"))
    assert evaluate(expr, _ROW, _SCHEMA) == "5" * _DEEP


def test_deep_comparison_chain_with_affinity():
    """`n = '5' = 1 = 1 ...`: the first comparison applies n's INTEGER
    affinity to '5' (TRUE, i.e. 1), every later one compares 1 = 1."""
    from historian.exec.expression import coerce_to_value, evaluate

    expr = _bin(Operator.EQ, _col("n"), _lit("5"))
    for _ in range(_DEEP):
        expr = _bin(Operator.EQ, expr, _lit(1))
    assert coerce_to_value(evaluate(expr, _ROW, _SCHEMA)) == 1


@pytest.mark.parametrize("leftmost, expected", [(0, False), (None, None)], ids=["false", "null"])
def test_deep_and_chain_stops_on_the_leftmost_false_or_null_in_condition_context(leftmost, expected):
    """A FALSE or NULL leftmost leaf decides every AND above it at the
    root of a condition without touching a right operand - each right
    operand here is a FunctionCall, which raises EvalError if it is
    ever evaluated (#111)."""
    from historian.exec.expression import evaluate_condition

    call = FunctionCall(name="f", args=(), position=_POS)
    expr = _lit(leftmost)
    for _ in range(_DEEP):
        expr = And(left=expr, right=call, position=_POS)
    assert _at_depth(700, lambda: evaluate_condition(expr, _ROW, _SCHEMA)) is expected


def test_deep_or_chain_stops_on_the_leftmost_true_in_condition_context():
    from historian.exec.expression import evaluate_condition

    call = FunctionCall(name="f", args=(), position=_POS)
    expr = _lit(1)
    for _ in range(_DEEP):
        expr = Or(left=expr, right=call, position=_POS)
    assert _at_depth(700, lambda: evaluate_condition(expr, _ROW, _SCHEMA)) is True


def test_deep_not_and_chain_in_condition_context():
    """`NOT (NOT (... (NULL AND f) ...))` with an odd number of NOTs
    around every AND: NULL counts as TRUE there, so the AND does not
    stop and the FunctionCall raises; with an even number it stops."""
    from historian.exec.expression import EvalError, evaluate_condition

    call = FunctionCall(name="f", args=(), position=_POS)
    even = _lit(None)
    for _ in range(_DEEP // 2):
        even = Not(Not(And(left=even, right=call, position=_POS), _POS), _POS)
    assert evaluate_condition(even, _ROW, _SCHEMA) is None
    with pytest.raises(EvalError):
        evaluate_condition(Not(And(left=_lit(None), right=call, position=_POS), _POS), _ROW, _SCHEMA)


def test_deep_and_chain_evaluates_every_operand_in_value_context():
    """The same chain as above in value context: the first right
    operand is evaluated, and raises."""
    from historian.exec.expression import EvalError, evaluate

    call = FunctionCall(name="f", args=(), position=_POS)
    expr = _lit(0)
    for _ in range(_DEEP):
        expr = And(left=expr, right=call, position=_POS)
    with pytest.raises(EvalError):
        _at_depth(700, lambda: evaluate(expr, _ROW, _SCHEMA))


def test_deep_and_chain_evaluates_every_right_operand_when_not_decided():
    """With a TRUE left side, AND does evaluate its right operand: a
    FunctionCall on the deepest right raises EvalError."""
    from historian.exec.expression import EvalError, evaluate

    expr = _lit(1)
    for i in range(_DEEP):
        right = FunctionCall(name="f", args=(), position=_POS) if i == _DEEP - 1 else _lit(1)
        expr = And(left=expr, right=right, position=_POS)
    with pytest.raises(EvalError):
        evaluate(expr, _ROW, _SCHEMA)


@pytest.mark.parametrize(
    "zero_or_null, expected",
    [(0, False), (None, None)],
    ids=["zero", "null"],
)
def test_deep_and_chain_of_ones_with_one_zero_or_null(zero_or_null, expected):
    from historian.exec.expression import evaluate

    expr = _left_chain(
        _DEEP,
        lambda l, r: And(left=l, right=r, position=_POS),
        lambda i: _lit(zero_or_null if i == _DEEP // 2 else 1),
    )
    assert evaluate(expr, _ROW, _SCHEMA) is expected


def test_deep_not_and_unary_chains():
    from historian.exec.expression import evaluate

    not_chain = _lit(1)
    minus_chain = _col("n")
    for _ in range(_DEEP + 1):
        not_chain = Not(operand=not_chain, position=_POS)
        minus_chain = _unary(UnaryOperator.NEG, minus_chain)
    # An odd number of NOTs / minus signs.
    assert evaluate(not_chain, _ROW, _SCHEMA) is False
    assert evaluate(minus_chain, _ROW, _SCHEMA) == -5


@pytest.mark.parametrize("entry", ["evaluate", "evaluate_condition"])
def test_deep_is_like_in_between_chains(entry):
    """Both entry points: IN and BETWEEN step through their operands
    differently in condition context (#111), still without recursing."""
    from historian.exec import expression

    evaluate = getattr(expression, entry)

    is_chain = _col("n")
    like_chain = _col("s")
    in_chain = _lit(1)
    for _ in range(_DEEP):
        is_chain = _is(is_chain, _lit(None), negated=True)  # IS NOT NULL -> 1
        like_chain = _like(like_chain, _lit("%"))  # anything LIKE '%' -> 1
        # One element only: `IN` evaluates its left operand once per
        # element (#137), so a two-element list at every level would be
        # 2**5000 evaluations. 1 IN (1) -> 1, and so on up the chain.
        in_chain = _in(in_chain, (_lit(1),))
    assert evaluate(is_chain, _ROW, _SCHEMA) is True
    assert evaluate(like_chain, _ROW, _SCHEMA) is True
    assert evaluate(in_chain, _ROW, _SCHEMA) is True
    # `BETWEEN` evaluates its operand twice (#137), so only a short
    # chain here; the depth is in the operand of a long + chain instead.
    short_between = _col("n")
    for _ in range(10):
        short_between = _between(short_between, _lit(0), _lit(10))
    assert evaluate(short_between, _ROW, _SCHEMA) is True
    deep_operand = _left_chain(_DEEP, lambda l, r: _bin(Operator.ADD, l, r), lambda i: _lit(1))
    assert evaluate(_between(deep_operand, _lit(0), _lit(_DEEP)), _ROW, _SCHEMA) is True
    assert evaluate(_in(_lit(_DEEP), (deep_operand,)), _ROW, _SCHEMA) is True
    assert evaluate(_like(deep_operand, _lit(str(_DEEP))), _ROW, _SCHEMA) is True


# --- Issue #136: numeric-text whitespace is exactly the six characters
# SQLite skips (space, \t, \n, \v, \f, \r), leading and trailing, and
# nothing else. Oracle: tests/oracle.py (the oracle), the
# string as a quoted literal and as a bound parameter, same results:
#   select '\v12'+0, '\v12'%5, -'\v12', '\x1c12'+0, '\xa012'+0;
#     -> 12|2|-12|0|0
#   select 1 where '\v1';  -> 1
# One test per site that reads `_NUMERIC_WHITESPACE`, so a failure
# names the site.

_NUMERIC_WS = [" ", "\t", "\n", "\v", "\f", "\r"]
_NUMERIC_WS_IDS = ["space", "tab", "newline", "vtab", "formfeed", "cr"]
_NOT_NUMERIC_WS = [
    "\x1c", "\x1d", "\x1e", "\x1f", "\x85", "\xa0", " ", "　", "﻿",
]
_NOT_NUMERIC_WS_IDS = [
    "x1c", "x1d", "x1e", "x1f", "x85", "xa0", "u2003", "u3000", "ufeff",
]


@pytest.mark.parametrize("ws", _NUMERIC_WS, ids=_NUMERIC_WS_IDS)
def test_numeric_whitespace_arithmetic_skips_leading(ws):
    from historian.exec.expression import evaluate

    result = evaluate(_bin(Operator.ADD, _lit(ws + "12"), _lit(0)), _ROW, _SCHEMA)
    assert result == 12
    assert type(result) is int


@pytest.mark.parametrize("ws", _NUMERIC_WS, ids=_NUMERIC_WS_IDS)
def test_numeric_whitespace_modulo_skips_leading(ws):
    from historian.exec.expression import evaluate

    result = evaluate(_bin(Operator.MOD, _lit(ws + "12"), _lit(5)), _ROW, _SCHEMA)
    assert result == 2
    assert type(result) is int


@pytest.mark.parametrize("ws", _NUMERIC_WS, ids=_NUMERIC_WS_IDS)
def test_numeric_whitespace_unary_minus_skips_leading(ws):
    from historian.exec.expression import evaluate

    result = evaluate(_unary(UnaryOperator.NEG, _lit(ws + "12")), _ROW, _SCHEMA)
    assert result == -12
    assert type(result) is int


@pytest.mark.parametrize("ws", _NUMERIC_WS, ids=_NUMERIC_WS_IDS)
def test_numeric_whitespace_text_condition_truthiness_skips_leading(ws):
    from historian.exec.expression import coerce_to_bool3, evaluate

    assert coerce_to_bool3(evaluate(_lit(ws + "1"), _ROW, _SCHEMA)) is True


@pytest.mark.parametrize("ws", _NUMERIC_WS, ids=_NUMERIC_WS_IDS)
def test_numeric_whitespace_affinity_skips_both_ends(ws):
    from historian.exec.expression import try_numeric_affinity

    for text in (ws + "12", "12" + ws, ws + "12" + ws):
        result = try_numeric_affinity(text)
        assert result == 12
        assert type(result) is int


@pytest.mark.parametrize(
    "text",
    ["\v12\v.5", "1\v2", "\v-\v5", "\v", " ", "\v\v", "-\v5"],
)
def test_numeric_whitespace_affinity_not_skipped_inside_or_alone(text):
    """Not skipped between sign and digits or inside a number, and a
    whitespace-only string is not a number: the text comes back
    unchanged (`n INTEGER` holding `'\\v'` stays `text` in SQLite)."""
    from historian.exec.expression import try_numeric_affinity

    assert try_numeric_affinity(text) == text


@pytest.mark.parametrize("ws", _NUMERIC_WS, ids=_NUMERIC_WS_IDS)
def test_numeric_whitespace_only_text_reads_as_zero(ws):
    from historian.exec.expression import evaluate

    for op in (Operator.ADD, Operator.MOD):
        rhs = _lit(0) if op is Operator.ADD else _lit(5)
        result = evaluate(_bin(op, _lit(ws), rhs), _ROW, _SCHEMA)
        assert result == 0
        assert type(result) is int


@pytest.mark.parametrize("ctl", _NOT_NUMERIC_WS, ids=_NOT_NUMERIC_WS_IDS)
def test_non_whitespace_controls_are_not_skipped_in_arithmetic(ctl):
    """`'\\x1c12' + 0` is `0` in SQLite: only the six ASCII characters
    are skipped, not `str.strip()`/`isspace()`'s wider set."""
    from historian.exec.expression import evaluate

    result = evaluate(_bin(Operator.ADD, _lit(ctl + "12"), _lit(0)), _ROW, _SCHEMA)
    assert result == 0
    assert type(result) is int
    result = evaluate(_bin(Operator.MOD, _lit(ctl + "12"), _lit(5)), _ROW, _SCHEMA)
    assert result == 0


@pytest.mark.parametrize("ctl", _NOT_NUMERIC_WS, ids=_NOT_NUMERIC_WS_IDS)
def test_non_whitespace_controls_are_not_skipped_by_affinity(ctl):
    from historian.exec.expression import try_numeric_affinity

    assert try_numeric_affinity(ctl + "12") == ctl + "12"
    assert try_numeric_affinity("12" + ctl) == "12" + ctl


# --- Constant propagation (#142): the stored value and the fixed column -------
#
# `apply_column_affinity` is SQLite's storage conversion (`OP_Affinity`),
# the value a constant would have if stored in the column - what the
# planner puts in a `FixedColumnRef`. Measured on the pinned oracle
# through `||` on the replaced column (`tests/differential/
# test_where_propagation.py`).


@pytest.mark.parametrize(
    "column_type, value, expected",
    [
        (ColumnType.INTEGER, 5, 5),
        (ColumnType.INTEGER, "05", 5),
        (ColumnType.INTEGER, " 5", 5),
        (ColumnType.INTEGER, "5.0", 5),
        (ColumnType.INTEGER, "5e0", 5),
        (ColumnType.INTEGER, 5.0, 5),
        (ColumnType.INTEGER, -0.0, 0),
        (ColumnType.INTEGER, 5.5, 5.5),
        (ColumnType.INTEGER, "5.5", 5.5),
        (ColumnType.INTEGER, "x", "x"),
        (ColumnType.INTEGER, None, None),
        (ColumnType.INTEGER, 9223372036854775807, 9223372036854775807),
        (ColumnType.INTEGER, 9223372036854775808.0, 9223372036854775808.0),
        (ColumnType.INTEGER, -9223372036854775808.0, -9223372036854775808.0),
        (ColumnType.INTEGER, -9223372036854774784.0, -9223372036854774784),
        (ColumnType.INTEGER, float("inf"), float("inf")),
        (ColumnType.REAL, 1, 1.0),
        (ColumnType.REAL, "1", 1.0),
        (ColumnType.REAL, " 1 ", 1.0),
        (ColumnType.REAL, 1.5, 1.5),
        (ColumnType.REAL, 9007199254740993, 9007199254740992.0),
        (ColumnType.REAL, "y", "y"),
        (ColumnType.REAL, None, None),
        (ColumnType.TEXT, 5, "5"),
        (ColumnType.TEXT, 5.0, "5.0"),
        (ColumnType.TEXT, "05", "05"),
        (ColumnType.TEXT, None, None),
        (None, "05", "05"),
        (None, 5.0, 5.0),
    ],
)
def test_apply_column_affinity_is_the_stored_value(column_type, value, expected):
    from historian.exec.expression import apply_column_affinity

    got = apply_column_affinity(value, column_type)
    assert type(got) is type(expected)
    if isinstance(expected, float):
        assert got.hex() == expected.hex()
    else:
        assert got == expected


def _fixed(name: str, value):
    from historian.sql.walk import FixedColumnRef

    return FixedColumnRef(offset=_SCHEMA.index_of(name), name=name, value=value, position=_POS)


def test_a_fixed_column_evaluates_to_its_value_not_the_rows():
    from historian.exec.expression import evaluate

    assert evaluate(_fixed("n", 7), _ROW, _SCHEMA) == 7
    assert evaluate(_bin(Operator.CONCAT, _fixed("n", 7), _lit("x")), _ROW, _SCHEMA) == "7x"


@pytest.mark.parametrize(
    "expr, expected",
    [
        # `n` fixed to 5 keeps INTEGER affinity: `5 > '4'` compares numerically.
        (lambda: _bin(Operator.GT, _fixed("n", 5), _lit("4")), True),
        (lambda: _bin(Operator.LT, _lit("4"), _fixed("n", 5)), True),
        (lambda: _bin(Operator.EQ, _fixed("n", 5), _lit("5")), True),
        (lambda: Is(left=_fixed("n", 5), right=_lit("5"), negated=False, position=_POS), True),
        (lambda: In(left=_fixed("n", 5), values=(_lit("5"),), negated=False, position=_POS), True),
        (lambda: Between(operand=_fixed("n", 5), low=_lit("4"), high=_lit("6"), negated=False, position=_POS), True),
        # An IN list element has no affinity, fixed or not.
        (lambda: In(left=_lit("5"), values=(_fixed("n", 5),), negated=False, position=_POS), False),
        # Inside an expression there is no affinity.
        (lambda: _bin(Operator.EQ, _bin(Operator.ADD, _fixed("n", 5), _lit(0)), _lit("5")), False),
        # TEXT: `'5' < '10'` is FALSE as text, `'5' > 4` TRUE.
        (lambda: _bin(Operator.LT, _fixed("s", "5"), _lit("10")), False),
        (lambda: _bin(Operator.GT, _fixed("s", "5"), _lit(4)), True),
        # REAL fixed to 1.0 against '1'.
        (lambda: _bin(Operator.EQ, _fixed("r", 1.0), _lit("1")), True),
        # Two fixed columns: numeric affinity wins.
        (lambda: _bin(Operator.EQ, _fixed("s", "5"), _fixed("n", 5)), True),
    ],
)
def test_a_fixed_column_keeps_its_columns_affinity_in_comparisons(expr, expected):
    """Oracle: `t(n INTEGER, s TEXT, r REAL)` with the `WHERE` shapes in
    `tests/differential/test_where_propagation.py`. The row is `(5,
    '5', 5.0)` but a fixed column never reads it, so a row value that
    disagrees would not change the answer either."""
    from historian.exec.expression import evaluate

    assert evaluate(expr(), (99, "99", 99.0), _SCHEMA) is expected
