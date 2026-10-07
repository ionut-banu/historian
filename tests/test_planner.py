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

import pytest

from historian.catalog import SCAN_FACTORIES
from historian.exec.operators import Aggregate, Distinct, Filter, Limit, Project, Scan, ScanEstimate, Sort, child_of
from historian.plan import planner
from historian.sql import walk
from historian.plan.explain import format_plan
from historian.plan.planner import plan
from historian.schema import Column, ColumnType, Row, Schema
from historian.sql.ast import And, BinaryOp, FunctionCall, Is, Like, Literal, Not, OrderDirection, Operator as Op, Star
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


# --- the table -> scan-factory mapping is a required parameter ------------


def test_tables_parameter_is_required():
    """Issue #35: `plan()`'s `tables` parameter has no hardcoded real
    default any more - this module never imports `tables/blame.py` or
    `historian.catalog` itself, so it has no real catalog to fall back
    to. Calling it with no `tables` argument is a `TypeError` (missing
    required argument), not a silent fallback to an empty or stale
    catalog."""
    stmt = _stmt([_select_item(_col("path"))], where=None, from_table="blame")

    with pytest.raises(TypeError):
        plan(stmt, Path("/nonexistent"))


def test_plan_against_the_real_catalog_builds_a_real_blame_scan():
    """The real catalog - `historian.catalog.SCAN_FACTORIES`, built
    from a direct import of `tables/blame.py` - is not this file's
    concern to construct (this module never imports `historian.tables.
    blame` or `historian.catalog` itself, matching the module
    docstring's own claim), but a caller that does pass it in gets a
    real `BlameScan` bound to the given repo path back - proven
    without ever calling `.rows()`, so no git subprocess runs."""
    stmt = _stmt([_select_item(_col("path"))], where=None, from_table="blame")
    repo = Path("/nonexistent/for/this/test")

    tree = plan(stmt, repo, tables=SCAN_FACTORIES)

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


# --- count(x) and count(DISTINCT x) keep separate slots (issue #131) ------
#
# `_split_expr` gives every call its own slot and never deduplicates, so
# no query reaches the planner's shape equality with two aggregate
# calls. These pin that end to end, so the `distinct` comparison in
# `sql/walk.py`'s shared shape equality cannot start sharing a slot
# between the two.

_TWO_PATHS_THREE_ROWS = [
    ("a.py", 1, "ana@x.com"),
    ("a.py", 2, "ana@x.com"),
    ("b.py", 1, "bo@x.com"),
]


def test_plan_count_and_count_distinct_of_same_column_get_separate_calls():
    """`SELECT count(path), count(DISTINCT path) FROM widgets`: two
    `AggregateCall`s, flags False then True, giving `(3, 2)`."""
    source = _FakeSource(_TWO_PATHS_THREE_ROWS)
    stmt = _stmt(
        [
            _select_item(_func("count", _col("path"))),
            _select_item(_func("count", _col("path"), distinct=True)),
        ]
    )

    tree = plan(stmt, Path("/nonexistent"), tables=_fake_tables(source))

    assert [call.distinct for call in tree._child._calls] == [False, True]
    assert list(tree.rows()) == [(3, 2)]


def test_plan_having_with_count_and_count_distinct_of_same_column_gets_separate_calls():
    """`SELECT count(path), count(DISTINCT path) FROM widgets HAVING
    count(DISTINCT path) = 2 AND count(path) = 3`: the HAVING calls
    get their own slots too, flags as written, and the filter passes
    only because the two are evaluated separately."""
    source = _FakeSource(_TWO_PATHS_THREE_ROWS)
    having = And(
        left=_bin(Op.EQ, _func("count", _col("path"), distinct=True), _lit(2)),
        right=_bin(Op.EQ, _func("count", _col("path")), _lit(3)),
        position=_POS,
    )
    stmt = _stmt(
        [
            _select_item(_func("count", _col("path"))),
            _select_item(_func("count", _col("path"), distinct=True)),
        ],
        having=having,
    )

    tree = plan(stmt, Path("/nonexistent"), tables=_fake_tables(source))

    assert isinstance(tree._child, Filter)
    aggregate = tree._child._child
    assert isinstance(aggregate, Aggregate)
    assert [call.distinct for call in aggregate._calls] == [False, True, True, False]
    assert list(tree.rows()) == [(3, 2)]


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


def test_plan_hand_built_having_without_any_aggregate_filters_the_scan_rows():
    """`plan()`'s `elif having is not None` shape (issue #112): a
    hand-built statement with `HAVING` and no `GROUP BY` or aggregate
    anywhere - unreachable through `bind()` - is not an aggregate query,
    so `HAVING` is a plain `Filter` over the scan rows, with no
    `Aggregate`."""
    source = _FakeSource([("a.py", 1, "ana@x.com"), ("b.py", 2, "bo@x.com")])
    having = _bin(Op.EQ, _col("path"), _lit("b.py"))
    stmt = _stmt([_select_item(_col("line_no"))], where=None, having=having)

    tree = plan(stmt, Path("/nonexistent"), tables=_fake_tables(source))

    assert isinstance(tree, Project)
    assert isinstance(tree._child, Filter)
    assert isinstance(tree._child._child, Scan)
    assert list(tree.rows()) == [(2,)]


