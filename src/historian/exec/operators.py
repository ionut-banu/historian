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
from typing import Protocol

from historian import values
from historian.exec.expression import coerce_to_bool3, coerce_to_value, evaluate
from historian.schema import Column, ColumnType, Row, Schema
from historian.sql.ast import Expr
from historian.sql.binder import BoundColumnRef, BoundSelectItem

__all__ = ["Filter", "Operator", "Project", "Scan", "ScanSource"]


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
