"""The operator layer: `Scan`, `Filter`, `Project`, `Aggregate`,
`Sort`, `Limit`, `Distinct`.

Issue #34 (spec §6 M2 item 8b) built `Scan`/`Filter`/`Project`. `Sort`
(issue #61), `Limit` (issue #77) and `Distinct` (issue #78) are the
fifth, sixth and seventh - the last - of phase 1's seven operators -
plus, at this layer, two rules §3 states
elsewhere and this is where they are actually enforced: "Expression
evaluation" (every predicate and select-list expression goes through
`exec/expression.py`'s `evaluate(expr, row, schema)`, #12, merged) and
"Determinism and row order" ("the same repository and the same query
always produce the same rows in the same order").

Volcano-style iteration, per §3 verbatim: *"Each operator pulls rows
from its children... A `Row` is a tuple of values. The schema lives on
the operator, not in the row... Generators are used inside `rows()`,
but the operator is an object rather than a bare generator function, so
the tree can be inspected, printed by `EXPLAIN`, and asserted on in
tests."* Every operator below is therefore a plain class with a
`schema: Schema` attribute and a `rows(self) -> Iterator[Row]` method
that uses a generator internally; none is a bare `def rows(): yield
...` function standing in for an object.

`Operator` (a `Protocol`) documents that shared shape without adding a
runtime dispatch mechanism none of the three classes needs -
`AGENTS.md`'s "no metaclasses, no dynamic dispatch tricks, no clever
descriptors" rules out both a shared ABC with template-method hooks and
an `isinstance`-based dispatcher; a `Protocol` is a static-typing
convention, checked only where a type checker looks, and costs nothing
at runtime. `Scan`, `Filter` and `Project` do not inherit from it or
from each other - they merely happen to satisfy it, which is the point.

No `git`, no `subprocess` (`AGENTS.md`) - this module never imports
`tables/blame.py`. `Scan` adapts any object shaped like `ScanSource`
below (`tables/blame.py`'s `BlameScan` is one, but this module never
imports or names it) - see `Scan`'s own docstring for how that
adaptation is deliberately limited in this issue.

Determinism (`AGENTS.md`, spec §3): neither `Filter` nor `Project` may
reorder, deduplicate, or otherwise introduce non-deterministic
iteration. Both are implemented as a single pass over `child.rows()`
in order, with no `set`, no `dict`-keyed grouping, and no sort of any
kind - row order in is row order out, restricted (`Filter`) or
transformed per-row (`Project`), never rearranged. `Sort` (issue #61)
is the one operator in this module that *is* allowed to reorder rows -
that is its entire job - but the reordering itself must still be
deterministic: see `Sort`'s own docstring for how it gets that from
`values.py`'s stable, per-key, last-to-first contract rather than from
anything ad hoc.

The `Value`/`Bool3` coercion boundary (#38)
--------------------------------------------

`exec/expression.py`'s `evaluate()` returns a `historian.values.Value`
for a value-shaped node (`Literal`, a bare column, arithmetic,
concatenation) and a `historian.values.Bool3` for a predicate-shaped
one (a comparison, `AND`/`OR`/`NOT`, `IS`, `LIKE`, `IN`, `BETWEEN`),
decided by the node's own shape. Two grammar-reachable shapes land on
the "wrong" side of that split for the operator that has to consume
them, and both are handled here, by calling one of
`exec/expression.py`'s two caller-side coercion helpers on
`evaluate()`'s result before doing anything else with it:

- `Project` calls `coerce_to_value` on every select-list item's
  `evaluate()` result before storing it in the output row, so a
  `Bool3`-shaped select-list expression (`SELECT 1 = 1 FROM blame`, or
  any bare predicate in select-list position) stores SQLite's own
  storage-class answer (`sqlite3`: `select 1 = 1, typeof(1 = 1)` ->
  `1|integer`) rather than a Python `True`/`False`/`None`.
- `Filter` calls `coerce_to_bool3` on `evaluate()`'s result before
  handing it to `values.is_true`, so a `Value`-shaped predicate
  (`WHERE line_no`, a bare column with no comparison) gets SQLite's
  C-style truthiness (`sqlite3`: `select x from t where x` keeps
  nonzero numeric rows, drops `0`, `NULL`, and non-numeric text)
  instead of `values.is_true` raising `TypeError` on a raw `Value`.

Both directions were named explicitly in #12's own closing comment as
belonging to "whoever builds #34," landed as #38 rather than in #34
itself: the coercion helpers live next to `evaluate()` in
`exec/expression.py`, and this module only calls them.
"""

from __future__ import annotations

import math
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Protocol

from historian import values
from historian.exec.expression import (
    EvalError,
    arithmetic_operand,
    coerce_to_bool3,
    coerce_to_value,
    evaluate,
    try_numeric_affinity,
)
from historian.schema import Column, ColumnType, Row, Schema
from historian.sql.ast import Expr
from historian.sql.binder import BoundColumnRef, BoundSelectItem
from historian.sql.lexer import Position

__all__ = [
    "Aggregate",
    "AggregateCall",
    "Distinct",
    "Filter",
    "Limit",
    "Operator",
    "Project",
    "Scan",
    "ScanSource",
    "Sort",
    "SortKey",
]


class Operator(Protocol):
    """The shape every operator in the tree satisfies (spec §3): a
    `schema` describing its output rows, and a `rows()` iterator that
    produces them. See the module docstring for why this is a
    `Protocol` rather than a shared base class."""

    schema: Schema

    def rows(self) -> Iterator[Row]: ...


class ScanSource(Protocol):
    """The interface a table's scan implementation exposes for `Scan`
    to wrap - exactly `tables/blame.py`'s `BlameScan` shape, confirmed
    against the merged code: `schema` is a plain class attribute,
    `capabilities()` takes no arguments and returns a `set` of
    pushdown-kind labels, and `scan()`'s one parameter is spelled and
    defaulted exactly `pushed: Sequence[object] = ()`. Structural, not
    nominal - nothing under `tables/` needs to know this `Protocol`
    exists, and this module never imports `tables/blame.py` itself."""

    schema: Schema

    def capabilities(self) -> set[str]: ...

    def scan(self, pushed: Sequence[object] = ()) -> Iterator[Row]: ...


