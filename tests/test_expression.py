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
    BinaryOp,
    FunctionCall,
    Literal,
    Operator,
    Star,
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


@pytest.mark.parametrize("op", [Operator.ADD, Operator.SUB, Operator.MUL, Operator.DIV])
def test_null_propagates_through_every_arithmetic_operator(op):
    """sqlite3: `select NULL+1, NULL-1, NULL*1, NULL/1;` -> all NULL."""
    from historian.exec.expression import evaluate

    assert evaluate(_bin(op, _lit(None), _lit(1)), _ROW, _SCHEMA) is None
    assert evaluate(_bin(op, _lit(1), _lit(None)), _ROW, _SCHEMA) is None


# --- Arithmetic: leading-prefix text-to-number coercion -----------------
#
# sqlite3: `select '5'+1, 'abc'+1, '5abc'+1, '  5  '+1, '0x10'+1, '5e'+1,
# '+5'+1, '-5'+1, '1e2'+1, typeof('1e2'+1), '.5'+1, '5.'+1, '5.5.5'+1,
# '5e+'+1, '   '+1;`
# -> 6|1|6|6|1|6|6|-4|101.0|real|1.5|6.0|6.5|6|1


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
