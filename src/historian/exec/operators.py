"""The operator layer: `Scan`, `Filter`, `ConstantGuard`, `Project`,
`Aggregate`, `Sort`, `Limit`, `Distinct`.

These are phase 1's seven operators, plus `ConstantGuard` (#171), which
decides a `WHERE`'s column-free terms before the first row is read. This is also where two rules
from spec §3 are enforced: "Expression evaluation" (every predicate
and select-list expression goes through `exec/expression.py`'s
`evaluate(expr, row, schema)`) and "Determinism and row order" ("the
same repository and the same query always produce the same rows in
the same order").

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
runtime dispatch mechanism none of the eight classes needs -
`AGENTS.md`'s "no metaclasses, no dynamic dispatch tricks, no clever
descriptors" rules out both a shared ABC with template-method hooks and
an `isinstance`-based dispatcher; a `Protocol` is a static-typing
convention, checked only where a type checker looks, and costs nothing
at runtime. No operator inherits from it or from another operator -
each merely happens to satisfy it, which is the point.

No `git`, no `subprocess` (`AGENTS.md`) - this module never imports
`tables/blame.py`. `Scan` adapts any object shaped like `ScanSource`
below (`tables/blame.py`'s `BlameScan` is one, but this module never
imports or names it) - see `Scan`'s own docstring for how the terms
it pushes are decided elsewhere, by `plan/optimizer.py` (#121).

Determinism (`AGENTS.md`, spec §3): no operator introduces
non-deterministic iteration. `Filter`, `ConstantGuard`, `Project` and
`Limit` are a single pass over `child.rows()` in order - row order in is row order
out, restricted or transformed per row, never rearranged. `Aggregate`
and `Distinct` hold a `dict` or `set`, but only for lookup: they emit
in first-seen order. `Sort` is the one operator that reorders rows,
and it does so deterministically: see `Sort`'s own docstring for how
it gets that from `values.py`'s stable, per-key, last-to-first
contract.

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
- `Filter` calls `coerce_to_bool3` on `evaluate_condition()`'s result before
  handing it to `values.is_true`, so a `Value`-shaped predicate
  (`WHERE line_no`, a bare column with no comparison) gets SQLite's
  C-style truthiness (`sqlite3`: `select x from t where x` keeps
  nonzero numeric rows, drops `0`, `NULL`, and non-numeric text)
  instead of `values.is_true` raising `TypeError` on a raw `Value`.

The coercion helpers live next to `evaluate()` in
`exec/expression.py` (#38), and this module only calls them.
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
    evaluate_condition,
    squash_nan,
    try_numeric_affinity,
)
from historian.schema import Column, ColumnType, Row, Schema
from historian.sql.ast import Expr
from historian.sql.binder import BoundColumnRef, BoundSelectItem
from historian.sql.lexer import Position
from historian.sql.walk import split_conjuncts
from historian.values import INT64_MAX, INT64_MIN

__all__ = [
    "Aggregate",
    "AggregateCall",
    "ConstantGuard",
    "Distinct",
    "Filter",
    "Limit",
    "Operator",
    "Predicate",
    "Project",
    "PushdownKind",
    "Scan",
    "ScanEstimate",
    "ScanSource",
    "Sort",
    "SortKey",
    "child_of",
]


class Operator(Protocol):
    """The shape every operator in the tree satisfies (spec §3): a
    `schema` describing its output rows, and a `rows()` iterator that
    produces them. See the module docstring for why this is a
    `Protocol` rather than a shared base class."""

    schema: Schema

    def rows(self) -> Iterator[Row]: ...


#: A pushdown capability label a scan declares in `capabilities()`
#: (spec §2: `capabilities() -> set[PushdownKind]`). Each table names
#: its own kinds - `blame`'s are about `path` (#122) - so this is
#: a plain string rather than one shared enum every table would have
#: to extend. The optimizer never interprets a kind; it only asks
#: whether the set is empty (see `plan/optimizer.py`).
PushdownKind = str

#: One conjunctive term of a bound `WHERE` predicate, offered to and
#: pushed into a scan (spec §2: `scan(pushed: list[Predicate])`). It
#: is exactly the bound AST subexpression - no second predicate
#: representation - so a scan recognises the shapes it can use by the
#: same explicit `isinstance` checks `exec/expression.py` evaluates
#: them by. Its `BoundColumnRef` offsets index the scan's own
#: `schema`, because only the `WHERE` `Filter` directly above a `Scan`
#: is ever negotiated.
Predicate = Expr


@dataclass(frozen=True)
class ScanEstimate:
    """What a source reports for `--explain` (#42): its own name for
    the `Scan` line, how many of its *total* units `scan()` would read
    for the pushed terms (*selected*), and how many exist. Plain data,
    so `plan/explain.py` can print it without knowing any table."""

    name: str
    selected: int
    total: int


class ScanSource(Protocol):
    """The interface a table's scan implementation exposes for `Scan`
    to wrap (spec §2, "The scan capability contract"). Structural, not
    nominal - nothing under `tables/` needs to know this `Protocol`
    exists, and this module never imports `tables/blame.py` itself.

    - `schema`: the columns every row from `scan()` has.
    - `capabilities()`: the pushdown kinds this scan can use at all.
      Empty means "never offer me anything" - the optimizer then calls
      neither `accepts()` nor anything else, so a source with no
      capabilities (a test fake that implements no pushdown) need not
      implement `accepts()` meaningfully.
    - `accepts(term)`: the per-term negotiation (issue #121). `True`
      means the scan will use *term* to do less work when it is later
      passed in `pushed`. It must be a pure answer about *term*'s
      shape - no I/O, no dependence on which other terms were offered
      - and it promises only a superset: the `Filter` above the scan
      still enforces the term (spec §2, `_docs/decisions.md`
      2026-08-24).
    - `scan(pushed)`: every row, or - for accepted terms in `pushed` -
      a superset of the rows matching all of them. `pushed` only ever
      holds terms this source accepted, in offered order.
    - `estimate(pushed)` (#42): the `Scan` line of `--explain`, as a
      `ScanEstimate`. It reads as little as it can to know how many
      paths `scan(pushed)` would blame and never produces a row.
    - The work record (spec §4, "The pushdown layer"), read after a
      run by `--stats` and by tests: `blamed_paths` (what the scan
      did the expensive work on, in order), `git_invocations` (every
      `git` process started) and `tracked_path_count` (how many
      paths exist to be blamed). Reset by `scan()`, never by a reader.
    """

    schema: Schema
    blamed_paths: list[str]
    git_invocations: int
    tracked_path_count: int

    def capabilities(self) -> set[PushdownKind]: ...

    def accepts(self, term: Predicate) -> bool: ...

    def scan(self, pushed: Sequence[Predicate] = ()) -> Iterator[Row]: ...

    def estimate(self, pushed: Sequence[Predicate] = ()) -> ScanEstimate: ...


class Scan:
    """Adapts any `ScanSource`-shaped object into the `Operator` shape.

    `pushed` - the terms `scan()` is called with - starts empty, and
    `plan/planner.py` always builds a `Scan` that way. Deciding what
    to push is the optimizer's job (`plan/optimizer.py`, issue #121),
    not this operator's: it records the accepted terms here with
    `set_pushed()`, and `rows()` hands them to `source.scan()`
    verbatim. A tree the optimizer never touched (`--no-pushdown`,
    #43) therefore calls `source.scan(pushed=())`, exactly as before
    #121. `Scan` itself never calls `capabilities()` or `accepts()`.
    """

    def __init__(self, source: ScanSource, pushed: Sequence[Predicate] = ()) -> None:
        self._source = source
        self._pushed: tuple[Predicate, ...] = tuple(pushed)
        self.schema = source.schema

    def source(self) -> ScanSource:
        """The wrapped source, for the optimizer to negotiate with."""
        return self._source

    def pushed(self) -> tuple[Predicate, ...]:
        """The terms `rows()` passes to `source.scan()`, in order."""
        return self._pushed

    def set_pushed(self, pushed: Sequence[Predicate]) -> None:
        """Replace the pushed terms - the optimizer's one write into
        the tree. Only terms `source.accepts()` returned `True` for
        belong here; the `Filter` above still enforces every term."""
        self._pushed = tuple(pushed)

    def rows(self) -> Iterator[Row]:
        # `yield from` keeps this exactly as lazy as `source.scan()`
        # itself - for `BlameScan`, already a generator (#11) - rather
        # than materializing anything here.
        yield from self._source.scan(pushed=self._pushed)


class Filter:
    """`WHERE` / `HAVING` (spec §3): yields exactly the rows of `child`
    for which `predicate` evaluates to `TRUE`.

    The predicate goes through `evaluate_condition`, not `evaluate`
    (#111): this is the one condition context in the engine, the
    root of `WHERE`/`HAVING`, where `AND`/`OR`/`NOT`/`BETWEEN` stop as
    soon as whether the row is kept is decided, as SQLite's do. Every
    other operator here evaluates values and calls `evaluate`. A
    `WHERE` is evaluated term by term, a `HAVING` whole - see
    `split_terms` (#189).

    `evaluate_condition(predicate, row, child.schema)` returns a `Value` for a
    value-shaped predicate (`WHERE line_no`, a bare column with no
    comparison) or a `Bool3` - `True`, `False`, or `None`, meaning SQL
    `TRUE`, `FALSE`, or `NULL` - for a predicate-shaped one.
    `coerce_to_bool3` (`exec/expression.py`, #38) turns the former into
    the latter: a `bool`/`None` result passes through unchanged, and
    anything else gets SQLite's C-style truthiness. The `Bool3` that
    comes out is then routed through `values.is_true` rather than
    tested with a bare `if ...:`. Python truthiness drops `False` and
    `None` alike, which is also what `WHERE` does, so the two agree on
    a `Bool3`. They differ on anything else: `values.is_true` rejects
    a result that is not exactly `True`, `False` or `None` (SQLite's
    integer `1`/`0` spelling of a predicate result, say), where a bare
    `if` would accept it. §3 names conflating `FALSE` and `NULL` "the
    classic bug"; `is_true` keeps a mistyped result from slipping
    through - and `coerce_to_bool3` keeps a `Value`-shaped predicate
    from reaching `is_true` at all, rather than tripping its
    `TypeError`.
    """

    def __init__(self, child: Operator, predicate: Expr, negotiable: bool = True, split_terms: bool = True) -> None:
        self._child = child
        self._predicate = predicate
        self._negotiable = negotiable
        self._split_terms = split_terms
        # The conditions `rows()` evaluates, in order: the top-level
        # `AND` terms, or the whole predicate as one (`split_terms`).
        self._conditions = tuple(split_conjuncts(predicate)) if split_terms else (predicate,)
        # A predicate can only remove rows, never add, rename, or
        # retype a column - the output schema is exactly the child's.
        self.schema = child.schema

    def predicate(self) -> Expr:
        """The predicate this `Filter` enforces - read by the
        optimizer, never replaced by it."""
        return self._predicate

    def negotiable(self) -> bool:
        """Whether the optimizer may offer this `Filter`'s terms to the
        scan beneath it. `True` for `WHERE`; `False` for the `Filter`
        of `HAVING` terms that moved below the aggregate (#141), which
        can sit directly above the `Scan` when there is no `WHERE` and
        is never negotiated (#172). An explicit flag, read by
        `plan/optimizer.py`, rather than anything inferred from the
        tree's shape."""
        return self._negotiable

    def split_terms(self) -> bool:
        """Whether the predicate is a `WHERE` - split on its top-level
        `AND`s into terms, each evaluated as a condition of its own,
        left to right, stopping at the first that is not `TRUE` - or,
        when `False`, one condition, as SQLite evaluates a `HAVING`.
        `True` for `WHERE` and for the `Filter` of `HAVING` terms that
        moved below the aggregate (#141), which SQLite has made `WHERE`
        terms; `False` for `HAVING`. The two agree on every row; they
        differ only in which operands run, because `evaluate_condition`
        simplifies an `AND` with an always-false literal operand (#189):
        `HAVING CONSTERR AND 0` is the literal `0`, while `WHERE
        CONSTERR AND 0` is two terms and `CONSTERR` raises."""
        return self._split_terms

    def rows(self) -> Iterator[Row]:
        child_schema = self._child.schema
        conditions = self._conditions
        for row in self._child.rows():
            kept = True
            for condition in conditions:
                result = evaluate_condition(condition, row, child_schema)
                if not values.is_true(coerce_to_bool3(result)):
                    kept = False
                    break
            if kept:
                yield row


class ConstantGuard:
    """The `WHERE` terms with no column reference (#171, spec §3 "`WHERE`
    terms with no column reference"), decided once, before any row.

    SQLite evaluates a `WHERE` term that reads no column once, before it
    reads the first row, in `WHERE` order, whatever terms sit between
    them: the first that is not `TRUE` ends the query over zero rows,
    and one that raises raises whatever the input is, empty included.
    `plan/planner.py` collects those terms - after constant propagation
    (#142), with the moved `HAVING` terms (#141) that have no column
    after them - and puts this operator directly above the `WHERE`
    `Filter` and the moved-terms `Filter`, which keep every term.

    Evaluation happens in `rows()`, a generator, so on its first pull,
    never at construction: a tree that is built and printed
    (`--explain`) and never run evaluates nothing, and an operator
    above that never pulls (`LIMIT 0`) never evaluates it either. It
    happens again on every `rows()` call, so re-running the tree gives
    the same answer. Each term goes through `evaluate_condition`, as a
    `Filter` term does, against an empty row: a term here holds no
    `BoundColumnRef`, and a `FixedColumnRef` evaluates to its own value
    with its column's affinity from `schema`. The child is pulled only
    once every term is `TRUE`; a `NULL` term stops it as `FALSE` does.
    """

    def __init__(self, child: Operator, terms: Sequence[Expr]) -> None:
        self._child = child
        self._terms = tuple(terms)
        # Decides whether rows pass at all, never which: the schema is
        # exactly the child's.
        self.schema = child.schema

    def terms(self) -> tuple[Expr, ...]:
        """The column-free terms, in the order they are evaluated - read
        by `plan/explain.py`."""
        return self._terms

    def rows(self) -> Iterator[Row]:
        child_schema = self._child.schema
        empty_row: Row = ()
        for term in self._terms:
            result = evaluate_condition(term, empty_row, child_schema)
            if not values.is_true(coerce_to_bool3(result)):
                return
        yield from self._child.rows()


# --- Aggregate ---------------------------------------------------------------
#
# `_docs/spec.md` §3's `Aggregate` operator: count/sum/avg/min/max,
# whole-table and grouped. Sits between Filter (or Scan, when there is
# no WHERE) and Project, with `HAVING`'s own `Filter`
# (`plan/planner.py`) between `Aggregate` and `Project` when the query
# has one. "Whole-table" (`group_by` empty) means exactly one output
# row, always - even over zero input rows (spec §3: "with no `GROUP
# BY` and no rows, the whole-table case still emits exactly one row",
# so `count(*)` is `0`). Grouped (`group_by` non-empty) is the
# opposite over an empty table: zero groups in, zero rows out. This is
# the one place the two paths diverge on purpose; `rows()` below
# branches on `group_by` once, at the top.


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
    `sum`'s total overflows int64 (raised by `_Accumulator.finish()`).
    That is this operator's one raise of its own; `evaluate()` of an
    argument can also raise `EvalError` (`count(x LIKE y ESCAPE
    'ab')`).

    `distinct` (#84) mirrors the bound `FunctionCall`'s own
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


def _sum_add(total: int, value: int) -> tuple[int, bool]:
    """One step of the exact integer accumulator (`iSum`, SQLite's
    `SumCtx.iSum`) that `sum` and `avg` share: returns the new total
    (Python's `int` is unbounded, so nothing here overflows) and
    whether it left int64. The caller commits the total only if it did
    not; on overflow it folds the old total into the KBN pair
    (`_kbn_init`) and then adds the addend (`_kbn_step_int64`), the
    split SQLite's `sumStep` makes. Whether the overflow is ever an
    error is decided in `_Accumulator.finish()`; see that class.
    """
    new_total = total + value
    overflowed = not (INT64_MIN <= new_total <= INT64_MAX)
    return new_total, overflowed


#: The fold/split threshold both `kahanBabuskaNeumaierStepInt64` and
#: `kahanBabuskaNeumaierInit` use in SQLite's `src/func.c`: 2**52. An
#: integer at or past this magnitude is split into a multiple of 16384
#: plus a signed remainder (`_kbn_split_int64`) before either half is
#: converted to `float`, because a plain `int -> float` cast loses
#: precision past 2**53. The "big" half has at most 63 - 14 = 49
#: significant bits, so it fits a double's mantissa exactly.
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

    The grouping matters: C's `pSum->rErr += (s - t) + r` computes
    `(s - t) + r` as one unit first, then adds the old `rErr` last -
    not `(rErr + (s - t)) + r`, which a left-to-right transliteration
    would produce and which rounds differently once `rErr` and `s`/`r`
    are at very different magnitudes (`_docs/decisions.md`,
    2026-09-25, #91). Pure - never raises.
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
    see `_KBN_INT64_FOLD_THRESHOLD` for why both halves then convert
    to `float` exactly.

    C's `%` truncates toward zero; Python's `%` floors, so they differ
    in the remainder's sign (never its magnitude) for a negative
    *value* that is not a multiple of 16384. Computed with exact `int`
    arithmetic (never `math.fmod`, which would convert *value* to
    `float` first) and an explicit sign fixup.
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
    threshold, converts directly (still exact) and takes one step."""
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
    `_kbn_step_int64`."""
    if value <= -_KBN_INT64_FOLD_THRESHOLD or value >= _KBN_INT64_FOLD_THRESHOLD:
        return _kbn_split_int64(value)
    return float(value), 0.0


def _kbn_is_overflow(x: float) -> bool:
    """Port of SQLite's `sqlite3IsOverflow` (`src/util.c`): true iff
    *x* is NaN or +/-Inf. `sumFinalize`/`avgFinalize` both guard the
    `rSum + rErr` step with this. `rErr` only reaches NaN or Inf if an
    intermediate KBN step produced infinity (summing values near
    `DBL_MAX`), which is why it is ported even though ordinary input
    never triggers it."""
    return math.isnan(x) or math.isinf(x)


class _Accumulator:
    """Per-call running state for one aggregate. `Aggregate.rows()`
    builds one instance per `AggregateCall` (per group key, on the
    grouped path), steps each with every row of its group exactly once,
    then reads `finish()` - never a second pass over the child's rows.

    `step`/`finish` dispatch on `self._call.kind` with a plain `if`/
    `elif` chain - not a per-kind subclass and not a dispatch table
    (`AGENTS.md`: no dynamic dispatch tricks), the same style as
    `exec/expression.py`'s `evaluate()`. An enum match is the direct
    Rust idiom for it.

    `DISTINCT` (#84): `self._distinct_seen` is a set of
    `values.group_key` results (SQL grouping equality: `1` and `1.0`
    share a key, `'1'` does not), `None` when the call is not
    `DISTINCT`. `count(<expr>)`, `sum` and `avg` check it after the
    NULL test and skip a value whose key was already seen; `min` and
    `max` never consult it. The key is always on the raw value, never
    a coerced one: `'3'` and `3` stay distinct even though both coerce
    to the number 3 (#88).

    `sum`/`avg`: SQLite's `avg` shares `sum`'s `sumStep` and differs
    only in its final step (`_docs/decisions.md`, 2026-09-25, #88 and
    #91), so both kinds run one step algorithm over their own state.
    `try_numeric_affinity` classifies each value: an `INTEGER`, or TEXT
    that is wholly an integer, adds to the exact `self._sum_int`
    (`_sum_add`). Anything else - a REAL, other TEXT - goes into the
    KBN pair (`self._sum_r_sum`, `self._sum_r_err`) through
    `arithmetic_operand`'s leading-prefix coercion and latches
    `self._sum_saw_non_integer`. The exact path is abandoned for good
    (`self._sum_approx`) at the first non-integer value or the first
    int64 overflow; `_kbn_init` folds the old `_sum_int` in.

    `sum` raises in `finish()` only, iff `self._sum_overflowed` (the
    exact total left int64 at some step; latched, never cleared) and
    not `self._sum_saw_non_integer`. That equals SQLite's live-clearing
    `ovrfl` flag for every input order, since an overflow can only
    happen before the first non-integer value. Arithmetic promotes an
    overflow to REAL (decisions, 2026-09-01); `sum` raises instead.
    """

    def __init__(self, call: AggregateCall) -> None:
        self._call = call
        self._count = 0  # count(*)/count(): every row, NULL or not
        # count(<expr>); avg's denominator; and sum's "any value seen" test
        self._non_null_count = 0
        self._sum_int = 0  # exact integer running total (SumCtx.iSum) - sum and avg each own one
        self._sum_r_sum = 0.0  # KBN running sum (SumCtx.rSum), meaningful once self._sum_approx
        self._sum_r_err = 0.0  # KBN compensation term (SumCtx.rErr)
        self._sum_approx = False  # SumCtx.approx: latched True once the exact iSum path is abandoned
        self._sum_overflowed = False  # latched once a step's exact total leaves int64 range
        self._sum_saw_non_integer = False  # latched by any REAL, or TEXT that isn't a clean whole-string integer
        self._extreme: values.Value = None  # min/max's running extreme; None until the first non-NULL value
        self._distinct_seen: set | None = set() if call.distinct else None

    def _distinct_duplicate(self, value: values.Value) -> bool:
        """`True` when *value*'s `group_key` has already been recorded
        for this call - and records it when it has not. Only ever
        called from the `count(<expr>)`, `sum`, and `avg` branches of
        `step()`, and only when `self._call.distinct` (`self.
        _distinct_seen` is `None` otherwise, so this is never reached
        for a non-DISTINCT call - see the class docstring)."""
        key = values.group_key(value)
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
            # One step algorithm for both kinds (see the class
            # docstring). Classify by whole-string numeric affinity
            # first: a plain int, or TEXT whose trimmed string is
            # integer-shaped, stays on the exact int64 path as long as
            # possible. A float classification (a REAL, or TEXT such
            # as '3.0') and a str classification (TEXT with no
            # whole-string numeric reading: '3abc', 'abc', '') are
            # both "non-integer" and fold into the KBN pair through
            # the leading-prefix coercion.
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
            if self._non_null_count == 0:
                return None  # NULL: no non-NULL value was ever seen
            # Raise only here, never mid-accumulation, and only if the
            # exact integer total left int64 at some point and every
            # value was integer-classified. A non-integer value
            # anywhere (before or after the overflow) suppresses it,
            # and a later value bringing the total back into range
            # does not. See the class docstring.
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
                    total = self._sum_r_sum
                else:
                    total = self._sum_r_sum + self._sum_r_err
                # rSum itself can be NaN (+inf folded with -inf) even
                # when the _kbn_is_overflow(rErr) guard above does not
                # fire. SQLite's sqlite3_result_double(ctx, NaN) stores
                # NULL and finish() has no equivalent step, so squash
                # explicitly (#104). Infinity is untouched.
                return squash_nan(total)
            return self._sum_int
        if call.kind == "avg":
            # avgFinalize: same approx/iSum read as sum, no ovrfl check
            # at all (avg never raises), always divides as float. The
            # NULL-over-zero-rows case needs its own check, since 0/0
            # would otherwise raise.
            if self._non_null_count == 0:
                return None
            if self._sum_approx:
                total = self._sum_r_sum
                if not _kbn_is_overflow(self._sum_r_err):
                    total += self._sum_r_err
            else:
                total = float(self._sum_int)
            # Same NaN-in-rSum case as sum's branch above: a NaN total
            # divided by a non-zero count is still NaN, and must
            # squash to NULL rather than leak out of finish().
            return squash_nan(total / self._non_null_count)
        if call.kind in ("min", "max"):
            return self._extreme  # None (NULL) if no non-NULL value was ever seen
        raise AssertionError(f"exec/operators.py: unhandled aggregate kind {call.kind!r}")


def _aggregate_output_type(kind: str) -> ColumnType | None:
    """The declared type `Aggregate`'s own output schema gives one
    call's column: always ``None``, "no affinity" (#99), for every
    aggregate kind.

    This is load-bearing: `HAVING`'s `Filter` (#69) and `Project`
    evaluate comparisons against `Aggregate`'s output row, and
    `exec/expression.py`'s `_affinity_of` reads this declared type for
    the bare `BoundColumnRef` `plan/planner.py`'s `_split_expr`
    rewrites each aggregate call into. SQLite gives an aggregate
    result no affinity at all - confirmed against the oracle:
    `count(*) = '12'` is 0 even though `count(*)` is 12, and `sum(x) >
    3` compares as integers - so a declared type would make such
    comparisons coerce one side and give a wrong answer. `kind` is kept as a parameter so the call site
    stays explicit about what it is typing, even though every kind
    currently gets the same answer."""
    return None


def _group_key_output_type(expr: Expr, child_schema: Schema) -> ColumnType | None:
    """The declared type `Aggregate`'s own output schema gives one
    `group_by` key column: a bare `BoundColumnRef` keeps its source
    column's declared type (so `GROUP BY line_no HAVING line_no = '3'`
    still converts `'3'` to `3`), and every other expression shape -
    a computed key such as `line_no + 10` - is ``None``, "no
    affinity" (#99), matching SQLite, where a computed
    expression carries no affinity even when it mentions a column."""
    if isinstance(expr, BoundColumnRef):
        return child_schema.columns[expr.offset].type
    return None


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
    `SELECT`/`HAVING`/`ORDER BY` expressions - each gets one output
    column, after every `group_by` key column, in that order.
    `group_by` is `()` for the whole-table path: exactly one output row,
    always, computed by streaming `child.rows()` through one shared
    set of accumulators. A non-empty `group_by` (#69) instead computes
    one key tuple per child row (evaluated against `child`'s schema,
    same as every aggregate call's own argument), steps that key's own
    accumulator set, and - at the end - yields one row per distinct
    key, key columns first: zero groups, zero rows, over an empty
    table, the one place the two paths genuinely diverge.

    Two key tuples are the same group under SQL equality, not Python
    `==` - `values.group_key(value)` (#113) is the per-column
    dictionary key component. It spells SQL's grouping equality out
    explicitly: NULLs share a key, `1`/`1.0`/`-0.0`-style integer-
    valued numerics share a key, `2**53 + 1` and `float(2**53)` do not
    (exact, no float rounding), and `'1'` never shares a key with a
    number - see `values.group_key`'s own docstring. The first-seen
    key values are what a group shows (`1.0, 1` shows `1.0`). Groups are emitted in
    **first-row-encountered order**: a plain `dict` preserves
    insertion order, and no key is ever re-inserted once seen, so this
    falls out of the implementation rather than needing a separate
    sort - the determinism rule of `_docs/spec.md`'s "Determinism and
    row order" and `AGENTS.md`.

    Every call's and every `group_by` expression's argument is
    evaluated against `child`'s schema (the row shape *below*
    aggregation), never against this operator's own output schema -
    `Project`, above this operator (and `HAVING`'s own `Filter`,
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

    def group_by(self) -> tuple[Expr, ...]:
        """The `GROUP BY` key expressions, over the child's schema -
        read by `plan/explain.py`."""
        return self._group_by

    def calls(self) -> tuple[AggregateCall, ...]:
        """The aggregate calls, in output-column order after the group
        keys - read by `plan/explain.py`."""
        return self._calls

    def rows(self) -> Iterator[Row]:
        child_schema = self._child.schema
        if not self._group_by:
            # Whole-table path: one shared accumulator
            # set, exactly one output row, even over zero child rows.
            accumulators = [_Accumulator(call) for call in self._calls]
            for row in self._child.rows():
                for accumulator in accumulators:
                    accumulator.step(row, child_schema)
            yield tuple(accumulator.finish() for accumulator in accumulators)
            return

        # Grouped path: one accumulator set per distinct key,
        # keyed by `values.group_key` per column so grouping uses SQL
        # equality rather than Python's - see the class docstring.
        # `groups` maps that key to `(key_values, accumulators)`; a
        # plain dict's insertion order is what gives first-row-
        # encountered emission order, with no extra bookkeeping.
        groups: dict[tuple[object, ...], tuple[Row, list[_Accumulator]]] = {}
        for row in self._child.rows():
            key_values = tuple(
                coerce_to_value(evaluate(expr, row, child_schema)) for expr in self._group_by
            )
            key = tuple(values.group_key(value) for value in key_values)
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
#: expression). 1-based (`"column_2"` for the second item). `Project`
#: reads `output_name` for its schema; the placeholder only fills in
#: when the select-list item has none.
_PLACEHOLDER_COLUMN_NAME = "column_{position}"


def _project_column(position: int, item: BoundSelectItem, child_schema: Schema) -> Column:
    name = item.output_name if item.output_name is not None else _PLACEHOLDER_COLUMN_NAME.format(position=position)
    # A bare BoundColumnRef (aliased or not - only the expression's own
    # shape matters, per exec/expression.py's affinity convention this
    # mirrors) keeps its source column's declared type. Every other
    # expression shape - literal, arithmetic, concatenation, a
    # Bool3-shaped comparison, anything else - gets an explicit,
    # documented TEXT placeholder: nothing reads a computed column's
    # declared type (the CLI prints values, not types), and SQLite
    # itself has no fixed declared type for a computed result column
    # either.
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

    def select_list(self) -> tuple[BoundSelectItem, ...]:
        """The select list this `Project` evaluates - read by
        `plan/explain.py`."""
        return self._select_list

    def rows(self) -> Iterator[Row]:
        child_schema = self._child.schema
        select_list = self._select_list
        for row in self._child.rows():
            yield tuple(
                coerce_to_value(evaluate(item.expr, row, child_schema)) for item in select_list
            )


# --- Sort --------------------------------------------------------------------
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
    stable, and stays stable under `reverse=True`, so a later pass (an
    earlier key) keeps the order an earlier pass (a later key) gave
    rows that tie on it. Rows that tie on every key stay in the order
    they reached `Sort`, in `DESC` too - which is fixed already
    (`Scan`/`Filter`/`Aggregate` never reorder), so determinism
    (`AGENTS.md`, spec §3's "Determinism and row order") follows from
    the stability. `_docs/decisions.md`, 2026-09-24, has the tie
    order.

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

    def keys(self) -> tuple[SortKey, ...]:
        """The sort keys, first key first - read by `plan/explain.py`."""
        return self._keys

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


# --- Limit -------------------------------------------------------------------
#
# `_docs/spec.md` §3's `Limit` operator: `LIMIT`/`OFFSET`. Outermost in
# the tree (`plan/planner.py` places it above `Distinct`, which sits
# above `Project`). Semantics verified against sqlite3 3.51.0
# (`_docs/decisions.md`): `LIMIT 0` is zero rows; a negative `LIMIT` means "no limit" and
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
    laziness `Limit` has - not `LIMIT` pushdown into
    a scan (spec §3/§6, M4), which is a planner/scan negotiation this
    operator knows nothing about. `tests/test_operators.py`'s
    spy-`ScanSource` tests count the pulls.

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

    def limit(self) -> int:
        """The row limit; negative means no limit - read by
        `plan/explain.py`."""
        return self._limit

    def offset(self) -> int:
        """The rows skipped first, already clamped to 0 or more."""
        return self._offset

    def rows(self) -> Iterator[Row]:
        if self._limit == 0:
            # LIMIT 0: zero rows, and - per the laziness contract - not
            # even one row pulled from `child` to discover that, OFFSET
            # or not. Checked before the offset is skipped: SQLite runs
            # nothing for `LIMIT 0 OFFSET 1` (`WHERE <raises> LIMIT 0
            # OFFSET 1` is no rows, #171), where skipping first would
            # pull a row.
            return
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
        for row in child_iter:
            yield row
            remaining -= 1
            if remaining == 0:
                # Stop the instant the limit is satisfied, before ever
                # asking `child_iter` for another row - this is what
                # keeps the total pull count at exactly `offset +
                # limit`, never `offset + limit + 1`.
                return


# --- Distinct ----------------------------------------------------------------
#
# `_docs/spec.md` §3's `Distinct` operator: `SELECT DISTINCT`. Sits
# directly above `Project` (`plan/planner.py` places it there) -
# `Distinct` dedups *projected* output rows, never the wider
# pre-`Project` row `Sort` (below `Project`) sorts by; see
# `_docs/decisions.md` for why that placement still gives the right
# answer once `sql/binder.py`'s DISTINCT/ORDER BY narrowing is in
# place.


class Distinct:
    """`SELECT DISTINCT` (spec §3): yields every row of `child` the
    first time its dedup key is seen, and never again.

    Two key tuples are the same dedup group under SQL equality, not
    Python `==` - `tuple(values.group_key(value) for value in row)`
    (#113) is exactly the per-column key `Aggregate`'s own
    grouped path (`Aggregate.rows()`) builds: `NULL`s are equal to
    each other and form one group, `1` and `1.0` merge into one group
    (the first-encountered representative is what gets yielded), and
    `'1'` (`TEXT`) stays its own group, never merging with numeric `1`
    (`values.group_key` tags TEXT apart from every number).

    Streams: pulls one row at a time from `child` and yields it
    immediately the first time its key is new, holding only the
    growing set of already-seen keys in memory - unlike `Sort`, it
    never materializes `child.rows()` up front. This also gives
    determinism for free: first-seen order is exactly the order rows
    arrive from `child`, which is already fixed by everything below it
    (`Scan`/`Filter`/`Aggregate` never reorder; `Sort`, when present,
    reorders deterministically via its own stable, per-key contract;
    `Project` is a 1-in-1-out, order-preserving generator).

    Which row a group of duplicates keeps is observable: two rows that
    share a dedup key are SQL-equal in every column but not necessarily
    the same storage class (`1` and `1.0`), so the first one seen is
    the one yielded, matching SQLite (`SELECT DISTINCT` over bound
    `1.0, 1` returns `1.0`; over `-0.0, 0, 0.0` returns `-0.0`).
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
            key = tuple(values.group_key(value) for value in row)
            if key in seen:
                continue
            seen.add(key)
            yield row


def child_of(op: Operator) -> Operator | None:
    """The one input operator of *op*, or `None` for a `Scan` (a
    leaf). Every phase 1 operator has at most one child, so the tree
    is a chain; this is how `plan/optimizer.py` walks it without
    reaching into another module's private attributes. An explicit
    `isinstance` chain, one branch per operator class (AGENTS.md: no
    dynamic dispatch) - phase 3's `HashJoin`, with two children, will
    need its own accessor rather than a branch here."""
    if isinstance(op, Scan):
        return None
    if isinstance(op, Filter):
        return op._child
    if isinstance(op, ConstantGuard):
        return op._child
    if isinstance(op, Aggregate):
        return op._child
    if isinstance(op, Project):
        return op._child
    if isinstance(op, Sort):
        return op._child
    if isinstance(op, Limit):
        return op._child
    if isinstance(op, Distinct):
        return op._child
    raise AssertionError(f"exec/operators.py: unhandled operator type {type(op).__name__}")