def test_plan_hand_built_aggregate_only_in_having_builds_aggregate():
    """The planner's aggregate-query decision looks at `HAVING` and
    `ORDER BY` as well as the select list (issue #112): an aggregate
    call written only in `HAVING` of a hand-built statement still gets
    `Aggregate`, below `HAVING`'s `Filter`."""
    source = _FakeSource([("a.py", 1, "ana@x.com"), ("b.py", 2, "bo@x.com")])
    having = _bin(Op.GT, _count_star(), _lit(1))
    stmt = _stmt([_select_item(_lit(7))], where=None, having=having)

    tree = plan(stmt, Path("/nonexistent"), tables=_fake_tables(source))

    assert isinstance(tree, Project)
    assert isinstance(tree._child, Filter)
    assert isinstance(tree._child._child, Aggregate)
    assert list(tree.rows()) == [(7,)]


def test_plan_hand_built_aggregate_only_in_order_by_builds_aggregate():
    source = _FakeSource([("a.py", 1, "ana@x.com"), ("b.py", 2, "bo@x.com")])
    order_by = [BoundOrderByItem(expr=_count_star(), direction=OrderDirection.ASC, position=_POS)]
    stmt = _stmt([_select_item(_lit(7))], where=None, order_by=order_by)

    tree = plan(stmt, Path("/nonexistent"), tables=_fake_tables(source))

    assert isinstance(tree, Project)
    assert isinstance(tree._child, Sort)
    assert isinstance(tree._child._child, Aggregate)
    assert list(tree.rows()) == [(7,)]


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


# --- LIKE ... ESCAPE joins shape equality and `_split_expr` (#101) -----
#
# The planner's shape equality (`_group_key_index`'s dependency) is
# `sql/walk.py`'s `expr_shape_equal` since #112, shared with the
# binder. A black-box query alone cannot prove the planner's use of it
# handles `Like`, since the binder rejects the same query first.
# Pinned directly here instead; `tests/test_walk.py` checks every field.


def test_expr_shape_equal_like_differing_only_in_escape_is_not_equal():
    with_escape = Like(
        left=_col("path"),
        pattern=_lit("c%"),
        negated=False,
        position=_POS,
        escape=_lit("c"),
    )
    without_escape = Like(
        left=_col("path"),
        pattern=_lit("c%"),
        negated=False,
        position=_POS,
        escape=None,
    )
    assert walk.expr_shape_equal(with_escape, without_escape) is False


def test_expr_shape_equal_like_with_different_escape_operands_is_not_equal():
    escape_c = Like(
        left=_col("path"), pattern=_lit("c%"), negated=False, position=_POS, escape=_lit("c")
    )
    escape_x = Like(
        left=_col("path"), pattern=_lit("c%"), negated=False, position=_POS, escape=_lit("x")
    )
    assert walk.expr_shape_equal(escape_c, escape_x) is False


def test_expr_shape_equal_like_with_identical_escape_operands_is_equal():
    a = Like(left=_col("path"), pattern=_lit("c%"), negated=False, position=_POS, escape=_lit("c"))
    b = Like(left=_col("path"), pattern=_lit("c%"), negated=False, position=_POS, escape=_lit("c"))
    assert walk.expr_shape_equal(a, b) is True


def test_expr_shape_equal_count_differing_only_in_distinct_is_not_equal():
    """Issue #131: the `distinct` check, as the planner uses it.
    Unreachable from a query - `_split_expr` never compares two
    aggregate calls - so hand-built nodes, in both argument orders."""
    plain = _func("count", _col("path"))
    flagged = _func("count", _col("path"), distinct=True)
    assert walk.expr_shape_equal(plain, flagged) is False
    assert walk.expr_shape_equal(flagged, plain) is False


def test_expr_shape_equal_count_with_equal_distinct_flags_is_equal():
    """The reverse: equal flags still compare equal, both set and both
    clear."""
    assert walk.expr_shape_equal(
        _func("count", _col("path"), distinct=True), _func("count", _col("path"), distinct=True)
    ) is True
    assert walk.expr_shape_equal(_func("count", _col("path")), _func("count", _col("path"))) is True


def test_expr_shape_equal_literals_differing_only_in_int_versus_real_type_are_not_equal():
    """Issue #108, A5: the literal-type check, as the planner uses
    it. `1 == 1.0` in Python, but they are different literals.
    Unreachable from a query (the binder rejects first), so hand-built
    nodes."""
    assert walk.expr_shape_equal(_lit(1), _lit(1.0)) is False


def test_expr_shape_equal_is_differing_only_in_negated_is_not_equal():
    """Issue #108, A5: the `Is.negated` check, as the planner uses
    it - `x IS NULL` against `x IS NOT NULL`."""
    is_null = Is(left=_col("path"), right=_lit(None), negated=False, position=_POS)
    is_not_null = Is(left=_col("path"), right=_lit(None), negated=True, position=_POS)
    assert walk.expr_shape_equal(is_null, is_not_null) is False


