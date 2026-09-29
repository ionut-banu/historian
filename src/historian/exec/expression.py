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
dispatcher, an explicit `isinstance` chain with no dynamic dispatch and
no metaclasses (`AGENTS.md`). Since issue #107 it is a loop over an
explicit work stack rather than a function calling itself per node, so
Python's stack does not grow with the tree; the text below still says
"recursive dispatch" for evaluating an operand from inside a node's
own evaluation, which is what the work stack does, in the same order.

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

#38's two root call sites (a select-list root, a `WHERE`/`HAVING`
predicate root) left one direction incomplete: a predicate-shaped
result reaching a **nested** `Value`-requiring operand - a comparison
operand, `IS`'s two sides, `BETWEEN`'s operand/low/high, `IN`'s left
operand and each list element, `||`'s two sides, `LIKE`'s two sides,
and arithmetic/unary-minus's operand - still raised a bare `TypeError`
one level deeper than either root. Issue #63 closes that gap the same
structural way: `coerce_to_value` gains callers *inside* `evaluate()`'s
own recursive dispatch, exactly mirroring how `coerce_to_bool3` already
had one for `And`/`Or`/`Not` - `_affinity_pair` wraps both of
its `evaluate()` calls in `coerce_to_value` before affinity is applied
(fixing comparison, `IS`, `BETWEEN`, and `IN` in one shared chokepoint,
since all four route their operands through it), and `_finish_binary`'s
`||`/arithmetic branches, `_finish_like`, and `_finish_negate`
each do the same at their own call site. Still no `position` parameter:
each of these already knows, structurally, that the operand it is about
to hand to `values.eq`/`_coerce_to_text`/`arithmetic_operand` must be a
`Value`, from its own node shape alone.

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
from enum import Enum, auto

from historian import values
from historian.ascii import ascii_fold, is_ascii_digit
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
from historian.values import INT64_MAX, INT64_MIN, Bool3, Value

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

__all__ = [
    "EvalError",
    "arithmetic_operand",
    "coerce_to_bool3",
    "coerce_to_value",
    "evaluate",
    "try_numeric_affinity",
]

# `INT64_MIN`/`INT64_MAX` (`historian.values`, issue #53), imported
# above: SQLite's `int64` bounds. Unlike the parser (which only ever
# needs the positive bound, to detect an overflowing literal),
# arithmetic here needs both - subtraction and negation can overflow
# toward either end.

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


class _Step(Enum):
    """What `evaluate()` does with one entry of its work stack - see
    `evaluate()`. `EVAL` starts a node; every other step finishes one
    whose operands have already been evaluated onto the value stack."""

    EVAL = auto()
    FINISH_BINARY = auto()
    FINISH_NEGATE = auto()
    FINISH_IS = auto()
    AND_AFTER_LEFT = auto()
    FINISH_AND = auto()
    OR_AFTER_LEFT = auto()
    FINISH_OR = auto()
    FINISH_NOT = auto()
    FINISH_LIKE = auto()
    FINISH_IN = auto()
    FINISH_BETWEEN = auto()


def evaluate(expr: Expr, row: Row, schema: Schema) -> Value | Bool3:
    """Evaluate *expr* against *row*, described by *schema*.

    Returns a `historian.values.Value` for a value-shaped node, a
    `historian.values.Bool3` for a predicate-shaped one - see the
    module docstring for exactly which shapes are which and why that
    split is structural rather than context-driven.

    Not recursive (issue #107): one loop over an explicit work stack of
    `(step, node)` pairs and a value stack of finished results, so the
    Python stack does not grow with the tree. A recursive walk crashed
    with `RecursionError` from about 330 levels (three frames per level
    for a comparison), well inside what SQLite itself evaluates, and a
    bound tree can be taller still than any parsed one (a select-list
    alias spliced into `WHERE`). `_Step.EVAL` on a node pushes a finish
    step for it and then its operands, in reverse, so they are
    evaluated left to right - the same order, operand for operand, as
    the recursive version this replaces, which keeps which error is
    raised first, and every short-circuit, unchanged:

    - `AND`/`OR` evaluate the left operand, then decide
      (`AND_AFTER_LEFT`/`OR_AFTER_LEFT`) whether the right one is
      needed at all (issue #51).
    - `IN` evaluates the left operand once per list element, paired
      with that element, and not at all for `IN ()`; `BETWEEN`
      evaluates its operand twice, once per bound (#137 tracks both).
    - `LIKE` evaluates `left`, `pattern`, then `escape`, before any
      check on the escape.

    Each finish step is a plain function below (`_finish_binary`,
    `_finish_like`, ...) over already-evaluated operands.
    """
    work: list[tuple[_Step, Expr]] = [(_Step.EVAL, expr)]
    results: list[Value | Bool3] = []
    while work:
        step, node = work.pop()
        if step is _Step.EVAL:
            _start(node, row, work, results)
        elif step is _Step.FINISH_BINARY:
            right = results.pop()
            left = results.pop()
            results.append(_finish_binary(node, left, right, schema))
        elif step is _Step.FINISH_NEGATE:
            results.append(_finish_negate(results.pop()))
        elif step is _Step.FINISH_IS:
            right = results.pop()
            left = results.pop()
            results.append(_finish_is(node, left, right, schema))
        elif step is _Step.AND_AFTER_LEFT:
            left_bool3 = coerce_to_bool3(results.pop())
            if left_bool3 is False:
                # Short-circuit (issue #51): SQLite evaluates AND left to
                # right and stops once the left operand is FALSE, never
                # touching the right - see the module docstring's "Short-
                # circuit AND/OR" section. Exact under three-valued logic:
                # and3(FALSE, x) is FALSE for every x, NULL included, so
                # this changes nothing about the *result*, only whether the
                # right operand is evaluated at all. A NULL left operand is
                # not "decided" and still falls through.
                results.append(False)
            else:
                results.append(left_bool3)
                work.append((_Step.FINISH_AND, node))
                work.append((_Step.EVAL, node.right))
        elif step is _Step.FINISH_AND:
            right_bool3 = coerce_to_bool3(results.pop())
            left_bool3 = results.pop()
            results.append(values.and3(left_bool3, right_bool3))
        elif step is _Step.OR_AFTER_LEFT:
            left_bool3 = coerce_to_bool3(results.pop())
            if left_bool3 is True:
                # Short-circuit (issue #51): or3(TRUE, x) is TRUE for every
                # x, NULL included - same reasoning as AND above, mirrored.
                results.append(True)
            else:
                results.append(left_bool3)
                work.append((_Step.FINISH_OR, node))
                work.append((_Step.EVAL, node.right))
        elif step is _Step.FINISH_OR:
            right_bool3 = coerce_to_bool3(results.pop())
            left_bool3 = results.pop()
            results.append(values.or3(left_bool3, right_bool3))
        elif step is _Step.FINISH_NOT:
            results.append(values.not3(coerce_to_bool3(results.pop())))
        elif step is _Step.FINISH_LIKE:
            escape = results.pop() if node.escape is not None else None
            pattern = results.pop()
            left = results.pop()
            results.append(_finish_like(node, left, pattern, escape))
        elif step is _Step.FINISH_IN:
            count = 2 * len(node.values)
            pairs = results[len(results) - count :]
            del results[len(results) - count :]
            results.append(_finish_in(node, pairs, schema))
        elif step is _Step.FINISH_BETWEEN:
            high = results.pop()
            operand_for_high = results.pop()
            low = results.pop()
            operand_for_low = results.pop()
            results.append(_finish_between(node, operand_for_low, low, operand_for_high, high, schema))
        else:
            raise AssertionError(f"exec/expression.py: unhandled evaluation step {step}")
    (result,) = results
    return result


