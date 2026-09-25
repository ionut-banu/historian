"""Tests for historian.exec.operators: Scan, Filter, Project, Aggregate.

Issue #34 (spec §6 M2 item 8b) for Scan/Filter/Project; issue #60 adds
the `Aggregate` section near the end. Unit-style per spec §4's test-
architecture table ("`tests/test_operators.py` ... against in-memory
rows, no repository") and `AGENTS.md`'s "no git and no subprocess" rule
for everything above the scan layer: every fixture below is a hand-
built `Schema`/`Row`/fake scan source, and no test in this file
constructs a real `BlameScan` or runs a git subprocess.

`_SCHEMA` mirrors `blame`'s own shape closely enough to read naturally
against spec §2, without being `blame` itself: `path` (TEXT), `line_no`
(INTEGER), `author_email` (TEXT, nullable) - the three columns this
file's predicates and select lists actually exercise. Expected values
for every predicate below were checked against the `sqlite3` command-
line tool (3.51.0), matching `tests/test_expression.py`'s convention.

The `Aggregate` section exists because real `blame` data cannot
exercise several of its edge cases at all: `tables/blame.py` asserts
every blame column is non-`NULL` before a row is ever emitted, and
`CASE` does not exist yet (no AST node), so no expression built over
real `blame` columns can ever be `NULL` for some rows and a real value
for others in the same column - the exact shape `count(x)`'s and
`sum`/`min`/`max`'s NULL-skipping behaviour needs to be seen actually
skipping something. Those cases are unit tests against `Aggregate`
with synthetic rows here, not differential tests - see
`tests/differential/test_blame.py`'s own "Aggregate (issue #60)"
section for what *is* covered differentially and why.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterator, Sequence

import pytest

from historian.exec.expression import EvalError
from historian.exec.operators import (
    Aggregate,
    AggregateCall,
    Distinct,
    Filter,
    Limit,
    Project,
    Scan,
    Sort,
    SortKey,
)
from historian.schema import Column, ColumnType, Row, Schema
from historian.sql.ast import And, BinaryOp, Literal, Not, Operator as Op
from historian.sql.binder import BoundColumnRef, BoundSelectItem
from historian.sql.lexer import Position

_POS = Position(line=1, column=1, offset=0)

#: `blame`-shaped but not `blame`: path TEXT, line_no INTEGER,
#: author_email TEXT (nullable) - the three columns this file's
#: predicates and select lists exercise.
_SCHEMA = Schema(
    columns=(
        Column("path", ColumnType.TEXT),
        Column("line_no", ColumnType.INTEGER),
        Column("author_email", ColumnType.TEXT),
    )
)


def _lit(value) -> Literal:
    return Literal(value, _POS)


def _col(name: str) -> BoundColumnRef:
    return BoundColumnRef(offset=_SCHEMA.index_of(name), name=name, position=_POS)


def _bin(op: Op, left, right) -> BinaryOp:
    return BinaryOp(op=op, left=left, right=right, position=_POS)


# --- fake scan sources -----------------------------------------------------


class _FakeSource:
    """A minimal fake satisfying exactly the three-member interface
    `Scan` adapts: `schema`, `capabilities()`, `scan(pushed=())`. No
    relation to `BlameScan` at all - this is the "obviously not
    coincidentally shaped like blame" fake."""

    schema = _SCHEMA

    def __init__(self, rows: Sequence[Row]) -> None:
        self._rows = rows
        self.scan_calls: list[Sequence[object]] = []

    def capabilities(self) -> set[str]:
        return set()

    def scan(self, pushed: Sequence[object] = ()) -> Iterator[Row]:
        self.scan_calls.append(pushed)
        yield from self._rows


class _BlameShapedSource:
    """A second, independently-built fake exposing `BlameScan`'s real,
    already-merged public shape (spec §2 / `tables/blame.py`) - schema
    is a plain class attribute, `capabilities()` takes no arguments and
    returns `set()`, `scan()`'s one parameter is spelled and defaulted
    exactly `pushed: Sequence[object] = ()`. Deliberately has an extra
    constructor argument and an unrelated internal attribute `BlameScan`
    does not have, so nothing about this class could accidentally be
    the *same* object `Scan` was written against - proving the adapter
    in `exec/operators.py` is structural, not coincidental. Never
    imports `historian.tables.blame`, never touches git."""

    schema = _SCHEMA

    def __init__(self, rows: Sequence[Row], label: str) -> None:
        self._rows = rows
        self._label = label  # unrelated to BlameScan's own __init__

    def capabilities(self) -> set[str]:
        return set()

    def scan(self, pushed: Sequence[object] = ()) -> Iterator[Row]:
        assert pushed == ()
        yield from self._rows


class _CountingSource:
    """A fake whose `scan()` increments `pulled` once per row actually
    consumed by the caller - used to prove `Scan.rows()` streams rather
    than materializing (spec §3's "Generators are used inside
    `rows()`")."""

    schema = _SCHEMA

    def __init__(self, rows: Sequence[Row]) -> None:
        self._rows = rows
        self.pulled = 0

    def capabilities(self) -> set[str]:
        return set()

    def scan(self, pushed: Sequence[object] = ()) -> Iterator[Row]:
        for row in self._rows:
            self.pulled += 1
            yield row


# --- Scan --------------------------------------------------------------


def test_scan_yields_exactly_the_source_rows_in_order():
    rows = (
        ("a.py", 3, "ana@x.com"),
        ("a.py", 1, None),
        ("b.py", 2, "bob@x.com"),
    )
    source = _FakeSource(rows)
    scan = Scan(source)

    assert scan.schema is _SCHEMA
    assert tuple(scan.rows()) == rows


def test_scan_always_offers_zero_pushed_terms():
    """Pushdown (predicate splitting, capability negotiation) is §6 M4,
    items 13-14 - not built yet. `Scan` can only ever call
    `.scan(())`, confirmed by inspecting what the fake source actually
    received."""
    source = _FakeSource((("a.py", 1, None),))
    scan = Scan(source)

    list(scan.rows())

    assert source.scan_calls == [()]


def test_scan_adapts_a_second_independently_shaped_source_with_no_special_casing():
    """A different fake, sharing only `BlameScan`'s public shape (see
    `_BlameShapedSource`'s own docstring) - not `_FakeSource` above,
    not `BlameScan` itself - still works, unchanged, proving `Scan`'s
    adapter is structural rather than written against one particular
    class."""
    rows = (("c.py", 7, "cara@x.com"),)
    source = _BlameShapedSource(rows, label="not-a-blame-scan")
    scan = Scan(source)

    assert scan.schema is _SCHEMA
    assert tuple(scan.rows()) == rows


def test_scan_streams_rather_than_materializing():
    rows = (
        ("a.py", 1, None),
        ("a.py", 2, None),
        ("a.py", 3, None),
    )
    source = _CountingSource(rows)
    scan = Scan(source)

    first = next(iter(scan.rows()))

    assert first == rows[0]
    assert source.pulled == 1


# --- Filter --------------------------------------------------------------

_ROWS = (
    ("a.py", 2, "ana@x.com"),
    ("a.py", 5, "bob@x.com"),
    ("a.py", 1, None),
    ("a.py", 4, "cara@x.com"),
)


def _child(rows=_ROWS) -> Scan:
    return Scan(_FakeSource(rows))


def test_filter_drops_every_row_when_predicate_is_false():
    """`1 = 0` is `FALSE` for every row, regardless of content -
    confirmed `select 1 = 0;` -> `0`."""
    predicate = _bin(Op.EQ, _lit(1), _lit(0))
    result = Filter(_child(), predicate)

    assert tuple(result.rows()) == ()


def test_filter_drops_every_row_when_predicate_is_null():
    """`author_email = NULL` is `NULL` for every row, including the row
    whose own `author_email` is already `NULL` (`NULL = NULL` is
    `NULL`, not `TRUE` - confirmed `select null = null;` -> empty/NULL).
    This is the case that catches a bare `if evaluate(...):` bug: such
    a predicate is falsy in Python exactly like `FALSE` is, but must be
    rejected for the same reason, not because it happens to look the
    same."""
    predicate = _bin(Op.EQ, _col("author_email"), _lit(None))
    result = Filter(_child(), predicate)

    assert tuple(result.rows()) == ()


def test_filter_keeps_only_true_rows_and_preserves_input_order():
    """`line_no > 1` keeps three of the four rows above. The surviving
    rows' `line_no` values are 2, 5, 4 in that order - neither
    ascending nor descending - which is the point: the output order
    must be exactly the input order restricted to survivors, not any
    sort order a coincidentally-sorted fixture could hide a bug
    behind."""
    predicate = _bin(Op.GT, _col("line_no"), _lit(1))
    result = Filter(_child(), predicate)

    assert tuple(result.rows()) == (
        ("a.py", 2, "ana@x.com"),
        ("a.py", 5, "bob@x.com"),
        ("a.py", 4, "cara@x.com"),
    )


def test_filter_schema_is_exactly_the_child_schema():
    predicate = _bin(Op.EQ, _lit(1), _lit(1))
    result = Filter(_child(), predicate)

    assert result.schema is _SCHEMA


def test_filter_streams_rather_than_materializing():
    source = _CountingSource(_ROWS)
    scan = Scan(source)
    predicate = _bin(Op.GT, _col("line_no"), _lit(0))
    result = Filter(scan, predicate)

    first = next(iter(result.rows()))

    assert first == _ROWS[0]
    assert source.pulled == 1