def test_plan_split_expr_like_escape_column_matching_group_key_reads_from_aggregate_output():
    """`_split_expr`'s `Like` branch must rewrite `escape` recursively
    exactly like `left`/`pattern` - here `line_no` (the GROUP BY key)
    used as the escape operand must resolve to a `BoundColumnRef`
    reading `Aggregate`'s group-key output column (offset 0), not stay
    a raw pre-aggregation row offset."""
    group_by = (_col("line_no"),)
    like = Like(
        left=_lit("x%"),
        pattern=_bin(Op.CONCAT, _lit("x"), _col("line_no")),
        negated=False,
        position=_POS,
        escape=_col("line_no"),
    )
    calls: list = []
    split = planner._split_expr(like, calls, group_by)
    assert isinstance(split, Like)
    assert isinstance(split.escape, BoundColumnRef)
    assert split.escape.offset == 0


def test_plan_split_expr_like_escape_aggregate_call_gets_routed_to_aggregate():
    """An aggregate call as the `ESCAPE` operand must be split into its
    own `Aggregate` slot like any other aggregate call, not left as a
    `FunctionCall` for `exec/expression.py` to reject."""
    like = Like(
        left=_lit("x%"),
        pattern=_lit("x%"),
        negated=False,
        position=_POS,
        escape=FunctionCall(name="count", args=(), position=_POS, distinct=False),
    )
    calls: list = []
    split = planner._split_expr(like, calls, group_by=())
    assert isinstance(split, Like)
    assert isinstance(split.escape, BoundColumnRef)
    assert split.escape.offset == 0
    assert len(calls) == 1
    assert calls[0].kind == "count"


# --- Depth: the planner's walks do not recurse per tree level (#107) ----

_DEEP = 5000


def _deep_chain(n: int, leaf):
    node = leaf()
    for _ in range(n - 1):
        node = _bin(Op.ADD, node, leaf())
    return node


def test_split_expr_over_a_deep_chain_of_aggregate_calls():
    """Every aggregate call in a 5000-term chain gets its own slot, in
    left-to-right order, and the chain is rebuilt around the slots."""
    expr = _deep_chain(_DEEP, lambda: FunctionCall(name="count", args=(Star(None, _POS),), position=_POS))
    calls: list = []
    split = planner._split_expr(expr, calls, group_by=())
    assert len(calls) == _DEEP
    offsets = []
    node = split
    while isinstance(node, BinaryOp):
        offsets.append(node.right.offset)
        node = node.left
    offsets.append(node.offset)
    assert offsets[::-1] == list(range(_DEEP))


def test_split_expr_replaces_a_deep_group_key_subtree():
    """A deep GROUP BY key found as a subtree is replaced wholesale by a
    reference to its group-key column."""
    key = _deep_chain(1500, lambda: _col("line_no"))
    expr = _bin(Op.ADD, key, _lit(1))
    calls: list = []
    split = planner._split_expr(expr, calls, group_by=(key,))
    assert isinstance(split, BinaryOp)
    assert isinstance(split.left, BoundColumnRef) and split.left.offset == 0
    assert calls == []


def test_expr_shape_equal_on_deep_trees():
    a = _deep_chain(_DEEP, lambda: _col("line_no"))
    b = _deep_chain(_DEEP, lambda: _col("line_no"))
    c = _bin(Op.ADD, _deep_chain(_DEEP - 1, lambda: _col("line_no")), _col("path"))
    assert walk.expr_shape_equal(a, b) is True
    assert walk.expr_shape_equal(a, c) is False


def test_plan_of_a_deep_where_select_and_order_by():
    """`plan()` over a bound statement with 5000-level trees in WHERE,
    the select list and ORDER BY runs and produces the right rows."""
    select = (_select_item(_deep_chain(_DEEP, lambda: _lit(1))),)
    where = _deep_chain(_DEEP, lambda: _col("line_no"))
    stmt = _stmt(
        select,
        where=_bin(Op.GT, where, _lit(0)),
        order_by=(BoundOrderByItem(expr=where, direction=OrderDirection.ASC, position=_POS),),
    )
    source = _FakeSource([("a.py", 1, None), ("b.py", 2, None)])
    tree = plan(stmt, Path("/unused"), tables=_fake_tables(source))
    assert list(tree.rows()) == [(_DEEP,), (_DEEP,)]


# --- HAVING terms that move below the aggregate (#141) -----------------------
#
# Bound through the real parser and binder against `widgets` (the same
# `_SCHEMA`), planned against `_EstimatingSource`: no git anywhere.
# `ERR` is the one expression that can raise at run time.

_ERR = "path LIKE 'a' ESCAPE 'ab'"


class _EstimatingSource(_FakeSource):
    """`_FakeSource` plus the `estimate()` `format_plan` asks for."""

    def estimate(self, pushed: Sequence[object] = ()) -> ScanEstimate:
        return ScanEstimate(name="WidgetScan", selected=0, total=0)


def _bound(sql: str) -> BoundSelectStatement:
    return bind(parse(tokenize(sql)), catalog={"widgets": _SCHEMA})


