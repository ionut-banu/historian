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


def _like(left, pattern, negated=False) -> Like:
    return Like(left=left, pattern=pattern, negated=negated, position=_POS)


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
# Leading-prefix numeric coercion (the same rule `_arithmetic_operand`
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
    pin, so a future "tidy-up" cannot silently reintroduce a call the
    value-level test happens not to exercise - matching how
    `tests/test_values.py` pins the 2^53 case for `values.py`).

    Walks this module's own source with `ast`, and asserts every
    `float(` call site sits inside a function on an explicit allowlist
    of three - not the issue body's stated two:

    - `_scan_number` - exception (a): affinity's/arithmetic's own
      text-to-number *conversion*, constructing a new `Value` from a
      string. Not a lossy comparison cast.
    - `_format_float` - exception (b): the float-formatting helper for
      `||`/text-affinity, converting a number *to* text, never used to
      convert a number *for* comparison. Named here because the issue
      names it, even though this implementation's `_format_float`
      happens not to call `float()` itself (it only formats an
      already-`float` argument) - nothing about this test should
      depend on that being true if a future change makes it call
      `float()` too, e.g. to normalize an int argument.
    - `_int64_bounded` - a third, genuine exception this issue's own
      int64-overflow criteria require and the "two named exceptions"
      list does not mention: `9223372036854775807 + 1` must become
      `REAL`, which needs converting an already-overflowed *exact*
      Python `int` (computed first with unbounded `int` arithmetic) to
      `float`. This is arithmetic *result production*, a different
      path from comparison and never confused with it per this issue's
      own "applies only to arithmetic result production, never to
      comparison" criterion - kept true structurally by living in its
      own function, never called from `_apply_affinity`, `_eval_is`,
      `_eval_in`, `_eval_between`, or the comparison branch of
      `_eval_binary`.

    Any `float(` call appearing anywhere else in this module - most
    plausibly, a future "normalize this before comparing" edit to the
    comparison path - fails this test.
    """
    import ast
    import inspect

    from historian.exec import expression

    allowed_functions = {"_scan_number", "_format_float", "_int64_bounded"}
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