def _start(
    node: Expr, row: Row, work: list[tuple[_Step, Expr]], results: list[Value | Bool3]
) -> None:
    """`evaluate()`'s `_Step.EVAL`: a leaf's value goes straight onto
    *results*; any other node pushes its finish step onto *work*, then
    its operands in reverse, so they come off the stack - and are
    evaluated - left to right."""
    if isinstance(node, Literal):
        results.append(node.value)
        return
    if isinstance(node, BoundColumnRef):
        results.append(row[node.offset])
        return
    if isinstance(node, Star):
        # The binder expands every Star before this module ever sees a
        # tree (per sql/binder.py's own docstring) - reaching here is
        # a "should never happen" bug upstream, not UX to design for.
        raise AssertionError(
            "exec/expression.py: a Star reached the evaluator; the binder must expand it first"
        )
    if isinstance(node, FunctionCall):
        raise EvalError(
            f"{node.name}(...) is not supported here: aggregate and scalar function calls are "
            "not evaluated by exec/expression.py (see its module docstring)",
            node.position,
        )
    if isinstance(node, BinaryOp):
        work.append((_Step.FINISH_BINARY, node))
        work.append((_Step.EVAL, node.right))
        work.append((_Step.EVAL, node.left))
        return
    if isinstance(node, UnaryOp):
        if node.op is UnaryOperator.POS:
            # `+x` is `x`, untouched - see `_finish_negate`'s docstring.
            work.append((_Step.EVAL, node.operand))
            return
        if (
            isinstance(node.operand, Literal)
            and isinstance(node.operand.value, float)
            and node.operand.value == _INT64_MIN_MAGNITUDE_AS_FLOAT
        ):
            # `-9223372036854775808` written in source - see
            # `_finish_negate`'s docstring.
            results.append(INT64_MIN)
            return
        if isinstance(node.operand, Literal) and isinstance(node.operand.value, float):
            # A REAL literal directly under `-` (parentheses are not a
            # node): SQLite folds it to a negative literal, a true sign
            # flip, so `-(0.0)` is `-0.0` - unlike `_finish_negate`'s
            # `0 - x` for everything else (issue #110).
            results.append(-node.operand.value)
            return
        work.append((_Step.FINISH_NEGATE, node))
        work.append((_Step.EVAL, node.operand))
        return
    if isinstance(node, Is):
        work.append((_Step.FINISH_IS, node))
        work.append((_Step.EVAL, node.right))
        work.append((_Step.EVAL, node.left))
        return
    if isinstance(node, And):
        work.append((_Step.AND_AFTER_LEFT, node))
        work.append((_Step.EVAL, node.left))
        return
    if isinstance(node, Or):
        work.append((_Step.OR_AFTER_LEFT, node))
        work.append((_Step.EVAL, node.left))
        return
    if isinstance(node, Not):
        work.append((_Step.FINISH_NOT, node))
        work.append((_Step.EVAL, node.operand))
        return
    if isinstance(node, Like):
        work.append((_Step.FINISH_LIKE, node))
        if node.escape is not None:
            work.append((_Step.EVAL, node.escape))
        work.append((_Step.EVAL, node.pattern))
        work.append((_Step.EVAL, node.left))
        return
    if isinstance(node, In):
        if not node.values:
            # `IN ()` folds over zero elements: FALSE, and the left
            # operand is never evaluated - see `_finish_in`.
            results.append(values.not3(False) if node.negated else False)
            return
        work.append((_Step.FINISH_IN, node))
        for element in reversed(node.values):
            work.append((_Step.EVAL, element))
            work.append((_Step.EVAL, node.left))
        return
    if isinstance(node, Between):
        work.append((_Step.FINISH_BETWEEN, node))
        work.append((_Step.EVAL, node.high))
        work.append((_Step.EVAL, node.operand))
        work.append((_Step.EVAL, node.low))
        work.append((_Step.EVAL, node.operand))
        return
    raise AssertionError(f"exec/expression.py: unhandled expression node type {type(node).__name__}")