def _planned(sql: str, rows: Sequence[Row] = ()):
    bound = _bound(sql)
    return bound, plan(bound, Path("/nonexistent"), tables=_fake_tables(_EstimatingSource(rows)))


def _chain(tree) -> list:
    """The operators of *tree*, root first."""
    ops = []
    node = tree
    while node is not None:
        ops.append(node)
        node = child_of(node)
    return ops


def _kinds(tree) -> list[str]:
    return [type(op).__name__ for op in _chain(tree)]


def _having_terms(bound: BoundSelectStatement) -> list:
    return walk.split_conjuncts(bound.having)


def test_moved_term_gets_its_own_filter_below_the_aggregate():
    """`GROUP BY path HAVING count(*) > 5 AND ERR`: `Scan -> Filter(ERR,
    over the scan row) -> Aggregate -> Filter(count(*) > 5, over the
    aggregate's slots) -> Project`. The moved term is the bound term
    itself, column offsets into the scan row; the kept one reads slot 2
    (after the group key and the select list's own `count(*)`, which
    has a slot of its own, as before #141)."""
    bound, tree = _planned(f"SELECT count(*) FROM widgets GROUP BY path HAVING count(*) > 5 AND {_ERR}")
    assert _kinds(tree) == ["Project", "Filter", "Aggregate", "Filter", "Scan"]
    _project, kept, _aggregate, moved, _scan = _chain(tree)
    count_term, err_term = _having_terms(bound)
    assert moved.predicate() is err_term
    assert moved.negotiable() is False
    slot = kept.predicate()
    assert isinstance(slot, BinaryOp) and slot.op is Op.GT and slot.right is count_term.right
    assert isinstance(slot.left, BoundColumnRef) and slot.left.offset == 2


def test_moved_filter_sits_above_the_where_filter():
    bound, tree = _planned(
        f"SELECT count(*) FROM widgets WHERE line_no > 0 GROUP BY path HAVING count(*) > 5 AND {_ERR}"
    )
    assert _kinds(tree) == ["Project", "Filter", "Aggregate", "Filter", "Filter", "Scan"]
    _project, _kept, _aggregate, moved, where, _scan = _chain(tree)
    assert where.predicate() is bound.where
    assert where.negotiable() is True
    assert moved.predicate() is _having_terms(bound)[1]
    assert moved.negotiable() is False


def test_two_moved_terms_are_one_left_deep_and_in_having_order():
    bound, tree = _planned(
        f"SELECT count(*) FROM widgets GROUP BY path HAVING {_ERR} AND count(*) > 5 AND path > 'x' AND path < 'y'"
    )
    terms = _having_terms(bound)
    moved = _chain(tree)[3]
    predicate = moved.predicate()
    assert isinstance(predicate, And) and isinstance(predicate.left, And)
    assert predicate.right is terms[3]
    assert predicate.left.left is terms[0] and predicate.left.right is terms[2]
    got = walk.split_conjuncts(predicate)
    assert len(got) == 3 and all(a is b for a, b in zip(got, [terms[0], terms[2], terms[3]]))


def test_when_every_term_moves_there_is_no_filter_above_the_aggregate():
    _bound_stmt, tree = _planned(f"SELECT count(*) FROM widgets GROUP BY path HAVING path > 'x' AND {_ERR}")
    assert _kinds(tree) == ["Project", "Aggregate", "Filter", "Scan"]


#: Queries in which no `HAVING` term moves, and the plan `main` printed
#: for each before #141 (with no pushdown: `plan()` alone).
_UNMOVED_PLANS = {
    # Aggregate-only HAVING.
    "SELECT path, count(*) FROM widgets GROUP BY path HAVING count(*) > 1 AND max(line_no) < 5": (
        "Project (path, count(*))\n"
        "  Filter (count(*) > 1 AND max(line_no) < 5)\n"
        "    Aggregate (group=[path], aggs=[count(*), max(line_no)])\n"
        "      WidgetScan (pushed: none -> 0 of 0 paths)\n"
    ),
    # An OR at the root is one term, and it has an aggregate.
    f"SELECT path FROM widgets GROUP BY path HAVING count(*) > 1 OR {_ERR}": (
        "Project (path)\n"
        "  Filter (count(*) > 1 OR path LIKE 'a' ESCAPE 'ab')\n"
        "    Aggregate (group=[path], aggs=[count(*)])\n"
        "      WidgetScan (pushed: none -> 0 of 0 paths)\n"
    ),
    # No GROUP BY: nothing moves, a constant term included.
    "SELECT count(*) FROM widgets WHERE line_no > 0 HAVING count(*) > 1 AND 'a' LIKE 'a' ESCAPE 'ab'": (
        "Project (count(*))\n"
        "  Filter (count(*) > 1 AND 'a' LIKE 'a' ESCAPE 'ab')\n"
        "    Aggregate (group=[], aggs=[count(*)])\n"
        "      Filter (line_no > 0)\n"
        "        WidgetScan (pushed: none -> 0 of 0 paths)\n"
    ),
}


