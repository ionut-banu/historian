"""The operator layer: `Scan`, `Filter`, `Project`.

Issue #34 (spec §6 M2 item 8b). Implements `_docs/spec.md` §3's
"Operators" section for the first three of phase 1's seven operators
(`Aggregate`, `Sort`, `Limit`, `Distinct` are §6 M3, not this issue) -
plus, at this layer, two rules §3 states elsewhere and this is where
they are actually enforced: "Expression evaluation" (every predicate
and select-list expression goes through `exec/expression.py`'s
`evaluate(expr, row, schema)`, #12, merged) and "Determinism and row
order" ("the same repository and the same query always produce the
same rows in the same order").

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
transformed per-row (`Project`), never rearranged.

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

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Protocol

from historian import values
from historian.exec.expression import EvalError, coerce_to_bool3, coerce_to_value, evaluate
from historian.schema import Column, ColumnType, Row, Schema
from historian.sql.ast import Expr
from historian.sql.binder import BoundColumnRef, BoundSelectItem
from historian.sql.lexer import Position

__all__ = ["Aggregate", "AggregateCall", "Filter", "Operator", "Project", "Scan", "ScanSource"]


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
    """

    kind: str
    arg: Expr | None
    position: Position


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


def _sum_add(total: values.Value, value: values.Value, position: Position) -> values.Value:
    """One running-total step for `sum` only (never `avg` - see
    `_Accumulator.step`'s own `avg` branch, which accumulates
    independently and never raises). Both operands are already
    non-NULL `Value`s.

    While both `total` and `value` are `int`, the addition is exact
    Python `int` arithmetic, checked against int64 bounds afterward -
    confirmed against `sqlite3`: `sum` over two copies of int64's own
    max raises `integer overflow`, it does not wrap and does not
    promote. The moment either operand is a `float`, this switches to
    float addition permanently (a later `int` value added to an
    already-`float` total promotes through Python's own `int + float`)
    and never raises again from that point on - `sqlite3`'s own `sum()`
    switches to a floating accumulator the instant a REAL value is
    seen and stops checking for integer overflow, matching #60's own
    "sum of a mix of integer and real returns real" edge case.
    """
    if isinstance(total, int) and isinstance(value, int):
        result = total + value
        if not (_SUM_INT64_MIN <= result <= _SUM_INT64_MAX):
            raise EvalError(
                "integer overflow computing sum(...) - sqlite3 raises here too, "
                "rather than wrapping or promoting to REAL",
                position,
            )
        return result
    return float(total) + float(value)


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
    """

    def __init__(self, call: AggregateCall) -> None:
        self._call = call
        self._count = 0  # count(*)/count(): every row, NULL or not
        self._non_null_count = 0  # count(<expr>), and avg's denominator
        self._sum: values.Value = None  # sum's running total; None until the first non-NULL value
        self._avg_total = 0.0  # avg's own running total - always float, never raises
        self._extreme: values.Value = None  # min/max's running extreme; None until the first non-NULL value

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
            if coerce_to_value(evaluate(call.arg, row, schema)) is not None:
                self._non_null_count += 1
            return

        # sum/avg/min/max all ignore a NULL argument value entirely -
        # confirmed against sqlite3 (spec §3's aggregate edge-case
        # table: "sum/avg/min/max with some NULLs: NULLs ignored").
        value = coerce_to_value(evaluate(call.arg, row, schema))
        if value is None:
            return
        self._non_null_count += 1
        if call.kind == "sum":
            self._sum = value if self._sum is None else _sum_add(self._sum, value, call.position)
        elif call.kind == "avg":
            self._avg_total += value
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
            return self._sum  # None (NULL) if no non-NULL value was ever seen
        if call.kind == "avg":
            # Real-typed unconditionally, even when the division is
            # exact (confirmed against sqlite3: avg(2,4,6) is 4.0, not
            # 4) - Python's `/` already returns a float here since
            # self._avg_total starts at 0.0, so no explicit cast is
            # needed for that; the NULL-over-zero-rows case still needs
            # its own check, since 0/0 would otherwise raise.
            return None if self._non_null_count == 0 else self._avg_total / self._non_null_count
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
