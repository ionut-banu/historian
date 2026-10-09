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
2026-08-27: "`typeof(true)` is `integer`"). The *shape* of the result -
`Value` vs `Bool3` - is still decided from each node's own shape
alone, never from where the whole expression sits. Issue #38 adds
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

Where evaluation stops does depend on calling context (issue #111)
-------------------------------------------------------------------

Up to #111 this docstring said `evaluate()` "does not carry a notion
of the position this whole call's result is about to be used in".
That holds for the result's shape (above) and no longer for which
operands are evaluated. SQLite evaluates every operand of `AND`, `OR`,
`NOT` and `BETWEEN` when the result is used as a value, but at the
root of `WHERE`/`HAVING` - and through `AND`/`OR`/`NOT` below it -
stops as soon as whether the condition is `TRUE` is decided. Measured
with an exhaustive sweep against the oracle (`_docs/decisions.md`,
2026-10-01): a select-list `(line_no = 5 AND <raises>)` raises, a
`WHERE line_no = 5 AND <raises>` does not, and `WHERE line_no = NULL
AND <raises>` does not either, while `WHERE NOT (line_no = NULL AND
<raises>)` does, because under the `NOT` a `NULL` could still lead to
a kept row. `IN` stops at its first matching element everywhere.

So the context does reach the evaluator, as two entry points rather
than a mode flag on one: `evaluate()` for a value (`Project`, `Sort`,
`GROUP BY` keys, aggregate arguments) and `evaluate_condition()` for
`Filter`. Inside, each work-stack entry carries a `_Context` - `VALUE`,
or condition context with `NULL` counting as `FALSE` or as `TRUE` -
which only `AND`/`OR`/`NOT` pass on to their operands (`NOT` flipping
it); every other node evaluates its operands as values. See `_run`.

In condition context SQLite also simplifies an `AND`/`OR` with an
integer-literal operand before evaluating it (issue #189): `x OR 1` is
`1` and `x AND 0` is `0`, so `x` never runs. `evaluate_condition()`
applies that first, in one pass over the condition
(`_simplified_condition`); `evaluate()` never does.

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
(`_apply_affinity` and everything it calls) never does this. The only
functions in this file allowed a `float()` call are the three named in
`tests/test_expression.py`'s
`test_no_stray_float_calls_outside_the_named_exceptions`: the float
formatting helper, and the two that produce an arithmetic result from
an exact `int` (`_int64_bounded`, `_mod_result`) - see that test's
docstring for why neither can be confused with the comparison path.
Text-to-number conversion is not among them: since issue #134 it
calls `historian.atof.text_to_real`, SQLite's own text-to-REAL
algorithm, which is not correctly rounded the way `float()` is.
"""

from __future__ import annotations

import math
import re
from enum import Enum, auto

from historian import values
from historian.ascii import ascii_fold, is_ascii_digit
from historian.atof import text_to_real
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
from historian.sql.walk import FixedColumnRef
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
    "apply_column_affinity",
    "arithmetic_operand",
    "coerce_to_bool3",
    "coerce_to_value",
    "evaluate",
    "evaluate_condition",
    "try_numeric_affinity",
]

# `INT64_MIN`/`INT64_MAX` (`historian.values`, issue #53), imported
# above: SQLite's `int64` bounds. Unlike the parser (which only ever
# needs the positive bound, to detect an overflowing literal),
# arithmetic here needs both - subtraction and negation can overflow
# toward either end.

#: The ASCII whitespace SQLite skips around the digits of numeric text:
#: space, `\t`, `\n`, `\v`, `\f`, `\r` (0x20, 0x09, 0x0A, 0x0B, 0x0C,
#: 0x0D), exactly - leading and trailing for the whole-string (affinity)
#: conversion, leading only for the arithmetic and `%` scans, and never
#: between a sign and its digits or inside a number. Confirmed with
#: `tests/oracle.py` (the oracle), a quoted literal and a
#: bound parameter alike: every other character is a non-number
#: character - 0x00-0x08, 0x0E-0x1F (`\x1c`-`\x1f` included), 0x7F,
#: `\x85`, `\xa0` and the Unicode spaces (`'\x1c12' + 0` and
#: `'\xa012' + 0` are `0`). `_docs/decisions.md`, issue #136. Read by
#: `_scan_number`, `%`'s scan and `_strip_numeric_whitespace`. A plain
#: string, not `str.isspace()` or a bare `str.strip()`: those are
#: Unicode-aware and differ from SQLite. Deliberately its own constant
#: rather than `sql/lexer.py`'s `_WHITESPACE`: that set is which bytes
#: separate SQL tokens (`\v` is pointedly excluded there, per
#: `_docs/decisions.md` 2026-09-01, because SQLite's tokenizer rejects
#: it), a different question from which bytes numeric conversion skips.
_NUMERIC_WHITESPACE = " \t\n\v\f\r"


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
    whose operands have already been evaluated onto the value stack,
    or decides whether the next operand is needed at all."""

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
    IN_AFTER_ELEMENT = auto()
    FINISH_EMPTY_IN = auto()
    FINISH_BETWEEN = auto()
    BETWEEN_AFTER_LOW = auto()
    FINISH_BETWEEN_HIGH = auto()


class _Context(Enum):
    """Where a node's result is used, which decides whether `AND`, `OR`
    and `BETWEEN` may stop before their last operand (issue #111;
    `_docs/spec.md` §3 "Expression evaluation").

    `VALUE`: the result is a value - a select-list item, a sort or
    group key, an aggregate argument, or an operand of anything but
    `AND`/`OR`/`NOT`. Every operand is evaluated.

    `NULL_IS_FALSE` / `NULL_IS_TRUE`: condition context - the root of
    `WHERE`/`HAVING`, and the operands of `AND`/`OR`/`NOT` in condition
    context. Only whether the clause's outcome is reached matters, and
    a `NULL` counts towards it as `FALSE` at the root (only `TRUE` keeps
    a row) and under an even number of `NOT`s, as `TRUE` under an odd
    number. `NOT` flips it; `AND`/`OR` pass it to both operands."""

    VALUE = auto()
    NULL_IS_FALSE = auto()
    NULL_IS_TRUE = auto()


def _negated_context(context: _Context) -> _Context:
    """The context of `NOT`'s operand, given the `NOT`'s own."""
    if context is _Context.NULL_IS_FALSE:
        return _Context.NULL_IS_TRUE
    if context is _Context.NULL_IS_TRUE:
        return _Context.NULL_IS_FALSE
    return _Context.VALUE


#: One entry of `evaluate()`'s work stack: the step, the node it is for,
#: the context that node is evaluated in, and - for `IN_AFTER_ELEMENT`
#: only - which list element has just been evaluated (0 otherwise).
_Work = tuple[_Step, Expr, _Context, int]


def evaluate(expr: Expr, row: Row, schema: Schema) -> Value | Bool3:
    """Evaluate *expr* against *row*, described by *schema*, as a value:
    every operand is evaluated, except that `IN` stops at the first
    element equal to its left side (issue #111). This is what every
    caller except `Filter` wants - see `evaluate_condition`.

    Returns a `historian.values.Value` for a value-shaped node, a
    `historian.values.Bool3` for a predicate-shaped one - see the
    module docstring for exactly which shapes are which. The context
    decides only which operands are evaluated, never the result's
    shape.
    """
    return _run(expr, row, schema, _Context.VALUE)


def evaluate_condition(expr: Expr, row: Row, schema: Schema) -> Value | Bool3:
    """Evaluate *expr* as one condition - the whole `HAVING`, or one
    term of a `WHERE` (`Filter` splits a `WHERE` on its top-level
    `AND`s, as SQLite does): like `evaluate()`, but `AND`, `OR`, `NOT`
    and `BETWEEN` at the top of the tree stop as soon as whether the
    condition is `TRUE` is decided, exactly where SQLite does (issue
    #111), and an `AND`/`OR` there with an integer-literal operand that
    decides it is that literal, its other operand never evaluated
    (issue #189, `_simplified_condition`).

    The result can differ from `evaluate()`'s in which errors are
    raised, and otherwise only between `FALSE` and `NULL` - an `AND`
    stopped by a `NULL` left side is `NULL` even when its right side
    would have made it `FALSE` - or in being the deciding literal's own
    value (`x OR 5` is `5`), never in whether it is `TRUE`, which is
    all `Filter` asks of it.
    """
    return _run(_simplified_condition(expr), row, schema, _Context.NULL_IS_FALSE)


# --- A condition AND/OR with an integer-literal operand (issue #189) -------
#
# `_docs/spec.md` §3 "Expression evaluation". Before SQLite evaluates an
# `AND` or `OR` reached in condition context, it replaces it by a
# simpler expression when one operand is a literal that decides it
# (`sqlite3ExprSimplifiedAndOr`, called from `sqlite3ExprIfTrue`/
# `IfFalse`; measured on the pinned oracle, `_docs/decisions.md`
# 2026-10-09): `x OR <true>` and `x AND <false>` become the literal, so
# `x` never runs; `x AND <true>` and `x OR <false>` become `x`. Both
# operands are simplified first, so a nested `AND`/`OR` that leaves such
# a literal counts too (`ERR OR (0 OR 1)`). A literal is always true or
# always false only when it is an unsigned integer literal that fits in
# 32 bits - SQLite's `EP_IsTrue`/`EP_IsFalse`, set when the token
# passes `sqlite3GetInt32` - so `-1`, `+1`, `1.0`, `'1'`, `NULL`, `1 =
# 1`, `NOT 0` and `2147483648` are not. Parentheses are not nodes, so
# `(1)` is one; a `FixedColumnRef` (#142) is a column, not a literal.
#
# Only an `AND`/`OR` reached through `AND`/`OR`/`NOT` from the root is
# simplified: an operand of anything else is value context, where every
# operand runs. The rewrite never changes whether the condition is
# `TRUE` - `x OR 1` is `TRUE` whatever `x` is, `NULL` included - only
# which operands run.


#: The largest integer literal SQLite treats as always true:
#: `sqlite3GetInt32` accepts a non-negative literal up to 2^31 - 1.
_INT32_MAX = 2147483647


def _is_int32_literal(expr: Expr) -> bool:
    """Whether *expr* is an integer literal in `0..2147483647`. An `int`
    `Literal` only ever comes from an `INTEGER` token (`sql/parser.py`;
    a wider one is already a `float`), never from a sign, so the range
    is the whole test."""
    return isinstance(expr, Literal) and type(expr.value) is int and 0 <= expr.value <= _INT32_MAX


def _always_true(expr: Expr) -> bool:
    return _is_int32_literal(expr) and expr.value != 0


def _always_false(expr: Expr) -> bool:
    return _is_int32_literal(expr) and expr.value == 0


def _simplified_and_or(node: And | Or, left: Expr, right: Expr) -> Expr:
    """*node* given its already simplified operands *left* and *right*:
    SQLite's rule, in its order - an always-true left or always-false
    right side first, then the reverse. *node* itself when neither
    decides and neither operand changed."""
    is_and = isinstance(node, And)
    if _always_true(left) or _always_false(right):
        return right if is_and else left
    if _always_true(right) or _always_false(left):
        return left if is_and else right
    if left is node.left and right is node.right:
        return node
    if is_and:
        return And(left=left, right=right, position=node.position)
    return Or(left=left, right=right, position=node.position)


def _simplified_condition(expr: Expr) -> Expr:
    """*expr* with every `AND`/`OR` reached from its root through
    `AND`, `OR` and `NOT` replaced by SQLite's simplification of it -
    see the section comment above. *expr* itself, the same object, when
    nothing changes.

    Not recursive (#107): one walk over an explicit stack of `(node,
    operands_done)` pairs and a stack of finished subtrees, the shape
    of `plan/planner.py`'s `_split_expr`. Only `AND`, `OR` and `NOT` are
    descended into; any other node is a leaf here, and is evaluated as
    written."""
    pending: list[tuple[Expr, bool]] = [(expr, False)]
    finished: list[Expr] = []
    while pending:
        node, operands_done = pending.pop()
        if not operands_done:
            if isinstance(node, (And, Or)):
                pending.append((node, True))
                pending.append((node.right, False))
                pending.append((node.left, False))
            elif isinstance(node, Not):
                pending.append((node, True))
                pending.append((node.operand, False))
            else:
                finished.append(node)
            continue
        if isinstance(node, Not):
            operand = finished.pop()
            finished.append(node if operand is node.operand else Not(operand=operand, position=node.position))
            continue
        assert isinstance(node, (And, Or))
        right = finished.pop()
        left = finished.pop()
        finished.append(_simplified_and_or(node, left, right))
    (result,) = finished
    return result


def _run(expr: Expr, row: Row, schema: Schema, context: _Context) -> Value | Bool3:
    """The evaluation loop behind `evaluate()`/`evaluate_condition()`.

    Not recursive (issue #107): one loop over an explicit work stack of
    `(step, node, context, index)` entries and a value stack of finished
    results, so the Python stack does not grow with the tree. A
    recursive walk crashed with `RecursionError` from about 330 levels
    (three frames per level for a comparison), well inside what SQLite
    itself evaluates, and a bound tree can be taller still than any
    parsed one (a select-list alias spliced into `WHERE`). `_Step.EVAL`
    on a node pushes a finish step for it and then its operands, in
    reverse, so they are evaluated left to right, which keeps which
    error is raised first the same as a recursive walk would.

    Where evaluation stops early (issue #111, measured against SQLite;
    `_docs/decisions.md`, 2026-10-01):

    - `AND`/`OR` in condition context evaluate the left operand, then
      decide (`AND_AFTER_LEFT`/`OR_AFTER_LEFT`) whether the right one is
      needed: not after a `FALSE` left side of `AND` or a `TRUE` one of
      `OR`, nor after a `NULL` left side of `AND` where `NULL` counts
      as `FALSE` or of `OR` where it counts as `TRUE` (`_Context`). In
      value context both operands are always evaluated.
    - `BETWEEN` in condition context is `x >= low AND x <= high`
      (`NOT BETWEEN` is `NOT` over that): `BETWEEN_AFTER_LOW` stops
      after the first comparison by the same rule. In value context it
      evaluates the operand, the low bound, the operand again and the
      high bound (#137 tracks the second operand evaluation).
    - `IN` evaluates the left operand and the first element, compares
      them, and stops on a match (`IN_AFTER_ELEMENT`), in both
      contexts; otherwise the next element, the left operand again
      (#137), and so on. `IN ()` evaluates nothing in condition context.
    - `LIKE` evaluates `left`, `pattern`, then `escape`, before any
      check on the escape.

    Each finish step is a plain function below (`_finish_binary`,
    `_finish_like`, ...) over already-evaluated operands.
    """
    work: list[_Work] = [(_Step.EVAL, expr, context, 0)]
    results: list[Value | Bool3] = []
    while work:
        step, node, context, index = work.pop()
        if step is _Step.EVAL:
            _start(node, context, row, work, results)
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
            # Condition context only (`_start`). `and3(FALSE, x)` is
            # FALSE for every x; a NULL left side makes the AND NULL or
            # FALSE, and where NULL counts as FALSE that is decided too.
            # The NULL is kept as the result: it counts the same way in
            # the parent, which shares this context (issue #111).
            left_bool3 = coerce_to_bool3(results.pop())
            results.append(left_bool3)
            if left_bool3 is not False and not (
                left_bool3 is None and context is _Context.NULL_IS_FALSE
            ):
                work.append((_Step.FINISH_AND, node, context, 0))
                work.append((_Step.EVAL, node.right, context, 0))
        elif step is _Step.FINISH_AND:
            right_bool3 = coerce_to_bool3(results.pop())
            left_bool3 = coerce_to_bool3(results.pop())
            results.append(values.and3(left_bool3, right_bool3))
        elif step is _Step.OR_AFTER_LEFT:
            # Mirror of AND_AFTER_LEFT: `or3(TRUE, x)` is TRUE for every
            # x, and a NULL left side decides it where NULL counts as
            # TRUE (under an odd number of NOTs).
            left_bool3 = coerce_to_bool3(results.pop())
            results.append(left_bool3)
            if left_bool3 is not True and not (
                left_bool3 is None and context is _Context.NULL_IS_TRUE
            ):
                work.append((_Step.FINISH_OR, node, context, 0))
                work.append((_Step.EVAL, node.right, context, 0))
        elif step is _Step.FINISH_OR:
            right_bool3 = coerce_to_bool3(results.pop())
            left_bool3 = coerce_to_bool3(results.pop())
            results.append(values.or3(left_bool3, right_bool3))
        elif step is _Step.FINISH_NOT:
            results.append(values.not3(coerce_to_bool3(results.pop())))
        elif step is _Step.FINISH_LIKE:
            escape = results.pop() if node.escape is not None else None
            pattern = results.pop()
            left = results.pop()
            results.append(_finish_like(node, left, pattern, escape))
        elif step is _Step.FINISH_EMPTY_IN:
            results.pop()
            results.append(values.not3(False) if node.negated else False)
        elif step is _Step.IN_AFTER_ELEMENT:
            element = results.pop()
            left = results.pop()
            so_far = results.pop()
            matched = _in_element_matches(node, left, node.values[index], element, schema)
            so_far = values.or3(so_far, matched)
            if matched is not True and index + 1 < len(node.values):
                results.append(so_far)
                work.append((_Step.IN_AFTER_ELEMENT, node, _Context.VALUE, index + 1))
                work.append((_Step.EVAL, node.values[index + 1], _Context.VALUE, 0))
                work.append((_Step.EVAL, node.left, _Context.VALUE, 0))
            else:
                results.append(values.not3(so_far) if node.negated else so_far)
        elif step is _Step.FINISH_BETWEEN:
            high = results.pop()
            operand_for_high = results.pop()
            low = results.pop()
            operand_for_low = results.pop()
            results.append(
                _finish_between(node, operand_for_low, low, operand_for_high, high, schema)
            )
        elif step is _Step.BETWEEN_AFTER_LOW:
            # Condition context only. *context* is that of the inner
            # `x >= low AND x <= high`, already flipped for NOT BETWEEN
            # (`_start`), so the stopping rule is AND_AFTER_LEFT's.
            low = results.pop()
            operand = results.pop()
            above_low = _between_low(node, operand, low, schema)
            if above_low is False or (above_low is None and context is _Context.NULL_IS_FALSE):
                results.append(values.not3(above_low) if node.negated else above_low)
            else:
                results.append(above_low)
                work.append((_Step.FINISH_BETWEEN_HIGH, node, context, 0))
                work.append((_Step.EVAL, node.high, _Context.VALUE, 0))
                work.append((_Step.EVAL, node.operand, _Context.VALUE, 0))
        elif step is _Step.FINISH_BETWEEN_HIGH:
            high = results.pop()
            operand = results.pop()
            above_low = results.pop()
            result = values.and3(above_low, _between_high(node, operand, high, schema))
            results.append(values.not3(result) if node.negated else result)
        else:
            raise AssertionError(f"exec/expression.py: unhandled evaluation step {step}")
    (result,) = results
    return result


def _start(
    node: Expr,
    context: _Context,
    row: Row,
    work: list[_Work],
    results: list[Value | Bool3],
) -> None:
    """`evaluate()`'s `_Step.EVAL`: a leaf's value goes straight onto
    *results*; any other node pushes its finish step onto *work*, then
    its operands in reverse, so they come off the stack - and are
    evaluated - left to right. Only `AND`/`OR`/`NOT` pass a condition
    *context* on to their operands; every other node's operands are
    evaluated as values."""
    value = _Context.VALUE
    if isinstance(node, Literal):
        results.append(node.value)
        return
    if isinstance(node, BoundColumnRef):
        results.append(row[node.offset])
        return
    if isinstance(node, FixedColumnRef):
        # A column the planner replaced by a constant (#142): its value,
        # never the row's cell.
        results.append(node.value)
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
        work.append((_Step.FINISH_BINARY, node, value, 0))
        work.append((_Step.EVAL, node.right, value, 0))
        work.append((_Step.EVAL, node.left, value, 0))
        return
    if isinstance(node, UnaryOp):
        if node.op is UnaryOperator.POS:
            # `+x` is `x`, untouched - see `_finish_negate`'s docstring -
            # but its operand is a value even in a condition: SQLite
            # evaluates both sides of `WHERE +(a AND b)` (issue #111).
            work.append((_Step.EVAL, node.operand, value, 0))
            return
        # Unary plus between the `-` and a literal does not stop the
        # fold: the oracle (see `tests/conftest.py`) treats `-(+0.0)`
        # as `-0.0` and `-(+9223372036854775808)` as INT64_MIN (#117).
        literal_operand = node.operand
        while isinstance(literal_operand, UnaryOp) and literal_operand.op is UnaryOperator.POS:
            literal_operand = literal_operand.operand
        if (
            isinstance(literal_operand, Literal)
            and isinstance(literal_operand.value, float)
            and literal_operand.value == _INT64_MIN_MAGNITUDE_AS_FLOAT
        ):
            # `-9223372036854775808` written in source - see
            # `_finish_negate`'s docstring.
            results.append(INT64_MIN)
            return
        if isinstance(literal_operand, Literal) and isinstance(literal_operand.value, float):
            # A REAL literal directly under `-` (parentheses are not a
            # node): SQLite folds it to a negative literal, a true sign
            # flip, so `-(0.0)` is `-0.0` - unlike `_finish_negate`'s
            # `0 - x` for everything else (issue #110).
            results.append(-literal_operand.value)
            return
        work.append((_Step.FINISH_NEGATE, node, value, 0))
        work.append((_Step.EVAL, node.operand, value, 0))
        return
    if isinstance(node, Is):
        work.append((_Step.FINISH_IS, node, value, 0))
        work.append((_Step.EVAL, node.right, value, 0))
        work.append((_Step.EVAL, node.left, value, 0))
        return
    if isinstance(node, And):
        if context is value:
            work.append((_Step.FINISH_AND, node, value, 0))
            work.append((_Step.EVAL, node.right, value, 0))
        else:
            work.append((_Step.AND_AFTER_LEFT, node, context, 0))
        work.append((_Step.EVAL, node.left, context, 0))
        return
    if isinstance(node, Or):
        if context is value:
            work.append((_Step.FINISH_OR, node, value, 0))
            work.append((_Step.EVAL, node.right, value, 0))
        else:
            work.append((_Step.OR_AFTER_LEFT, node, context, 0))
        work.append((_Step.EVAL, node.left, context, 0))
        return
    if isinstance(node, Not):
        work.append((_Step.FINISH_NOT, node, context, 0))
        work.append((_Step.EVAL, node.operand, _negated_context(context), 0))
        return
    if isinstance(node, Like):
        work.append((_Step.FINISH_LIKE, node, value, 0))
        if node.escape is not None:
            work.append((_Step.EVAL, node.escape, value, 0))
        work.append((_Step.EVAL, node.pattern, value, 0))
        work.append((_Step.EVAL, node.left, value, 0))
        return
    if isinstance(node, In):
        if not node.values:
            # `IN ()` is FALSE. In condition context the left operand is
            # never evaluated; in value context it is, and an error in
            # it surfaces (measured on the oracle, #117; an older
            # SQLite, 3.45.1, did not evaluate it there).
            if context is _Context.VALUE:
                work.append((_Step.FINISH_EMPTY_IN, node, value, 0))
                work.append((_Step.EVAL, node.left, value, 0))
                return
            results.append(values.not3(False) if node.negated else False)
            return
        # The result so far - FALSE, matched by nothing yet - then the
        # left operand and the first element; IN_AFTER_ELEMENT compares
        # them and goes on to the next element only if they differ.
        results.append(False)
        work.append((_Step.IN_AFTER_ELEMENT, node, value, 0))
        work.append((_Step.EVAL, node.values[0], value, 0))
        work.append((_Step.EVAL, node.left, value, 0))
        return
    if isinstance(node, Between):
        if context is value:
            work.append((_Step.FINISH_BETWEEN, node, value, 0))
            work.append((_Step.EVAL, node.high, value, 0))
            work.append((_Step.EVAL, node.operand, value, 0))
            work.append((_Step.EVAL, node.low, value, 0))
            work.append((_Step.EVAL, node.operand, value, 0))
            return
        # `x NOT BETWEEN ...` is `NOT (x BETWEEN ...)` in SQLite, so the
        # inner AND sits under one more NOT than the node itself.
        inner = _negated_context(context) if node.negated else context
        work.append((_Step.BETWEEN_AFTER_LOW, node, inner, 0))
        work.append((_Step.EVAL, node.low, value, 0))
        work.append((_Step.EVAL, node.operand, value, 0))
        return
    raise AssertionError(f"exec/expression.py: unhandled expression node type {type(node).__name__}")


# --- The Value/Bool3 coercion boundary (issues #38, #63) -------------------
#
# Two small, pure functions of evaluate()'s own return value - deliberately
# not a `position` parameter threaded through evaluate()'s recursive
# dispatch. See the module docstring's "Value or Bool3, decided by node
# shape, not calling context" section for why: converting a result never
# needs to know what position it is about to be used in - each branch
# already knows, structurally, what its *children's* results must be,
# purely from which node it is currently dispatching on. (Which children
# are evaluated at all does depend on the calling context since #111 -
# `_Context` - but that never changes what a result is converted to.)
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
    """`x BETWEEN low AND high` in value context is
    `values.and3(values.ge(x, low), values.le(x, high))` - not bespoke
    logic. Confirmed against `sqlite3`: `20 BETWEEN 30 AND NULL` is
    `FALSE`, not `NULL` - the first comparison alone already makes it
    `FALSE`, and `and3(FALSE, NULL)` is `FALSE`. Affinity is applied
    to each bound independently: `x`'s own affinity can interact
    differently with `low` and with `high`.

    `evaluate()` has already evaluated, in this order, the operand, the
    low bound, the operand again and the high bound (#137 tracks the
    second evaluation of the operand). In condition context `_run`
    calls `_between_low`/`_between_high` itself, one at a time, so it
    can stop after the first (issue #111).
    """
    result = values.and3(
        _between_low(expr, operand_for_low, low, schema),
        _between_high(expr, operand_for_high, high, schema),
    )
    return values.not3(result) if expr.negated else result


def _between_low(expr: Between, operand: Value | Bool3, low: Value | Bool3, schema: Schema) -> Bool3:
    """`x >= low`, with each side's own affinity - `BETWEEN`'s first
    comparison, before any `NOT`."""
    left, right = _affinity_pair(expr.operand, operand, expr.low, low, schema)
    return values.ge(left, right)


def _between_high(expr: Between, operand: Value | Bool3, high: Value | Bool3, schema: Schema) -> Bool3:
    """`x <= high`, with each side's own affinity - `BETWEEN`'s second
    comparison, before any `NOT`."""
    left, right = _affinity_pair(expr.operand, operand, expr.high, high, schema)
    return values.le(left, right)


def _in_element_matches(
    expr: In, left: Value | Bool3, element_expr: Expr, element: Value | Bool3, schema: Schema
) -> Bool3:
    """`values.eq(x, vi)` for one already-evaluated list element of `x
    IN (v1, ..., vn)`. `evaluate()` folds these with `values.or3` in
    list order, starting from `FALSE`, and stops at the first `TRUE`
    (issue #111: SQLite does, in every context) - so the result is the
    `or3` fold over the elements it reached, which is the fold over all
    of them, since `or3(TRUE, x)` is `TRUE`. Confirmed against
    `sqlite3`: `5 IN (5, NULL)` is `TRUE`, `6 IN (5, NULL)` is `NULL`
    (no element matches, but a `NULL` element means "maybe", not "no"),
    and a `NULL` element or left side never stops it. `IN ()` never
    gets here: `evaluate()` answers it as `FALSE` without evaluating the
    left operand at all, matching `sql/ast.py`'s own docstring. `NOT
    IN` is `values.not3` of the fold, applied by `evaluate()`.

    Affinity (issue #47): a list element contributes no affinity of its
    own, ever - not "usually," not "unless the element happens to itself
    be a bare column." Confirmed against `sqlite3` (`t(n INTEGER, s
    TEXT, r REAL)`, row `(5, '5', 5.0)`): `'5' IN (n)` -> `0`, `'5' IN
    (r)` -> `0`, `5 IN (s)` -> `0` - each is `1` if the element's own
    affinity were (wrongly) consulted the way `=`'s right operand's is.
    Only `x`'s own affinity - the same structural question
    `_affinity_of` already asks for every other operator - is ever
    applied, and it is applied once per element independently: `x IN
    ('5', 'abc')` still converts `'5'` and `'abc'` against `x`'s
    affinity individually. `_affinity_pair`'s `right_has_affinity=False`
    is the single change this makes.

    This is genuinely different from `BETWEEN`, not a case that
    "tidying" the two onto one path would preserve: a `BETWEEN` bound
    is an independent RHS operand, symmetric with `=`, and keeps its
    own affinity. Confirmed against `sqlite3` for the identical
    operand shape, same row (`n = 1`): `'1' BETWEEN n AND n` -> `1`,
    `'1' IN (n)` -> `0`. `_between_low`/`_between_high` are
    intentionally left calling `_affinity_pair` with its default
    `right_has_affinity=True`.
    """
    left_value, right_value = _affinity_pair(
        expr.left, left, element_expr, element, schema, right_has_affinity=False
    )
    return values.eq(left_value, right_value)


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
    if isinstance(expr, FixedColumnRef):
        # Still the column, known to hold one value (#142): SQLite's
        # `EP_FixedCol` node keeps the column's affinity.
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


def apply_column_affinity(value: Value, column_type: ColumnType | None) -> Value:
    """*value* as a column of *column_type* would store it - SQLite's
    `applyAffinity` (`OP_Affinity`), the conversion `INSERT` applies.
    The planner uses it for the constant it puts in place of a column
    (#142): SQLite codes that constant and then applies the column's
    affinity to it, so `line_no = '05'` replaces `line_no` with the
    INTEGER `5` and `r = 1` (a REAL column) with `1.0`.

    Not the comparison conversion (`_apply_affinity`), which leaves an
    `int` an `int` and a `float` a `float` - a comparison cannot tell
    `5` from `5.0`, but `||` can:

    - `INTEGER`: text that is a whole number goes through
      `try_numeric_affinity`, the same whole-string rule a comparison
      uses; a REAL that is a whole number strictly inside the int64
      range then becomes that INTEGER (`5.0` and `'5.0'` are `5`,
      `-0.0` is `0`, `2.0**63` and `-2.0**63` stay REAL - SQLite's
      `sqlite3VdbeIntegerAffinity`).
    - `REAL`: the same text rule, then an INTEGER becomes the nearest
      REAL (`_int_as_real`).
    - `TEXT`: a number becomes its text (`_coerce_to_text`).
    - no affinity: unchanged.

    `NULL` is never converted, and text that is not a number stays text
    (`line_no = 'x'` replaces `line_no` with `'x'`). Measured on the
    pinned oracle, `tests/differential/test_where_propagation.py`."""
    if value is None or column_type is None:
        return value
    if column_type is ColumnType.TEXT:
        return _coerce_to_text(value)
    numeric = try_numeric_affinity(value)
    if column_type is ColumnType.INTEGER:
        if isinstance(numeric, float):
            return _real_as_integer(numeric)
        return numeric
    if isinstance(numeric, int):
        return _int_as_real(numeric)
    return numeric


def _real_as_integer(value: float) -> int | float:
    """SQLite's `sqlite3VdbeIntegerAffinity`: a REAL that is a whole
    number strictly between int64's minimum and maximum becomes that
    INTEGER; any other REAL (a fraction, an infinity, `-2.0**63`, or
    anything from `2.0**63` up) stays as it is. `int()` of a finite
    float is exact, so the range test compares exact integers."""
    if math.isinf(value) or not value.is_integer():
        return value
    whole = int(value)
    if INT64_MIN < whole < INT64_MAX:
        return whole
    return value


def _int_as_real(value: int) -> float:
    """An INTEGER stored in a REAL column: the nearest double, as C's
    `(double)` cast gives it (`9007199254740993` is `9007199254740992.0`).
    Storage, not comparison: the comparison path never converts an
    `int` to `float` (see `tests/test_expression.py`'s
    `test_no_stray_float_calls_outside_the_named_exceptions`, which
    names this function)."""
    return float(value)


def _strip_numeric_whitespace(text: str) -> str:
    """`text` with `_NUMERIC_WHITESPACE` characters trimmed from both
    ends - not Python's `str.strip()`, which trims a broader,
    Unicode-aware set. SQLite's whole-string numeric-affinity check
    trims exactly those six ASCII characters (`\\x1c`, `\\x85` and
    `\\xa0` are not trimmed; the oracle, issue #136)."""
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
    `_in_element_matches`.

    `right_has_affinity` (issue #47) is the explicit, structural
    escape hatch `_in_element_matches` uses: SQLite's own rule is that the
    right-hand side of `IN`/`NOT IN` *with a list* has no affinity at
    all, regardless of what kind of expression a given element is -
    unlike `=`/`IS`/`BETWEEN`, where each operand independently asks
    `_affinity_of` the normal way. `_in_element_matches` passes
    `right_has_affinity=False` for every element; every other caller
    takes the default and is unaffected. A plain keyword parameter,
    not a dynamic-dispatch trick (`AGENTS.md`), and it does not touch
    `_between_low`/`_between_high`, which must keep applying each
    bound's own affinity independently - see `_in_element_matches`'s
    docstring for the
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
    exact `9223372036854775807`). That REAL is converted from the digit
    *text*, never from a Python `int`: `float(int(text))` raises
    `OverflowError` on a huge numeral, and `int(text)` itself raises
    `ValueError` past Python's 4300-digit limit - so `_int64_digit_run`
    decides the range from the digit text alone and never builds a big
    `int`.

    Every REAL here - a digit run past int64, or text with a `.` or an
    exponent - comes from `historian.atof.text_to_real`, SQLite
    3.50.4's own `sqlite3AtoF`, not from `float()`, which is correctly
    rounded where SQLite is not (issue #134:
    `'18823239210196293635' * 1.0` is `0x1.05399454f5f45p+64` in
    SQLite, `0x1.05399454f5f46p+64` from `float()`). The scan above
    has already isolated the number, sign included and whitespace
    excluded, which is the input `text_to_real` expects.
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
    # A REAL from source text goes through SQLite's own conversion
    # (`text_to_real`, issue #134), never Python's correctly rounded
    # `float()`.
    value: int | float
    if is_float:
        value = text_to_real(number_text)
    else:
        exact = _int64_digit_run(number_text)
        if exact is None:
            value = text_to_real(number_text)
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
    tests/oracle.py (the oracle): `'1e3' % 7` is `1.0`,
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