@pytest.mark.parametrize("sql", list(_UNMOVED_PLANS))
def test_when_nothing_moves_the_tree_is_the_one_main_built(sql):
    _bound_stmt, tree = _planned(sql)
    assert format_plan(tree) == _UNMOVED_PLANS[sql]


def test_aggregate_calls_and_kept_slots_are_unchanged_by_a_moved_term():
    """Aggregates in the select list, `HAVING` and `ORDER BY`, and one
    key term in the middle of `HAVING`: `Aggregate.calls()` and the
    kept `Filter`'s predicate are exactly what the same query without
    that term plans to - the term had no aggregate, so no slot moves."""
    with_term = (
        "SELECT path, count(*), max(line_no) FROM widgets GROUP BY path "
        "HAVING max(line_no) >= 1 AND path >= '' AND sum(line_no) > 0 ORDER BY count(*) DESC, min(line_no)"
    )
    without_term = with_term.replace("AND path >= '' ", "")
    _b1, tree = _planned(with_term)
    _b2, expected = _planned(without_term)
    assert _kinds(tree) == ["Project", "Sort", "Filter", "Aggregate", "Filter", "Scan"]
    assert _kinds(expected) == ["Project", "Sort", "Filter", "Aggregate", "Scan"]
    aggregate = _chain(tree)[3]
    assert [call.kind for call in aggregate.calls()] == ["count", "max", "max", "sum", "count", "min"]
    expected_calls = _chain(expected)[3].calls()
    assert len(aggregate.calls()) == len(expected_calls)
    for got, want in zip(aggregate.calls(), expected_calls):
        # Positions differ: the term was cut out of the query text.
        assert (got.kind, got.distinct) == (want.kind, want.distinct)
        assert (got.arg is None) == (want.arg is None)
        assert got.arg is None or walk.expr_shape_equal(got.arg, want.arg)
    assert walk.expr_shape_equal(_chain(tree)[2].predicate(), _chain(expected)[2].predicate())
    assert walk.expr_shape_equal(_chain(tree)[0].select_list()[1].expr, _chain(expected)[0].select_list()[1].expr)
    rows = [("a.py", 1, None), ("a.py", 2, None), ("b.py", 3, None)]
    _b3, tree = _planned(with_term, rows)
    assert list(tree.rows()) == [("a.py", 2, 2), ("b.py", 1, 3)]


def test_a_term_built_on_an_expression_key_moves():
    bound, tree = _planned(
        "SELECT count(*) FROM widgets GROUP BY path || 'x' HAVING count(*) > 5 AND ((path || 'x') || 'y') > 'a'"
    )
    assert _kinds(tree) == ["Project", "Filter", "Aggregate", "Filter", "Scan"]
    assert _chain(tree)[3].predicate() is _having_terms(bound)[1]


def test_a_column_outside_the_key_expression_keeps_the_term():
    """Hand-built, since the binder rejects it: `GROUP BY path || 'x'`
    with `path > 'a'` in `HAVING` - `path` is not inside a key
    subexpression, so the term stays."""
    key = _bin(Op.CONCAT, _col("path"), _lit("x"))
    having = And(left=_bin(Op.GT, _count_star(), _lit(5)), right=_bin(Op.GT, _col("path"), _lit("a")), position=_POS)
    stmt = _stmt([_select_item(_count_star())], group_by=[key], having=having)
    tree = plan(stmt, Path("/nonexistent"), tables=_fake_tables(_FakeSource([])))
    assert _kinds(tree) == ["Project", "Filter", "Aggregate", "Scan"]


def test_a_constant_term_moves():
    bound, tree = _planned("SELECT count(*) FROM widgets GROUP BY path HAVING count(*) > 5 AND 1")
    assert _kinds(tree) == ["Project", "Filter", "Aggregate", "Filter", "Scan"]
    assert _chain(tree)[3].predicate() is _having_terms(bound)[1]


def test_an_integer_literal_zero_term_stays():
    """SQLite 3.50.4 leaves an always-false term (an integer literal
    `0`) in `HAVING` and moves the rest (`tests/differential/
    test_having_hoist.py`). `0.0` and `-0` are not that literal."""
    bound, tree = _planned(f"SELECT count(*) FROM widgets GROUP BY path HAVING count(*) > 5 AND 0 AND {_ERR}")
    assert _kinds(tree) == ["Project", "Filter", "Aggregate", "Filter", "Scan"]
    count_term, zero, err_term = _having_terms(bound)
    assert _chain(tree)[3].predicate() is err_term
    kept = walk.split_conjuncts(_chain(tree)[1].predicate())
    assert len(kept) == 2 and kept[1] is zero
    for constant in ("0.0", "-0"):
        _b, tree = _planned(f"SELECT count(*) FROM widgets GROUP BY path HAVING count(*) > 5 AND {constant}")
        assert _kinds(tree) == ["Project", "Filter", "Aggregate", "Filter", "Scan"], constant


def test_not_over_and_is_one_term_and_moves_whole():
    bound, tree = _planned(f"SELECT count(*) FROM widgets GROUP BY path HAVING count(*) > 5 AND NOT (path > 'a' AND {_ERR})")
    moved = _chain(tree)[3].predicate()
    assert isinstance(moved, Not)
    assert moved is _having_terms(bound)[1]


