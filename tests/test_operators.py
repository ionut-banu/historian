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

import pytest

from historian.exec.operators import Filter, Project, Scan
from historian.schema import Column, ColumnType, Row, Schema
from historian.sql.ast import BinaryOp, Literal, Operator as Op
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


# --- Documented Value/Bool3 boundary (#38, out of scope here) -------------


def test_operators_module_documents_the_value_bool3_boundary():
    """This issue does not implement #38 (see its own Out of scope
    section) - it must document the boundary instead, so whoever hits
    it finds an explanation rather than a mystery. Checked here as a
    real assertion, not a comment nobody enforces."""
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
