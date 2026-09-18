"""Tests for historian.exec.operators: Scan, Filter, Project.

Issue #34 (spec §6 M2 item 8b). Unit-style per spec §4's test-
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
"""

from __future__ import annotations

import itertools
from collections.abc import Iterator, Sequence

from historian.exec.operators import Filter, Project, Scan
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