class Scan:
    """Adapts any `ScanSource`-shaped object into the `Operator` shape.

    Pushdown - predicate splitting and capability negotiation - is
    spec §6 M4, items 13 and 14, and does not exist yet: there is no
    planner here to split a `WHERE` clause into conjunctive terms, and
    no negotiation step to offer them to `capabilities()`. So `Scan`
    can only ever do the one thing that is always correct regardless of
    what the source can push down: ask for nothing. Every call is
    `source.scan(pushed=())`, unconditionally - `capabilities()` is
    never even called here, because this issue has nothing to offer it
    yet. A future planner (#13) negotiates; this `Scan` never does.
    """

    def __init__(self, source: ScanSource) -> None:
        self._source = source
        self.schema = source.schema

    def rows(self) -> Iterator[Row]:
        # `yield from` keeps this exactly as lazy as `source.scan()`
        # itself - for `BlameScan`, already a generator (#11) - rather
        # than materializing anything here.
        yield from self._source.scan(pushed=())


class Filter:
    """`WHERE` / `HAVING` (spec §3): yields exactly the rows of `child`
    for which `predicate` evaluates to `TRUE`.

    `evaluate(predicate, row, child.schema)` returns a `Value` for a
    value-shaped predicate (`WHERE line_no`, a bare column with no
    comparison) or a `Bool3` - `True`, `False`, or `None`, meaning SQL
    `TRUE`, `FALSE`, or `NULL` - for a predicate-shaped one.
    `coerce_to_bool3` (`exec/expression.py`, #38) turns the former into
    the latter: a `bool`/`None` result passes through unchanged, and
    anything else gets SQLite's C-style truthiness. The `Bool3` that
    comes out is then routed through `values.is_true` rather than
    tested with a bare `if ...:` - the two look equivalent and are
    not. Python truthiness treats `False` and `None` identically (both
    falsy), which happens to give the right answer for a
    `FALSE`-producing predicate and the *wrong* answer for nothing -
    but only because both cases drop the row. The bug it hides is
    real: `values.is_true` additionally rejects anything that is not
    exactly `True`, `False`, or `None` (SQLite's own integer `1`/`0`
    spelling of a predicate result, in particular), which a bare
    Python truth test would silently accept. §3 names conflating
    `FALSE` and `NULL` "the classic bug"; routing through `is_true` is
    what keeps this operator from being an instance of it - and
    `coerce_to_bool3` is what keeps a `Value`-shaped predicate from
    reaching `is_true` at all, rather than tripping its `TypeError`.
    """

    def __init__(self, child: Operator, predicate: Expr) -> None:
        self._child = child
        self._predicate = predicate
        # A predicate can only remove rows, never add, rename, or
        # retype a column - the output schema is exactly the child's.
        self.schema = child.schema

    def rows(self) -> Iterator[Row]:
        child_schema = self._child.schema
        for row in self._child.rows():
            if values.is_true(coerce_to_bool3(evaluate(self._predicate, row, child_schema))):
                yield row


# --- Aggregate (issue #60, grouped path added by #69) -----------------------
#
# `_docs/spec.md` §3's `Aggregate` operator: count/sum/avg/min/max,
# whole-table (#60) and grouped (#69). Sits between Filter (or Scan,
# when there is no WHERE) and Project, with `HAVING`'s own `Filter`
# (issue #69, `plan/planner.py`) between `Aggregate` and `Project` when
# the query has one. "Whole-table" (`group_by` empty) means exactly
# one output row, always - even over zero input rows (spec §3's own
# named "classic mistake": `count(*)` over zero rows is `0`, not zero
# output rows). Grouped (`group_by` non-empty) is the opposite over an
# empty table: zero groups in, zero rows out - there is nothing to
# group. This is the one place the two paths diverge on purpose; both
# are implemented by the same `rows()` below, which special-cases the
# empty-`group_by` case exactly once, at the very end.


@dataclass(frozen=True)
class AggregateCall:
    """One aggregate call `plan/planner.py` split out of a `SELECT`-
    list expression: which of the five v1 aggregates (already
    validated by `sql/binder.py` - nothing else can ever reach this
    far), and its single bound argument expression, evaluated against
    the *child's* schema (the row shape below `Aggregate`, before
    aggregation) - `None` for `count(*)`/`count()`, which take no
    argument at all and mean the same thing (confirmed against
    `sqlite3`). `position` is the call's own position, used only if
    `sum`'s running total overflows int64 (`_eval_sum_step` below) -
    the one way this operator raises.

    `distinct` (issue #84) mirrors the bound `FunctionCall`'s own
    field, threaded straight through by `plan/planner.py`'s
    `_build_aggregate_call`. `_Accumulator.step` consults it for
    `count`/`sum`/`avg` only - `min`/`max` never change under
    deduplication, since removing a duplicate can never change which
    value is most extreme, so it is accepted here (for every kind,
    uniformly, since the planner does not special-case `min`/`max`)
    but has no effect on those two.
    """

    kind: str
    arg: Expr | None
    position: Position
    distinct: bool = False


#: SQLite's `int64` bounds - `sum`'s own overflow check. Kept separate
#: from `exec/expression.py`'s `_int64_bounded` (arithmetic's int64
#: rule *promotes to REAL* on overflow, `_docs/decisions.md`,
#: 2026-09-01) because `sum`'s rule is different and confirmed against
#: `sqlite3` directly (issue #60's own grooming): a purely-integer
#: running total that overflows int64 raises - it does not wrap and it
#: does not silently promote to a float the way ordinary arithmetic
#: does.
_SUM_INT64_MIN = -9223372036854775808
_SUM_INT64_MAX = 9223372036854775807