def test_filter_coerces_a_value_shaped_predicate_with_c_style_truthiness_not_bare_python_truthiness():
    """Pins `Filter` -> `coerce_to_bool3` -> `values.is_true`, not a
    bare `if evaluate(...):` - the same invariant
    `test_filter_raises_rather_than_silently_coercing_a_value_shaped_
    predicate` pinned before #38, rewritten because that test's own
    premise (`WHERE line_no` raises `TypeError`) is exactly what #38
    is chartered to remove: `WHERE line_no` is legal SQL now, and
    `Filter` must return a *result*, not an exception, for it.

    QA's FAIL on the original #34 found that three comparison-shaped
    predicates alone cannot tell `values.is_true(evaluate(...))` apart
    from a bare `if evaluate(...):`, because `evaluate()` only ever
    hands back a `Bool3` for those and `bool(None) == bool(False)` in
    Python. #38 reopens the same trap in a new shape: once `Filter`
    coerces a `Value`-shaped predicate into a `Bool3` at all, a
    *correct* coercion and a bare `if evaluate(...):` on the
    *uncoerced* `Value` can still disagree - and only a predicate where
    SQLite's own truthiness rule and Python's built-in truthiness give
    different answers can catch a `Filter` that skips the coercion
    step and falls back to testing `evaluate()`'s raw result directly.

    `'0abc'` is exactly that predicate (confirmed against `sqlite3`,
    also pinned as a differential case in `tests/differential/
    test_blame.py`: `create table t(s text); insert into t
    values('0abc'); select 'kept' from t where s;` -> no rows). As a
    bare Python string, `'0abc'` is truthy (nonempty) - a `Filter`
    that tested `evaluate()`'s result directly with `if ...:` would
    keep every row. SQLite's leading-prefix numeric coercion reads
    `'0abc'` as `0`, falsy, and drops every row instead - which is
    what `coerce_to_bool3` computes and what `values.is_true` then
    rejects. Confirmed by hand: substituting
    `if evaluate(self._predicate, row, child_schema):` for the
    `coerce_to_bool3`/`is_true` pair and rerunning the suite turns
    exactly this test red (all four rows kept instead of none) while
    every comparison-shaped `Filter` test above keeps passing.
    """
    predicate = _lit("0abc")
    result = Filter(_child(), predicate)

    assert tuple(result.rows()) == ()


def test_filter_no_longer_raises_on_a_value_shaped_predicate():
    """`WHERE line_no` (#38's own headline case) used to raise
    `TypeError` - `evaluate()` returns the row's plain `line_no`
    integer, a `Value`, and `values.is_true` rejected it outright. It
    is legal now: `coerce_to_bool3` gives it SQLite's C-style
    truthiness first. Every row here has a nonzero `line_no` (2, 5, 1,
    4), so every row is kept - matching `sqlite3`'s own
    `select x from t where x` behaviour for a nonzero numeric column."""
    predicate = _col("line_no")
    result = Filter(_child(), predicate)

    assert tuple(result.rows()) == _ROWS


def test_filter_coerces_a_value_shaped_operand_nested_inside_and():
    """Issue #38 round 2 (QA FAIL): `coerce_to_bool3` was only called
    at `Filter`'s own root call site, so a value-shaped operand
    *nested* inside `AND` - not the predicate root itself - reached
    `values.and3` raw and raised `TypeError`. QA's own reproduction:
    `WHERE line_no - line_no AND path = 'a.py'`.
    `line_no - line_no` is `0` (falsy) for every row here, so `AND`
    drops every row regardless of the right operand - `sqlite3`:
    `select p from t where n-n and p='a.py';` -> no rows."""
    value_falsy = _bin(Op.SUB, _col("line_no"), _col("line_no"))
    predicate = And(value_falsy, _bin(Op.EQ, _col("path"), _lit("a.py")), _POS)
    result = Filter(_child(), predicate)

    assert tuple(result.rows()) == ()


def test_filter_coerces_a_value_shaped_operand_nested_inside_not():
    """Same hole, `NOT` instead of `AND`: `NOT (line_no - line_no)` is
    `NOT (0)`, truthy, for every row - `sqlite3`: `select not(5-5);`
    -> `1`."""
    value_falsy = _bin(Op.SUB, _col("line_no"), _col("line_no"))
    predicate = Not(value_falsy, _POS)
    result = Filter(_child(), predicate)

    assert tuple(result.rows()) == _ROWS


# --- Project --------------------------------------------------------------


def _item(expr, alias, output_name) -> BoundSelectItem:
    return BoundSelectItem(expr=expr, alias=alias, output_name=output_name, position=_POS)


def test_project_evaluates_each_item_in_order():
    select_list = (
        _item(_col("path"), alias=None, output_name="path"),
        _item(_bin(Op.ADD, _col("line_no"), _lit(1)), alias="next_line", output_name="next_line"),
    )
    result = Project(_child(), select_list)

    assert tuple(result.rows()) == (
        ("a.py", 3),
        ("a.py", 6),
        ("a.py", 2),
        ("a.py", 5),
    )


def test_project_schema_uses_output_name_when_present():
    select_list = (
        _item(_col("path"), alias=None, output_name="path"),
        _item(_col("line_no"), alias="ln", output_name="ln"),
    )
    result = Project(_child(), select_list)

    assert result.schema.names == ("path", "ln")


def test_project_schema_falls_back_to_a_positional_placeholder_when_output_name_is_none():
    """An unaliased, non-column expression (`SELECT 1`) has no
    `output_name` per `sql/binder.py`'s own docstring. `Project`'s
    fallback here is `column_<1-based position>` - documented, not
    load-bearing, matching the criterion's own example spelling."""
    select_list = (
        _item(_col("path"), alias=None, output_name="path"),
        _item(_lit(1), alias=None, output_name=None),
    )
    result = Project(_child(), select_list)

    assert result.schema.names == ("path", "column_2")


def test_project_schema_column_type_is_the_source_type_for_a_bare_column_reference():
    """`line_no` is `INTEGER` in `_SCHEMA`; a bare, unaliased
    `BoundColumnRef` to it keeps that declared type."""
    select_list = (_item(_col("line_no"), alias=None, output_name="line_no"),)
    result = Project(_child(), select_list)

    assert result.schema.columns[0].type is ColumnType.INTEGER


def test_project_schema_column_type_is_the_source_type_for_an_aliased_bare_column_reference():
    """The same rule applies whether or not the bare column reference
    is aliased - only the *shape* of the expression (a bare
    `BoundColumnRef`) matters, not the presence of an alias."""
    select_list = (_item(_col("line_no"), alias="ln", output_name="ln"),)
    result = Project(_child(), select_list)

    assert result.schema.columns[0].type is ColumnType.INTEGER


def test_project_schema_column_type_is_text_placeholder_for_a_computed_expression():
    """Arithmetic, concatenation, literals - anything that is not a
    bare `BoundColumnRef` - gets the documented `TEXT` placeholder,
    per this issue's own criteria: not load-bearing, nothing downstream
    reads it yet."""
    select_list = (
        _item(_bin(Op.ADD, _col("line_no"), _lit(1)), alias="next_line", output_name="next_line"),
        _item(_lit("x"), alias=None, output_name=None),
    )
    result = Project(_child(), select_list)

    assert result.schema.columns[0].type is ColumnType.TEXT
    assert result.schema.columns[1].type is ColumnType.TEXT


def test_project_row_order_matches_child_row_order():
    select_list = (_item(_col("line_no"), alias=None, output_name="line_no"),)
    result = Project(_child(), select_list)

    assert tuple(result.rows()) == ((2,), (5,), (1,), (4,))


def test_project_coerces_a_bool3_shaped_item_to_sqlites_own_int_spelling():
    """`SELECT 1 = 1, 1 = 2, 1 = NULL` - a comparison is predicate-
    shaped, so `evaluate()` returns a `Bool3` (`True`/`False`/`None`)
    for each. `Project` must store SQLite's own `1`/`0`/`NULL`
    spelling instead (confirmed against `sqlite3`: `select 1 = 1,
    typeof(1 = 1), 1 = 2, typeof(1 = 2), 1 = null, typeof(1 = null);`
    -> `1|integer|0|integer||null`). Checked with `type(cell) is int`,
    never `bool` - `True == 1` in Python, so a bare `==` assertion
    would be blind to a `Filter`/`Project` that stored the raw
    `Bool3` unchanged, per this issue's own criteria."""
    select_list = (
        _item(_bin(Op.EQ, _lit(1), _lit(1)), alias=None, output_name=None),
        _item(_bin(Op.EQ, _lit(1), _lit(2)), alias=None, output_name=None),
        _item(_bin(Op.EQ, _lit(1), _lit(None)), alias=None, output_name=None),
    )
    result = Project(_child(rows=(("a.py", 2, "ana@x.com"),)), select_list)

    (row,) = tuple(result.rows())

    assert row == (1, 0, None)
    assert type(row[0]) is int
    assert type(row[1]) is int
    assert row[2] is None


def test_project_coerces_a_value_shaped_operand_nested_inside_and():
    """Issue #38 round 2 (QA FAIL), select-list side: `(line_no -
    line_no) AND 1` - `AND`'s own result is coerced to SQLite's int
    spelling by `Project`'s root-level `coerce_to_value` (already
    covered above), but the *left operand* of that `AND` is itself
    value-shaped and was never coerced before reaching `values.and3`,
    raising `TypeError` before this issue's fix. `sqlite3`: `select
    typeof((n-n) and 1), (n-n) and 1 from t;` -> `integer|0` for every
    row. QA's own reproduction case for this issue, select-list side."""
    value_falsy = _bin(Op.SUB, _col("line_no"), _col("line_no"))
    select_list = (_item(And(value_falsy, _lit(1), _POS), alias=None, output_name=None),)
    result = Project(_child(rows=(("a.py", 2, "ana@x.com"),)), select_list)

    (row,) = tuple(result.rows())

    assert row == (0,)
    assert type(row[0]) is int


def test_project_streams_rather_than_materializing():
    source = _CountingSource(_ROWS)
    scan = Scan(source)
    select_list = (_item(_col("path"), alias=None, output_name="path"),)
    result = Project(scan, select_list)

    first = next(iter(result.rows()))

    assert first == ("a.py",)
    assert source.pulled == 1


# --- End-to-end pipeline ---------------------------------------------------


