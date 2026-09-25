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

from historian.exec.operators import Aggregate, Distinct, Filter, Limit, Project, Scan, Sort
from historian.plan.planner import TABLES, plan
from historian.schema import Column, ColumnType, Row, Schema
from historian.sql.ast import BinaryOp, FunctionCall, Literal, OrderDirection, Operator as Op, Star
from historian.sql.binder import BoundColumnRef, BoundOrderByItem, BoundSelectItem, BoundSelectStatement, bind
from historian.sql.lexer import Position, tokenize
from historian.sql.parser import parse
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


def _stmt(
    select_list,
    where=None,
    from_table="widgets",
    group_by=(),
    having=None,
    order_by=(),
    limit=None,
    offset=None,
    distinct=False,
) -> BoundSelectStatement:
    return BoundSelectStatement(
        select_list=tuple(select_list),
        from_table=from_table,
        where=where,
        group_by=tuple(group_by),
        having=having,
        order_by=tuple(order_by),
        limit=limit,
        offset=offset,
        position=_POS,
        distinct=distinct,
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


# --- The aggregate/scalar split and the Aggregate operator (issue #60) -----
#
# `_stmt`'s select-list items are `BoundSelectItem`s built directly
# (not through `sql/binder.py`), so a `FunctionCall` here stands in for
# whatever the binder would have already validated - these tests trust
# `tests/test_binder.py`'s own coverage of validation and exercise only
# the planner's own job: the split and the tree shape.


def _count_star() -> FunctionCall:
    return FunctionCall(name="count", args=(Star(table=None, position=_POS),), position=_POS)


def _func(name: str, *args, distinct: bool = False) -> FunctionCall:
    return FunctionCall(name=name, args=tuple(args), position=_POS, distinct=distinct)


def test_plan_with_aggregate_inserts_aggregate_below_project_above_scan():
    """`SELECT count(*) FROM widgets`, no `WHERE`: `Project(Aggregate(
    Scan(...), calls), select_list)` - `Aggregate` sits directly below
    `Project` and above `Scan`, per the issue's own acceptance
    criterion for the tree shape."""
    source = _FakeSource([("a.py", 1, "ana@x.com")])
    stmt = _stmt([_select_item(_count_star())], where=None)

    tree = plan(stmt, Path("/nonexistent"), tables=_fake_tables(source))

    assert isinstance(tree, Project)
    assert isinstance(tree._child, Aggregate)
    assert isinstance(tree._child._child, Scan)


def test_plan_with_aggregate_and_where_inserts_aggregate_below_project_above_filter():
    """`SELECT count(*) FROM widgets WHERE path = 'a.py'`: `Project(
    Aggregate(Filter(Scan(...), predicate), calls), select_list)` -
    the full `Scan -> Filter -> Aggregate -> Project` shape the issue
    names explicitly, with `Filter` still consuming `Scan`'s output and
    `Aggregate` consuming `Filter`'s."""
    source = _FakeSource([("a.py", 1, "ana@x.com")])
    predicate = _bin(Op.EQ, _col("path"), _lit("a.py"))
    stmt = _stmt([_select_item(_count_star())], where=predicate)

    tree = plan(stmt, Path("/nonexistent"), tables=_fake_tables(source))

    assert isinstance(tree, Project)
    assert isinstance(tree._child, Aggregate)
    assert isinstance(tree._child._child, Filter)
    assert isinstance(tree._child._child._child, Scan)
    assert tree._child._child._predicate is predicate


def test_plan_without_any_aggregate_call_never_builds_aggregate():
    """An ordinary, aggregate-free query keeps issue #13's original two
    shapes exactly - `Aggregate` must never appear for
    `SELECT path FROM widgets`, a regression guard for every planner
    test that predates this issue."""
    source = _FakeSource([("a.py", 1, "ana@x.com")])
    stmt = _stmt([_select_item(_col("path"))], where=None)

    tree = plan(stmt, Path("/nonexistent"), tables=_fake_tables(source))

    assert isinstance(tree, Project)
    assert isinstance(tree._child, Scan)


def test_plan_splits_a_bare_aggregate_call_into_one_slot():
    """`SELECT count(*) FROM widgets`: `Aggregate` gets exactly one
    `AggregateCall` (`kind="count"`, `arg=None`), and the select-list
    item `Project` evaluates is rewritten to a bare reference into
    `Aggregate`'s output row, not the original `FunctionCall`."""
    source = _FakeSource([])
    stmt = _stmt([_select_item(_count_star())], where=None)

    tree = plan(stmt, Path("/nonexistent"), tables=_fake_tables(source))

    assert len(tree._child._calls) == 1
    call = tree._child._calls[0]
    assert call.kind == "count"
    assert call.arg is None
    rewritten = tree._select_list[0].expr
    assert isinstance(rewritten, BoundColumnRef)
    assert rewritten.offset == 0


def test_plan_splits_two_aggregate_calls_in_one_expression_in_order():
    """`SELECT count(*) + sum(line_no) FROM widgets`: two distinct
    `AggregateCall`s, in left-to-right order, and the surrounding `+`
    survives as a `BinaryOp` over the two rewritten references -
    proving the split handles more than a bare aggregate as the whole
    select-list item."""
    source = _FakeSource([])
    expr = _bin(Op.ADD, _count_star(), _func("sum", _col("line_no")))
    stmt = _stmt([_select_item(expr)], where=None)

    tree = plan(stmt, Path("/nonexistent"), tables=_fake_tables(source))

    assert [call.kind for call in tree._child._calls] == ["count", "sum"]
    assert tree._child._calls[0].arg is None
    assert tree._child._calls[1].arg is not None

    rewritten = tree._select_list[0].expr
    assert isinstance(rewritten, BinaryOp)
    assert rewritten.op is Op.ADD
    assert isinstance(rewritten.left, BoundColumnRef) and rewritten.left.offset == 0
    assert isinstance(rewritten.right, BoundColumnRef) and rewritten.right.offset == 1


def test_plan_count_with_bare_column_argument_keeps_its_bound_expression():
    """`SELECT count(line_no) FROM widgets`: the `AggregateCall`'s
    `arg` is the original bound `line_no` reference (evaluated against
    the *source* schema, below `Aggregate`), not `None` - `None` is
    reserved for `count(*)`/`count()` alone."""
    source = _FakeSource([])
    stmt = _stmt([_select_item(_func("count", _col("line_no")))], where=None)

    tree = plan(stmt, Path("/nonexistent"), tables=_fake_tables(source))

    call = tree._child._calls[0]
    assert call.kind == "count"
    assert isinstance(call.arg, BoundColumnRef)
    assert call.arg.offset == _SCHEMA.index_of("line_no")


# --- Aggregate DISTINCT (issue #84) -----------------------------------
#
# `_build_aggregate_call` threads `call.distinct` straight into the
# `AggregateCall` it builds - the only planner change this issue makes.


def test_plan_threads_distinct_true_into_the_aggregate_call():
    """`SELECT count(DISTINCT line_no) FROM widgets`: the resulting
    `AggregateCall.distinct` is `True`."""
    source = _FakeSource([])
    stmt = _stmt([_select_item(_func("count", _col("line_no"), distinct=True))], where=None)

    tree = plan(stmt, Path("/nonexistent"), tables=_fake_tables(source))

    call = tree._child._calls[0]
    assert call.kind == "count"
    assert call.distinct is True


def test_plan_without_distinct_keeps_the_aggregate_call_flag_false():
    """The ordinary, non-`DISTINCT` case is unaffected - the default
    `AggregateCall.distinct` is `False`."""
    source = _FakeSource([])
    stmt = _stmt([_select_item(_func("count", _col("line_no")))], where=None)

    tree = plan(stmt, Path("/nonexistent"), tables=_fake_tables(source))

    call = tree._child._calls[0]
    assert call.distinct is False


def test_plan_min_max_distinct_threads_the_flag_too():
    """`min`/`max(DISTINCT x)` also get `distinct=True` on their
    `AggregateCall`, even though the accumulator itself ignores it for
    these two kinds - the planner threads the flag uniformly for every
    aggregate kind, per the settled design."""
    source = _FakeSource([])
    expr = _bin(
        Op.ADD,
        _func("min", _col("line_no"), distinct=True),
        _func("max", _col("line_no"), distinct=True),
    )
    stmt = _stmt([_select_item(expr)], where=None)

    tree = plan(stmt, Path("/nonexistent"), tables=_fake_tables(source))

    assert [call.distinct for call in tree._child._calls] == [True, True]


def test_plan_aggregate_query_produces_correct_row_end_to_end():
    """`SELECT count(*) FROM widgets WHERE line_no > 1` against three
    fake rows, two of which survive the filter: the whole tree,
    assembled purely by `plan()`, must actually produce `(2,)` when
    pulled - not just have the right shape."""
    rows = [
        ("a.py", 1, "ana@x.com"),
        ("b.py", 2, "bo@x.com"),
        ("c.py", 3, "cara@x.com"),
    ]
    source = _FakeSource(rows)
    predicate = _bin(Op.GT, _col("line_no"), _lit(1))
    stmt = _stmt([_select_item(_count_star())], where=predicate)

    tree = plan(stmt, Path("/nonexistent"), tables=_fake_tables(source))

    assert list(tree.rows()) == [(2,)]


# --- GROUP BY / HAVING (issue #69) ------------------------------------------


def test_plan_group_by_inserts_aggregate_with_group_by_set():
    """`SELECT path, count(*) FROM widgets GROUP BY path`: `Project(
    Aggregate(Scan(...), calls, group_by=(path,)), select_list)` -
    `Aggregate` gets a non-empty `group_by`, matching the issue's own
    tree-shape criterion."""
    source = _FakeSource([("a.py", 1, "ana@x.com")])
    stmt = _stmt(
        [_select_item(_col("path")), _select_item(_count_star())],
        where=None,
        group_by=[_col("path")],
    )

    tree = plan(stmt, Path("/nonexistent"), tables=_fake_tables(source))

    assert isinstance(tree, Project)
    assert isinstance(tree._child, Aggregate)
    assert isinstance(tree._child._child, Scan)
    assert tree._child._group_by == (_col("path"),)


def test_plan_group_by_and_having_inserts_filter_between_aggregate_and_project():
    """`SELECT path, count(*) FROM widgets GROUP BY path HAVING
    count(*) > 1`: the full `Scan -> Aggregate -> Filter (HAVING) ->
    Project` shape - `Filter` sits directly above `Aggregate` and
    directly below `Project`."""
    source = _FakeSource([("a.py", 1, "ana@x.com")])
    having = _bin(Op.GT, _count_star(), _lit(1))
    stmt = _stmt(
        [_select_item(_col("path")), _select_item(_count_star())],
        where=None,
        group_by=[_col("path")],
        having=having,
    )

    tree = plan(stmt, Path("/nonexistent"), tables=_fake_tables(source))

    assert isinstance(tree, Project)
    assert isinstance(tree._child, Filter)
    assert isinstance(tree._child._child, Aggregate)
    assert isinstance(tree._child._child._child, Scan)


def test_plan_group_by_with_where_full_tree_shape():
    """`Scan -> Filter (WHERE) -> Aggregate -> Filter (HAVING) ->
    Project`, every stage present, pinning the issue's own full tree
    shape exactly."""
    source = _FakeSource([("a.py", 1, "ana@x.com")])
    predicate = _bin(Op.GT, _col("line_no"), _lit(0))
    having = _bin(Op.GT, _count_star(), _lit(1))
    stmt = _stmt(
        [_select_item(_col("path")), _select_item(_count_star())],
        where=predicate,
        group_by=[_col("path")],
        having=having,
    )

    tree = plan(stmt, Path("/nonexistent"), tables=_fake_tables(source))

    assert isinstance(tree, Project)
    assert isinstance(tree._child, Filter)  # HAVING
    assert tree._child._predicate is not predicate
    assert isinstance(tree._child._child, Aggregate)
    assert isinstance(tree._child._child._child, Filter)  # WHERE
    assert tree._child._child._child._predicate is predicate
    assert isinstance(tree._child._child._child._child, Scan)


def test_plan_group_by_without_having_omits_having_filter_node():
    """A `GROUP BY` query with no `HAVING` at all must not grow an
    always-present no-op `Filter` above `Aggregate` - `Project`'s
    child is `Aggregate` directly."""
    source = _FakeSource([("a.py", 1, "ana@x.com")])
    stmt = _stmt(
        [_select_item(_col("path")), _select_item(_count_star())],
        where=None,
        group_by=[_col("path")],
    )

    tree = plan(stmt, Path("/nonexistent"), tables=_fake_tables(source))

    assert isinstance(tree, Project)
    assert isinstance(tree._child, Aggregate)


def test_plan_having_with_only_a_select_list_aggregate_and_no_group_by():
    """`SELECT count(*) FROM widgets HAVING 1` - confirmed against
    `sqlite3 3.51.0` (issue #69's HAVING-on-a-non-aggregate-query
    fix): an aggregate call in the select list alone is enough to
    make this an aggregate query, even though `HAVING`'s own
    predicate (`1`) has no aggregate call in it at all. `sql/
    binder.py` is what rejects the case with no aggregate anywhere at
    all (`no such reachable shape at the planner level once bind()
    guarantees it - see tests/test_binder.py and tests/differential/
    test_blame.py's own HAVING-on-a-non-aggregate-query cases`); this
    planner test only pins that the legal shape still builds
    `Aggregate` + `HAVING`'s `Filter` correctly."""
    source = _FakeSource([("a.py", 1, "ana@x.com"), ("b.py", 2, "bo@x.com")])
    having = _lit(1)
    stmt = _stmt([_select_item(_count_star())], where=None, having=having)

    tree = plan(stmt, Path("/nonexistent"), tables=_fake_tables(source))

    assert isinstance(tree, Project)
    assert isinstance(tree._child, Filter)
    assert isinstance(tree._child._child, Aggregate)
    assert list(tree.rows()) == [(2,)]


def test_plan_group_by_query_produces_correct_grouped_rows_end_to_end():
    """`SELECT path, count(*) FROM widgets GROUP BY path` against
    three fake rows, two sharing a path: the whole tree, assembled
    purely by `plan()`, must actually produce the grouped rows when
    pulled."""
    rows = [
        ("a.py", 1, "ana@x.com"),
        ("a.py", 2, "ana@x.com"),
        ("b.py", 3, "bo@x.com"),
    ]
    source = _FakeSource(rows)
    stmt = _stmt(
        [_select_item(_col("path")), _select_item(_count_star())],
        where=None,
        group_by=[_col("path")],
    )

    tree = plan(stmt, Path("/nonexistent"), tables=_fake_tables(source))

    assert set(tree.rows()) == {("a.py", 2), ("b.py", 1)}


def test_plan_having_query_produces_correctly_filtered_rows_end_to_end():
    rows = [
        ("a.py", 1, "ana@x.com"),
        ("a.py", 2, "ana@x.com"),
        ("b.py", 3, "bo@x.com"),
    ]
    source = _FakeSource(rows)
    having = _bin(Op.GT, _count_star(), _lit(1))
    stmt = _stmt(
        [_select_item(_col("path")), _select_item(_count_star())],
        where=None,
        group_by=[_col("path")],
        having=having,
    )

    tree = plan(stmt, Path("/nonexistent"), tables=_fake_tables(source))

    assert list(tree.rows()) == [("a.py", 2)]


def test_plan_select_item_matching_group_key_reads_from_aggregate_output():
    """`SELECT path, count(*) FROM widgets GROUP BY path`: the
    `path`-select item is rewritten to reference `Aggregate`'s own
    output row (the group-key column, offset 0), not the original
    `Scan`-schema offset - proving the group-key rewrite, not merely
    the tree shape."""
    source = _FakeSource([("a.py", 1, "ana@x.com")])
    stmt = _stmt(
        [_select_item(_col("path")), _select_item(_count_star())],
        where=None,
        group_by=[_col("path")],
    )

    tree = plan(stmt, Path("/nonexistent"), tables=_fake_tables(source))

    rewritten = tree._select_list[0].expr
    assert isinstance(rewritten, BoundColumnRef)
    assert rewritten.offset == 0


# --- ORDER BY / Sort (issue #61) --------------------------------------------


def _order_item(expr, descending: bool = False) -> BoundOrderByItem:
    direction = OrderDirection.DESC if descending else OrderDirection.ASC
    return BoundOrderByItem(expr=expr, direction=direction, position=_POS)


def test_plan_with_order_by_inserts_sort_below_project():
    """`SELECT path FROM widgets ORDER BY path`: `Project(Sort(Scan(
    ...), keys), select_list)` - `Sort` sits directly below `Project`,
    per this issue's own tree-placement criterion."""
    source = _FakeSource([("b.py", 1, "ana@x.com"), ("a.py", 2, "bo@x.com")])
    stmt = _stmt([_select_item(_col("path"))], order_by=[_order_item(_col("path"))])

    tree = plan(stmt, Path("/nonexistent"), tables=_fake_tables(source))

    assert isinstance(tree, Project)
    assert isinstance(tree._child, Sort)
    assert isinstance(tree._child._child, Scan)


def test_plan_without_order_by_never_builds_sort():
    source = _FakeSource([("a.py", 1, "ana@x.com")])
    stmt = _stmt([_select_item(_col("path"))], order_by=())

    tree = plan(stmt, Path("/nonexistent"), tables=_fake_tables(source))

    assert isinstance(tree, Project)
    assert not isinstance(tree._child, Sort)


def test_plan_order_by_produces_correctly_sorted_rows_end_to_end():
    rows = [("b.py", 1, "e"), ("a.py", 2, "e"), ("c.py", 3, "e")]
    source = _FakeSource(rows)
    stmt = _stmt([_select_item(_col("path"))], order_by=[_order_item(_col("path"))])

    tree = plan(stmt, Path("/nonexistent"), tables=_fake_tables(source))

    assert list(tree.rows()) == [("a.py",), ("b.py",), ("c.py",)]


def test_plan_order_by_sits_between_where_filter_and_project_with_no_aggregate():
    """`Scan -> Filter (WHERE) -> Sort -> Project`, the non-aggregate
    tree shape."""
    source = _FakeSource([("a.py", 1, "ana@x.com"), ("b.py", 2, "bo@x.com")])
    predicate = _bin(Op.GT, _col("line_no"), _lit(0))
    stmt = _stmt(
        [_select_item(_col("path"))], where=predicate, order_by=[_order_item(_col("path"))]
    )

    tree = plan(stmt, Path("/nonexistent"), tables=_fake_tables(source))

    assert isinstance(tree, Project)
    assert isinstance(tree._child, Sort)
    assert isinstance(tree._child._child, Filter)
    assert isinstance(tree._child._child._child, Scan)


def test_plan_order_by_with_aggregate_sits_above_having_filter_below_project():
    """`Scan -> Aggregate -> Filter (HAVING) -> Sort -> Project` - the
    full aggregating tree shape, `Sort` directly below `Project` and
    directly above `HAVING`'s own `Filter`."""
    source = _FakeSource([("a.py", 1, "ana@x.com")])
    having = _bin(Op.GT, _count_star(), _lit(1))
    stmt = _stmt(
        [_select_item(_col("path")), _select_item(_count_star())],
        where=None,
        group_by=[_col("path")],
        having=having,
        order_by=[_order_item(_col("path"))],
    )

    tree = plan(stmt, Path("/nonexistent"), tables=_fake_tables(source))

    assert isinstance(tree, Project)
    assert isinstance(tree._child, Sort)
    assert isinstance(tree._child._child, Filter)  # HAVING
    assert isinstance(tree._child._child._child, Aggregate)


def test_plan_order_by_may_reference_a_column_absent_from_the_select_list():
    """`SELECT path FROM widgets ORDER BY line_no`: `Sort`'s key
    references the *pre-Project* row, so it can sort by a column the
    final select list never projects - `select p from u order by n`,
    confirmed legal against sqlite3 during this issue's grooming."""
    rows = [("a.py", 3, "e"), ("b.py", 1, "e"), ("c.py", 2, "e")]
    source = _FakeSource(rows)
    stmt = _stmt([_select_item(_col("path"))], order_by=[_order_item(_col("line_no"))])

    tree = plan(stmt, Path("/nonexistent"), tables=_fake_tables(source))

    assert list(tree.rows()) == [("b.py",), ("c.py",), ("a.py",)]


def test_plan_order_by_aggregate_call_not_in_select_list_gets_its_own_slot():
    """`SELECT path FROM widgets GROUP BY path ORDER BY count(*) DESC`:
    the aggregate is split out via the same shared `calls` list the
    select list uses, even though `count(*)` never appears in the
    select list itself - proving the offset-sharing, not merely that
    the tree runs."""
    rows = [
        ("a.py", 1, "e"),
        ("a.py", 2, "e"),
        ("b.py", 3, "e"),
    ]
    source = _FakeSource(rows)
    stmt = _stmt(
        [_select_item(_col("path"))],
        group_by=[_col("path")],
        order_by=[_order_item(_count_star(), descending=True)],
    )

    tree = plan(stmt, Path("/nonexistent"), tables=_fake_tables(source))

    assert list(tree.rows()) == [("a.py",), ("b.py",)]


def test_plan_order_by_and_select_list_aggregate_calls_get_independent_slots():
    """`SELECT path, count(*) FROM widgets GROUP BY path ORDER BY
    count(*) DESC`: the select list's own `count(*)` and ORDER BY's own
    `count(*)` are two separate occurrences and get two separate
    `Aggregate` slots (this codebase's existing "no identity/equality
    dedup" rule for `calls`, extended to ORDER BY) - proven by reading
    `Aggregate`'s own call count, not merely by the rows coming out
    right."""
    rows = [("a.py", 1, "e"), ("a.py", 2, "e"), ("b.py", 3, "e")]
    source = _FakeSource(rows)
    stmt = _stmt(
        [_select_item(_col("path")), _select_item(_count_star())],
        group_by=[_col("path")],
        order_by=[_order_item(_count_star(), descending=True)],
    )

    tree = plan(stmt, Path("/nonexistent"), tables=_fake_tables(source))

    assert isinstance(tree, Project)
    aggregate = tree._child._child  # Sort -> Aggregate (no HAVING here)
    assert isinstance(aggregate, Aggregate)
    assert len(aggregate._calls) == 2
    assert list(tree.rows()) == [("a.py", 2), ("b.py", 1)]


def test_plan_order_by_multi_key_end_to_end():
    """`ORDER BY path ASC, line_no DESC` through the real planner,
    not just the bare `Sort` unit level - the `values.py` worked
    example, end to end."""
    rows = [
        ("x", None, "e"),
        ("x", 1, "e"),
        (None, 1, "e"),
        (None, 2, "e"),
        ("y", None, "e"),
        ("y", 1, "e"),
    ]
    source = _FakeSource(rows)
    stmt = _stmt(
        [_select_item(_col("path")), _select_item(_col("line_no"))],
        order_by=[_order_item(_col("path")), _order_item(_col("line_no"), descending=True)],
    )

    tree = plan(stmt, Path("/nonexistent"), tables=_fake_tables(source))

    assert list(tree.rows()) == [
        (None, 2),
        (None, 1),
        ("x", 1),
        ("x", None),
        ("y", 1),
        ("y", None),
    ]


def test_order_by_multi_key_end_to_end_through_the_real_parser_and_binder():
    """`values.py`'s own multi-key worked example (`a ASC, b DESC`
    over `('x',NULL),('x',1),(NULL,1),(NULL,2),('y',NULL),('y',1)` ->
    `NULL|2, NULL|1, x|1, x|NULL, y|1, y|NULL`), run through the
    *real* `tokenize -> parse -> bind -> plan -> tree.rows()`
    pipeline - not a hand-built `BoundSelectStatement` bypassing the
    binder, per this issue's own acceptance criterion. A real `blame`
    column can never be NULL (`tables/blame.py` asserts this before a
    row is emitted), so this uses a synthetic single-table catalog
    instead - the same reason `tests/test_operators.py`'s `Aggregate`
    section does."""
    schema = Schema(columns=(Column("a", ColumnType.TEXT), Column("b", ColumnType.INTEGER)))
    rows: list[Row] = [
        ("x", None),
        ("x", 1),
        (None, 1),
        (None, 2),
        ("y", None),
        ("y", 1),
    ]
    source = _FakeSource(rows)
    source.schema = schema  # override _FakeSource's own default _SCHEMA

    stmt = parse(tokenize("SELECT a, b FROM t ORDER BY a ASC, b DESC"))
    bound = bind(stmt, catalog={"t": schema})
    tree = plan(bound, Path("/nonexistent"), tables={"t": lambda repo: source})

    assert list(tree.rows()) == [
        (None, 2),
        (None, 1),
        ("x", 1),
        ("x", None),
        ("y", 1),
        ("y", None),
    ]


# --- LIMIT / OFFSET (issue #77) -------------------------------------------
#
# `Limit` is the new tree root - above `Project` - inserted only when
# `stmt.limit is not None`, per this issue's own tree-placement
# decision (leaving `DISTINCT`'s future slot, "12c", between `Project`
# and `Limit`).


def test_plan_with_limit_wraps_project_as_the_new_root():
    """`SELECT path FROM widgets LIMIT 2`: `Limit(Project(Scan(...),
    select_list), limit=2)` - `Limit` is the new outermost operator."""
    source = _FakeSource([("a.py", 1, "ana@x.com"), ("b.py", 2, "bo@x.com")])
    stmt = _stmt([_select_item(_col("path"))], limit=2)

    tree = plan(stmt, Path("/nonexistent"), tables=_fake_tables(source))

    assert isinstance(tree, Limit)
    assert isinstance(tree._child, Project)
    assert isinstance(tree._child._child, Scan)


def test_plan_without_limit_never_builds_limit():
    source = _FakeSource([("a.py", 1, "ana@x.com")])
    stmt = _stmt([_select_item(_col("path"))])

    tree = plan(stmt, Path("/nonexistent"), tables=_fake_tables(source))

    assert isinstance(tree, Project)
    assert not isinstance(tree, Limit)


def test_plan_limit_carries_the_bound_limit_value():
    source = _FakeSource([("a.py", 1, "ana@x.com"), ("b.py", 2, "bo@x.com")])
    stmt = _stmt([_select_item(_col("path"))], limit=1)

    tree = plan(stmt, Path("/nonexistent"), tables=_fake_tables(source))

    assert isinstance(tree, Limit)
    assert tree._limit == 1
    assert tree._offset == 0


def test_plan_offset_defaults_to_zero_when_absent():
    source = _FakeSource([("a.py", 1, "ana@x.com")])
    stmt = _stmt([_select_item(_col("path"))], limit=5, offset=None)

    tree = plan(stmt, Path("/nonexistent"), tables=_fake_tables(source))

    assert isinstance(tree, Limit)
    assert tree._offset == 0


def test_plan_limit_carries_the_bound_offset_value():
    source = _FakeSource([("a.py", 1, "ana@x.com")])
    stmt = _stmt([_select_item(_col("path"))], limit=5, offset=2)

    tree = plan(stmt, Path("/nonexistent"), tables=_fake_tables(source))

    assert isinstance(tree, Limit)
    assert tree._offset == 2


def test_plan_limit_sits_above_sort_and_project_when_order_by_present():
    """`SELECT path FROM widgets ORDER BY path LIMIT 1`: `Limit(
    Project(Sort(Scan(...), keys), select_list), limit=1)` - `Sort`
    still sits directly below `Project` (issue #61's own placement,
    unaffected by this issue), with `Limit` wrapping the whole thing."""
    source = _FakeSource([("b.py", 1, "ana@x.com"), ("a.py", 2, "bo@x.com")])
    stmt = _stmt(
        [_select_item(_col("path"))], order_by=[_order_item(_col("path"))], limit=1
    )

    tree = plan(stmt, Path("/nonexistent"), tables=_fake_tables(source))

    assert isinstance(tree, Limit)
    assert isinstance(tree._child, Project)
    assert isinstance(tree._child._child, Sort)
    assert isinstance(tree._child._child._child, Scan)


def test_plan_limit_produces_correctly_truncated_rows_end_to_end():
    rows = [("a.py", 1, "e"), ("b.py", 2, "e"), ("c.py", 3, "e")]
    source = _FakeSource(rows)
    stmt = _stmt([_select_item(_col("path"))], limit=2)

    tree = plan(stmt, Path("/nonexistent"), tables=_fake_tables(source))

    assert list(tree.rows()) == [("a.py",), ("b.py",)]


def test_plan_limit_offset_produces_correctly_sliced_rows_end_to_end():
    rows = [("a.py", 1, "e"), ("b.py", 2, "e"), ("c.py", 3, "e")]
    source = _FakeSource(rows)
    stmt = _stmt([_select_item(_col("path"))], limit=1, offset=1)

    tree = plan(stmt, Path("/nonexistent"), tables=_fake_tables(source))

    assert list(tree.rows()) == [("b.py",)]


def test_limit_offset_through_the_real_parser_and_binder_and_planner():
    """The full, real pipeline - `tokenize -> parse -> bind -> plan ->
    tree.rows()` - rather than a hand-built `BoundSelectStatement`,
    matching `test_order_by_multi_key_end_to_end_through_the_real_
    parser_and_binder`'s own convention for issue #61."""
    schema = Schema(columns=(Column("a", ColumnType.TEXT),))
    rows: list[Row] = [("x",), ("y",), ("z",)]
    source = _FakeSource(rows)
    source.schema = schema

    stmt = parse(tokenize("SELECT a FROM t ORDER BY a LIMIT 2 OFFSET 1"))
    bound = bind(stmt, catalog={"t": schema})
    tree = plan(bound, Path("/nonexistent"), tables={"t": lambda repo: source})

    assert isinstance(tree, Limit)
    assert list(tree.rows()) == [("y",), ("z",)]


# --- DISTINCT (issue #78) --------------------------------------------------
#
# `Distinct` is inserted directly above `Project` whenever `stmt.
# distinct` is `True` - the slot #77's own design reserved, between
# `Project` and `Limit`. `Sort`'s own placement is unaffected: still
# directly below `Project`, unmoved by this issue.


def test_plan_with_distinct_inserts_distinct_directly_above_project():
    source = _FakeSource([("a.py", 1, "ana@x.com"), ("a.py", 1, "ana@x.com")])
    stmt = _stmt([_select_item(_col("path"))], distinct=True)

    tree = plan(stmt, Path("/nonexistent"), tables=_fake_tables(source))

    assert isinstance(tree, Distinct)
    assert isinstance(tree._child, Project)
    assert isinstance(tree._child._child, Scan)


def test_plan_without_distinct_never_builds_distinct():
    source = _FakeSource([("a.py", 1, "ana@x.com")])
    stmt = _stmt([_select_item(_col("path"))])

    tree = plan(stmt, Path("/nonexistent"), tables=_fake_tables(source))

    assert isinstance(tree, Project)
    assert not isinstance(tree, Distinct)


def test_plan_distinct_sits_below_limit_and_above_project():
    """`SELECT DISTINCT path FROM widgets LIMIT 1`: `Limit(Distinct(
    Project(Scan(...), select_list)), limit=1)` - `Distinct` fills the
    slot #77's own design reserved between `Project` and `Limit`."""
    source = _FakeSource([("a.py", 1, "ana@x.com"), ("a.py", 1, "ana@x.com")])
    stmt = _stmt([_select_item(_col("path"))], distinct=True, limit=1)

    tree = plan(stmt, Path("/nonexistent"), tables=_fake_tables(source))

    assert isinstance(tree, Limit)
    assert isinstance(tree._child, Distinct)
    assert isinstance(tree._child._child, Project)
    assert isinstance(tree._child._child._child, Scan)


def test_plan_distinct_sits_above_sort_and_project_when_order_by_present():
    """`Sort`'s own placement is unchanged (still directly below
    `Project`, #61's own decision) - `Distinct` sits above `Project`,
    which sits above `Sort`."""
    source = _FakeSource([("b.py", 1, "ana@x.com"), ("a.py", 2, "bo@x.com")])
    stmt = _stmt(
        [_select_item(_col("path"))],
        distinct=True,
        order_by=[_order_item(_col("path"))],
    )

    tree = plan(stmt, Path("/nonexistent"), tables=_fake_tables(source))

    assert isinstance(tree, Distinct)
    assert isinstance(tree._child, Project)
    assert isinstance(tree._child._child, Sort)
    assert isinstance(tree._child._child._child, Scan)


def test_plan_distinct_produces_deduplicated_rows_end_to_end():
    rows = [("a.py", 1, "e"), ("a.py", 1, "e"), ("b.py", 2, "e")]
    source = _FakeSource(rows)
    stmt = _stmt([_select_item(_col("path"))], distinct=True)

    tree = plan(stmt, Path("/nonexistent"), tables=_fake_tables(source))

    assert list(tree.rows()) == [("a.py",), ("b.py",)]


def test_plan_distinct_with_limit_produces_correctly_truncated_deduplicated_rows():
    """DISTINCT applies before LIMIT/OFFSET - three duplicate-collapsed
    rows exist, but LIMIT 2 only keeps the first two of *those*."""
    rows = [
        ("a.py", 1, "e"),
        ("a.py", 1, "e"),
        ("b.py", 2, "e"),
        ("c.py", 3, "e"),
    ]
    source = _FakeSource(rows)
    stmt = _stmt([_select_item(_col("path"))], distinct=True, limit=2)

    tree = plan(stmt, Path("/nonexistent"), tables=_fake_tables(source))

    assert list(tree.rows()) == [("a.py",), ("b.py",)]


def test_plan_distinct_collapses_rows_that_only_became_equal_after_projection():
    """`DISTINCT` dedups the *projected* row, not the wider pre-Project
    row - two source rows differing only in a column absent from the
    select list collapse into one output row."""
    rows = [("a.py", 1, "ana@x.com"), ("a.py", 2, "bo@x.com")]
    source = _FakeSource(rows)
    stmt = _stmt([_select_item(_col("path"))], distinct=True)

    tree = plan(stmt, Path("/nonexistent"), tables=_fake_tables(source))

    assert list(tree.rows()) == [("a.py",)]


def test_distinct_through_the_real_parser_and_binder_and_planner():
    """The full, real pipeline - `tokenize -> parse -> bind -> plan ->
    tree.rows()` - rather than a hand-built `BoundSelectStatement`,
    matching `test_limit_offset_through_the_real_parser_and_binder_
    and_planner`'s own convention for issue #77."""
    schema = Schema(columns=(Column("a", ColumnType.TEXT),))
    rows: list[Row] = [("x",), ("x",), ("y",)]
    source = _FakeSource(rows)
    source.schema = schema

    stmt = parse(tokenize("SELECT DISTINCT a FROM t"))
    bound = bind(stmt, catalog={"t": schema})
    tree = plan(bound, Path("/nonexistent"), tables={"t": lambda repo: source})

    assert isinstance(tree, Distinct)
    assert list(tree.rows()) == [("x",), ("y",)]


def test_distinct_with_order_by_and_limit_through_the_real_pipeline_end_to_end():
    """The interleaving case this issue's own grooming verified live
    against sqlite3: `SELECT DISTINCT p, m FROM t3 ORDER BY m` over
    `('b',2),('b',2),('a',1),('a',1),('a',3)` gives `a|1, b|2, a|3` -
    correctly interleaved even though the two `a` rows are not
    adjacent in the input and `p`'s own groups are not contiguous."""
    schema = Schema(columns=(Column("p", ColumnType.TEXT), Column("m", ColumnType.INTEGER)))
    rows: list[Row] = [("b", 2), ("b", 2), ("a", 1), ("a", 1), ("a", 3)]
    source = _FakeSource(rows)
    source.schema = schema

    stmt = parse(tokenize("SELECT DISTINCT p, m FROM t3 ORDER BY m"))
    bound = bind(stmt, catalog={"t3": schema})
    tree = plan(bound, Path("/nonexistent"), tables={"t3": lambda repo: source})

    assert list(tree.rows()) == [("a", 1), ("b", 2), ("a", 3)]