def _sum_add(total: int, value: int) -> tuple[int, bool]:
    """One running-total step for the *exact* integer accumulator
    (`iSum`, SQLite's `SumCtx.iSum`) shared by both `sum`'s and `avg`'s
    own `_Accumulator` instances (issue #91: `avg` reuses `sum`'s
    accumulator, per SQLite's `sumStep` being both aggregates' shared
    `xStep` - see the module-level KBN helpers below and `_Accumulator`
    itself).

    Its role changed with issue #91: on `main` before this issue, the
    caller committed this function's `new_total` to `self._sum_int`
    unconditionally, *and* kept an unconditional parallel naive float
    accumulator (`self._sum_float += float(value)`) on every step -
    the bug this issue fixes (a parallel naive float total is not what
    `sqlite3` computes; see the module's own KBN helpers). Now, this
    function is consulted only while the caller is still on the exact
    path (`not self._sum_approx`), and the caller commits `new_total`
    to `self._sum_int` only when this step did *not* overflow; on
    overflow, `self._sum_int` is left exactly as it was (the pre-
    overflow exact total) so the caller can fold that value into the
    KBN accumulator via `_kbn_init` before adding the overflowing
    addend itself via `_kbn_step_int64` - the same "fold, then add
    the addend" split SQLite's own `sumStep` performs in its
    `sqlite3AddInt64` failure branch.

    It returns the new exact total - Python's `int` is unbounded, so
    nothing here ever actually overflows - and whether *this step's*
    total left SQLite's int64 range. It does not matter whether a
    later step's total comes back into range; overflow is recorded by
    the caller (`_Accumulator._sum_overflowed`, latched permanently
    once set) and only ever turned into an `EvalError` at `finish()`
    time, and only if no non-integer-classified value (a REAL, or TEXT
    that is not a clean whole-string integer) was seen anywhere in the
    aggregate's input - confirmed against `sqlite3` 3.51.0 directly
    (issue #88's own grooming, correcting #60's "order-dependent
    asymmetry" description, and reconfirmed equivalent to SQLite's own
    live-clearing `ovrfl` state machine by issue #91's grooming - see
    `_Accumulator`'s own docstring): a value that permanently switches
    the running total to float suppresses the overflow check for the
    rest of the aggregate regardless of whether it arrives before or
    after the overflowing addition, and an overflow that happens while
    every value is still integer-classified stays an error even if a
    later addition would bring the exact total back within int64
    range.
    """
    new_total = total + value
    overflowed = not (_SUM_INT64_MIN <= new_total <= _SUM_INT64_MAX)
    return new_total, overflowed


#: The fold/split threshold both `kahanBabuskaNeumaierStepInt64` and
#: `kahanBabuskaNeumaierInit` use in SQLite's `src/func.c`: 2**52. An
#: integer at or past this magnitude is split into a multiple of 16384
#: plus a signed remainder (`_kbn_split_int64`) before either half is
#: converted to `float`, because a plain `int -> float` cast loses
#: precision past 2**53 - splitting off a multiple of 2**14 first
#: leaves at most 63 - 14 = 49 significant bits in the "big" half,
#: which fits a double's 53-bit mantissa exactly regardless of the
#: original magnitude.
_KBN_INT64_FOLD_THRESHOLD = 4503599627370496


def _kbn_step(r_sum: float, r_err: float, addend: float) -> tuple[float, float]:
    """One Kahan-Babuska-Neumaier compensated-summation step, ported
    from SQLite's `kahanBabuskaNeumaierStep` (`src/func.c`) verbatim,
    including its exact floating-point operation grouping:

    ```c
    static void kahanBabuskaNeumaierStep(volatile SumCtx *pSum, volatile double r){
      volatile double s = pSum->rSum;
      volatile double t = s + r;
      if( fabs(s) > fabs(r) ){
        pSum->rErr += (s - t) + r;
      }else{
        pSum->rErr += (r - t) + s;
      }
      pSum->rSum = t;
    }
    ```

    The grouping matters and is easy to get wrong in translation: C's
    `pSum->rErr += (s - t) + r` computes `(s - t) + r` as one unit
    *first*, then adds the old `rErr` to that unit last - not
    `(rErr + (s - t)) + r`, which a left-to-right `a + b + c`
    transliteration would silently produce and which rounds
    differently once `rErr` and `s`/`r` are at very different
    magnitudes (confirmed while porting this: the naive left-to-right
    grouping fails case B of issue #91's own verification table).
    Pure - never raises, never touches `_Accumulator` state directly.
    """
    total = r_sum + addend
    if abs(r_sum) > abs(addend):
        delta = (r_sum - total) + addend
    else:
        delta = (addend - total) + r_sum
    return total, r_err + delta


def _kbn_split_int64(value: int) -> tuple[float, float]:
    """Splits *value* into a multiple of 16384 (`big`) and a signed
    remainder (`small`, `-16383..16383`) the way SQLite's
    `kahanBabuskaNeumaierStepInt64`/`Init` do for `|value| >= 2**52` -
    see `_KBN_INT64_FOLD_THRESHOLD`'s own docstring for why both
    halves then convert to `float` exactly.

    C's `%` truncates toward zero; Python's `%` floors - the two
    disagree on the remainder's *sign* (never its magnitude, since
    16384 is a power of two) whenever *value* is negative and not an
    exact multiple of 16384. Computed here with exact Python `int`
    arithmetic (never `math.fmod`, which would first convert *value*
    to `float` - already lossy for an `int` this large, defeating the
    entire point of the split) and an explicit sign fixup: Python's
    `value % 16384` is always in `[0, 16383]`; C's truncating result
    for a negative *value* with a nonzero remainder is that same
    magnitude, negated.
    """
    remainder = value % 16384
    if remainder != 0 and value < 0:
        remainder -= 16384
    big = value - remainder
    return float(big), float(remainder)


def _kbn_step_int64(r_sum: float, r_err: float, value: int) -> tuple[float, float]:
    """Port of SQLite's `kahanBabuskaNeumaierStepInt64`: adds the
    exact integer *value* to the running `(r_sum, r_err)` pair,
    splitting first via `_kbn_split_int64` when `|value| >= 2**52` so
    neither half loses precision in the `int -> float` conversion,
    then performing two ordinary `_kbn_step` calls; below the
    threshold, converts directly (still exact - every `int` below
    2**52 fits a double's mantissa) and takes one step."""
    if value <= -_KBN_INT64_FOLD_THRESHOLD or value >= _KBN_INT64_FOLD_THRESHOLD:
        big, small = _kbn_split_int64(value)
        r_sum, r_err = _kbn_step(r_sum, r_err, big)
        return _kbn_step(r_sum, r_err, small)
    return _kbn_step(r_sum, r_err, float(value))