def test_scan_filter_project_pipeline_end_to_end():
    """`SELECT path, line_no FROM <fake> WHERE line_no > 1`, built
    entirely from in-memory fakes - no repository, no planner."""
    source = _FakeSource(_ROWS)
    scan = Scan(source)
    predicate = _bin(Op.GT, _col("line_no"), _lit(1))
    filtered = Filter(scan, predicate)
    select_list = (
        _item(_col("path"), alias=None, output_name="path"),
        _item(_col("line_no"), alias=None, output_name="line_no"),
    )
    projected = Project(filtered, select_list)

    assert projected.schema.names == ("path", "line_no")
    assert tuple(projected.rows()) == (
        ("a.py", 2),
        ("a.py", 5),
        ("a.py", 4),
    )


# --- Shared operator shape --------------------------------------------------


def test_scan_filter_project_all_expose_the_same_operator_shape():
    scan = Scan(_FakeSource(_ROWS))
    filt = Filter(scan, _bin(Op.EQ, _lit(1), _lit(1)))
    proj = Project(filt, (_item(_col("path"), alias=None, output_name="path"),))

    for operator in (scan, filt, proj):
        assert isinstance(operator.schema, Schema)
        produced = operator.rows()
        assert isinstance(produced, Iterator) or hasattr(produced, "__next__") or hasattr(produced, "__iter__")
        # Actually consuming it proves rows() really is iterable, not
        # just something with the right attribute names.
        list(itertools.islice(produced, 1))


# --- Documented Value/Bool3 boundary (#38, handled here) ------------------


def test_operators_module_documents_the_value_bool3_boundary():
    """#38 handles the `Value`/`Bool3` coercion boundary this module's
    docstrings used to describe as out of scope - they must describe
    it as handled now, naming the two `exec/expression.py` coercion
    helpers each operator actually calls, rather than still reading
    like an open gap. Checked here as a real assertion, not a comment
    nobody enforces."""
    import historian.exec.operators as operators_module

    combined_text = "\n".join(
        filter(
            None,
            [
                operators_module.__doc__,
                Filter.__doc__,
                Project.__doc__,
            ],
        )
    )

    assert "Bool3" in combined_text
    assert "WHERE line_no" in combined_text or "line_no" in combined_text
    assert "1 = 1" in combined_text or "SELECT 1" in combined_text
    assert "coerce_to_bool3" in combined_text
    assert "coerce_to_value" in combined_text


# --- Aggregate (issue #60): the whole-table path only -----------------------
#
# See the module docstring's own note on why these are unit tests
# against synthetic rows rather than differential cases: real `blame`
# data cannot produce a column that is NULL for some rows and not
# others, and several of these cases exist specifically to prove
# NULL-skipping and mixed-storage-class ordering.


def _agg_child(rows: Sequence[Row]) -> Scan:
    return Scan(_FakeSource(rows))


def _call(kind: str, arg=None, distinct: bool = False) -> AggregateCall:
    return AggregateCall(kind=kind, arg=arg, position=_POS, distinct=distinct)


def test_aggregate_over_zero_rows_still_yields_exactly_one_row():
    """Spec §3's named "classic mistake": an ungrouped `count(*)` over
    zero input rows is `0`, not zero output rows - confirmed against
    `sqlite3`: `create table e(n integer); select count(*) from e;` ->
    one row, `0`."""
    result = Aggregate(_agg_child([]), [_call("count")])

    assert tuple(result.rows()) == ((0,),)


def test_aggregate_sum_avg_min_max_over_zero_rows_are_all_null():
    """`create table e(n integer); select count(*), sum(n), avg(n),
    min(n), max(n) from e;` -> `0||||` (one row): `count(*)` is `0`,
    every other aggregate is `NULL` over zero rows - the same input,
    different answers, per the issue's own edge-case table."""
    calls = [_call("count"), _call("sum", _col("line_no")), _call("avg", _col("line_no")),
             _call("min", _col("line_no")), _call("max", _col("line_no"))]
    result = Aggregate(_agg_child([]), calls)

    assert tuple(result.rows()) == ((0, None, None, None, None),)


def test_count_star_counts_every_row_including_null_valued_ones():
    """`count(*)` counts rows regardless of `NULL` - confirmed against
    `sqlite3`: `insert into t values (1,NULL),(1,NULL),(1,NULL); select
    count(*), count(b) from t;` -> `count(*)=3`. Every row here has a
    `NULL` `author_email`, and all three are still counted."""
    rows = [("a.py", 1, None), ("a.py", 2, None), ("a.py", 3, None)]
    result = Aggregate(_agg_child(rows), [_call("count")])

    assert tuple(result.rows()) == ((3,),)


def test_count_of_column_skips_null_values_unlike_count_star():
    """`count(*)` vs `count(x)` vs `count(x)` over `(1),(2),(NULL),(2),
    (NULL)`: confirmed against `sqlite3`, `count(*)=5`, `count(x)=3` -
    `count(x)` counts only the rows where `x` is not `NULL`. Not
    reachable differentially - see the module docstring."""
    rows = [
        ("a.py", 1, "x"),
        ("a.py", 1, "y"),
        ("a.py", 1, None),
        ("a.py", 1, "z"),
        ("a.py", 1, None),
    ]
    result = Aggregate(_agg_child(rows), [_call("count"), _call("count", _col("author_email"))])

    assert tuple(result.rows()) == ((5, 3),)


def test_sum_avg_min_max_ignore_null_valued_rows():
    """A group where every value is `NULL`: `count(*)=3`, `count(b)=0`,
    `sum(b)`/`avg(b)`/`min(b)`/`max(b)` all `NULL` - confirmed against
    `sqlite3`. Uses `path` (TEXT) as the nullable argument here since
    `_SCHEMA`'s only other nullable column, `author_email`, is also
    TEXT - either works for this case, which is about NULL-skipping,
    not type."""
    rows = [(None, 1, "a"), (None, 2, "b"), (None, 3, "c")]
    calls = [
        _call("count"),
        _call("count", _col("path")),
        _call("sum", _col("path")),
        _call("avg", _col("path")),
        _call("min", _col("path")),
        _call("max", _col("path")),
    ]
    result = Aggregate(_agg_child(rows), calls)

    assert tuple(result.rows()) == ((3, 0, None, None, None, None),)


def test_sum_avg_min_max_skip_only_the_null_rows_among_a_mix():
    """Not every row is NULL this time: `sum`/`avg`/`min`/`max` must
    ignore exactly the NULL rows and use only the rest (`1` and `5`,
    skipping the middle row's NULL `line_no`), not treat a partially-
    NULL column as entirely NULL or entirely non-NULL."""
    calls = [
        _call("sum", _col("line_no")),
        _call("avg", _col("line_no")),
        _call("min", _col("line_no")),
        _call("max", _col("line_no")),
    ]
    mixed_rows: list[Row] = [("a.py", 1, "x"), ("a.py", None, "y"), ("a.py", 5, "z")]
    result = Aggregate(_agg_child(mixed_rows), calls)

    assert tuple(result.rows()) == ((6, 3.0, 1, 5),)


def test_min_max_order_by_storage_class_then_by_value():
    """`min`/`max` across mixed storage classes (`5` int, `'abc'` text,
    `2.5` real, `NULL`, `'10'` text) - confirmed against `sqlite3`:
    `min` is `2.5` (`typeof` real, the smallest *numeric* value -
    `NULL` excluded first, then numeric ranks below text regardless of
    magnitude), `max` is `'abc'` (`typeof` text - text ranks above
    numeric, and `'abc' > '10'` bytewise since `'a'` (0x61) > `'1'`
    (0x31))."""
    rows: list[Row] = [("a.py", 5, "e1"), ("a.py", "abc", "e2"), ("a.py", 2.5, "e3"),
                        ("a.py", None, "e4"), ("a.py", "10", "e5")]
    result = Aggregate(
        _agg_child(rows), [_call("min", _col("line_no")), _call("max", _col("line_no"))]
    )

    (row,) = tuple(result.rows())
    assert row == (2.5, "abc")
    assert type(row[0]) is float
    assert type(row[1]) is str


def test_sum_of_mixed_integer_and_real_promotes_to_real():
    """`sum` of `(1, 2.5)` -> `3.5`, `typeof` real - confirmed against
    `sqlite3`. `sum` switches to a floating accumulator the instant a
    REAL value is seen."""
    rows: list[Row] = [("a.py", 1, "e"), ("a.py", 2.5, "e")]
    (row,) = tuple(Aggregate(_agg_child(rows), [_call("sum", _col("line_no"))]).rows())

    assert row == (3.5,)
    assert type(row[0]) is float


def test_sum_of_all_integers_stays_an_integer():
    """The other half of the pair above: `sum` of purely-integer values
    is not force-promoted to real just because it could be - confirmed
    against `sqlite3`: `select sum(n), typeof(sum(n)) from (select 1
    as n union all select 2);` -> `3|integer`."""
    rows: list[Row] = [("a.py", 1, "e"), ("a.py", 2, "e")]
    (row,) = tuple(Aggregate(_agg_child(rows), [_call("sum", _col("line_no"))]).rows())

    assert row == (3,)
    assert type(row[0]) is int


def test_avg_is_always_real_even_when_the_division_is_exact():
    """`avg(2,4,6)` -> `4.0`, `typeof` real - confirmed against
    `sqlite3`: `avg` never falls back to an integer result even when
    the division has no remainder."""
    rows: list[Row] = [("a.py", 2, "e"), ("a.py", 4, "e"), ("a.py", 6, "e")]
    (row,) = tuple(Aggregate(_agg_child(rows), [_call("avg", _col("line_no"))]).rows())

    assert row == (4.0,)
    assert type(row[0]) is float


