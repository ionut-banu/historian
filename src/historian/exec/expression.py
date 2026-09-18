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
`Bool3` - unconditionally, because that is a property of the `And`/
`Or`/`Not` node itself, not of where the whole expression sits in the
query. `evaluate()`'s own `And`/`Or`/`Not` branches enforce this
directly, each wrapping its operand's `evaluate()` result in
`coerce_to_bool3` before handing it to `values.and3`/`or3`/`not3` -
see that section below for why this still needs no `position`
parameter.

A predicate-shaped node used where a value is expected
(`SELECT 1 = 1`) or a value-shaped node used where a predicate is
expected (`WHERE line_no`, SQLite's C-style truthiness, at the
`WHERE`/`HAVING` root or nested under `AND`/`OR`/`NOT`) is each a
real, grammar-reachable shape - real SQLite accepts both, since
booleans have no storage class of their own (`_docs/decisions.md`,
2026-08-27: "`typeof(true)` is `integer`"). `evaluate()` itself still
does not carry a notion of "the position this whole call's result is
about to be used in" - it stays structural throughout, deciding
`Value` vs `Bool3` from each node's own shape alone. Issue #38 adds
`coerce_to_value` (`Bool3 -> Value`, `True`/`False`/`None` becoming
SQLite's own `1`/`0`/`NULL` spelling) and `coerce_to_bool3` (`Value ->
Bool3`, via the same leading-prefix numeric coercion arithmetic uses,
then `!= 0`) immediately below `evaluate()`, as pure functions of a
single `evaluate()` result. `Project` calls `coerce_to_value` on every
select-list item's root result; `Filter` calls `coerce_to_bool3` on
the `WHERE`/`HAVING` predicate's root result. Both call sites are
outside `evaluate()`'s own recursive dispatch. `coerce_to_bool3` has a
second caller *inside* that dispatch, though: `evaluate()`'s `And`,
`Or` and `Not` branches call it on each operand's result before
`values.and3`/`or3`/`not3` ever sees it - round 2 of #38, below. That
still is not a `position` parameter: `evaluate()` reaches those calls
because the node it is currently dispatching on is itself `And`/`Or`/
`Not`, exactly the same structural knowledge every other branch here
already uses, never because a caller told it what position it is in.

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

# The BoundColumnRef import above is the one place this module's import
# graph is not literally subprocess-free, and it is worth being honest
# about rather than letting it pass silently: sql/binder.py's own
# module-level code does `from historian.tables.blame import
# BLAME_SCHEMA`, so importing BoundColumnRef from it transitively loads
# tables/blame.py, which imports `subprocess` at module level. Verified
# directly - `import historian.exec.expression` puts both `subprocess`
# and `historian.tables.blame` in `sys.modules`. This is not a choice
# this module makes and cannot avoid: the issue's own goal statement
# requires consuming `BoundColumnRef` from `sql/binder.py`, and no
# other module defines it. It mirrors the exact trade-off
# `sql/binder.py`'s own docstring already accepts and documents for
# itself ("the module is merely imported, never invoked, so no git
# repository or subprocess call is needed to exercise this module").
# `evaluate()` never calls anything from `tables/blame.py` or
# `subprocess`, and no test in `tests/test_expression.py` needs a
# repository - the module is loaded, never invoked - so `AGENTS.md`'s
# actual concern ("only scan operators touch git") still holds in
# behaviour, even though the import graph is not literally free of the
# word `subprocess`.

__all__ = ["EvalError", "coerce_to_bool3", "coerce_to_value", "evaluate"]

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
        return values.and3(
            coerce_to_bool3(evaluate(expr.left, row, schema)),
            coerce_to_bool3(evaluate(expr.right, row, schema)),
        )
    if isinstance(expr, Or):
        return values.or3(
            coerce_to_bool3(evaluate(expr.left, row, schema)),
            coerce_to_bool3(evaluate(expr.right, row, schema)),
        )
    if isinstance(expr, Not):
        return values.not3(coerce_to_bool3(evaluate(expr.operand, row, schema)))
    if isinstance(expr, Like):
        return _eval_like(expr, row, schema)
    if isinstance(expr, In):
        return _eval_in(expr, row, schema)
    if isinstance(expr, Between):
        return _eval_between(expr, row, schema)
    raise AssertionError(f"exec/expression.py: unhandled expression node type {type(expr).__name__}")


# --- The Value/Bool3 coercion boundary (issue #38) ------------------------
#
# Two small, pure functions of evaluate()'s own return value - deliberately
# not a `position` parameter threaded through evaluate()'s recursive
# dispatch. See the module docstring's "Value or Bool3, decided by node
# shape, not calling context" section for why: `evaluate()` never needs to
# know what position its *own* result is about to be used in - each of its
# branches already knows, structurally, what position its *children's*
# results are in, purely from which node it is currently dispatching on.
#
# `coerce_to_value` is called only from outside evaluate()'s own recursion:
# by `Project` (`exec/operators.py`), on a select-list item's root result.
#
# `coerce_to_bool3` has two kinds of caller. `Filter` (`exec/operators.py`)
# calls it from outside the recursion too, on a WHERE/HAVING predicate's
# root result - both of these are the two call sites #38's first round
# implemented. Round 2 (QA FAIL, confirmed against sqlite3 3.51.0) found a
# false premise in this section's original text: it claimed the Value/Bool3
# ambiguity "only ever exists at exactly two points... never at any
# recursive call evaluate() makes internally." That is wrong - SQLite
# applies the same leading-prefix truthiness independently to *each operand*
# of AND/OR/NOT, confirmed with plain arithmetic and no comparison anywhere
# in the query (`select (3-3) and 1;` -> `0`; `select not(3-3);` -> `1`).
# So `evaluate()`'s own `And`/`Or`/`Not` branches, above, are themselves
# callers of `coerce_to_bool3`, one per operand, before handing the result
# to `values.and3`/`or3`/`not3`. This still needs no `position` parameter:
# `And`/`Or`/`Not`'s operands are predicate positions unconditionally, a
# property of the node evaluate() is already dispatching on, not something
# a caller has to tell it. Between, In and Like never need this - each
# already builds its own Bool3 result from values.py's own comparison
# functions (`values.eq`/`ge`/`le`/...), never from a raw, uncoerced
# evaluate() result, so there is nothing left to coerce there.
#
# Both coercions are sound as functions of the return value alone, with no
# need to re-inspect the AST: values.py's own module docstring excludes
# `bool` from `Value` by construction, so a Python `bool` coming back from
# evaluate() is unambiguous proof a predicate-shaped subexpression was just
# evaluated - and `None` already means the same thing, "NULL", in both a
# `Value` and a `Bool3` position (values.py's "Two representations, both
# using None"), so it needs no direction-specific handling at all.


def coerce_to_value(result: Value | Bool3) -> Value:
    """`Bool3 -> Value`, for a select-list item (`exec/operators.py`'s
    `Project`): SQLite's own `1`/`0`/`NULL` spelling of a predicate
    result, never Python's `True`/`False`/`None`. Confirmed against
    `sqlite3`: `select 1 = 1, typeof(1 = 1), 1 = 2, typeof(1 = 2),
    1 = null, typeof(1 = null);` -> `1|integer|0|integer||null`.

    `result is True`/`result is False` rather than `result == True` or
    `isinstance(result, bool)`: identity, not equality, so an ordinary
    `Value` that merely compares equal to a bool (nothing in `Value`
    ever does, by `values.py`'s own construction, but this function
    should not rely on that invariant holding two modules away to stay
    correct) can never be mistaken for one. Anything that is not
    exactly the `True`/`False` singleton - including `None`, which
    means NULL identically on both sides of this boundary - passes
    through unchanged: a value-shaped `evaluate()` result was already
    the right SQLite value and needs no conversion at all.
    """
    if result is True:
        return 1
    if result is False:
        return 0
    return result


def coerce_to_bool3(result: Value | Bool3) -> Bool3:
    """`Value -> Bool3`: SQLite's C-style truthiness for a value-shaped
    predicate (`WHERE line_no`, `WHERE path`), confirmed case by case
    against `sqlite3` in issue #38's own body - not "nonempty string is
    truthy", but the exact leading-prefix numeric coercion
    `_arithmetic_operand` already implements for arithmetic (`'0abc'`
    -> `0`, falsy; `'1abc'` -> `1`, truthy; `'  1  '` -> `1`, truthy;
    `''`/`'abc'`, no digit anywhere, -> `0`, falsy), followed by
    `!= 0`.

    Two call sites, both confirmed against `sqlite3` 3.51.0.
    `exec/operators.py`'s `Filter` calls this on a `WHERE`/`HAVING`
    predicate's *root* result, ahead of `values.is_true`. `evaluate()`
    itself, above, calls this a second way: on each operand of `And`,
    `Or` and `Not` before handing it to `values.and3`/`or3`/`not3` -
    SQLite applies the identical truthiness independently per operand,
    not only at a predicate's root (`select (3-3) and 1;` -> `0`;
    `select not(3-3);` -> `1`; issue #38 round 2, QA FAIL on the first
    round's narrower "two call sites only" design).

    A `bool` or `None` is already a `Bool3` - a predicate-shaped
    `evaluate()` result - and passes through unchanged; `None` again
    needs no direction-specific handling, since NULL propagates as
    "the predicate is unknown, the row is dropped" whether it arrived
    as a `Value` or a `Bool3`, per `values.py`'s own "Two
    representations" section (confirmed: `create table t(n); insert
    into t values(null); select 'kept' from t where n;` -> no rows,
    exactly like any other NULL predicate, not a new rule).
    """
    if isinstance(result, bool) or result is None:
        return result
    return _arithmetic_operand(result) != 0


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
    `IN ()` is always `FALSE`.

    Affinity (issue #47, correcting this docstring's own former claim
    that it worked "exactly as for `=`" - it does not): a list element
    contributes no affinity of its own, ever - not "usually," not
    "unless the element happens to itself be a bare column." Confirmed
    against `sqlite3` (`t(n INTEGER, s TEXT, r REAL)`, row `(5, '5',
    5.0)`): `'5' IN (n)` -> `0`, `'5' IN (r)` -> `0`, `5 IN (s)` -> `0`
    - each is `1` if the element's own affinity were (wrongly)
    consulted the way `=`'s right operand's is. Only `x`'s own
    affinity - the same structural question `_affinity_of` already
    asks for every other operator - is ever applied, and it is applied
    once per element independently: `x IN ('5', 'abc')` still converts
    `'5'` and `'abc'` against `x`'s affinity individually, per the
    already-correct `test_in_applies_affinity_to_each_element_independently`.
    `_evaluate_affinity_pair`'s `right_has_affinity=False` is the
    single change this makes: `x`'s own affinity still applies to each
    element, the element's affinity never does.

    This is genuinely different from `_eval_between`, not a case that
    "tidying" the two onto one path would preserve: a `BETWEEN` bound
    is an independent RHS operand, symmetric with `=`, and keeps its
    own affinity. Confirmed against `sqlite3` for the identical
    operand shape, same row (`n = 1`): `'1' BETWEEN n AND n` -> `1`,
    `'1' IN (n)` -> `0`. `_eval_between` is intentionally left calling
    `_evaluate_affinity_pair` with its default `right_has_affinity=True`.
    """
    result: Bool3 = False
    for element in expr.values:
        left, right = _evaluate_affinity_pair(expr.left, element, row, schema, right_has_affinity=False)
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
    left_expr: Expr, right_expr: Expr, row: Row, schema: Schema, *, right_has_affinity: bool = True
) -> tuple[Value, Value]:
    """Evaluate both sides of a comparison-shaped pair of operands
    (`=`/`<>`/.../`IS`/`IS NOT`, each bound of `BETWEEN`) and apply
    column affinity to the result. Shared by every predicate that goes
    through "the identical affinity algorithm" as `=` - which is every
    caller except `_eval_in`.

    `right_has_affinity` (issue #47) is the explicit, structural
    escape hatch `_eval_in` uses: SQLite's own rule is that the
    right-hand side of `IN`/`NOT IN` *with a list* has no affinity at
    all, regardless of what kind of expression a given element is -
    unlike `=`/`IS`/`BETWEEN`, where each operand independently asks
    `_affinity_of` the normal way. `_eval_in` passes
    `right_has_affinity=False` for every element; every other caller
    takes the default and is unaffected. A plain keyword parameter,
    not a dynamic-dispatch trick (`AGENTS.md`), and it does not touch
    `_eval_between`, which must keep applying each bound's own
    affinity independently - see `_eval_in`'s docstring for the
    evidence that the two operators, though structurally identical
    here, are not supposed to behave alike."""
    left = evaluate(left_expr, row, schema)
    right = evaluate(right_expr, row, schema)
    right_affinity = _affinity_of(right_expr, schema) if right_has_affinity else None
    return _apply_affinity(left, _affinity_of(left_expr, schema), right, right_affinity)


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

    SQLite also normalises negative zero to positive when rendering to
    TEXT - confirmed against `sqlite3`: `(-0.0)||''` and `(0.0*-1)||''`
    are both `'0.0'`, never `'-0.0'` - where Python's own `-0.0`,
    `0.0 * -1`, and `"%.15g" % -0.0` (`'-0'`) all preserve the sign.
    Comparison is unaffected either way (`-0.0 = 0.0` is TRUE under
    IEEE 754, in both engines, and in Python), so this is purely a
    presentation fix, made here rather than on the arithmetic path: no
    observable in the v1 grammar distinguishes "normalise at negation"
    from "normalise at formatting" (`printf('%.20f', ...)`, `sign()`,
    `hex(cast(... as blob))` all show positive zero either way), and
    confining the fix to this one function is the smaller change and
    cannot affect arithmetic, comparison, or ordering.
    """
    if math.isinf(value):
        return "-Inf" if value < 0 else "Inf"
    if value == 0.0:
        value = 0.0  # comparison, not a float() call: folds -0.0 to 0.0
    text = "%.15g" % value
    if "e" in text:
        mantissa, _, exponent = text.partition("e")
        if "." not in mantissa:
            mantissa += ".0"
        return f"{mantissa}e{exponent}"
    if "." not in text:
        text += ".0"
    return text


def _coerce_to_text(value: Value) -> str | None:
    """A `Value` as SQLite would render it as `TEXT` - used by `||`
    (after the NULL check, which happens in that caller, so `value`
    is never `None` there), by `LIKE`'s unconditional text coercion
    (same: NULL is checked by that caller first), and by column
    affinity's numeric-to-text conversion in `_apply_affinity`, which
    calls this on a raw operand with no NULL check of its own - `s =
    NULL` against a `TEXT` column reaches this with `value is None`.
    `None` falls through every `isinstance` check below and returns
    unchanged, which is exactly right there: affinity never turns
    `NULL` into anything else, and the eventual `values.eq`/`is_`
    call is what actually decides what a `NULL` operand means."""
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