def _kbn_init(value: int) -> tuple[float, float]:
    """Port of SQLite's `kahanBabuskaNeumaierInit`: folds the exact
    integer accumulator *value* into a fresh `(r_sum, r_err)` pair by
    direct assignment - not a step - the first time the exact `iSum`
    path is abandoned (`_Accumulator.step`'s transition into
    `self._sum_approx`). Same `_KBN_INT64_FOLD_THRESHOLD` split as
    `_kbn_step_int64`, for the same exactness reason."""
    if value <= -_KBN_INT64_FOLD_THRESHOLD or value >= _KBN_INT64_FOLD_THRESHOLD:
        return _kbn_split_int64(value)
    return float(value), 0.0


def _kbn_is_overflow(x: float) -> bool:
    """Port of SQLite's `sqlite3IsOverflow` (`src/util.c`): true iff
    *x* is NaN or +/-Inf. `sumFinalize`/`avgFinalize` both guard the
    `rSum + rErr` step with this - in practice `rErr` only reaches it
    if an intermediate KBN step itself produced infinity (summing
    values near `DBL_MAX`); no test in this issue's scope needs it,
    but it is ported for fidelity rather than assumed unreachable."""
    return math.isnan(x) or math.isinf(x)


class _Accumulator:
    """Per-call running state for one whole-table aggregate. `Aggregate.
    rows()` builds one instance per `AggregateCall`, steps every one of
    them with every child row exactly once, then reads `finish()` -
    never a second pass over the child's rows.

    `step`/`finish` dispatch on `self._call.kind` with a plain `if`/
    `elif` chain - not a per-kind subclass and not a dispatch table
    keyed by function (`AGENTS.md`: no dynamic dispatch tricks), the
    same style `exec/expression.py`'s own `evaluate()` already uses for
    its node-type dispatch. This is meant to port to Rust later, where
    an enum match is the direct idiom for exactly this shape.

    `self._distinct_seen` (issue #84) is a `set` of `values.order_key`
    results, `None` when `call.distinct` is `False` - mirroring
    `self._extreme`'s own "`None` until seen" style, except this one
    stays `None` for the whole call's life rather than being populated
    lazily. Consulted only in the `count(<expr>)`, `sum`, and `avg`
    branches of `step()` below: before counting/summing/accumulating a
    non-NULL value, its `order_key` is checked against the set -
    always on the *raw* value `evaluate()`/`coerce_to_value()` produced,
    never on a coerced-for-arithmetic version of it (issue #88: `'3'`
    and `3` carry different storage-class ranks and must not merge just
    because both would coerce to the number `3`) - already present
    means the value's SQL-equal group has already contributed and the
    whole step is skipped (no count, no sum, no running total change),
    otherwise the key is recorded and the step proceeds exactly as it
    would without `DISTINCT`. NULLs are excluded (the existing `is
    None` checks below) before this gate is ever reached, so a `NULL`
    argument value never touches the dedup set. `min`/`max` never
    consult it - removing a duplicate can never change which value is
    most extreme, so those two branches are byte-for-byte what they
    were before this issue.

    `sum`/`avg` over TEXT (issue #88): both reuse `exec/expression.py`'s
    `try_numeric_affinity` (whole-string numeric-affinity
    classification) and `arithmetic_operand` (leading-prefix coercion)
    rather than duplicating either parser (#53). Both classify each
    value with `try_numeric_affinity` first: a plain `INTEGER`, or TEXT
    whose entire trimmed string is integer-shaped, stays on the exact
    `self._sum_int` int64 path (`self._sum_overflowed` latches if a
    step's exact total leaves int64 range); anything else - a `REAL`,
    or TEXT that is not a clean whole-string integer - is folded into
    the KBN accumulator via `arithmetic_operand`'s leading-prefix
    coercion and latches `self._sum_saw_non_integer`. `sum.finish()`
    raises only at the very end, and only if `self._sum_overflowed` and
    not `self._sum_saw_non_integer` - never mid-accumulation, and never
    un-latched by a later value bringing the total back in range.

    `sum`/`avg`'s shared KBN accumulator (issue #91): SQLite's own
    `avg` is not a separately-implemented "cast everything to float and
    sum" aggregate - it shares `sum`'s `xStep` (`sumStep`, `src/func.c`)
    outright, differing only in `xFinal`. So `_Accumulator.step`'s
    `sum`/`avg` branches below are one code path, not two: both kinds
    build the exact same `(self._sum_int, self._sum_approx,
    self._sum_r_sum, self._sum_r_err)` state (each call still gets its
    *own* `_Accumulator` instance - a query with both `sum(x)` and
    `avg(x)` steps two independent accumulators over the same rows, per
    `Aggregate.rows()` - only the *algorithm* is shared, not the
    state). While every value seen so far is integer-classified and the
    exact `self._sum_int` addition never leaves int64 range, nothing
    about `self._sum_r_sum`/`self._sum_r_err` is touched at all - not
    even a parallel naive float accumulation, which is exactly the bug
    this issue fixes (`main` kept `self._sum_float`/`self._avg_total`
    running unconditionally, in parallel, every step - not what
    `sqlite3` computes, and observably wrong for `avg` past 2**53,
    since `float` is not distributive over integer addition). The
    first row that is either non-integer-classified, or an integer
    whose exact addition overflows int64, triggers a one-time
    transition: `_kbn_init` folds the *current* `self._sum_int` (the
    pre-overflow total, for the overflow case - the overflowing row
    itself is added afterward via `_kbn_step_int64`, not folded into
    the init) into `(self._sum_r_sum, self._sum_r_err)`, and
    `self._sum_approx` latches `True` permanently; `self._sum_int` is
    never read or written again after that. `sum.finish()` never
    raises on `avg`'s own accumulator, and vice versa - each kind's
    `finish()` only ever reads its own instance's state.

    `sum`'s live-clearing `ovrfl` vs. #88's permanent latch: SQLite's
    own `sumStep` clears `p->ovrfl = 0` on every non-integer value
    stepped while `p->approx` is already set, so a prior overflow can
    be silently un-flagged - but issue #91's own grooming proved this
    is exactly equivalent, for every possible input sequence, to #88's
    simpler design here (`self._sum_overflowed` and
    `self._sum_saw_non_integer`, both latched, neither ever un-latched,
    gated together only once at `finish()` time): an overflow can only
    occur while `self._sum_approx` is still `False`, which is only
    true before the first non-integer value in the whole input, so
    whenever any non-integer value appears anywhere, any overflow must
    have happened strictly before it and is therefore always
    unconditionally suppressed - matching `not self._sum_saw_non_
    integer` exactly. No live clearing is implemented here; `_sum_add`
    and this class's own docstring below carry the citation.
    """

    def __init__(self, call: AggregateCall) -> None:
        self._call = call
        self._count = 0  # count(*)/count(): every row, NULL or not
        self._non_null_count = 0  # count(<expr>), and avg's denominator
        self._sum_seen = False  # sum: whether any non-NULL value has been accumulated yet
        self._sum_int = 0  # exact integer running total (SumCtx.iSum) - sum and avg each own one
        self._sum_r_sum = 0.0  # KBN running sum (SumCtx.rSum), meaningful once self._sum_approx
        self._sum_r_err = 0.0  # KBN compensation term (SumCtx.rErr)
        self._sum_approx = False  # SumCtx.approx: latched True once the exact iSum path is abandoned
        self._sum_overflowed = False  # issue #88: latched once a step's exact total leaves int64 range
        self._sum_saw_non_integer = False  # issue #88: latched by any REAL, or TEXT that isn't a clean whole-string integer
        self._extreme: values.Value = None  # min/max's running extreme; None until the first non-NULL value
        self._distinct_seen: set | None = set() if call.distinct else None

    def _distinct_duplicate(self, value: values.Value) -> bool:
        """`True` when *value*'s `order_key` has already been recorded
        for this call - and records it when it has not. Only ever
        called from the `count(<expr>)`, `sum`, and `avg` branches of
        `step()`, and only when `self._call.distinct` (`self.
        _distinct_seen` is `None` otherwise, so this is never reached
        for a non-DISTINCT call - see the class docstring)."""
        key = values.order_key(value)
        if key in self._distinct_seen:
            return True
        self._distinct_seen.add(key)
        return False

    def step(self, row: Row, schema: Schema) -> None:
        call = self._call
        if call.kind == "count":
            # Every row counts for count(*)/count() (call.arg is None) -
            # confirmed against sqlite3: count(*) counts rows
            # regardless of NULL. count(<expr>) counts only the rows
            # where the expression is non-NULL instead.
            self._count += 1
            if call.arg is None:
                return
            value = coerce_to_value(evaluate(call.arg, row, schema))
            if value is None:
                return
            if call.distinct and self._distinct_duplicate(value):
                return
            self._non_null_count += 1
            return

        # sum/avg/min/max all ignore a NULL argument value entirely -
        # confirmed against sqlite3 (spec §3's aggregate edge-case
        # table: "sum/avg/min/max with some NULLs: NULLs ignored").
        value = coerce_to_value(evaluate(call.arg, row, schema))
        if value is None:
            return
        if call.distinct and call.kind in ("sum", "avg") and self._distinct_duplicate(value):
            return
        self._non_null_count += 1
        if call.kind == "sum" or call.kind == "avg":
            # Shared step algorithm (issue #91): `avg` reuses `sum`'s
            # own exact-iSum/KBN accumulator, per SQLite's own
            # `sumStep` being both aggregates' shared `xStep` - see
            # this class's docstring. issue #88: classify by
            # whole-string numeric affinity first. A plain int, or
            # TEXT whose entire trimmed string is integer-shaped,
            # stays on the exact int64 path for as long as possible; a
            # float classification (an ordinary REAL, or TEXT whose
            # whole string is a well-formed real, e.g. '3.0') and a
            # str classification (TEXT with no whole-string numeric
            # reading at all, e.g. '3abc'/'abc'/'') are both
            # "non-integer" and are folded into the KBN accumulator via
            # the leading-prefix coercion instead - confirmed against
            # sqlite3 (issue #88's own grooming).
            if call.kind == "sum":
                self._sum_seen = True
            classified = try_numeric_affinity(value)
            if isinstance(classified, int):
                if not self._sum_approx:
                    new_total, overflowed = _sum_add(self._sum_int, classified)
                    if overflowed:
                        # The exact iSum path is abandoned here: fold
                        # the pre-overflow total (self._sum_int, not
                        # yet reassigned) into the KBN accumulator,
                        # then add this overflowing addend via the
                        # int64-aware step - the same split SQLite's
                        # own sumStep performs.
                        self._sum_overflowed = True
                        self._sum_r_sum, self._sum_r_err = _kbn_init(self._sum_int)
                        self._sum_approx = True
                        self._sum_r_sum, self._sum_r_err = _kbn_step_int64(
                            self._sum_r_sum, self._sum_r_err, classified
                        )
                    else:
                        self._sum_int = new_total
                else:
                    self._sum_r_sum, self._sum_r_err = _kbn_step_int64(
                        self._sum_r_sum, self._sum_r_err, classified
                    )
            else:
                self._sum_saw_non_integer = True
                if not self._sum_approx:
                    self._sum_r_sum, self._sum_r_err = _kbn_init(self._sum_int)
                    self._sum_approx = True
                self._sum_r_sum, self._sum_r_err = _kbn_step(
                    self._sum_r_sum, self._sum_r_err, float(arithmetic_operand(value))
                )
        elif call.kind == "min":
            if self._extreme is None or values.order_key(value) < values.order_key(self._extreme):
                self._extreme = value
        elif call.kind == "max":
            if self._extreme is None or values.order_key(value) > values.order_key(self._extreme):
                self._extreme = value
        else:
            raise AssertionError(f"exec/operators.py: unhandled aggregate kind {call.kind!r}")

    def finish(self) -> values.Value:
        call = self._call
        if call.kind == "count":
            return self._count if call.arg is None else self._non_null_count
        if call.kind == "sum":
            if not self._sum_seen:
                return None  # NULL: no non-NULL value was ever seen
            # issue #88: raise only here, at the very end - never
            # mid-accumulation - and only if the exact integer total
            # left int64 range at some point *and* every value was
            # integer-classified. A non-integer value anywhere (before
            # or after the overflowing addition) permanently suppresses
            # this check; the flag is never un-latched by a later value
            # bringing the exact total back into range. Proven
            # equivalent to SQLite's own live-clearing `ovrfl` by issue
            # #91's grooming - see this class's own docstring.
            if self._sum_overflowed and not self._sum_saw_non_integer:
                raise EvalError(
                    "integer overflow computing sum(...) - sqlite3 raises here too, "
                    "rather than wrapping or promoting to REAL",
                    call.position,
                )
            if self._sum_approx:
                # sumFinalize: rSum + rErr, unless rErr itself is NaN
                # or Inf (_kbn_is_overflow - SQLite's sqlite3IsOverflow),
                # in which case fall back to rSum alone.
                if _kbn_is_overflow(self._sum_r_err):
                    return self._sum_r_sum
                return self._sum_r_sum + self._sum_r_err
            return self._sum_int
        if call.kind == "avg":
            # avgFinalize: same approx/iSum read as sum, no ovrfl check
            # at all (avg never raises), always divides as float. The
            # NULL-over-zero-rows case still needs its own check, since
            # 0/0 would otherwise raise.
            if self._non_null_count == 0:
                return None
            if self._sum_approx:
                total = self._sum_r_sum
                if not _kbn_is_overflow(self._sum_r_err):
                    total += self._sum_r_err
            else:
                total = float(self._sum_int)
            return total / self._non_null_count
        if call.kind in ("min", "max"):
            return self._extreme  # None (NULL) if no non-NULL value was ever seen
        raise AssertionError(f"exec/operators.py: unhandled aggregate kind {call.kind!r}")