def test_sum_integer_overflow_raises_eval_error_not_wrap_or_promote():
    """`create table o(n integer); insert into o values
    (9223372036854775807),(1); select sum(n) from o;` -> `Runtime
    error: integer overflow` in `sqlite3` - it does not wrap and does
    not silently promote to REAL. historian's `sum` must raise too,
    not return a wrong number."""
    rows: list[Row] = [("a.py", 9223372036854775807, "e"), ("a.py", 1, "e")]
    result = Aggregate(_agg_child(rows), [_call("sum", _col("line_no"))])

    with pytest.raises(EvalError):
        list(result.rows())


def test_sum_overflow_does_not_raise_once_a_real_value_has_been_seen():
    """Confirmed against `sqlite3` (issue #60's own grooming: "two
    copies of 9223372036854775807... and with one real added to force
    promotion first - all three overflow attempts error the same
    way")... except this specific ordering (REAL *first*, then a huge
    integer) is exactly the case where sqlite3's own accumulator has
    already switched to floating-point and stops checking for integer
    overflow at all - confirmed directly: `select sum(n) from (select
    1.0 as n union all select 9223372036854775807 union all select
    9223372036854775807);` does not error. This is the asymmetric half
    of `_sum_add`'s own docstring ("never raises again from that point
    on") that the overflow test above cannot exercise by itself."""
    rows: list[Row] = [
        ("a.py", 1.0, "e"),
        ("a.py", 9223372036854775807, "e"),
        ("a.py", 9223372036854775807, "e"),
    ]
    (row,) = tuple(Aggregate(_agg_child(rows), [_call("sum", _col("line_no"))]).rows())

    assert type(row[0]) is float


def test_count_star_and_count_paren_are_identical():
    """`count()` (no arguments at all) means the same thing as
    `count(*)` - confirmed against `sqlite3`. At the `Aggregate` level
    both are simply `AggregateCall(kind="count", arg=None)`; the
    planner (issue #60) is what makes the two parse to the same call,
    checked there."""
    rows: list[Row] = [("a.py", 1, "e"), ("b.py", 2, "e")]
    result = Aggregate(_agg_child(rows), [_call("count", arg=None)])

    assert tuple(result.rows()) == ((2,),)


def test_aggregate_schema_declares_count_integer_and_avg_real():
    """Not load-bearing (no `HAVING` yet to compare against it - #69),
    but documented rather than arbitrary: `count`'s output column is
    declared `INTEGER`, `avg`'s is declared `REAL`."""
    result = Aggregate(_agg_child([]), [_call("count"), _call("avg", _col("line_no"))])

    assert result.schema.columns[0].type is ColumnType.INTEGER
    assert result.schema.columns[1].type is ColumnType.REAL


def test_aggregate_consumes_child_rows_exactly_once():
    """`Aggregate` cannot stream its own output (it needs every row
    before it can produce the one row it emits), but it must still
    pull each child row exactly once - not once per call, even with
    several calls sharing the same child."""
    source = _CountingSource([("a.py", 1, "e"), ("a.py", 2, "e"), ("a.py", 3, "e")])
    scan = Scan(source)
    result = Aggregate(scan, [_call("count"), _call("sum", _col("line_no")), _call("max", _col("line_no"))])

    assert tuple(result.rows()) == ((3, 6, 3),)
    assert source.pulled == 3


# --- Aggregate DISTINCT (issue #84): unit-only cases -------------------
#
# NULL exclusion, mixed storage classes, order-dependent representative
# typing, and the int64-overflow interaction are all unreachable through
# real `blame` data - see `tests/differential/test_blame.py`'s own
# "Aggregate DISTINCT (issue #84)" section for what real `blame` data
# does cover (whole-table DISTINCT, computed-expression DISTINCT,
# grouped DISTINCT with HAVING, both DISTINCT mechanisms together).
# Every expected value below was checked against `sqlite3` 3.51.0
# directly - see issue #84's own grooming.


def test_distinct_null_exclusion_happens_before_the_dedup_set_is_consulted():
    """A NULL argument value is discarded by the pre-existing `if value
    is None: return` before the new dedup set is ever consulted -
    confirmed against `sqlite3` over an all-NULL column: `count(DISTINCT
    x)` is `0`, `sum`/`avg`/`min`/`max(DISTINCT x)` are all `NULL` -
    the ordinary zero-non-NULL-values rule, unaffected by DISTINCT."""
    rows: list[Row] = [("a.py", None, "e"), ("a.py", None, "e"), ("a.py", None, "e")]
    calls = [
        _call("count", _col("line_no"), distinct=True),
        _call("sum", _col("line_no"), distinct=True),
        _call("avg", _col("line_no"), distinct=True),
        _call("min", _col("line_no"), distinct=True),
        _call("max", _col("line_no"), distinct=True),
    ]
    result = Aggregate(_agg_child(rows), calls)

    assert tuple(result.rows()) == ((0, None, None, None, None),)


def test_distinct_empty_input_matches_the_all_null_case():
    """Zero rows at all - `count(*)`'s own "classic mistake" case
    still applies: one output row, `count(DISTINCT x)=0`, the rest
    `NULL`."""
    calls = [
        _call("count", _col("line_no"), distinct=True),
        _call("sum", _col("line_no"), distinct=True),
        _call("avg", _col("line_no"), distinct=True),
        _call("min", _col("line_no"), distinct=True),
        _call("max", _col("line_no"), distinct=True),
    ]
    result = Aggregate(_agg_child([]), calls)

    assert tuple(result.rows()) == ((0, None, None, None, None),)


def test_distinct_dedups_by_order_key_across_mixed_storage_classes():
    """Confirmed live over one column holding `1, 1.0, '1', NULL,
    NULL, 1`: `count(DISTINCT x)=2`, `sum(DISTINCT x)=2`,
    `avg(DISTINCT x)=1.0`, `min(DISTINCT x)=1` (typeof integer),
    `max(DISTINCT x)='1'` (typeof text) - the two numeric storage
    classes merge into one distinct value, the TEXT `'1'` stays a
    separate one, and NULLs are excluded before dedup even runs.

    `avg(DISTINCT x)=1.0` here (issue #88) because the two distinct
    values that survive dedup are `1` (the numeric representative) and
    `'1'` (TEXT) - `avg` now runs every accumulated value through
    `exec/expression.py`'s leading-prefix coercion before adding it in
    (`arithmetic_operand`), so `'1'` contributes `1.0` the same as `1`
    does: `(1 + 1.0) / 2 = 1.0`."""
    rows: list[Row] = [
        ("a.py", 1, "e"),
        ("a.py", 1.0, "e"),
        ("a.py", "1", "e"),
        ("a.py", None, "e"),
        ("a.py", None, "e"),
        ("a.py", 1, "e"),
    ]
    calls = [
        _call("count", _col("line_no"), distinct=True),
        _call("sum", _col("line_no"), distinct=True),
        _call("avg", _col("line_no"), distinct=True),
        _call("min", _col("line_no"), distinct=True),
        _call("max", _col("line_no"), distinct=True),
    ]
    (row,) = tuple(Aggregate(_agg_child(rows), calls).rows())

    assert row == (2, 2, 1.0, 1, "1")
    assert type(row[3]) is int
    assert type(row[4]) is str


def test_distinct_sum_keeps_the_first_encountered_representative_int_then_float():
    """`values(1),(1.0)` -> `sum(DISTINCT x) = 1`, `typeof` integer -
    confirmed live. The dedup gate never runs `_sum_add` a second time
    for the same key, so the survivor is whichever raw value first
    flipped the key from unseen to seen."""
    rows: list[Row] = [("a.py", 1, "e"), ("a.py", 1.0, "e")]
    (row,) = tuple(
        Aggregate(_agg_child(rows), [_call("sum", _col("line_no"), distinct=True)]).rows()
    )

    assert row == (1,)
    assert type(row[0]) is int


def test_distinct_sum_keeps_the_first_encountered_representative_float_then_int():
    """The reverse order of the case above: `values(1.0),(1)` ->
    `sum(DISTINCT x) = 1.0`, `typeof` real - confirmed live. Pins that
    the representative genuinely depends on insertion order, not on
    some canonical "prefer int" or "prefer float" rule."""
    rows: list[Row] = [("a.py", 1.0, "e"), ("a.py", 1, "e")]
    (row,) = tuple(
        Aggregate(_agg_child(rows), [_call("sum", _col("line_no"), distinct=True)]).rows()
    )

    assert row == (1.0,)
    assert type(row[0]) is float


def test_distinct_count_and_avg_are_unaffected_by_which_representative_survives():
    """Same two orderings as the pair above: `count(DISTINCT x)` is
    `1` and `avg(DISTINCT x)` is `1.0` regardless of order - a single
    merged group's count and average do not depend on which raw value
    was kept."""
    calls = [_call("count", _col("line_no"), distinct=True), _call("avg", _col("line_no"), distinct=True)]

    int_then_float: list[Row] = [("a.py", 1, "e"), ("a.py", 1.0, "e")]
    (row_a,) = tuple(Aggregate(_agg_child(int_then_float), calls).rows())
    assert row_a == (1, 1.0)

    float_then_int: list[Row] = [("a.py", 1.0, "e"), ("a.py", 1, "e")]
    (row_b,) = tuple(Aggregate(_agg_child(float_then_int), calls).rows())
    assert row_b == (1, 1.0)


def test_distinct_sum_integer_overflow_does_not_raise_once_the_duplicate_is_removed():
    """`sum(x)` over two copies of int64-max raises `EvalError`
    (`test_sum_integer_overflow_raises_eval_error_not_wrap_or_promote`
    above) - `sum(DISTINCT x)` over the exact same two rows does not,
    because the duplicate is removed by the dedup gate before
    `_sum_add` is ever called a second time, so the running total
    never exceeds int64-max. Confirmed live."""
    rows: list[Row] = [("a.py", 9223372036854775807, "e"), ("a.py", 9223372036854775807, "e")]

    plain_result = Aggregate(_agg_child(rows), [_call("sum", _col("line_no"))])
    with pytest.raises(EvalError):
        list(plain_result.rows())

    (row,) = tuple(
        Aggregate(_agg_child(rows), [_call("sum", _col("line_no"), distinct=True)]).rows()
    )
    assert row == (9223372036854775807,)