def test_planning_does_not_mutate_the_bound_statement():
    sql = f"SELECT count(*) FROM widgets WHERE line_no > 0 GROUP BY path HAVING count(*) > 5 AND {_ERR} AND path > 'a'"
    bound = _bound(sql)
    having_before = bound.having
    snapshot = repr(bound)
    plan(bound, Path("/nonexistent"), tables=_fake_tables(_FakeSource([])))
    assert bound.having is having_before
    assert repr(bound) == snapshot
    assert bound == _bound(sql)


def test_moved_terms_filter_rows_end_to_end():
    rows = [("a.py", 1, None), ("a.py", 2, None), ("b.py", 3, None)]
    _bound_stmt, tree = _planned(
        "SELECT path, count(*) FROM widgets GROUP BY path HAVING count(*) > 0 AND path > 'a.py'", rows
    )
    assert list(tree.rows()) == [("b.py", 1)]


def test_a_900_term_having_half_moved_plans_and_runs():
    """900 terms joined by `AND` through the real parser and binder,
    alternating a key term (moves) and an aggregate term (stays)."""
    terms = []
    for index in range(450):
        terms.append(f"path >= '{index % 3}'")
        terms.append(f"count(*) > {index % 2 - 1}")
    sql = f"SELECT path, count(*) FROM widgets GROUP BY path HAVING {' AND '.join(terms)}"
    rows = [("a.py", 1, None), ("a.py", 2, None), ("b.py", 3, None), ("0.py", 4, None)]
    _bound_stmt, tree = _planned(sql, rows)
    assert _kinds(tree) == ["Project", "Filter", "Aggregate", "Filter", "Scan"]
    assert len(walk.split_conjuncts(_chain(tree)[1].predicate())) == 450
    assert len(walk.split_conjuncts(_chain(tree)[3].predicate())) == 450
    assert list(tree.rows()) == [("a.py", 2), ("b.py", 1)]
    format_plan(tree)


def test_a_5000_term_hand_built_having_plans_and_runs():
    """Deeper than the parser allows, so a recursive split, partition or
    join would hit the recursion limit."""
    having = _bin(Op.GE, _col("path"), _lit(""))
    for index in range(1, _DEEP):
        term = _bin(Op.GE, _col("path"), _lit("")) if index % 2 == 0 else _bin(Op.GT, _count_star(), _lit(0))
        having = And(left=having, right=term, position=_POS)
    stmt = _stmt([_select_item(_col("path"))], group_by=[_col("path")], having=having)
    tree = plan(stmt, Path("/nonexistent"), tables=_fake_tables(_FakeSource([("a.py", 1, None), ("b.py", 2, None)])))
    assert len(walk.split_conjuncts(_chain(tree)[1].predicate())) == _DEEP // 2
    assert len(walk.split_conjuncts(_chain(tree)[3].predicate())) == _DEEP // 2
    assert list(tree.rows()) == [("a.py",), ("b.py",)]


# --- Constant propagation in WHERE (#142) -------------------------------------
#
# Bound through the real parser and binder against `widgets` (path TEXT,
# line_no INTEGER, author_email TEXT), planned against `_EstimatingSource`:
# no git anywhere. `walk.FixedColumnRef` is looked up at run time.


def _where_terms(tree) -> list:
    """The terms of the `Filter` directly above the `Scan`."""
    ops = _chain(tree)
    assert isinstance(ops[-1], Scan) and isinstance(ops[-2], Filter)
    return walk.split_conjuncts(ops[-2].predicate())


def _fixed_refs(expr) -> list:
    """Every `FixedColumnRef` in *expr*, left to right."""
    found = []
    pending = [expr]
    while pending:
        node = pending.pop()
        if isinstance(node, walk.FixedColumnRef):
            found.append(node)
        pending.extend(reversed(walk.children(node)))
    return found


def _column_refs(expr) -> list:
    found = []
    pending = [expr]
    while pending:
        node = pending.pop()
        if isinstance(node, BoundColumnRef):
            found.append(node)
        pending.extend(reversed(walk.children(node)))
    return found


def test_the_source_is_kept_and_the_guard_rewritten():
    """`NOT (line_no = 5 AND ERR) AND line_no = 5`: the `Filter` holds
    the source as bound and the guard with `line_no` replaced by a
    `FixedColumnRef` holding `5`; `ERR` is the bound object itself."""
    bound, tree = _planned(f"SELECT path FROM widgets WHERE NOT (line_no = 5 AND {_ERR}) AND line_no = 5")
    guard, source = _where_terms(tree)
    bound_guard, bound_source = walk.split_conjuncts(bound.where)
    assert source is bound_source
    assert isinstance(guard, Not) and guard is not bound_guard
    inner = guard.operand
    assert isinstance(inner, And)
    assert inner.right is bound_guard.operand.right
    fixed = inner.left.left
    assert isinstance(fixed, walk.FixedColumnRef)
    assert (fixed.offset, fixed.name, fixed.value) == (1, "line_no", 5)
    assert type(fixed.value) is int
    assert inner.left.right is bound_guard.operand.left.right
    assert _column_refs(guard) == [bound_guard.operand.right.left]