def _aggregate_output_type(kind: str) -> ColumnType:
    """The declared type `Aggregate`'s own output schema gives one
    call's column. Not load-bearing the way a real table column's type
    is - nothing compares against an `Aggregate` output column in this
    issue's scope (no `HAVING` until #69) - documented rather than
    arbitrary: `count` is always `INTEGER`, `avg` is always `REAL`
    (confirmed above), and `sum`/`min`/`max` are whatever the actual
    computed value turns out to be at runtime, which this schema cannot
    know in advance - `TEXT` is `exec/operators.py`'s own existing
    placeholder for exactly this situation (`_project_column`, below)."""
    if kind == "count":
        return ColumnType.INTEGER
    if kind == "avg":
        return ColumnType.REAL
    return ColumnType.TEXT


def _group_key_output_type(expr: Expr, child_schema: Schema) -> ColumnType:
    """The declared type `Aggregate`'s own output schema gives one
    `group_by` key column - the same rule `_project_column` (below)
    already uses for a `Project` output column: a bare
    `BoundColumnRef` keeps its source column's declared type, and
    every other expression shape gets the same documented `TEXT`
    placeholder."""
    if isinstance(expr, BoundColumnRef):
        return child_schema.columns[expr.offset].type
    return ColumnType.TEXT


def _group_key_name(index: int, expr: Expr) -> str:
    """The header `Aggregate`'s own output schema gives one `group_by`
    key column - a bare `BoundColumnRef` keeps its declared name (so
    `GROUP BY author_name` produces a column literally named
    `author_name`), and every other expression shape falls back to a
    positional placeholder, mirroring `_PLACEHOLDER_COLUMN_NAME`
    below. 1-based, matching that convention."""
    if isinstance(expr, BoundColumnRef):
        return expr.name
    return f"group_{index + 1}"


