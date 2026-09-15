"""Tests for historian.plan.planner: `BoundSelectStatement` -> operator tree.

Issue #13 (spec §6 M2 item 9). Unit-style, per `AGENTS.md`'s "the
planner ... is plain Python with no git and no subprocess imports" and
this issue's own acceptance criteria: every test below plans against a
hand-built `_FakeSource`, injected through `plan()`'s `tables`
parameter, and never exercises `tables.blame` or a git subprocess -
`historian.tables.blame` is imported nowhere in this file. `repo` is
passed through as an arbitrary `Path` that no fake factory ever reads.

Mirrors `tests/test_operators.py`'s own fixtures closely (`_SCHEMA`,
`_lit`, `_col`, `_bin`, `_POS`) rather than reusing them by import,
matching that file's own convention of a self-contained, independently
built schema per test module.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path

from historian.exec.operators import Filter, Project, Scan
from historian.plan.planner import TABLES, plan
from historian.schema import Column, ColumnType, Row, Schema
from historian.sql.ast import BinaryOp, Literal, Operator as Op
from historian.sql.binder import BoundColumnRef, BoundSelectItem, BoundSelectStatement
from historian.sql.lexer import Position
from historian.tables.blame import BlameScan

_POS = Position(line=1, column=1, offset=0)

#: `blame`-shaped but not `blame` - path TEXT, line_no INTEGER,
#: author_email TEXT (nullable) - matching `tests/test_operators.py`'s
#: own schema exactly, so a `BoundSelectStatement` built here plans
#: sensibly against it.
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


def _select_item(expr) -> BoundSelectItem:
    output_name = expr.name if isinstance(expr, BoundColumnRef) else None
    return BoundSelectItem(expr=expr, alias=None, output_name=output_name, position=_POS)


def _stmt(select_list, where=None, from_table="widgets") -> BoundSelectStatement:
    return BoundSelectStatement(
        select_list=tuple(select_list), from_table=from_table, where=where, position=_POS
    )


class _FakeSource:
    """A minimal fake satisfying `Scan`'s adapted shape - `schema`,
    `capabilities()`, `scan(pushed=())` - with no relation to
    `BlameScan` at all. Named "widgets" throughout this file (never
    "blame") so a test that accidentally fell through to the real
    default `TABLES` catalog would fail with a `KeyError` rather than
    silently working."""

    schema = _SCHEMA

    def __init__(self, rows: Sequence[Row]) -> None:
        self._rows = rows
        self.scan_calls: list[Sequence[object]] = []

    def capabilities(self) -> set[str]:
        return set()

    def scan(self, pushed: Sequence[object] = ()) -> Iterator[Row]:
        self.scan_calls.append(pushed)
        yield from self._rows


def _fake_tables(source: "_FakeSource") -> dict:
    """A `tables` catalog whose factory ignores the `Path` it is given
    and always returns the same pre-built `source` - so the returned
    tree's `Scan` wraps an object the test already holds a reference
    to, for shape and call assertions."""
    return {"widgets": lambda repo: source}


# --- tree shape --------------------------------------------------------


def test_plan_with_where_builds_project_filter_scan():
    """`Project(Filter(Scan(...), predicate), select_list)` - the exact
    tree acceptance criterion #1 names for a query with a WHERE
    clause."""
    source = _FakeSource([("a.py", 1, "ana@x.com")])
    predicate = _bin(Op.EQ, _col("path"), _lit("a.py"))
    stmt = _stmt([_select_item(_col("path"))], where=predicate)

    tree = plan(stmt, Path("/nonexistent"), tables=_fake_tables(source))

    assert isinstance(tree, Project)
    assert isinstance(tree._child, Filter)
    assert isinstance(tree._child._child, Scan)
    assert tree._child._predicate is predicate


def test_plan_without_where_builds_project_scan_with_no_filter():
    """`Project(Scan(...), select_list)` - no `Filter` node at all -
    for a query with no WHERE clause, per acceptance criterion #1."""
    source = _FakeSource([("a.py", 1, "ana@x.com")])
    stmt = _stmt([_select_item(_col("path"))], where=None)

    tree = plan(stmt, Path("/nonexistent"), tables=_fake_tables(source))

    assert isinstance(tree, Project)
    assert isinstance(tree._child, Scan)


