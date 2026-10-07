"""Unit tests for `plan/optimizer.py` (issue #121): pushdown
negotiation - split the bound `WHERE` on top-level `AND`, offer each
term to the scan beneath it, hand `Scan` only the accepted ones, and
leave `Filter` in place holding every term unchanged (spec §3,
"Pushdown negotiation").

No repository and no git anywhere in this file. Every tree is planned
against a hand-written recording `ScanSource` double over a made-up
`gadgets` table (never the real catalog), mirroring
`tests/test_planner.py`'s `_FakeSource` convention: it declares one
made-up capability, decides itself which offered terms it accepts
(only `a = <integer literal>`), and records what it was offered, what
it accepted and every `scan()` call as plain instance attributes -
never module-level state (spec §4, "The pushdown layer").

Expected row sets were confirmed against the oracle
(`tests/oracle.py`) over the same rows loaded into a
table `g(a INTEGER, b INTEGER, c INTEGER)`.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path

from historian.exec.operators import Aggregate, Filter, Project, Scan, child_of
from historian.plan.optimizer import optimize, split_conjuncts
from historian.plan.planner import plan
from historian.schema import Column, ColumnType, Row, Schema
from historian.sql.ast import And, BinaryOp, Literal, Not, Operator as Op, Or
from historian.sql.binder import BoundColumnRef, BoundSelectStatement, bind
from historian.sql.lexer import Position, tokenize
from historian.sql.parser import parse

_POS = Position(line=1, column=1, offset=0)

_SCHEMA = Schema(
    columns=(
        Column("a", ColumnType.INTEGER),
        Column("b", ColumnType.INTEGER),
        Column("c", ColumnType.INTEGER),
    )
)

#: The one made-up capability the double declares.
_A_EQ = "a_eq_literal"

#: The rows the oracle checks below were run over.
_ROWS: tuple[Row, ...] = (
    (1, 2, 3),
    (1, 5, 3),  # a = 1 but b != 2: the row a narrowed Filter would leak
    (2, 2, 3),
    (None, 2, None),
    (1, None, 7),
    (1, 2, 9),
)


def _is_a_eq_literal(term: object) -> bool:
    """`a = <integer literal>`, column on the left - the only shape the
    double accepts."""
    return (
        isinstance(term, BinaryOp)
        and term.op is Op.EQ
        and isinstance(term.left, BoundColumnRef)
        and term.left.offset == _SCHEMA.index_of("a")
        and isinstance(term.right, Literal)
        and type(term.right.value) is int
    )


class _RecordingGadgetSource:
    """A recording `ScanSource` double for the made-up `gadgets` table.

    `scan()` really uses what it is handed: for every pushed
    `a = <literal>` term it keeps only rows whose `a` equals that
    literal - then yields `leaked_rows` regardless, simulating a scan
    that narrows less precisely than it could (pushdown is allowed to
    return a superset). A term it was never offered cannot reach
    `pushed`, so the narrowing is only ever by terms it accepted."""

    schema = _SCHEMA

    def __init__(
        self,
        rows: Sequence[Row] = _ROWS,
        capabilities: frozenset[str] = frozenset({_A_EQ}),
        leaked_rows: Sequence[Row] = (),
    ) -> None:
        self._rows = tuple(rows)
        self._capabilities = capabilities
        self._leaked_rows = tuple(leaked_rows)
        self.offered: list[object] = []
        self.accepted: list[object] = []
        self.scan_calls: list[Sequence[object]] = []

    def capabilities(self) -> set[str]:
        return set(self._capabilities)

    def accepts(self, term: object) -> bool:
        self.offered.append(term)
        ok = _is_a_eq_literal(term)
        if ok:
            self.accepted.append(term)
        return ok

    def scan(self, pushed: Sequence[object] = ()) -> Iterator[Row]:
        self.scan_calls.append(pushed)
        for row in self._rows:
            keep = True
            for term in pushed:
                assert _is_a_eq_literal(term), "a term this source rejected was pushed"
                if row[0] != term.right.value:
                    keep = False
            if keep:
                yield row
        yield from self._leaked_rows


class _NoCapabilitySource(_RecordingGadgetSource):
    """Declares no capabilities at all - like `blame` today. The
    optimizer must not offer it anything."""

    def __init__(self, rows: Sequence[Row] = _ROWS) -> None:
        super().__init__(rows, capabilities=frozenset())


def _bind(sql: str) -> BoundSelectStatement:
    return bind(parse(tokenize(sql)), catalog={"gadgets": _SCHEMA})


def _plan(sql: str, source: _RecordingGadgetSource):
    return plan(_bind(sql), Path("/nonexistent"), tables={"gadgets": lambda repo: source})


def _optimized(sql: str, source: _RecordingGadgetSource):
    return optimize(_plan(sql, source))


def _scan_of(tree) -> Scan:
    node = tree
    while not isinstance(node, Scan):
        node = child_of(node)
    return node


def _col(name: str) -> BoundColumnRef:
    return BoundColumnRef(offset=_SCHEMA.index_of(name), name=name, position=_POS)


def _eq(name: str, value: int) -> BinaryOp:
    return BinaryOp(op=Op.EQ, left=_col(name), right=Literal(value, _POS), position=_POS)


# --- Splitting: what gets offered ------------------------------------------


def test_two_term_and_offers_both_terms_left_to_right():
    source = _RecordingGadgetSource()
    bound = _bind("SELECT a FROM gadgets WHERE a = 1 AND b = 2")
    tree = optimize(plan(bound, Path("/nonexistent"), tables={"gadgets": lambda repo: source}))
    list(tree.rows())

    assert len(source.offered) == 2
    assert source.offered[0] is bound.where.left
    assert source.offered[1] is bound.where.right
    assert source.offered[0].left.name == "a"
    assert source.offered[1].left.name == "b"


def test_or_is_one_term():
    source = _RecordingGadgetSource()
    bound = _bind("SELECT a FROM gadgets WHERE (a = 1 OR c = 3) AND b = 2")
    optimize(plan(bound, Path("/nonexistent"), tables={"gadgets": lambda repo: source}))

    assert len(source.offered) == 2
    assert isinstance(source.offered[0], Or)
    assert source.offered[0] is bound.where.left
    assert isinstance(source.offered[1], BinaryOp)
    assert source.offered[1].left.name == "b"
    # A disjunction containing an acceptable shape is still not accepted.
    assert source.accepted == []


def test_single_term_offers_exactly_one_term():
    source = _RecordingGadgetSource()
    bound = _bind("SELECT a FROM gadgets WHERE a = 1")
    optimize(plan(bound, Path("/nonexistent"), tables={"gadgets": lambda repo: source}))

    assert source.offered == [bound.where]
    assert source.accepted == [bound.where]


def test_no_where_offers_nothing_and_scan_gets_empty_pushed():
    source = _RecordingGadgetSource()
    tree = _optimized("SELECT a FROM gadgets", source)
    rows = list(tree.rows())

    assert source.offered == []
    assert source.scan_calls == [()]
    assert rows == [(1,), (1,), (2,), (None,), (1,), (1,)]


def test_parenthesised_and_groups_flatten_left_to_right():
    """Parens produce no AST node, and conjunction is associative, so
    `(a = 1 AND b = 2) AND c = 3` and `a = 1 AND (b = 2 AND c = 3)` both
    split into the same three terms, in source order."""
    for sql in (
        "SELECT a FROM gadgets WHERE (a = 1 AND b = 2) AND c = 3",
        "SELECT a FROM gadgets WHERE a = 1 AND (b = 2 AND c = 3)",
        "SELECT a FROM gadgets WHERE a = 1 AND b = 2 AND c = 3",
    ):
        source = _RecordingGadgetSource()
        _optimized(sql, source)
        assert [term.left.name for term in source.offered] == ["a", "b", "c"], sql


def test_not_over_and_is_one_term():
    source = _RecordingGadgetSource()
    _optimized("SELECT a FROM gadgets WHERE NOT (a = 1 AND b = 2)", source)
    assert len(source.offered) == 1
    assert isinstance(source.offered[0], Not)


def test_split_conjuncts_is_iterative_over_a_deep_and_chain():
    """A left-deep chain far past Python's recursion limit splits
    without a `RecursionError`, in left-to-right order."""
    count = 20_000
    expr = _eq("a", 0)
    for i in range(1, count):
        expr = And(left=expr, right=_eq("a", i), position=_POS)
    terms = split_conjuncts(expr)
    assert [term.right.value for term in terms] == list(range(count))


def test_split_conjuncts_leaves_a_non_and_expression_whole():
    term = Or(left=_eq("a", 1), right=_eq("b", 2), position=_POS)
    assert split_conjuncts(term) == [term]


def test_having_terms_are_never_offered():
    """Only the `WHERE` `Filter` directly above the `Scan` is
    negotiated. `HAVING`'s `Filter` sits above `Aggregate` and its
    terms are over aggregate output rows, not scan rows."""
    source = _RecordingGadgetSource()
    bound = _bind("SELECT b, count(*) FROM gadgets WHERE a = 1 GROUP BY b HAVING b = 2")
    tree = optimize(plan(bound, Path("/nonexistent"), tables={"gadgets": lambda repo: source}))
    assert source.offered == [bound.where]
    assert list(tree.rows()) == [(2, 2)]


def test_a_scan_with_no_capabilities_is_never_offered_anything():
    source = _NoCapabilitySource()
    tree = _optimized("SELECT a FROM gadgets WHERE a = 1 AND b = 2", source)
    list(tree.rows())
    assert source.offered == []
    assert source.scan_calls == [()]


# --- What reaches scan() ------------------------------------------------------


def test_only_the_accepted_subset_is_pushed_in_order():
    source = _RecordingGadgetSource()
    bound = _bind("SELECT a FROM gadgets WHERE b = 2 AND a = 1 AND c = 3 AND a = 1")
    tree = optimize(plan(bound, Path("/nonexistent"), tables={"gadgets": lambda repo: source}))
    list(tree.rows())

    a_terms = [term for term in source.offered if term.left.name == "a"]
    assert len(source.offered) == 4
    assert source.accepted == a_terms
    assert len(source.scan_calls) == 1
    assert list(source.scan_calls[0]) == a_terms
    assert all(term.left.name == "a" for term in source.scan_calls[0])
    assert _scan_of(tree).pushed() == tuple(a_terms)


def test_rejected_term_never_appears_in_pushed():
    source = _RecordingGadgetSource()
    bound = _bind("SELECT a FROM gadgets WHERE a = 1 AND b = 2")
    tree = optimize(plan(bound, Path("/nonexistent"), tables={"gadgets": lambda repo: source}))
    list(tree.rows())

    assert source.accepted == [bound.where.left]
    assert list(source.scan_calls[0]) == [bound.where.left]
    assert bound.where.right not in list(source.scan_calls[0])


def test_optimize_returns_the_tree_plan_built():
    source = _RecordingGadgetSource()
    tree = _plan("SELECT a FROM gadgets WHERE a = 1", source)
    assert optimize(tree) is tree


# --- The Filter stays, unchanged ---------------------------------------------


def test_filter_is_kept_with_the_original_predicate():
    source = _RecordingGadgetSource()
    bound = _bind("SELECT a FROM gadgets WHERE a = 1 AND b = 2")
    tree = optimize(plan(bound, Path("/nonexistent"), tables={"gadgets": lambda repo: source}))

    assert isinstance(tree, Project)
    filter_node = child_of(tree)
    assert isinstance(filter_node, Filter)
    assert filter_node.predicate() is bound.where
    assert isinstance(child_of(filter_node), Scan)


def test_optimized_and_unoptimized_trees_agree_on_every_row_set():
    sql = "SELECT a, b, c FROM gadgets WHERE a = 1 AND b = 2"
    row_sets = (
        _ROWS,
        ((1, 5, 3),),  # a = 1, b != 2 only: must still be dropped
        (),
        ((1, 2, 3), (1, 2, 3), (2, 2, 2)),
        ((None, None, None), (1, 2, None)),
    )
    for rows in row_sets:
        without = _plan(sql, _RecordingGadgetSource(rows))
        with_ = _optimized(sql, _RecordingGadgetSource(rows))
        assert list(without.rows()) == list(with_.rows()), rows

    # Oracle: SELECT a, b, c FROM g WHERE a = 1 AND b = 2 over _ROWS.
    assert list(_optimized(sql, _RecordingGadgetSource()).rows()) == [(1, 2, 3), (1, 2, 9)]


def test_or_query_matches_the_oracle_after_optimizing():
    rows = ((1, 2, 3), (1, 5, 3), (2, 2, 3), (None, 2, None), (1, None, 7), (2, 2, 9))
    sql = "SELECT a, b, c FROM gadgets WHERE (a = 1 OR c = 3) AND b = 2"
    # Oracle: (1, 2, 3), (2, 2, 3).
    assert list(_optimized(sql, _RecordingGadgetSource(rows)).rows()) == [(1, 2, 3), (2, 2, 3)]
    assert list(_plan(sql, _RecordingGadgetSource(rows)).rows()) == [(1, 2, 3), (2, 2, 3)]


def test_filter_removes_a_row_the_scan_returned_despite_failing_a_pushed_term():
    """Pushdown may return a superset. The double leaks `(7, 2, 0)` -
    failing the accepted, pushed `a = 1` - and the `Filter` above it
    must still drop it."""
    leaked = (7, 2, 0)
    source = _RecordingGadgetSource(leaked_rows=(leaked,))
    tree = _optimized("SELECT a, b, c FROM gadgets WHERE a = 1 AND b = 2", source)

    scanned = list(_RecordingGadgetSource(leaked_rows=(leaked,)).scan(pushed=tuple(source.accepted)))
    assert leaked in scanned
    rows = list(tree.rows())
    assert len(source.accepted) == 1
    assert leaked not in rows
    assert rows == [(1, 2, 3), (1, 2, 9)]


def test_only_rejected_terms_pushes_nothing_and_filter_alone_decides():
    source = _RecordingGadgetSource(rows=((1, 2, 3), (1, 5, 3), (2, 2, 3)))
    tree = _optimized("SELECT a, b, c FROM gadgets WHERE b = 7 AND c > 100", source)
    rows = list(tree.rows())

    assert len(source.offered) == 2
    assert source.accepted == []
    assert source.scan_calls == [()]
    # Oracle: no rows.
    assert rows == []


def test_whole_table_aggregate_over_where_still_negotiates():
    source = _RecordingGadgetSource()
    bound = _bind("SELECT count(*) FROM gadgets WHERE a = 1 AND b = 2")
    tree = optimize(plan(bound, Path("/nonexistent"), tables={"gadgets": lambda repo: source}))
    assert isinstance(child_of(tree), Aggregate)
    assert list(tree.rows()) == [(2,)]
    assert list(source.scan_calls[0]) == [bound.where.left]


# --- HAVING terms that move below the aggregate (#141) -----------------------


class _AcceptEverythingSource(_RecordingGadgetSource):
    """Accepts every term it is offered and narrows by none of them -
    a superset, which pushdown allows. So anything offered shows up in
    `offered` and in `Scan.pushed()`."""

    def accepts(self, term: object) -> bool:
        self.offered.append(term)
        self.accepted.append(term)
        return True

    def scan(self, pushed: Sequence[object] = ()) -> Iterator[Row]:
        self.scan_calls.append(pushed)
        yield from self._rows


def test_a_moved_filter_directly_above_the_scan_is_not_negotiated():
    """No `WHERE`: the `Filter` of the moved `b > 1` is the operator
    directly above the `Scan`, and a source that accepts everything is
    still offered nothing - as on `main`, where `b > 1` sat above the
    `Aggregate`. Oracle: `2`, `5`."""
    source = _AcceptEverythingSource()
    tree = _optimized("SELECT b FROM gadgets GROUP BY b HAVING b > 1", source)
    scan = _scan_of(tree)
    assert isinstance(child_of(child_of(tree)), Filter)
    assert child_of(child_of(tree)).negotiable() is False
    assert source.offered == []
    assert scan.pushed() == ()
    assert sorted(tree.rows()) == [(2,), (5,)]
    assert source.scan_calls == [()]


def test_with_a_where_only_the_where_term_is_negotiated():
    """Oracle: `(2, 2)`, `(5, 1)`."""
    source = _AcceptEverythingSource()
    bound = _bind("SELECT b, count(*) FROM gadgets WHERE a = 1 GROUP BY b HAVING b > 1")
    tree = optimize(plan(bound, Path("/nonexistent"), tables={"gadgets": lambda repo: source}))
    assert source.offered == [bound.where]
    assert list(_scan_of(tree).pushed()) == [bound.where]
    assert sorted(tree.rows()) == [(2, 2), (5, 1)]


def test_a_moved_term_the_scan_would_reject_is_not_offered_either():
    source = _RecordingGadgetSource()
    tree = _optimized("SELECT b FROM gadgets GROUP BY b HAVING b > 1 AND count(*) > 0", source)
    assert source.offered == []
    assert _scan_of(tree).pushed() == ()
    assert sorted(tree.rows()) == [(2,), (5,)]


# --- Constant propagation in WHERE (#142) -------------------------------------
#
# `plan()` rewrites the `WHERE` before `optimize()` sees it, and the terms
# offered are the rewritten ones - the same objects the `Filter` keeps.


def test_the_rewritten_terms_are_the_ones_offered_and_kept():
    """`a > 0 AND a = 1`: the source `a = 1` is kept as bound, and `a >
    0` becomes `1 > 0` with the constant in `a`'s place. A source that
    accepts everything is offered exactly the `Filter`'s own terms, in
    order, and pushes both. Oracle: `2`, `5`, `NULL`, `2`."""
    source = _AcceptEverythingSource()
    bound = _bind("SELECT b FROM gadgets WHERE a > 0 AND a = 1")
    tree = optimize(plan(bound, Path("/nonexistent"), tables={"gadgets": lambda repo: source}))
    from historian.sql.walk import FixedColumnRef

    filter_terms = split_conjuncts(child_of(tree).predicate())
    assert len(source.offered) == 2
    assert all(offered is kept for offered, kept in zip(source.offered, filter_terms))
    rewritten, kept_source = source.offered
    assert kept_source is bound.where.right
    assert rewritten is not bound.where.left
    assert isinstance(rewritten.left, FixedColumnRef) and rewritten.left.value == 1
    assert rewritten.right is bound.where.left.right
    assert list(_scan_of(tree).pushed()) == [rewritten, kept_source]
    assert sorted(tree.rows(), key=repr) == sorted([(2,), (5,), (None,), (2,)], key=repr)


def test_a_rewritten_earlier_source_is_not_accepted():
    """`a = 1 AND a = 1`: the last source is used, the first becomes
    `1 = 1`, which is not `a = <literal>`, so the recording double
    accepts only the last."""
    source = _RecordingGadgetSource()
    bound = _bind("SELECT b FROM gadgets WHERE a = 1 AND a = 1")
    tree = optimize(plan(bound, Path("/nonexistent"), tables={"gadgets": lambda repo: source}))
    assert len(source.offered) == 2
    assert source.offered[0] is not bound.where.left
    assert source.accepted == [bound.where.right]
    assert list(_scan_of(tree).pushed()) == [bound.where.right]
    assert sorted(tree.rows(), key=repr) == sorted([(2,), (5,), (None,), (2,)], key=repr)