# --- The Value/Bool3 coercion boundary (issues #38, #63) -------------------
#
# Two small, pure functions of evaluate()'s own return value - deliberately
# not a `position` parameter threaded through evaluate()'s recursive
# dispatch. See the module docstring's "Value or Bool3, decided by node
# shape, not calling context" section for why: `evaluate()` never needs to
# know what position its *own* result is about to be used in - each of its
# branches already knows, structurally, what position its *children's*
# results are in, purely from which node it is currently dispatching on.
#
# `coerce_to_value` originally (#38) had exactly one caller, from outside
# evaluate()'s own recursion: `Project` (`exec/operators.py`), on a
# select-list item's root result. Issue #63 found the same gap #38 round 2
# already found for `coerce_to_bool3` below, one recursion level deeper for
# the opposite direction: a predicate-shaped result reaching a *nested*
# Value-requiring operand - a comparison operand, `IS`'s two sides,
# `BETWEEN`'s operand/low/high, `IN`'s left operand and each list element,
# `||`'s two sides, `LIKE`'s two sides, and arithmetic/unary-minus's operand
# - still raised a bare TypeError. `coerce_to_value` now has callers
# *inside* evaluate()'s own recursive dispatch too: `_affinity_pair`
# wraps both its `evaluate()` calls in `coerce_to_value` before affinity is
# applied (one shared chokepoint fixing comparison, `IS`, `BETWEEN`, and
# `IN`, since all four route their operands through it), and `_finish_binary`'s
# `||`/arithmetic branches, `_finish_like`, and `_finish_negate` each
# add their own call, equally small. Same non-`position` reasoning as
# `coerce_to_bool3`'s second caller below: each of these already knows,
# structurally, that its own operand must be a `Value`, from the node it is
# currently dispatching on, never from an external mode passed in.
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
# a caller has to tell it. Between, In and Like never need `coerce_to_bool3`
# - each already builds its own Bool3 result from values.py's own comparison
# functions (`values.eq`/`ge`/`le`/...), never from a raw, uncoerced
# evaluate() result, so there is nothing left to coerce in that direction;
# #63 above is what fixes their *operands*, the opposite direction, instead.
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
    `arithmetic_operand` already implements for arithmetic (`'0abc'`
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
    return arithmetic_operand(result) != 0


def _finish_between(
    expr: Between,
    operand_for_low: Value | Bool3,
    low: Value | Bool3,
    operand_for_high: Value | Bool3,
    high: Value | Bool3,
    schema: Schema,
) -> Bool3:
    """`x BETWEEN low AND high` is `values.and3(values.ge(x, low),
    values.le(x, high))`, per this issue's own criteria - not bespoke
    logic. Confirmed against `sqlite3`: `20 BETWEEN 30 AND NULL` is
    `FALSE`, not `NULL` - the first comparison alone already makes it
    `FALSE`, and `and3(FALSE, NULL)` is `FALSE`. Affinity is applied
    to each bound independently: `x`'s own affinity can interact
    differently with `low` and with `high`.

    `evaluate()` has already evaluated, in this order, the operand, the
    low bound, the operand again and the high bound (#137 tracks the
    second evaluation of the operand).
    """
    operand_low_left, operand_low_right = _affinity_pair(
        expr.operand, operand_for_low, expr.low, low, schema
    )
    operand_high_left, operand_high_right = _affinity_pair(
        expr.operand, operand_for_high, expr.high, high, schema
    )
    result = values.and3(
        values.ge(operand_low_left, operand_low_right),
        values.le(operand_high_left, operand_high_right),
    )
    return values.not3(result) if expr.negated else result


def _finish_in(expr: In, pairs: list[Value | Bool3], schema: Schema) -> Bool3:
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
    `_affinity_pair`'s `right_has_affinity=False` is the
    single change this makes: `x`'s own affinity still applies to each
    element, the element's affinity never does.

    This is genuinely different from `_finish_between`, not a case that
    "tidying" the two onto one path would preserve: a `BETWEEN` bound
    is an independent RHS operand, symmetric with `=`, and keeps its
    own affinity. Confirmed against `sqlite3` for the identical
    operand shape, same row (`n = 1`): `'1' BETWEEN n AND n` -> `1`,
    `'1' IN (n)` -> `0`. `_finish_between` is intentionally left calling
    `_affinity_pair` with its default `right_has_affinity=True`.

    *pairs* is what `evaluate()` produced for a non-empty list: the left
    operand and then the element, once per element, in list order -
    `[x, v1, x, v2, ...]`. `IN ()` never reaches here: `evaluate()`
    answers it without evaluating the left operand at all.
    """
    result: Bool3 = False
    for index, element in enumerate(expr.values):
        left, right = _affinity_pair(
            expr.left, pairs[2 * index], element, pairs[2 * index + 1], schema, right_has_affinity=False
        )
        result = values.or3(result, values.eq(left, right))
    return values.not3(result) if expr.negated else result


# --- LIKE: unconditional text coercion, no affinity, ASCII-only fold ----
#
# `ascii_fold` (imported above, `historian.ascii`, issue #53): SQLite's
# own identifier- and `LIKE`-matching rule, not Python's Unicode-aware
# `str.lower()` (confirmed against `sqlite3`: `'café' LIKE 'CAFÉ'` is
# `FALSE`, the é/É pair is not folded). Before this issue this module
# kept its own second copy rather than importing `sql/binder.py`'s:
# importing `sql/binder.py` transitively imports `historian.tables.
# blame` (for `BLAME_SCHEMA`), which imports `subprocess` at module
# level, which this module's own constraints ruled out. `historian.
# ascii` has no such import, so that trade-off no longer applies.


def _like_pattern_to_regex(pattern: str, escape: str | None = None) -> re.Pattern[str]:
    """Compile a `LIKE` pattern (`%` any sequence including empty, `_`
    exactly one character) to a `re.fullmatch`-ready pattern. Every
    other character is escaped literally via `re.escape`, so the
    pattern text can never be interpreted as a regex metacharacter by
    accident. `re.DOTALL` so `_`/`%` match a newline too - `LIKE` has
    no notion of "line" - confirmed against `sqlite3`: `select ('a' ||
    char(10) || 'c') like 'a_c';` -> `1`. This still holds with an
    `ESCAPE` clause present: `select ('a' || char(10) || '%') like
    'a_!%' escape '!';` -> `1`.

    *escape* (issue #51), when given, is a single character read from
    *pattern* **before** any ASCII fold - `_finish_like` below passes the
    caller's raw, un-folded pattern text, never `ascii_fold`ed first,
    because escape-character recognition is case-sensitive / exact-
    codepoint even though `LIKE`'s *matched text* comparison is
    ASCII-case-insensitive. Confirmed against `sqlite3`, all four
    probes: `select 'a%b' like 'axb' escape 'X';` -> `0` (lowercase `x`
    in the pattern is not recognised as uppercase escape `X`, so it
    stays an ordinary letter); `select 'aXb' like 'axb' escape 'X';` ->
    `1` (same: pattern's `x` is ordinary and ASCII-folds against input
    `X`); `select 'a%b' like 'aXb' escape 'x';` -> `0`; `select 'a%b'
    like 'ax%b' escape 'X';` -> `0`. Folding first (i.e. scanning
    `ascii_fold(pattern)` for the escape character) would make
    recognition wrongly case-insensitive - this is why `_finish_like`
    folds only the characters that end up literal, one at a time,
    inside this function, rather than folding the whole pattern text up
    front the way it still does for `left_text`.

    An escape character immediately preceding `%`, `_`, or itself makes
    that following character literal (confirmed: `select 'ab' like
    'a!b' escape '!';` -> `1`; `select 'a!b' like 'a!!b' escape '!';`
    -> `1`); immediately preceding any other character it is still a
    no-op, matching that character literally, which the plain `else`
    branch below already does once the escape has been consumed. An
    escape character with nothing after it - at the very end of the
    pattern - makes the whole pattern unsatisfiable, not a literal `!`
    and not an error: confirmed, `select 'a!' like 'a!' escape '!';`
    -> `0` even though the two strings are identical. `(?!)` is a
    standard "never matches" regex idiom (a negative lookahead on the
    empty string, which always matches, so the lookahead always
    fails) - used here rather than raising, since an unsatisfiable
    pattern is a valid `LIKE` outcome (`FALSE`), not a runtime error.
    """
    pieces = []
    index = 0
    length = len(pattern)
    while index < length:
        ch = pattern[index]
        if escape is not None and ch == escape:
            index += 1
            if index >= length:
                pieces.append("(?!)")  # trailing escape: unsatisfiable
                break
            pieces.append(re.escape(ascii_fold(pattern[index])))
            index += 1
            continue
        if ch == "%":
            pieces.append(".*")
        elif ch == "_":
            pieces.append(".")
        else:
            pieces.append(re.escape(ascii_fold(ch)))
        index += 1
    return re.compile("".join(pieces), re.DOTALL)


def _finish_like(
    expr: Like, left_result: Value | Bool3, pattern_result: Value | Bool3, escape_result: Value | Bool3
) -> Bool3:
    """`LIKE` / `NOT LIKE` [`ESCAPE <expr>`] (the clause added by issue
    #51). Confirmed against `sqlite3`: unlike every comparison above,
    `LIKE` never applies column affinity - both operands are cast to
    their SQLite text representation unconditionally (`n LIKE '5'` is
    `TRUE` for the INTEGER column `n=5`; `5 LIKE 5`, two integer
    literals, is also `TRUE`). `NULL` on either side makes the whole
    expression `NULL` (`NULL LIKE anything`, `anything LIKE NULL`).
    `NOT LIKE` is `values.not3` applied to the plain (un-negated)
    result - never a separately reasoned-out negation - which is what
    keeps `NULL` propagation correct through the negation for free.

    The escape operand (when `expr.escape is not None`) goes through
    the same `coerce_to_value` pipeline as `left`/
    `pattern` (#63's coercion applies here too - `ESCAPE (1=1)` reads
    as the text `'1'`, confirmed: `select '1' like '11' escape (1=1);`
    -> `1`) and is evaluated **unconditionally**, before the combined
    `NULL` check below - confirmed live, the single-character check
    fires even when `left`/`pattern` is `NULL`: `select null like 'x'
    escape 'ab';` and `select 'x' like null escape 'ab';` both raise
    `ESCAPE expression must be a single character`, not `NULL`. A
    `NULL` escape operand itself, though, makes the *whole* predicate
    `NULL` with no error at all - confirmed: `select typeof('10%' LIKE
    '10!%' ESCAPE NULL);` -> `null`, even though the missing escape
    text could never pass the length check; the `NULL` check comes
    first for that one specific operand.

    "Single character" is counted the same way SQLite's own `length()`
    counts it: Unicode code points (`len()` on a decoded `str`), not
    UTF-8 bytes - confirmed: `select length('😀');` -> `1` although
    `😀` is 4 bytes in UTF-8. An escape of the wrong length raises
    `EvalError` at evaluate()-time (never at parse/bind time - see
    `sql/parser.py`'s `_parse_optional_escape`, which never inspects
    the escape operand's value) with `sqlite3`'s own wording, reused
    verbatim: "ESCAPE expression must be a single character" - the
    identical message for both empty and 2+-character escapes,
    confirmed: `select '10%' like '10!%' escape '';` and `select '10%'
    like '10!%' escape '!!';` raise the same text. This still applies
    under `NOT LIKE`, before the negation, the same as `NULL`
    propagation already does.

    The three operands arrive already evaluated by `evaluate()`, in the
    order `left`, `pattern`, `escape` (*escape_result* is `None` when
    there is no `ESCAPE` clause).
    """
    left = coerce_to_value(left_result)
    pattern = coerce_to_value(pattern_result)
    escape_char: str | None = None
    escape_is_null = False
    if expr.escape is not None:
        escape_value = coerce_to_value(escape_result)
        if escape_value is None:
            escape_is_null = True
        else:
            escape_text = _coerce_to_text(escape_value)
            if len(escape_text) != 1:
                raise EvalError(
                    "ESCAPE expression must be a single character",
                    expr.escape.position,
                )
            escape_char = escape_text
    if left is None or pattern is None or escape_is_null:
        result: Bool3 = None
    else:
        left_text = ascii_fold(_coerce_to_text(left))
        pattern_text = _coerce_to_text(pattern)
        result = bool(_like_pattern_to_regex(pattern_text, escape_char).fullmatch(left_text))
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
    expression, even one that merely mentions a column.

    A bare `BoundColumnRef` can itself point at a column declared
    `None` (issue #99): `Aggregate`'s output column for an aggregate
    call or a computed `GROUP BY` key, which `plan/planner.py`'s
    `_split_expr` rewrites into exactly the same node shape as a real
    column reference. The answer is then `None` too - no affinity -
    read straight from the schema, so this function never needs to
    know which operator produced the row."""
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
        return try_numeric_affinity(left), try_numeric_affinity(right)
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


def try_numeric_affinity(value: Value) -> Value:
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


def _finish_negate(operand_result: Value | Bool3) -> Value:
    """Unary `-`, over its already-evaluated operand; unary `+` and the
    int64-minimum literal never get here - `evaluate()` handles both
    itself (`_start`), for the reasons below.

    `-` performs real numeric negation, going through the same
    leading-prefix text coercion arithmetic uses (`-'5'` is `-5`,
    `-'abc'` is `0`) and the same int64-overflow-to-REAL rule.

    A REAL is negated as SQLite does it, `0 - x` rather than a sign
    flip (issue #110): the two agree for every non-zero value and for
    the infinities, and differ only for a zero, where `0 - x` gives
    `+0.0` for both `0.0` and `-0.0` - so `-(0.0 * 1)` and `-'0.0'`
    are `+0.0`. The one exception is a REAL literal directly under `-`,
    which SQLite folds to a negative literal (`-(0.0)` is `-0.0`);
    `_start` handles that structurally, so it never gets here. Unary
    `+` is its own node, so `-(+0.0)` is not that shape and is `+0.0`.

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
    operand = coerce_to_value(operand_result)
    if operand is None:
        return None
    numeric = arithmetic_operand(operand)
    if isinstance(numeric, int):
        return _int64_bounded(-numeric)
    # `0 - x`, not `-x`: they differ only for a zero, where SQLite's
    # subtraction gives `+0.0` for both `0.0` and `-0.0` (issue #110).
    return squash_nan(0.0 - numeric)


# --- BinaryOp: arithmetic, concatenation, comparison --------------------

_ARITHMETIC_OPS = frozenset(
    {Operator.ADD, Operator.SUB, Operator.MUL, Operator.DIV, Operator.MOD}
)

#: One `values.py` comparison function per comparison `Operator`. All
#: six take affinity-adjusted operands - see `_finish_binary` below.
_COMPARISON_FNS = {
    Operator.EQ: values.eq,
    Operator.NE: values.ne,
    Operator.LT: values.lt,
    Operator.LE: values.le,
    Operator.GT: values.gt,
    Operator.GE: values.ge,
}


def _finish_binary(
    expr: BinaryOp, left_result: Value | Bool3, right_result: Value | Bool3, schema: Schema
) -> Value | Bool3:
    if expr.op in _ARITHMETIC_OPS:
        left = coerce_to_value(left_result)
        right = coerce_to_value(right_result)
        return _arithmetic(expr.op, left, right)
    if expr.op is Operator.CONCAT:
        left = coerce_to_value(left_result)
        right = coerce_to_value(right_result)
        if left is None or right is None:
            return None
        return _coerce_to_text(left) + _coerce_to_text(right)
    if expr.op in _COMPARISON_FNS:
        left, right = _affinity_pair(expr.left, left_result, expr.right, right_result, schema)
        return _COMPARISON_FNS[expr.op](left, right)
    raise AssertionError(f"exec/expression.py: BinaryOp operator not yet handled: {expr.op}")


def _affinity_pair(
    left_expr: Expr,
    left_result: Value | Bool3,
    right_expr: Expr,
    right_result: Value | Bool3,
    schema: Schema,
    *,
    right_has_affinity: bool = True,
) -> tuple[Value, Value]:
    """Apply column affinity to both sides of a comparison-shaped pair
    of operands (`=`/`<>`/.../`IS`/`IS NOT`, each bound of `BETWEEN`),
    already evaluated by `evaluate()`. *left_expr*/*right_expr* are the
    nodes that produced them, which is all affinity looks at (see
    `_affinity_of`). Shared by every predicate that goes through "the
    identical affinity algorithm" as `=` - which is every caller except
    `_finish_in`.

    `right_has_affinity` (issue #47) is the explicit, structural
    escape hatch `_finish_in` uses: SQLite's own rule is that the
    right-hand side of `IN`/`NOT IN` *with a list* has no affinity at
    all, regardless of what kind of expression a given element is -
    unlike `=`/`IS`/`BETWEEN`, where each operand independently asks
    `_affinity_of` the normal way. `_finish_in` passes
    `right_has_affinity=False` for every element; every other caller
    takes the default and is unaffected. A plain keyword parameter,
    not a dynamic-dispatch trick (`AGENTS.md`), and it does not touch
    `_finish_between`, which must keep applying each bound's own
    affinity independently - see `_finish_in`'s docstring for the
    evidence that the two operators, though structurally identical
    here, are not supposed to behave alike."""
    left = coerce_to_value(left_result)
    right = coerce_to_value(right_result)
    right_affinity = _affinity_of(right_expr, schema) if right_has_affinity else None
    return _apply_affinity(left, _affinity_of(left_expr, schema), right, right_affinity)


def _finish_is(expr: Is, left_result: Value | Bool3, right_result: Value | Bool3, schema: Schema) -> bool:
    """`IS` / `IS NOT`, including the `IS NULL` / `IS NOT NULL`
    spelling (`sql/ast.py`'s own docstring: `IS NULL` is `IS` against
    a `NULL` literal, not a separate node). Goes through the identical
    affinity algorithm as `=`/`<>` before calling `values.is_`/
    `values.is_not` - confirmed against `sqlite3`: `5 IS '5'` is
    `FALSE` like `5 = '5'`, but `n IS '5'` (INTEGER column) is `TRUE`."""
    left, right = _affinity_pair(expr.left, left_result, expr.right, right_result, schema)
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

    Deliberately a separate scanner from `sql/lexer.py`'s
    `_read_number`, not merged into it (see that function's own
    comment): this one accepts an exponent, because SQLite's own
    text-to-number conversion does, while the lexer must reject one
    for now. Merging the two, or teaching the lexer exponents, is
    issue #6 (v2 backlog) - confirmed still out of scope by issue
    #53's own grooming, which only moved the two int64-bound and
    ASCII-predicate duplicates, not this one.

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

    A plain digit run - no `.`, no exponent - is an `int` only if it
    fits int64; otherwise it is REAL, right here at conversion time,
    before any operator sees it (issue #105, SQLite's own rule:
    `'9223372036854775808' - 1` is `9.22337203685478e+18`, not the
    exact `9223372036854775807`). That REAL is `float()` of the digit
    *text*, never of a Python `int`: `float(text)` overflows a huge
    numeral to `inf` as SQLite does, while `float(int(text))` raises
    `OverflowError`, and `int(text)` itself raises `ValueError` past
    Python's 4300-digit limit - so `_int64_digit_run` decides the
    range from the digit text alone and never builds a big `int`.
    """
    n = len(text)
    i = start
    while i < n and text[i] in _NUMERIC_WHITESPACE:
        i += 1
    mantissa_start = i
    if i < n and text[i] in "+-":
        i += 1
    has_digits = False
    while i < n and is_ascii_digit(text[i]):
        i += 1
        has_digits = True
    is_float = False
    if i < n and text[i] == ".":
        i += 1
        is_float = True
        while i < n and is_ascii_digit(text[i]):
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
        while j < n and is_ascii_digit(text[j]):
            j += 1
        if j > exponent_digits_start:
            exponent_end = j
            is_float = True
    end = exponent_end
    number_text = text[mantissa_start:end]
    # Constructing a new Value from source text, per this module's own
    # float()-call test - not a lossy comparison cast, the thing the
    # 2026-08-27 decision actually forbids. See that test's docstring.
    value: int | float
    if is_float:
        value = float(number_text)
    else:
        exact = _int64_digit_run(number_text)
        if exact is None:
            value = float(number_text)
        else:
            value = exact
    return value, end


#: The number of digits in `INT64_MIN`'s magnitude, the longest int64.
#: A digit run with more significant digits than this cannot fit.
_INT64_MAX_DIGITS = 19


def _int64_digit_run(number_text: str) -> int | None:
    """`number_text` - an optional sign then ASCII digits only, as
    `_scan_number` scanned it - as an `int` if it fits SQLite's int64
    range, else `None` (the caller converts the text to REAL instead).

    Leading zeros are dropped before converting, and a run with more
    than `_INT64_MAX_DIGITS` significant digits is rejected without
    converting at all, so `int()` only ever sees at most 19 digits -
    never a string past Python's 4300-digit `int()` limit, and never a
    value large enough to matter."""
    negative = False
    digits = number_text
    if digits[0] == "+" or digits[0] == "-":
        negative = digits[0] == "-"
        digits = digits[1:]
    first_significant = 0
    while first_significant < len(digits) and digits[first_significant] == "0":
        first_significant += 1
    significant = digits[first_significant:]
    if len(significant) > _INT64_MAX_DIGITS:
        return None
    magnitude = int(significant) if significant else 0
    value = -magnitude if negative else magnitude
    if value < INT64_MIN or value > INT64_MAX:
        return None
    return value


def _coerce_arithmetic_text(text: str) -> int | float:
    """SQLite's leading-prefix text-to-number coercion for arithmetic
    (distinct from affinity's whole-string rule below, per this
    issue's own grooming): the longest valid numeric prefix, or `0`
    (an `int`) if the text contains no digit at all - confirmed
    against `sqlite3`: `'abc'+1` is `1`, `'   '+1` is `1`."""
    scanned = _scan_number(text, 0)
    return scanned[0] if scanned is not None else 0


def arithmetic_operand(value: Value) -> int | float:
    """A `Value` as an arithmetic operand: a number passes through
    unchanged, text goes through the leading-prefix coercion above.
    Never called with `None` - the caller checks for NULL first, since
    NULL's propagation through arithmetic is "the whole expression is
    NULL", not "NULL contributes 0".

    Defensive `bool` guard, mirroring the exact pattern `values.py`'s
    own `_rank` and this module's own `_coerce_to_text` already use for
    the identical reason (issue #63): a raw Python `bool` must never
    reach arithmetic. Not reachable through `evaluate()` itself once
    `_finish_binary`'s arithmetic branch and `_finish_negate`
    both call `coerce_to_value()` on their operand first - this guard
    exists so a future regression that removes either call-site
    coercion fails loudly (a `TypeError` here) rather than silently
    keeping today's `bool`-subclasses-`int` accident. See the module
    docstring's arithmetic section for why no black-box test on
    `evaluate()`'s return value alone can tell the deliberate fix from
    the accident - this guard, paired with the call-site coercion, is
    what makes it possible."""
    if isinstance(value, bool):
        raise TypeError(
            "bool is not a SQL Value; a Bool3 predicate result has leaked "
            f"into an arithmetic operand position (got {value!r})"
        )
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
    if INT64_MIN <= exact <= INT64_MAX:
        return exact
    return float(exact)


def squash_nan(result: float) -> float | None:
    """NaN can only ever be a computed *result* here, never a stored
    value (`values.py` rejects one outright) - catches `Inf-Inf`,
    `Inf*0`, `Inf/Inf`, not only `0.0/0.0`
    (`_docs/decisions.md`, 2026-08-31). Exported (issue #104) so
    `exec/operators.py`'s `_Accumulator.finish()` can squash a NaN
    produced *inside* a running `sum`/`avg` total the same way, rather
    than reimplementing this check a second time (issue #53's
    precedent, already followed once for `try_numeric_affinity`/
    `arithmetic_operand` in #88)."""
    if math.isnan(result):
        return None
    return result


def _int64_truncated(value: int | float) -> int:
    """`value` truncated toward zero to an `int`, then clamped to
    SQLite's `int64` range - the same conversion `CAST(x AS INTEGER)`
    uses, and the REAL-operand half of `%`'s own contract (issue #75):
    REAL operands are truncated toward zero and clamped to int64
    *before* the remainder is computed. An `int` operand passes
    through unchanged - it already fits, since every `Value` int is
    kept within int64 range by `_int64_bounded` at the point it was
    produced.

    Mirrors `_int64_bounded`'s style but is not a variant of it:
    `_int64_bounded` narrows an already-exact Python `int` that may
    have overflowed int64 through arithmetic; this instead starts from
    a `float` that may carry a fractional part and truncates it first.
    `math.trunc()` on a `float`, not `int()` - both discard the
    fractional part identically, but `math.trunc` reads as "the
    conversion this function documents", while a bare `int()` reads as
    an accident waiting to be un-clamped by a future edit. Unbounded
    Python `int` throughout: `math.trunc(1e300)` is an exact (if huge)
    Python integer, not a lossy cast, so the clamp below compares it
    against `INT64_MIN`/`INT64_MAX` exactly rather than through a
    second `float` conversion."""
    if isinstance(value, int):
        return value
    # Infinity first (issue #106): `math.trunc(inf)` raises
    # `OverflowError`, while SQLite's `doubleToInt64` clamps it like
    # any other out-of-range magnitude - `('1e400'+0) % 3` is `1.0`.
    # NaN never reaches here: `squash_nan` has already made it NULL.
    if value == math.inf:
        return INT64_MAX
    if value == -math.inf:
        return INT64_MIN
    truncated = math.trunc(value)
    if truncated < INT64_MIN:
        return INT64_MIN
    if truncated > INT64_MAX:
        return INT64_MAX
    return truncated


def _modulo_text_operand(text: str) -> tuple[int, bool]:
    """A TEXT operand of `%` (issue #106): its integer value, and
    whether it counts as REAL for the result's storage class.

    The two come from different scans, because SQLite's own
    `OP_Remainder` takes them from different places. The class is
    `numericType`'s, the general text-to-number conversion
    `arithmetic_operand` already mirrors - so `'1e3'` and `'12.0'` are
    REAL, `'7abc'` and `'5e'` INTEGER. The value is
    `sqlite3VdbeIntValue`'s, which for TEXT is `sqlite3Atoi64`: skip
    whitespace, an optional sign, then digits up to the first
    non-digit, clamped to int64. A `.` or an exponent is never part of
    it, so `'1e3'` reads `1`, `'1.5e2'` reads `1`, `'1e400'` reads `1`
    (never `inf`), and no digits at all reads `0`. Confirmed with
    tests/oracle.py (module sqlite3 3.45.1): `'1e3' % 7` is `1.0`,
    `'99999999999999999999e0' % 7` is `0.0` (int64 max % 7).

    `_scan_number` is not touched: `+ - * /` and `sum`/`avg` still
    need its exponent-accepting grammar for their values."""
    is_real = isinstance(_coerce_arithmetic_text(text), float)
    n = len(text)
    i = 0
    while i < n and text[i] in _NUMERIC_WHITESPACE:
        i += 1
    negative = False
    if i < n and (text[i] == "+" or text[i] == "-"):
        negative = text[i] == "-"
        i += 1
    digits_start = i
    while i < n and is_ascii_digit(text[i]):
        i += 1
    if i == digits_start:
        return 0, is_real
    sign = "-" if negative else ""
    exact = _int64_digit_run(sign + text[digits_start:i])
    if exact is not None:
        return exact, is_real
    return (INT64_MIN if negative else INT64_MAX), is_real


def _modulo_operand(value: Value) -> tuple[int, bool]:
    """A non-NULL `%` operand as the `int` the remainder is computed
    from, plus whether it counts as REAL for the result's class: an
    INTEGER passes through, a REAL is truncated and clamped
    (`_int64_truncated`), TEXT goes through `_modulo_text_operand`.
    The `bool` guard is `arithmetic_operand`'s own (issue #63)."""
    if isinstance(value, bool):
        raise TypeError(
            "bool is not a SQL Value; a Bool3 predicate result has leaked "
            f"into an arithmetic operand position (got {value!r})"
        )
    if isinstance(value, str):
        return _modulo_text_operand(value)
    if isinstance(value, float):
        return _int64_truncated(value), True
    return value, False


def _mod_result(remainder: int, is_real: bool) -> int | float:
    """`%`'s remainder, in its final storage class. The remainder
    itself is always computed as an exact `int` (`_int64_truncated`
    narrows both operands to `int` first, and `_truncating_int_div`
    stays in `int` throughout), but the storage class it is reported
    in follows the *original* operands, not the truncated ones - REAL
    if either was REAL, per issue #75.

    The `float()` call here is the same "arithmetic result production"
    exception `_int64_bounded` documents, not a comparison cast - see
    the module docstring and
    `test_no_stray_float_calls_outside_the_named_exceptions`, which
    names this function as an allowed exception alongside it."""
    return float(remainder) if is_real else remainder


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
    """`+ - * / %`. NULL propagates through every operator (`NULL + 1`
    is `NULL`). Division by zero - integer or float - is `None`
    directly, checked before any division is attempted: Python's `/`
    raises `ZeroDivisionError` for a zero denominator in both the
    `int/int` and `float/float` cases - it does not produce
    `inf`/`nan` the way SQLite's underlying arithmetic does - so this
    cannot be discovered by computing first and inspecting the result
    afterward the way the `Inf - Inf` family can (`_docs/spec.md`
    §3's own NaN note; `_docs/decisions.md`, 2026-08-31).

    `%` (issue #75) is its own branch, not a variant of `DIV`'s, and
    runs before `arithmetic_operand` is called: `_modulo_operand` reads
    each operand's `int` and its REAL-or-not class separately - for
    TEXT the two come from different scans (issue #106), so `'1e3' % 7`
    is REAL `1.0` - and the result is REAL if either operand was. Reuses `_truncating_int_div` exactly as
    `DIV` does (`remainder = left - _truncating_int_div(left, right) *
    right`), so no separate sign-fixup logic exists for `%` - a wrong
    quotient sign in `_truncating_int_div` would break both operators
    identically. The zero check is against the truncated divisor, not
    the original value (`7 % 0.5` is `NULL`, since `0.5` truncates to
    `0`), so `_int64_truncated` runs before that check, not after.
    """
    if left is None or right is None:
        return None
    if op is Operator.MOD:
        left_int, left_is_real = _modulo_operand(left)
        right_int, right_is_real = _modulo_operand(right)
        if right_int == 0:
            return None
        remainder = left_int - _truncating_int_div(left_int, right_int) * right_int
        return _mod_result(remainder, left_is_real or right_is_real)
    left_num = arithmetic_operand(left)
    right_num = arithmetic_operand(right)
    if op is Operator.DIV:
        if right_num == 0:
            return None
        if isinstance(left_num, int) and isinstance(right_num, int):
            return _int64_bounded(_truncating_int_div(left_num, right_num))
        return squash_nan(left_num / right_num)
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
    return squash_nan(result)