def test_a_source_in_the_middle_rewrites_terms_before_and_after():
    bound, tree = _planned("SELECT path FROM widgets WHERE line_no > 0 AND line_no = 5 AND line_no < 9")
    before, source, after = _where_terms(tree)
    assert source is walk.split_conjuncts(bound.where)[1]
    for term in (before, after):
        assert [ref.value for ref in _fixed_refs(term)] == [5]
        assert _column_refs(term) == []


def test_sources_on_two_columns_are_both_used():
    bound, tree = _planned(
        "SELECT path FROM widgets WHERE NOT (line_no = 1 AND path = 'q') AND line_no = 1 AND path = 'q'"
    )
    guard, line_source, path_source = _where_terms(tree)
    _g, bound_line, bound_path = walk.split_conjuncts(bound.where)
    assert line_source is bound_line and path_source is bound_path
    assert [(ref.name, ref.value) for ref in _fixed_refs(guard)] == [("line_no", 1), ("path", "q")]
    assert _column_refs(guard) == []


def test_of_two_sources_for_one_column_the_last_is_used():
    """SQLite takes the last (`findConstInWhere` walks right to left and
    keeps the first source it meets per column); the earlier one is
    rewritten like any other term, so `1 = 1.0` reads `1`, not `1.0`."""
    bound, tree = _planned("SELECT path FROM widgets WHERE line_no = 1.0 AND line_no = 1")
    first, last = _where_terms(tree)
    assert last is bound.where.right
    assert first is not bound.where.left
    (fixed,) = _fixed_refs(first)
    assert fixed.value == 1 and type(fixed.value) is int
    assert first.right is bound.where.left.right


@pytest.mark.parametrize(
    "constant, value",
    [("'05'", 5), ("5.0", 5), ("' 5'", 5), ("'5.0'", 5), ("5.5", 5.5), ("'x'", "x"), ("NULL", None), ("-5", -5)],
)
def test_the_replacement_is_converted_by_the_columns_affinity(constant, value):
    _bound_stmt, tree = _planned(f"SELECT path FROM widgets WHERE line_no > 0 AND line_no = {constant}")
    (fixed,) = _fixed_refs(_where_terms(tree)[0])
    assert fixed.value == value and type(fixed.value) is type(value)


def test_a_text_column_gets_a_text_replacement():
    _bound_stmt, tree = _planned("SELECT path FROM widgets WHERE path > '' AND path = 5.0")
    (fixed,) = _fixed_refs(_where_terms(tree)[0])
    assert fixed.value == "5.0"


@pytest.mark.parametrize(
    "where",
    [
        "line_no IS 5 AND line_no > 0",
        "line_no <> 5 AND line_no > 0",
        "line_no BETWEEN 5 AND 5 AND line_no > 0",
        "line_no IN (5, 5) AND line_no > 0",
        "line_no NOT IN (5) AND line_no > 0",
        "+line_no = 5 AND line_no > 0",
        "line_no + 0 = 5 AND line_no > 0",
        "line_no = line_no AND line_no > 0",
        "NOT (line_no = 5) AND line_no > 0",
        "(line_no = 5 OR line_no = 6) AND line_no > 0",
        "path LIKE 'a' AND path > ''",
    ],
)
def test_terms_that_are_not_sources_leave_the_where_as_bound(where):
    bound, tree = _planned(f"SELECT path FROM widgets WHERE {where}")
    assert _chain(tree)[-2].predicate() is bound.where


#: A `WHERE` with no source plans to exactly what `main` built.
_NO_SOURCE_PLANS = {
    "SELECT path FROM widgets WHERE path LIKE 'a%' AND line_no > 1": (
        "Project (path)\n"
        "  Filter (path LIKE 'a%' AND line_no > 1)\n"
        "    WidgetScan (pushed: none -> 0 of 0 paths)\n"
    ),
    "SELECT path FROM widgets WHERE line_no = 5 OR line_no = 6": (
        "Project (path)\n"
        "  Filter (line_no = 5 OR line_no = 6)\n"
        "    WidgetScan (pushed: none -> 0 of 0 paths)\n"
    ),
    "SELECT path FROM widgets WHERE line_no <> 5": (
        "Project (path)\n"
        "  Filter (line_no <> 5)\n"
        "    WidgetScan (pushed: none -> 0 of 0 paths)\n"
    ),
    "SELECT path FROM widgets": (
        "Project (path)\n"
        "  WidgetScan (pushed: none -> 0 of 0 paths)\n"
    ),
}


@pytest.mark.parametrize("sql", list(_NO_SOURCE_PLANS))
def test_a_where_with_no_source_plans_the_tree_main_built(sql):
    bound, tree = _planned(sql)
    assert format_plan(tree) == _NO_SOURCE_PLANS[sql]
    if bound.where is not None:
        assert _chain(tree)[-2].predicate() is bound.where