def test_plan_result_is_a_single_operator_tree_with_no_new_node_types():
    """Every node in the tree is one of `exec/operators.py`'s own
    three classes - nothing from a separate plan-node hierarchy, per
    §3's "one plan representation, not two"."""
    source = _FakeSource([])
    predicate = _bin(Op.EQ, _col("path"), _lit("a.py"))
    stmt = _stmt([_select_item(_col("path"))], where=predicate)

    tree = plan(stmt, Path("/nonexistent"), tables=_fake_tables(source))

    assert type(tree) is Project
    assert type(tree._child) is Filter
    assert type(tree._child._child) is Scan


# --- Scan is constructed and iterated exactly as exec/operators.py does ----


def test_scan_is_always_iterated_with_no_pushed_predicates():
    """Acceptance criterion #3: `Scan` always calls
    `source.scan(pushed=())`, unconditionally - this issue negotiates
    nothing. Pulling the tree's rows must leave the fake source's
    `scan_calls` showing exactly one call with an empty `pushed`."""
    source = _FakeSource([("a.py", 1, "ana@x.com")])
    predicate = _bin(Op.EQ, _col("path"), _lit("a.py"))
    stmt = _stmt([_select_item(_col("path"))], where=predicate)

    tree = plan(stmt, Path("/nonexistent"), tables=_fake_tables(source))
    list(tree.rows())

    assert source.scan_calls == [()]


# --- rows actually flow through the tree correctly --------------------


def test_plan_with_where_filters_and_projects_rows():
    rows = [
        ("a.py", 1, "ana@x.com"),
        ("b.py", 2, "bo@x.com"),
    ]
    source = _FakeSource(rows)
    predicate = _bin(Op.EQ, _col("path"), _lit("a.py"))
    stmt = _stmt([_select_item(_col("path")), _select_item(_col("author_email"))], where=predicate)

    tree = plan(stmt, Path("/nonexistent"), tables=_fake_tables(source))

    assert list(tree.rows()) == [("a.py", "ana@x.com")]


def test_plan_without_where_projects_every_row():
    rows = [
        ("a.py", 1, "ana@x.com"),
        ("b.py", 2, "bo@x.com"),
    ]
    source = _FakeSource(rows)
    stmt = _stmt([_select_item(_col("path"))], where=None)

    tree = plan(stmt, Path("/nonexistent"), tables=_fake_tables(source))

    assert list(tree.rows()) == [("a.py",), ("b.py",)]


# --- the table -> scan-factory mapping is a parameter with a default ------


def test_tables_parameter_defaults_to_blame_scan():
    """Mirrors `sql/binder.py`'s `bind(stmt, catalog=TABLES)`
    precedent exactly (acceptance criterion #2): `plan`'s `tables`
    parameter defaults to the module's own `TABLES`, mapping `"blame"`
    to the real `BlameScan` factory - checked here by identity, with
    no repository and no git subprocess run, since nothing calls
    `.scan()`."""
    assert TABLES == {"blame": BlameScan}


def test_plan_uses_default_tables_catalog_when_none_given():
    """Calling `plan()` with no `tables` argument at all resolves
    `"blame"` against the module-level default and constructs a real
    `BlameScan` bound to the given repo path - proven without ever
    calling `.rows()`, so no git subprocess runs."""
    stmt = _stmt([_select_item(_col("path"))], where=None, from_table="blame")
    repo = Path("/nonexistent/for/this/test")

    tree = plan(stmt, repo)

    assert isinstance(tree, Project)
    assert isinstance(tree._child, Scan)
    assert isinstance(tree._child._source, BlameScan)
    assert tree._child._source._repo == repo