class Aggregate:
    """`_docs/spec.md` §3's `Aggregate` operator: `calls` is the
    ordered list of aggregate calls `plan/planner.py` split out of the
    `SELECT`/`HAVING` expressions - each gets one output column, after
    every `group_by` key column, in that order. `group_by` is `()` for
    the whole-table path (#60, unchanged): exactly one output row,
    always, computed by streaming `child.rows()` through one shared
    set of accumulators. A non-empty `group_by` (#69) instead computes
    one key tuple per child row (evaluated against `child`'s schema,
    same as every aggregate call's own argument), steps that key's own
    accumulator set, and - at the end - yields one row per distinct
    key, key columns first: zero groups, zero rows, over an empty
    table, the one place the two paths genuinely diverge.

    Two key tuples are the same group under SQL equality, not Python
    `==` - `values.order_key(value)` is used as the per-column
    dictionary key component, which already normalizes storage-class-
    insensitive numeric equality (`1`/`1.0` share a key; `'1'` does
    not, since it carries a different storage-class rank) - see
    `values.py`'s own docstring. Groups are emitted in
    **first-row-encountered order**: a plain `dict` preserves
    insertion order, and no key is ever re-inserted once seen, so this
    falls out of the implementation rather than needing a separate
    sort - the concrete, checkable determinism rule this issue commits
    to (`_docs/spec.md`'s "Determinism and row order", AGENTS.md).

    Every call's and every `group_by` expression's argument is
    evaluated against `child`'s schema (the row shape *below*
    aggregation), never against this operator's own output schema -
    `Project`, above this operator (and `HAVING`'s own `Filter`, #69,
    directly above `Aggregate`), is what evaluates the surrounding
    scalar expression against *this* operator's output row instead,
    per spec §3's "Expression evaluation" split.
    """

    def __init__(
        self,
        child: Operator,
        calls: Sequence[AggregateCall],
        group_by: Sequence[Expr] = (),
    ) -> None:
        self._child = child
        self._calls = tuple(calls)
        self._group_by = tuple(group_by)
        child_schema = child.schema
        group_columns = tuple(
            Column(_group_key_name(index, expr), _group_key_output_type(expr, child_schema))
            for index, expr in enumerate(self._group_by)
        )
        call_columns = tuple(
            Column(f"{call.kind}_{index + 1}", _aggregate_output_type(call.kind))
            for index, call in enumerate(self._calls)
        )
        self.schema = Schema(columns=group_columns + call_columns)

    def rows(self) -> Iterator[Row]:
        child_schema = self._child.schema
        if not self._group_by:
            # Whole-table path (#60, unchanged): one shared accumulator
            # set, exactly one output row, even over zero child rows.
            accumulators = [_Accumulator(call) for call in self._calls]
            for row in self._child.rows():
                for accumulator in accumulators:
                    accumulator.step(row, child_schema)
            yield tuple(accumulator.finish() for accumulator in accumulators)
            return

        # Grouped path (#69): one accumulator set per distinct key,
        # keyed by `values.order_key` per column so grouping uses SQL
        # equality rather than Python's - see the class docstring.
        # `groups` maps that key to `(key_values, accumulators)`; a
        # plain dict's insertion order is what gives first-row-
        # encountered emission order, with no extra bookkeeping.
        groups: dict[tuple[object, ...], tuple[Row, list[_Accumulator]]] = {}
        for row in self._child.rows():
            key_values = tuple(
                coerce_to_value(evaluate(expr, row, child_schema)) for expr in self._group_by
            )
            key = tuple(values.order_key(value) for value in key_values)
            entry = groups.get(key)
            if entry is None:
                entry = (key_values, [_Accumulator(call) for call in self._calls])
                groups[key] = entry
            _key_values, accumulators = entry
            for accumulator in accumulators:
                accumulator.step(row, child_schema)

        for key_values, accumulators in groups.values():
            yield key_values + tuple(accumulator.finish() for accumulator in accumulators)