def test_distinct_min_max_never_change_under_deduplication():
    """`min`/`max(DISTINCT x)` always equal their plain forms - removing
    a duplicate can never change which value is most extreme. Over
    `5,3,3,9,9,1`: confirmed live, `min(x)=min(DISTINCT x)=1`,
    `max(x)=max(DISTINCT x)=9`. This is the accumulator-level pin that
    `_Accumulator`'s min/max branches are untouched by this issue."""
    rows: list[Row] = [
        ("a.py", 5, "e"),
        ("a.py", 3, "e"),
        ("a.py", 3, "e"),
        ("a.py", 9, "e"),
        ("a.py", 9, "e"),
        ("a.py", 1, "e"),
    ]
    plain = [_call("min", _col("line_no")), _call("max", _col("line_no"))]
    distinct = [_call("min", _col("line_no"), distinct=True), _call("max", _col("line_no"), distinct=True)]

    (plain_row,) = tuple(Aggregate(_agg_child(rows), plain).rows())
    (distinct_row,) = tuple(Aggregate(_agg_child(rows), distinct).rows())

    assert plain_row == (1, 9)
    assert distinct_row == (1, 9)


def test_distinct_count_star_is_unaffected_by_distinct_flag():
    """`count(*)`/`count()` (`call.arg is None`) can never carry
    `distinct=True` by construction (the parser never reads `DISTINCT`
    on those shapes) - but even if an `AggregateCall` were built with
    `arg=None, distinct=True` directly, `step`'s existing `call.arg is
    None` early return means the flag has no way to be consulted,
    since `count(*)` counts every row regardless of value."""
    rows: list[Row] = [("a.py", 1, "e"), ("a.py", 1, "e"), ("a.py", 2, "e")]
    (row,) = tuple(Aggregate(_agg_child(rows), [_call("count", arg=None, distinct=True)]).rows())

    assert row == (3,)


# --- Aggregate sum/avg TEXT coercion and overflow (issue #88) ----------
#
# `sum`/`avg` over TEXT operands, reusing `exec/expression.py`'s own
# `try_numeric_affinity` (whole-string numeric-affinity classification)
# and `arithmetic_operand` (leading-prefix coercion) rather than a
# second implementation - see that module for the parsers themselves.
# Every expected value below was checked live against `sqlite3` 3.51.0
# during this issue's grooming and again during implementation (see
# issue #88's own comments for the transcripts) - not reasoned out.
#
# `sum`'s overflow rule (the widened part of this issue's scope, a bug
# fix for #60): `sum` raises `integer overflow` if and only if the
# exact integer running total left the int64 range at *any* point and
# *every* non-NULL input classified as a clean whole-string integer.
# A REAL, or TEXT that is not a clean whole-string integer, anywhere in
# the input - before or after the overflow - permanently suppresses
# the check and the result is the REAL sum instead. The check happens
# once, at `finish()`, never mid-accumulation.


def test_sum_of_whole_string_integer_text_stays_exact_integer():
    """`sum('3'), sum('4')` (i.e. sum over two rows `'3'`, `'4'`) is
    `7`, `type` `int` - confirmed against `sqlite3`: both are clean
    whole-string integer-looking TEXT, so they stay on the exact int64
    path exactly like plain `INTEGER` values, not forced to `float`."""
    rows: list[Row] = [("a.py", "3", "e"), ("a.py", "4", "e")]
    (row,) = tuple(Aggregate(_agg_child(rows), [_call("sum", _col("line_no"))]).rows())

    assert row == (7,)
    assert type(row[0]) is int


def test_sum_of_non_whole_string_text_becomes_real_via_leading_prefix():
    """`sum('3abc')` over one row is `3.0`, `type` `float` - `'3abc'`
    fails the whole-string classification (trailing `abc`), so it goes
    through the leading-prefix coercion (`arithmetic_operand`, which
    reads the leading `3`) and permanently flips the running total to
    REAL - confirmed against `sqlite3`."""
    rows: list[Row] = [("a.py", "3abc", "e")]
    (row,) = tuple(Aggregate(_agg_child(rows), [_call("sum", _col("line_no"))]).rows())

    assert row == (3.0,)
    assert type(row[0]) is float


def test_sum_of_non_numeric_text_contributes_zero_but_still_flips_to_real():
    """`sum('abc')` over one row is `0.0`, `type` `float` - no digit
    anywhere in `'abc'`, so the leading-prefix coercion contributes
    `0`, but the value still is not a clean whole-string integer, so
    the result is still REAL, not the plain integer `0`."""
    rows: list[Row] = [("a.py", "abc", "e")]
    (row,) = tuple(Aggregate(_agg_child(rows), [_call("sum", _col("line_no"))]).rows())

    assert row == (0.0,)
    assert type(row[0]) is float


def test_sum_of_empty_string_contributes_zero_and_flips_to_real():
    """`sum('')` over one row is `0.0`, `type` `float` - confirmed
    against `sqlite3`: an empty string has no digits, contributes `0`,
    and is not a clean whole-string integer either."""
    rows: list[Row] = [("a.py", "", "e")]
    (row,) = tuple(Aggregate(_agg_child(rows), [_call("sum", _col("line_no"))]).rows())

    assert row == (0.0,)
    assert type(row[0]) is float


def test_sum_of_real_looking_leading_prefix_text():
    """`sum('3.5x')` over one row is `3.5`, `type` `float` - confirmed
    against `sqlite3`: the leading-prefix scan reads `3.5` and stops at
    `x`."""
    rows: list[Row] = [("a.py", "3.5x", "e")]
    (row,) = tuple(Aggregate(_agg_child(rows), [_call("sum", _col("line_no"))]).rows())

    assert row == (3.5,)
    assert type(row[0]) is float


def test_sum_of_negative_whole_string_integer_text_stays_exact_integer():
    """`sum('-2')` over one row is `-2`, `type` `int` - confirmed
    against `sqlite3`: a leading `-` is still a clean whole-string
    integer."""
    rows: list[Row] = [("a.py", "-2", "e")]
    (row,) = tuple(Aggregate(_agg_child(rows), [_call("sum", _col("line_no"))]).rows())

    assert row == (-2,)
    assert type(row[0]) is int


def test_sum_of_whitespace_padded_whole_string_integer_text_stays_exact_integer():
    """`sum(' 3')` over one row is `3`, `type` `int` - confirmed
    against `sqlite3`: leading/trailing whitespace around a
    whole-string integer is still clean."""
    rows: list[Row] = [("a.py", " 3", "e")]
    (row,) = tuple(Aggregate(_agg_child(rows), [_call("sum", _col("line_no"))]).rows())

    assert row == (3,)
    assert type(row[0]) is int


def test_sum_of_mixed_integer_and_whole_string_integer_text_stays_exact_integer():
    """`sum` over `1`, `2`, `'3'` (plain `INTEGER`s mixed with a
    whole-string-integer-looking TEXT) is `6`, `type` `int` - confirmed
    against `sqlite3`. The case the original bug report's "any TEXT
    forces REAL" phrasing gets wrong."""
    rows: list[Row] = [("a.py", 1, "e"), ("a.py", 2, "e"), ("a.py", "3", "e")]
    (row,) = tuple(Aggregate(_agg_child(rows), [_call("sum", _col("line_no"))]).rows())

    assert row == (6,)
    assert type(row[0]) is int


def test_avg_over_non_numeric_text_is_zero_real():
    """`avg('abc'), avg('def')` (i.e. avg over two rows `'abc'`,
    `'def'`) is `0.0` - confirmed against `sqlite3`: `avg` always
    accumulates as float via the leading-prefix coercion, and neither
    value has a digit."""
    rows: list[Row] = [("a.py", "abc", "e"), ("a.py", "def", "e")]
    (row,) = tuple(Aggregate(_agg_child(rows), [_call("avg", _col("line_no"))]).rows())

    assert row == (0.0,)
    assert type(row[0]) is float


def test_avg_over_mixed_integer_and_text_uses_leading_prefix_coercion():
    """`avg` over `1`, `2`, `'3'` is `2.0` - confirmed against
    `sqlite3`: `'3'` contributes `3` via the leading-prefix coercion,
    same as `avg` over three plain integers `1, 2, 3`."""
    rows: list[Row] = [("a.py", 1, "e"), ("a.py", 2, "e"), ("a.py", "3", "e")]
    (row,) = tuple(Aggregate(_agg_child(rows), [_call("avg", _col("line_no"))]).rows())

    assert row == (2.0,)
    assert type(row[0]) is float


def test_distinct_sum_coerces_only_after_the_dedup_check_not_before():
    """`sum(DISTINCT x)` over raw values `'3'`, `3` (one row each) is
    `6`, `type` `int`, and `count(DISTINCT x)` over the same is `2` -
    confirmed against `sqlite3`: `'3'` (TEXT storage class) and `3`
    (INTEGER storage class) carry different `order_key`s and are not
    deduped against each other, even though both would coerce to the
    same number `3`. This proves DISTINCT's dedup check
    (`_distinct_duplicate`) still runs on the raw, pre-coercion value -
    coercing first and deduping second would wrongly merge them into
    one distinct value and produce `3`, not `6`."""
    rows: list[Row] = [("a.py", "3", "e"), ("a.py", 3, "e")]
    calls = [_call("count", _col("line_no"), distinct=True), _call("sum", _col("line_no"), distinct=True)]
    (row,) = tuple(Aggregate(_agg_child(rows), calls).rows())

    assert row == (2, 6)
    assert type(row[1]) is int