def test_having_is_neither_a_source_nor_a_target():
    """A `WHERE` source does not reach the moved `HAVING` terms or the
    kept ones, and a `HAVING` `column = constant` is no source: the
    moved `Filter` holds the bound terms themselves, and nothing above
    the `WHERE` `Filter` holds a `FixedColumnRef`."""
    sql = (
        f"SELECT count(*) FROM widgets WHERE line_no = 5 AND path > '' GROUP BY line_no, path "
        f"HAVING NOT (line_no = 5 AND {_ERR}) AND count(*) > 0 AND path = 'q'"
    )
    bound, tree = _planned(sql)
    assert _kinds(tree) == ["Project", "Filter", "Aggregate", "Filter", "Filter", "Scan"]
    _project, kept, _aggregate, moved, where, _scan = _chain(tree)
    guard, _count, path_term = walk.split_conjuncts(bound.having)
    moved_terms = walk.split_conjuncts(moved.predicate())
    assert len(moved_terms) == 2 and moved_terms[0] is guard and moved_terms[1] is path_term
    assert _fixed_refs(kept.predicate()) == []
    # The WHERE itself is rewritten: `path > ''` has no source, `line_no = 5` is one.
    assert walk.split_conjuncts(where.predicate())[0] is bound.where.left
    # Without the WHERE source the HAVING side plans identically.
    _b2, plain = _planned(sql.replace("WHERE line_no = 5 AND path > ''", "WHERE line_no >= 5 AND path > ''"))
    assert walk.expr_shape_equal(kept.predicate(), _chain(plain)[1].predicate())
    assert walk.expr_shape_equal(moved.predicate(), _chain(plain)[3].predicate())


def test_propagation_does_not_mutate_the_bound_statement():
    sql = f"SELECT path FROM widgets WHERE NOT (line_no = 5 AND {_ERR}) AND line_no = 5 AND path > ''"
    bound = _bound(sql)
    where_before = bound.where
    snapshot = repr(bound)
    plan(bound, Path("/nonexistent"), tables=_fake_tables(_FakeSource([])))
    assert bound.where is where_before
    assert repr(bound) == snapshot
    assert bound == _bound(sql)


def test_propagated_rows_are_the_rows_without_propagation():
    rows = [("a.py", 1, None), ("b.py", 1, "x"), ("c.py", 2, None)]
    _b, tree = _planned("SELECT path FROM widgets WHERE line_no = 1.0 AND line_no || 'x' = '1x'", rows)
    assert list(tree.rows()) == [("a.py",), ("b.py",)]
    _b, tree = _planned("SELECT path FROM widgets WHERE line_no = 1.0 AND line_no || 'x' = '1.0x'", rows)
    assert list(tree.rows()) == []


def test_a_900_term_where_with_the_source_last_plans_and_runs():
    """Through the real parser: 899 `line_no >= 0` terms then `line_no =
    1`, every earlier term rewritten, with no `RecursionError` (#107)."""
    terms = ["line_no >= 0"] * 899 + ["line_no = 1"]
    rows = [("a.py", 1, None), ("b.py", 2, None)]
    bound, tree = _planned(f"SELECT path FROM widgets WHERE {' AND '.join(terms)}", rows)
    got = _where_terms(tree)
    assert len(got) == 900
    assert got[-1] is walk.split_conjuncts(bound.where)[-1]
    assert all(len(_fixed_refs(term)) == 1 for term in got[:-1])
    assert list(tree.rows()) == [("a.py",)]
    format_plan(tree)


def test_a_5000_term_hand_built_where_plans_and_runs():
    """Deeper than the parser allows: a recursive split, rewrite or
    rebuild would hit the recursion limit."""
    where = _bin(Op.GE, _col("line_no"), _lit(0))
    for _index in range(1, _DEEP - 1):
        where = And(left=where, right=_bin(Op.GE, _col("line_no"), _lit(0)), position=_POS)
    where = And(left=where, right=_bin(Op.EQ, _col("line_no"), _lit(2)), position=_POS)
    stmt = _stmt([_select_item(_col("path"))], where=where)
    tree = plan(stmt, Path("/nonexistent"), tables=_fake_tables(_FakeSource([("a.py", 1, None), ("b.py", 2, None)])))
    got = _where_terms(tree)
    assert len(got) == _DEEP
    assert got[-1] is where.right
    assert list(tree.rows()) == [("b.py",)]


def test_a_deep_guard_is_rewritten_without_recursion():
    """One guard term 5000 levels deep (`line_no + 1 + 1 ...`), hand
    built: the column replacement walk is iterative too."""
    deep = _deep_chain(_DEEP, lambda: _col("line_no"))
    where = And(left=_bin(Op.GT, deep, _lit(0)), right=_bin(Op.EQ, _col("line_no"), _lit(1)), position=_POS)
    stmt = _stmt([_select_item(_col("path"))], where=where)
    tree = plan(stmt, Path("/nonexistent"), tables=_fake_tables(_FakeSource([("a.py", 1, None), ("b.py", 2, None)])))
    guard, _source = _where_terms(tree)
    assert len(_fixed_refs(guard)) == len(_column_refs(deep))
    assert list(tree.rows()) == [("a.py",)]