#: The positional placeholder used for a `Project` output column whose
#: select-list item has no `output_name` (an unaliased, non-column
#: expression - `sql/binder.py`'s own docstring: "nothing downstream
#: yet consumes it"). 1-based, matching the acceptance criterion's own
#: example spelling (`"column_2"` for the second item). Not load-
#: bearing - `sql/binder.py` already left this exact question open for
#: its own header text, and this issue inherits rather than resolves
#: it; nothing downstream reads this name yet either.
_PLACEHOLDER_COLUMN_NAME = "column_{position}"


def _project_column(position: int, item: BoundSelectItem, child_schema: Schema) -> Column:
    name = item.output_name if item.output_name is not None else _PLACEHOLDER_COLUMN_NAME.format(position=position)
    # A bare BoundColumnRef (aliased or not - only the expression's own
    # shape matters, per exec/expression.py's affinity convention this
    # mirrors) keeps its source column's declared type. Every other
    # expression shape - literal, arithmetic, concatenation, a
    # Bool3-shaped comparison, anything else - gets an explicit,
    # documented TEXT placeholder: nothing downstream reads a computed
    # column's declared type in this milestone (the eventual CLI in
    # #13 prints values, not types), and SQLite itself has no fixed
    # declared type for a computed result column either.
    if isinstance(item.expr, BoundColumnRef):
        column_type = child_schema.columns[item.expr.offset].type
    else:
        column_type = ColumnType.TEXT
    return Column(name, column_type)


class Project:
    """`SELECT` list evaluation (spec §3): yields one output row per
    input row, evaluating each select-list item's `.expr` against the
    input row, in select-list order.

    `select_list` is a `tuple[BoundSelectItem, ...]` - the shape
    `BoundSelectStatement.select_list` carries from `sql/binder.py`.
    Every item's `.expr` is evaluated with
    `evaluate(item.expr, row, child.schema)` (#12): a `Value` for a
    value-shaped expression (`Literal`, `BoundColumnRef`, arithmetic,
    concatenation, unary +/-) or a `Bool3` for a predicate-shaped one
    (`SELECT 1 = 1`). `coerce_to_value` (`exec/expression.py`, #38)
    turns the latter into the former before it lands in the output
    row - SQLite's own `1`/`0`/`NULL` spelling of a predicate result,
    not the Python `True`/`False`/`None` `evaluate()` itself returns;
    a value-shaped result passes through `coerce_to_value` unchanged.

    The output `Schema` is computed once, at construction, from
    `select_list` and `child.schema` - see `_project_column` for the
    per-item name and declared-type rules.
    """

    def __init__(self, child: Operator, select_list: tuple[BoundSelectItem, ...]) -> None:
        self._child = child
        self._select_list = select_list
        child_schema = child.schema
        self.schema = Schema(
            columns=tuple(
                _project_column(position, item, child_schema)
                for position, item in enumerate(select_list, start=1)
            )
        )

    def rows(self) -> Iterator[Row]:
        child_schema = self._child.schema
        select_list = self._select_list
        for row in self._child.rows():
            yield tuple(
                coerce_to_value(evaluate(item.expr, row, child_schema)) for item in select_list
            )


# --- Sort (issue #61) --------------------------------------------------------
#
# `_docs/spec.md` §3's `Sort` operator: `ORDER BY`. Sits below `Project`
# (`plan/planner.py`'s job to place it there) because an `ORDER BY` key
# may reference a column or aggregate slot absent from the final select
# list - `Sort` needs the wider pre-`Project` row, never the narrower
# projected one. Each key's own expression is already split by the
# planner (`_split_expr`, the same machinery `HAVING`/`select_list` use)
# so it never contains a raw aggregate `FunctionCall` by the time it
# reaches here - only `BoundColumnRef`s and ordinary scalar expressions,
# evaluated against `child`'s own schema exactly like every other
# operator's expressions.


@dataclass(frozen=True)
class SortKey:
    """One `ORDER BY` key `plan/planner.py` builds: the (already
    aggregate-split) expression to sort by, and whether this key is
    `DESC` (`ASC` when `False`)."""

    expr: Expr
    descending: bool


class Sort:
    """`ORDER BY` (spec §3): yields every row of `child`, reordered by
    `keys`.

    Applies `values.py`'s own multi-key `Sort` contract verbatim rather
    than re-deriving it: a **stable** sort once per key, processing
    keys from the **last** to the **first**, each pass using
    `values.order_key` on that key's evaluated value with
    `reverse=True` iff that key is `DESC`. Python's `list.sort` is
    stable, so an earlier pass (a later key) never disturbs the
    relative order two rows already have from a later pass (an earlier
    key) among rows that tie on it - which is also what gives
    historian's own determinism guarantee (`AGENTS.md`, spec §3's
    "Determinism and row order") for rows that tie on every key: the
    row order reaching `Sort` is already fixed (`Scan`/`Filter`/
    `Aggregate` never reorder), and a stable sort preserves that
    original relative order among ties rather than scrambling it.

    Every key's expression is evaluated against `child`'s own schema
    with `exec/expression.py`'s `evaluate()`, `coerce_to_value`d first
    exactly as `Project` does - an `ORDER BY` key may be a bare
    predicate shape (`ORDER BY x = 1`) as legally as a value shape.

    Unlike every other operator in this module, `Sort` cannot stream:
    a sort needs every row before it can produce the first one. It
    materializes `child.rows()` exactly once, up front; the schema is
    otherwise exactly `child`'s own - `ORDER BY` can only reorder rows,
    never add, remove, or rename a column.
    """

    def __init__(self, child: Operator, keys: Sequence[SortKey]) -> None:
        self._child = child
        self._keys = tuple(keys)
        self.schema = child.schema

    def rows(self) -> Iterator[Row]:
        child_schema = self._child.schema
        rows = list(self._child.rows())
        for key in reversed(self._keys):
            rows.sort(
                key=lambda row, expr=key.expr: values.order_key(
                    coerce_to_value(evaluate(expr, row, child_schema))
                ),
                reverse=key.descending,
            )
        yield from rows