# --- Aggregate sum overflow table (issue #88, widened scope) -----------
#
# Every row below is a live `sqlite3` 3.51.0 transcript from issue #88's
# orchestrator comment, which corrects #60's own "order-dependent
# asymmetry" description: `sum` raises `integer overflow` only at the
# very end, and only if the exact integer total left int64 range at
# any point *and* every input was integer-classified (a plain INTEGER
# or a whole-string-integer-looking TEXT) - never if any REAL or
# non-whole-string TEXT appeared anywhere, regardless of order.

_INT64_MAX = 9223372036854775807
_INT64_MIN = -9223372036854775808


def test_sum_overflow_table_row_1_two_int64_max_raises():
    """`int64max, 1` -> `Error: integer overflow`."""
    rows: list[Row] = [("a.py", _INT64_MAX, "e"), ("a.py", 1, "e")]
    with pytest.raises(EvalError):
        list(Aggregate(_agg_child(rows), [_call("sum", _col("line_no"))]).rows())


def test_sum_overflow_table_row_2_real_before_overflow_suppresses_it():
    """`1.5, int64max, 1` -> `9.22337203685478e+18` (real), no error -
    the REAL arrives before the overflowing addition, matching #60's
    own original test."""
    rows: list[Row] = [("a.py", 1.5, "e"), ("a.py", _INT64_MAX, "e"), ("a.py", 1, "e")]
    (row,) = tuple(Aggregate(_agg_child(rows), [_call("sum", _col("line_no"))]).rows())

    assert type(row[0]) is float
    assert row[0] == 1.5 + float(_INT64_MAX) + 1.0


def test_sum_overflow_table_row_3_real_after_overflow_still_suppresses_it():
    """`int64max, 1, 1.5` -> `9.22337203685478e+18` (real), no error -
    the REAL arrives *after* the overflowing addition. This is the row
    historian's `main` branch gets wrong (it raises here, since
    `_sum_add` used to raise the instant the int64+int64 step went out
    of range, before it could see the 1.5 that comes next) - the bug
    #60 shipped and this issue's widened scope fixes."""
    rows: list[Row] = [("a.py", _INT64_MAX, "e"), ("a.py", 1, "e"), ("a.py", 1.5, "e")]
    (row,) = tuple(Aggregate(_agg_child(rows), [_call("sum", _col("line_no"))]).rows())

    assert type(row[0]) is float
    assert row[0] == float(_INT64_MAX) + 1.0 + 1.5


def test_sum_overflow_table_row_4_non_clean_text_after_overflow_suppresses_it():
    """`int64max, 1, 'abc'` -> `9.22337203685478e+18` (real), no error
    - `'abc'` is not a clean whole-string integer, so it counts as
    non-integer exactly like a REAL does."""
    rows: list[Row] = [("a.py", _INT64_MAX, "e"), ("a.py", 1, "e"), ("a.py", "abc", "e")]
    (row,) = tuple(Aggregate(_agg_child(rows), [_call("sum", _col("line_no"))]).rows())

    assert type(row[0]) is float
    assert row[0] == float(_INT64_MAX) + 1.0 + 0.0


def test_sum_overflow_table_row_5_whole_string_integer_text_does_not_suppress_overflow():
    """`int64max, 1, '3'` -> `Error: integer overflow` - `'3'` is a
    clean whole-string integer, so it does *not* count as "a
    non-integer was seen" and the overflow from `int64max + 1` still
    raises."""
    rows: list[Row] = [("a.py", _INT64_MAX, "e"), ("a.py", 1, "e"), ("a.py", "3", "e")]
    with pytest.raises(EvalError):
        list(Aggregate(_agg_child(rows), [_call("sum", _col("line_no"))]).rows())


def test_sum_overflow_table_row_6_overflow_is_permanent_even_if_total_returns_to_range():
    """`int64max, 1, -1` -> `Error: integer overflow` - the running
    total goes out of range at `int64max + 1` and then `-1` would bring
    the *exact* total back to `int64max`, in range, but the overflow
    still raises: once triggered (with no non-integer value ever seen)
    it is permanent, not re-checked against the final total."""
    rows: list[Row] = [("a.py", _INT64_MAX, "e"), ("a.py", 1, "e"), ("a.py", -1, "e")]
    with pytest.raises(EvalError):
        list(Aggregate(_agg_child(rows), [_call("sum", _col("line_no"))]).rows())


def test_sum_overflow_table_row_7_overflow_permanent_with_a_second_integer_after():
    """`int64max, 1, -5` -> `Error: integer overflow` - same as row 6,
    a different in-range-again integer offset, still raises."""
    rows: list[Row] = [("a.py", _INT64_MAX, "e"), ("a.py", 1, "e"), ("a.py", -5, "e")]
    with pytest.raises(EvalError):
        list(Aggregate(_agg_child(rows), [_call("sum", _col("line_no"))]).rows())


def test_sum_overflow_table_row_8_negative_overflow_with_a_real_suppresses_it():
    """`int64min, -1, 0.0` -> `-9.22337203685478e+18` (real), no error
    - the negative-direction mirror of row 2/3/4: an ordinary REAL
    anywhere suppresses the overflow check, including on underflow."""
    rows: list[Row] = [("a.py", _INT64_MIN, "e"), ("a.py", -1, "e"), ("a.py", 0.0, "e")]
    (row,) = tuple(Aggregate(_agg_child(rows), [_call("sum", _col("line_no"))]).rows())

    assert type(row[0]) is float
    assert row[0] == float(_INT64_MIN) + -1.0 + 0.0


# --- Aggregate (issue #69): the grouped path --------------------------------
#
# Unit tests against synthetic rows, same rationale as the whole-table
# section above: `NULL`-valued and mixed-storage-class grouping keys
# cannot be produced from real `blame` data (every column is
# non-`NULL`, and `CASE` does not exist), so those cases live only
# here - see `tests/differential/test_blame.py`'s own "Aggregate
# (issue #69)" section for what real `blame` data does cover.


def test_grouped_aggregate_over_zero_rows_yields_zero_rows():
    """The one place the grouped and whole-table paths diverge on
    purpose (spec §3): a `GROUP BY` over zero input rows has no groups
    at all, unlike the whole-table case's one implicit row."""
    result = Aggregate(_agg_child([]), [_call("count")], group_by=[_col("path")])

    assert tuple(result.rows()) == ()


def test_grouped_aggregate_one_row_per_distinct_key():
    rows = [
        ("a.py", 1, "x"),
        ("a.py", 2, "x"),
        ("b.py", 3, "x"),
    ]
    result = Aggregate(_agg_child(rows), [_call("count")], group_by=[_col("path")])

    assert set(tuple(row) for row in result.rows()) == {("a.py", 2), ("b.py", 1)}


def test_grouped_aggregate_emits_groups_in_first_row_encountered_order():
    """Determinism (`AGENTS.md`, spec §3): groups are emitted in the
    order their first row was seen in the child's own row order, not
    sorted or otherwise rearranged - pinned exactly rather than merely
    "some deterministic order", so a change that reorders groups fails
    this test directly."""
    rows = [
        ("b.py", 1, "x"),
        ("a.py", 2, "x"),
        ("b.py", 3, "x"),
        ("c.py", 4, "x"),
        ("a.py", 5, "x"),
    ]
    result = Aggregate(_agg_child(rows), [_call("count")], group_by=[_col("path")])

    assert [row[0] for row in result.rows()] == ["b.py", "a.py", "c.py"]


def test_grouped_aggregate_is_stable_across_repeated_runs():
    """The same operator, iterated twice, must produce the same order
    both times - `rows()` is a generator method, not a cached list, so
    this also proves a second call re-derives the same order rather
    than depending on leftover state from the first."""
    rows = [("b.py", 1, "x"), ("a.py", 2, "x"), ("b.py", 3, "x")]
    result = Aggregate(_agg_child(rows), [_call("count")], group_by=[_col("path")])

    first = [row[0] for row in result.rows()]
    second = [row[0] for row in result.rows()]
    assert first == second == ["b.py", "a.py"]


def test_grouped_aggregate_two_columns_groups_on_the_combination():
    rows = [
        ("a.py", 1, "x"),
        ("a.py", 1, "x"),
        ("a.py", 2, "x"),
        ("b.py", 1, "x"),
    ]
    result = Aggregate(
        _agg_child(rows), [_call("count")], group_by=[_col("path"), _col("line_no")]
    )

    assert set(tuple(row) for row in result.rows()) == {
        ("a.py", 1, 2),
        ("a.py", 2, 1),
        ("b.py", 1, 1),
    }


def test_grouped_aggregate_null_valued_key_forms_one_group():
    """`_docs/spec.md`'s aggregate edge-case table: `GROUP BY` a
    column containing `NULL`s -> all `NULL`s form one group. Real
    `blame` data can never produce this (every column is non-`NULL`),
    so this is unit-only."""
    rows = [
        ("a.py", 1, None),
        ("a.py", 2, None),
        ("a.py", 3, "x@example.com"),
    ]
    result = Aggregate(_agg_child(rows), [_call("count")], group_by=[_col("author_email")])

    assert set(tuple(row) for row in result.rows()) == {(None, 2), ("x@example.com", 1)}


def test_grouped_aggregate_key_group_by_expression_not_bare_column():
    """`GROUP BY` on an expression, not a bare column - groups by the
    *value* of `line_no + 1`, matching `_docs/spec.md`'s "an arbitrary
    expression is a legal grouping key" note."""
    rows = [("a.py", 1, "x"), ("a.py", 2, "x"), ("a.py", 4, "x")]
    key_expr = _bin(Op.ADD, _col("line_no"), _lit(1))
    result = Aggregate(_agg_child(rows), [_call("count")], group_by=[key_expr])

    assert set(tuple(row) for row in result.rows()) == {(2, 1), (3, 1), (5, 1)}