# --- Limit (issue #77) --------------------------------------------------------
#
# `_docs/spec.md` §3's `Limit` operator: `LIMIT`/`OFFSET`. Outermost in
# the tree (`plan/planner.py` places it above `Project`) - see issue
# #77's own design, which leaves `DISTINCT`'s future slot ("12c")
# between `Project` and `Limit`. Semantics verified against sqlite3
# 3.51.0 during this issue's own grooming (`_docs/decisions.md`):
# `LIMIT 0` is zero rows; a negative `LIMIT` means "no limit" and
# `OFFSET` still applies; a negative `OFFSET` is clamped to 0; an
# `OFFSET` at or past the end of `child`'s rows is zero rows, not an
# error.


class Limit:
    """`LIMIT`/`OFFSET` (spec §3): yields up to `limit` rows of `child`,
    after skipping the first `offset` (clamped to 0 if negative).

    Unlike `Sort`/`Aggregate`, `Limit` is a plain Volcano generator: it
    never materializes `child.rows()`. It pulls at most `offset +
    limit` rows from `child` before it stops asking for more - fewer,
    if `child` itself runs out first - so a query like `SELECT path
    FROM blame LIMIT 3` can stop the underlying scan's work (`git
    blame`, for `blame`) once three rows exist, rather than computing
    every row and discarding the rest. This is the one narrow slice of
    laziness this issue's own scope covers - not `LIMIT` pushdown into
    a scan (spec §3/§6, M4), which is a planner/scan negotiation this
    operator knows nothing about. See `tests/test_operators.py`'s own
    spy-`ScanSource` tests for the pull-count proof.

    A negative `limit` is the one case where `Limit` is *not* bounded
    by `offset + limit` - "no limit" means every remaining row of
    `child` is yielded, so the whole child is necessarily pulled
    (still without ever calling `list(...)` on it up front).
    """

    def __init__(self, child: Operator, limit: int, offset: int = 0) -> None:
        self._child = child
        self._limit = limit
        # A negative OFFSET behaves exactly like OFFSET 0 - confirmed
        # against sqlite3 - clamped once here rather than at every call
        # site that reads `self._offset`.
        self._offset = max(0, offset)
        # LIMIT/OFFSET only ever remove rows from the end or the start
        # of the sequence - never add, rename, or retype a column.
        self.schema = child.schema

    def rows(self) -> Iterator[Row]:
        child_iter = iter(self._child.rows())
        for _ in range(self._offset):
            try:
                next(child_iter)
            except StopIteration:
                # OFFSET at or past the end of child's rows: zero rows,
                # not an error - confirmed against sqlite3. Nothing left
                # to skip or yield.
                return
        if self._limit < 0:
            # Negative LIMIT: "no limit" - every row after OFFSET,
            # unbounded. The one case this operator pulls all of
            # `child`, necessarily, since every remaining row must be
            # yielded.
            yield from child_iter
            return
        remaining = self._limit
        if remaining == 0:
            # LIMIT 0: zero rows, and - per the laziness contract - not
            # even one row pulled from `child` to discover that.
            return
        for row in child_iter:
            yield row
            remaining -= 1
            if remaining == 0:
                # Stop the instant the limit is satisfied, before ever
                # asking `child_iter` for another row - this is what
                # keeps the total pull count at exactly `offset +
                # limit`, never `offset + limit + 1`.
                return


# --- Distinct (issue #78) ----------------------------------------------------
#
# `_docs/spec.md` §3's `Distinct` operator: `SELECT DISTINCT`. Sits
# directly above `Project` (`plan/planner.py`'s job to place it there,
# in the slot #77's own design reserved) - `Distinct` dedups
# *projected* output rows, never the wider pre-`Project` row `Sort`
# (below `Project`, unmoved by this issue - see `_docs/decisions.md`
# for why that placement still gives the right answer once `sql/
# binder.py`'s own DISTINCT/ORDER BY narrowing is in place) sorts by.


class Distinct:
    """`SELECT DISTINCT` (spec §3): yields every row of `child` the
    first time its dedup key is seen, and never again.

    Two key tuples are the same dedup group under SQL equality, not
    Python `==` - `tuple(values.order_key(value) for value in row)` is
    exactly the per-column key `Aggregate`'s own grouped path
    (`exec/operators.py`'s `Aggregate.rows()`) already builds to group
    by SQL equality rather than Python's: `NULL`s are equal to each
    other and form one group, `1` and `1.0` merge into one group (the
    first-encountered representative is what gets yielded), and `'1'`
    (`TEXT`) stays its own group, never merging with numeric `1`
    (`values.order_key`'s own storage-class ranking keeps them apart).

    Streams: pulls one row at a time from `child` and yields it
    immediately the first time its key is new, holding only the
    growing set of already-seen keys in memory - unlike `Sort`, it
    never materializes `child.rows()` up front. This also gives
    determinism for free: first-seen order is exactly the order rows
    arrive from `child`, which is already fixed by everything below it
    (`Scan`/`Filter`/`Aggregate` never reorder; `Sort`, when present,
    reorders deterministically via its own stable, per-key contract;
    `Project` is a 1-in-1-out, order-preserving generator).

    Which row a group of duplicates keeps is moot and needs no design
    of its own, let alone a test that could observe it: `Distinct`
    groups by the *entire* output row, so two rows sharing a dedup key
    are, by construction, identical in every column - there is no
    other row's value that could leak through regardless of which one
    happens to be first.
    """

    def __init__(self, child: Operator) -> None:
        self._child = child
        # DISTINCT can only remove rows, never add, rename, or retype
        # a column - the output schema is exactly the child's, the
        # same reasoning `Filter`'s own schema assignment already
        # uses.
        self.schema = child.schema

    def rows(self) -> Iterator[Row]:
        seen: set[tuple[object, ...]] = set()
        for row in self._child.rows():
            key = tuple(values.order_key(value) for value in row)
            if key in seen:
                continue
            seen.add(key)
            yield row