def test_grouped_aggregate_mixed_storage_class_numeric_keys_merge():
    """A key of `1` and `1.0` group together (storage-class-
    insensitive numeric equality, confirmed against `sqlite3` during
    this issue's grooming); a same-valued text key `'1'` stays
    separate - not yet a code-level test before this issue."""
    # Three rows, three distinct Python-typed key values sharing one
    # column (author_email is TEXT-declared but nothing here enforces
    # that at the row level - the accumulator only sees raw Values).
    rows_with_keys: list[Row] = [
        ("a.py", 1, 1),
        ("a.py", 1, 1.0),
        ("a.py", 1, "1"),
    ]
    result = Aggregate(
        _agg_child(rows_with_keys), [_call("count")], group_by=[_col("author_email")]
    )

    assert set(tuple(row) for row in result.rows()) == {(1, 2), ("1", 1)}


def test_grouped_aggregate_schema_has_group_columns_before_aggregate_columns():
    result = Aggregate(_agg_child([]), [_call("count")], group_by=[_col("path"), _col("line_no")])

    assert [c.name for c in result.schema.columns] == ["path", "line_no", "count_1"]
    assert result.schema.columns[0].type is ColumnType.TEXT
    assert result.schema.columns[1].type is ColumnType.INTEGER
    assert result.schema.columns[2].type is ColumnType.INTEGER


def test_grouped_aggregate_consumes_each_child_row_exactly_once():
    source = _CountingSource([("a.py", 1, "x"), ("b.py", 2, "x"), ("a.py", 3, "x")])
    scan = Scan(source)
    result = Aggregate(scan, [_call("count")], group_by=[_col("path")])

    assert set(tuple(row) for row in result.rows()) == {("a.py", 2), ("b.py", 1)}
    assert source.pulled == 3


# --- Sort (issue #61) --------------------------------------------------------
#
# Unit-style against synthetic rows, mirroring the `Aggregate` section's
# own fixtures (`_agg_child`, `_SCHEMA`) - `Sort` never touches git, and
# the multi-key/NULL cases below are the exact worked example from
# `values.py`'s own module docstring, exercised here end to end through
# the real operator rather than at the bare `order_key` unit level.


def _key(expr, descending: bool = False) -> SortKey:
    return SortKey(expr=expr, descending=descending)


def test_sort_ascending_single_key_is_the_default():
    rows = [("c.py", 1, "e"), ("a.py", 1, "e"), ("b.py", 1, "e")]
    result = Sort(_agg_child(rows), [_key(_col("path"))])

    assert list(result.rows()) == [("a.py", 1, "e"), ("b.py", 1, "e"), ("c.py", 1, "e")]


def test_sort_descending_single_key():
    rows = [("a.py", 1, "e"), ("c.py", 1, "e"), ("b.py", 1, "e")]
    result = Sort(_agg_child(rows), [_key(_col("path"), descending=True)])

    assert list(result.rows()) == [("c.py", 1, "e"), ("b.py", 1, "e"), ("a.py", 1, "e")]


def test_sort_nulls_first_ascending():
    rows = [("a.py", 1, "e"), (None, 1, "e"), ("b.py", 1, "e")]
    result = Sort(_agg_child(rows), [_key(_col("path"))])

    assert [row[0] for row in result.rows()] == [None, "a.py", "b.py"]


def test_sort_nulls_last_descending():
    """DESC is the ascending order reversed for a single key
    (`values.py`'s own contract), so NULLs land last, not first."""
    rows = [("a.py", 1, "e"), (None, 1, "e"), ("b.py", 1, "e")]
    result = Sort(_agg_child(rows), [_key(_col("path"), descending=True)])

    assert [row[0] for row in result.rows()] == ["b.py", "a.py", None]


def test_sort_mixed_storage_class_numeric_then_text():
    """NULL < numeric < TEXT, matching `values.order_key`'s own
    storage-class rank - reusing `author_email` (TEXT-declared but
    unenforced at the row level, per this file's own convention) to
    hold a genuine mix."""
    rows = [("p", 1, "abc"), ("p", 1, 5), ("p", 1, None), ("p", 1, -3), ("p", 1, "a"), ("p", 1, 1.5)]
    result = Sort(_agg_child(rows), [_key(_col("author_email"))])

    assert [row[2] for row in result.rows()] == [None, -3, 1.5, 5, "a", "abc"]


def test_sort_multi_key_matches_values_py_worked_example_end_to_end():
    """`ORDER BY path ASC, line_no DESC` over `values.py`'s own
    module-docstring example (`a`/`b` renamed to `path`/`line_no`):
    `('x',NULL),('x',1),(NULL,1),(NULL,2),('y',NULL),('y',1)` ->
    `NULL|2, NULL|1, x|1, x|NULL, y|1, y|NULL` - confirmed against
    `sqlite3` there, exercised here through the real `Sort` operator
    rather than at the bare `order_key` level."""
    rows = [
        ("x", None, "e"),
        ("x", 1, "e"),
        (None, 1, "e"),
        (None, 2, "e"),
        ("y", None, "e"),
        ("y", 1, "e"),
    ]
    result = Sort(
        _agg_child(rows),
        [_key(_col("path")), _key(_col("line_no"), descending=True)],
    )

    assert [(row[0], row[1]) for row in result.rows()] == [
        (None, 2),
        (None, 1),
        ("x", 1),
        ("x", None),
        ("y", 1),
        ("y", None),
    ]


def test_sort_is_stable_among_rows_tied_on_every_key():
    """Rows that tie on the sort key keep their original relative
    order - the concrete determinism guarantee `AGENTS.md` names,
    which falls out of a stable sort applied to already-deterministic
    input rather than needing its own bookkeeping."""
    rows = [("a.py", 3, "e"), ("a.py", 1, "e"), ("a.py", 2, "e")]
    result = Sort(_agg_child(rows), [_key(_col("path"))])

    assert list(result.rows()) == rows


def test_sort_repeated_runs_give_identical_order():
    """The same rows, sorted twice via two fresh `Sort` instances over
    two fresh children, produce byte-identical order both times - the
    direct (non-oracle) determinism test this issue's own grooming
    asks for, with a genuinely non-unique key."""
    rows = [("a.py", 2, "e"), ("b.py", 1, "e"), ("a.py", 1, "e"), ("b.py", 2, "e")]
    keys = [_key(_col("path"))]

    first = list(Sort(_agg_child(rows), keys).rows())
    second = list(Sort(_agg_child(rows), keys).rows())

    assert first == second


def test_sort_schema_is_exactly_the_child_schema():
    result = Sort(_agg_child([]), [_key(_col("path"))])
    assert result.schema is _SCHEMA


def test_sort_reads_child_rows_exactly_once():
    source = _CountingSource([("b.py", 1, "e"), ("a.py", 2, "e")])
    result = Sort(Scan(source), [_key(_col("path"))])

    list(result.rows())

    assert source.pulled == 2


def test_sort_key_expression_may_be_a_computed_value_not_a_bare_column():
    """A sort key need not be a bare column - `line_no + 1`, evaluated
    per row exactly like any other value-shaped expression."""
    rows = [("a.py", 3, "e"), ("a.py", 1, "e"), ("a.py", 2, "e")]
    key_expr = _bin(Op.ADD, _col("line_no"), _lit(1))
    result = Sort(_agg_child(rows), [_key(key_expr)])

    assert [row[1] for row in result.rows()] == [1, 2, 3]


# --- Limit (issue #77) ----------------------------------------------------
#
# `_docs/spec.md` §3's `Limit` operator: `LIMIT`/`OFFSET`. Outermost in
# the tree (`plan/planner.py`'s job to place it there), above
# `Project` - see issue #77's own design, which leaves `DISTINCT`'s
# future slot (#78) between `Project` and `Limit`. Semantics verified
# against sqlite3 3.51.0 during this issue's own grooming (see
# `_docs/decisions.md`): `LIMIT 0` is zero rows, a negative `LIMIT`
# means "no limit" (`OFFSET` still applies), a negative `OFFSET` is
# clamped to 0, and an `OFFSET` past the end of the child's rows is
# zero rows, not an error.

_LIMIT_ROWS = (
    ("a.py", 1, "ana@x.com"),
    ("a.py", 2, "ana@x.com"),
    ("a.py", 3, "ana@x.com"),
    ("a.py", 4, "ana@x.com"),
    ("a.py", 5, "ana@x.com"),
)


def test_limit_truncates_to_the_first_n_rows():
    result = Limit(_child(_LIMIT_ROWS), limit=2)
    assert tuple(result.rows()) == _LIMIT_ROWS[:2]


def test_limit_zero_yields_no_rows():
    result = Limit(_child(_LIMIT_ROWS), limit=0)
    assert tuple(result.rows()) == ()


def test_negative_limit_means_no_limit():
    """Confirmed against sqlite3: a negative `LIMIT` is "no limit", not
    zero rows and not an error - every row of the child, unbounded."""
    result = Limit(_child(_LIMIT_ROWS), limit=-1)
    assert tuple(result.rows()) == _LIMIT_ROWS


def test_offset_skips_the_first_m_rows():
    result = Limit(_child(_LIMIT_ROWS), limit=10, offset=2)
    assert tuple(result.rows()) == _LIMIT_ROWS[2:]


def test_limit_and_offset_combined():
    result = Limit(_child(_LIMIT_ROWS), limit=2, offset=1)
    assert tuple(result.rows()) == _LIMIT_ROWS[1:3]


def test_negative_offset_is_clamped_to_zero():
    """Confirmed against sqlite3: a negative `OFFSET` behaves exactly
    like `OFFSET 0`."""
    result = Limit(_child(_LIMIT_ROWS), limit=2, offset=-1)
    assert tuple(result.rows()) == _LIMIT_ROWS[:2]


def test_negative_offset_is_clamped_on_the_operator_itself():
    """The clamp is applied once, at construction (`__init__`), not
    merely an incidental consequence of how `rows()` happens to
    iterate - checked directly on the stored attribute rather than
    only through behaviour, so a future rewrite of `rows()` (e.g. via
    `itertools.islice`, which raises on a negative start) cannot
    silently reintroduce a negative offset."""
    result = Limit(_child(_LIMIT_ROWS), limit=2, offset=-5)
    assert result._offset == 0


def test_negative_limit_with_positive_offset_still_skips():
    """Confirmed against sqlite3: a negative `LIMIT` does not suppress
    `OFFSET` - only the truncation is skipped."""
    result = Limit(_child(_LIMIT_ROWS), limit=-1, offset=2)
    assert tuple(result.rows()) == _LIMIT_ROWS[2:]


def test_offset_past_the_end_yields_no_rows():
    result = Limit(_child(_LIMIT_ROWS), limit=5, offset=100)
    assert tuple(result.rows()) == ()


def test_limit_past_the_end_yields_only_what_the_child_has():
    result = Limit(_child(_LIMIT_ROWS), limit=100)
    assert tuple(result.rows()) == _LIMIT_ROWS


def test_offset_defaults_to_zero():
    result = Limit(_child(_LIMIT_ROWS), limit=2)
    assert tuple(result.rows()) == _LIMIT_ROWS[:2]


def test_limit_over_empty_child_yields_no_rows():
    result = Limit(_child(()), limit=3)
    assert tuple(result.rows()) == ()


def test_limit_schema_is_exactly_the_child_schema():
    result = Limit(_child(_LIMIT_ROWS), limit=2)
    assert result.schema is _SCHEMA


def test_limit_preserves_child_row_order():
    """`Limit` never reorders - the rows it yields are exactly the
    child's own prefix, in the child's own order."""
    rows = (("c.py", 1, None), ("a.py", 1, None), ("b.py", 1, None))
    result = Limit(_child(rows), limit=2)
    assert tuple(result.rows()) == rows[:2]


def test_limit_streams_rather_than_pulling_more_than_offset_plus_limit_rows():
    """The laziness criterion (issue #77's own acceptance criteria): a
    plain generator that stops pulling from `child` once it has
    produced `offset + limit` rows - never `list(child.rows())[offset:
    offset+limit]`. Built directly on `Scan` over a `_CountingSource`
    with 20 rows available (mirroring `test_sort_reads_child_rows_
    exactly_once`'s own pattern, one layer further down the tree, with
    no `ORDER BY`/`GROUP BY` so nothing between `Scan` and `Limit`
    consumes the child eagerly either)."""
    rows = tuple(("a.py", n, None) for n in range(20))
    source = _CountingSource(rows)
    result = Limit(Scan(source), limit=3)

    pulled = tuple(result.rows())

    assert pulled == rows[:3]
    assert source.pulled <= 3


def test_limit_offset_streams_rather_than_pulling_more_than_offset_plus_limit_rows():
    """Same laziness property, with a nonzero `OFFSET`: at most
    `offset + limit` rows pulled from the spy source, never all 20."""
    rows = tuple(("a.py", n, None) for n in range(20))
    source = _CountingSource(rows)
    result = Limit(Scan(source), limit=3, offset=5)

    pulled = tuple(result.rows())

    assert pulled == rows[5:8]
    assert source.pulled <= 8


def test_limit_zero_pulls_nothing_from_the_child():
    """`LIMIT 0` needs no row at all from `child` - not even one pulled
    and discarded."""
    rows = tuple(("a.py", n, None) for n in range(20))
    source = _CountingSource(rows)
    result = Limit(Scan(source), limit=0)

    assert tuple(result.rows()) == ()
    assert source.pulled == 0


def test_negative_limit_over_a_spy_source_still_pulls_every_row_eventually():
    """"No limit" is still lazy in the sense that matters (a plain
    generator, no upfront materialization via `list(...)`) but it is
    necessarily unbounded in how much of `child` it eventually pulls,
    since every row must be yielded - this is the one shape where
    pulling all of `child` is correct, not a laziness bug."""
    rows = tuple(("a.py", n, None) for n in range(5))
    source = _CountingSource(rows)
    result = Limit(Scan(source), limit=-1)

    assert tuple(result.rows()) == rows
    assert source.pulled == 5


# --- Distinct (issue #78) --------------------------------------------------
#
# `_docs/spec.md` §3's `Distinct` operator: `SELECT DISTINCT`. Sits
# directly above `Project` (`plan/planner.py`'s job to place it there),
# in the slot #77's own design reserved between `Project` and `Limit`.


def test_distinct_removes_exact_duplicate_rows():
    rows = (("a.py", 1, "e"), ("a.py", 1, "e"), ("b.py", 2, "e"))
    result = Distinct(_child(rows))

    assert tuple(result.rows()) == (("a.py", 1, "e"), ("b.py", 2, "e"))


def test_distinct_keeps_first_occurrence_order():
    """First-seen order, not sorted - the concrete determinism
    guarantee this operator commits to (spec §3's "Determinism and row
    order", `AGENTS.md`)."""
    rows = (("c.py", 1, "e"), ("a.py", 1, "e"), ("c.py", 1, "e"), ("b.py", 1, "e"))
    result = Distinct(_child(rows))

    assert tuple(result.rows()) == (("c.py", 1, "e"), ("a.py", 1, "e"), ("b.py", 1, "e"))


def test_distinct_with_no_duplicates_returns_every_row_unchanged():
    result = Distinct(_child(_ROWS))
    assert tuple(result.rows()) == _ROWS


def test_distinct_over_empty_child_yields_no_rows():
    result = Distinct(_child(()))
    assert tuple(result.rows()) == ()


def test_distinct_schema_is_exactly_the_child_schema():
    result = Distinct(_child(()))
    assert result.schema is _SCHEMA


def test_distinct_dedups_numeric_storage_classes_via_order_key():
    """`1` and `1.0` merge into one dedup group - the same
    storage-class-insensitive numeric equality `values.order_key`
    already gives `Aggregate`'s own grouped path, reused here."""
    rows = (("a.py", 1, "e"), ("a.py", 1.0, "e"), ("a.py", 2, "e"))
    result = Distinct(_child(rows))

    assert tuple(result.rows()) == (("a.py", 1, "e"), ("a.py", 2, "e"))


def test_distinct_nulls_are_equal_to_each_other():
    """Two `NULL`s in the same column position form one dedup group -
    spec §3's aggregate edge-case table ("DISTINCT over NULLs: NULLs
    are equal to each other"), which this issue is what makes
    reachable."""
    rows = (("a.py", 1, None), ("a.py", 1, None), ("a.py", 1, "e"))
    result = Distinct(_child(rows))

    assert tuple(result.rows()) == (("a.py", 1, None), ("a.py", 1, "e"))


def test_distinct_text_and_numeric_do_not_merge():
    """`'1'` (TEXT) and `1` (INTEGER) stay separate dedup groups -
    `author_email` is declared TEXT but unenforced at the row level
    (this file's own convention, see `test_sort_mixed_storage_class_
    numeric_then_text`), so it can carry a genuine mix here too."""
    rows = (("a.py", 1, "1"), ("a.py", 1, 1))
    result = Distinct(_child(rows))

    assert tuple(result.rows()) == (("a.py", 1, "1"), ("a.py", 1, 1))


def test_distinct_full_mixed_storage_class_and_null_dedup_end_to_end():
    """The exact worked example from this issue's own grooming,
    confirmed live against sqlite3: over `1, 1.0, '1', NULL, NULL, 1`
    in one column, `SELECT DISTINCT x` keeps exactly three rows - the
    first-seen integer `1` (absorbing the later `1.0` and the final
    repeated `1`), the text `'1'`, and one `NULL` (absorbing the
    second `NULL`) - in that first-seen order. NULL and mixed-storage-
    class dedup cannot be reached through `blame` (every blame column
    is non-NULL with one fixed storage class), so this case is
    unit-only, per this issue's own scope note."""
    rows = (
        ("a.py", 1, 1),
        ("a.py", 1, 1.0),
        ("a.py", 1, "1"),
        ("a.py", 1, None),
        ("a.py", 1, None),
        ("a.py", 1, 1),
    )
    result = Distinct(_child(rows))

    values_seen = [row[2] for row in result.rows()]

    assert values_seen == [1, "1", None]
    assert type(values_seen[0]) is int  # the first-seen representative, not the later 1.0


def test_distinct_which_duplicate_is_kept_is_unobservable():
    """Design note mirrored from the operator's own docstring: two rows
    sharing a dedup key are identical in every column by definition -
    `Distinct` groups by the whole row, so there is nothing else to
    assert here beyond "exactly one survives, with every column
    intact"."""
    rows = (("a.py", 1, "e"), ("a.py", 1, "e"), ("a.py", 1, "e"))
    result = Distinct(_child(rows))

    assert tuple(result.rows()) == (("a.py", 1, "e"),)


def test_distinct_repeated_runs_give_identical_order():
    """The same rows, deduplicated twice via two fresh `Distinct`
    instances over two fresh children, produce byte-identical order
    both times - mirroring `test_sort_repeated_runs_give_identical_
    order`'s own direct (non-oracle) determinism check."""
    rows = (("b.py", 1, "e"), ("a.py", 1, "e"), ("b.py", 1, "e"))

    first = list(Distinct(_child(rows)).rows())
    second = list(Distinct(_child(rows)).rows())

    assert first == second


def test_distinct_streams_rather_than_materializing_the_child():
    """The laziness criterion (this issue's own acceptance criteria):
    `Distinct` pulls one row at a time and yields a row the first
    time its key is new, without ever materializing `child.rows()` up
    front - mirroring `test_limit_streams_rather_than_pulling_more_
    than_offset_plus_limit_rows`'s own pattern, one operator over.
    Built directly on `Scan` over a `_CountingSource` so nothing
    between `Scan` and `Distinct` consumes the child eagerly; pulling
    only the first three distinct rows out of twenty available proves
    `Distinct` never pulled the rest."""
    rows = tuple(("a.py", n, None) for n in range(20))
    source = _CountingSource(rows)
    result = Distinct(Scan(source))

    first_three = list(itertools.islice(result.rows(), 3))

    assert first_three == list(rows[:3])
    assert source.pulled == 3
