"""Where evaluation stops early, against SQLite (issue #111, spec §3
"Expression evaluation").

Only an error is observable: every expression historian evaluates is
pure except `LIKE ... ESCAPE` with an escape that is not one
character, which raises `ESCAPE expression must be a single
character` in both engines. `ERR` below is that expression. A query
passes when the two engines agree on the outcome - the same rows, or
both raising that exact message.

The rule, measured against the oracle (sqlite3 module 3.45.1):

- Value context (a select-list item, an `ORDER BY`/`GROUP BY` key,
  an aggregate argument, any operand of a comparison, arithmetic,
  `||`, `IS`, `LIKE`, unary `+`/`-`, `IN` or `BETWEEN`): every operand
  of `AND`/`OR`/`NOT`/`BETWEEN` is evaluated.
- Condition context (the root of `WHERE` and `HAVING`, and operands of
  `AND`/`OR`/`NOT` in condition context): `AND` stops after a `FALSE`
  left side, `OR` after a `TRUE` one, and a `NULL` left side stops
  `AND` where `NULL` counts as `FALSE` (an even number of `NOT`s
  above it) and `OR` where it counts as `TRUE` (an odd number).
  `BETWEEN` there is `x >= low AND x <= high`; `NOT BETWEEN` is
  `NOT (x BETWEEN ...)`.
- `IN` stops at the first element equal to the left side, in both
  contexts; a `NULL` never stops it.

Two parts: the queries the issue lists by hand, against `tiny`
through the real pipeline, and an exhaustive sweep over generated
formulas. The sweep loads SQLite once from `tiny`'s unfiltered scan
(the harness's step 1) and runs historian through `run_historian`
with a factory serving those same rows, so each of its ~14,000
queries costs one parse-to-rows pass and no `git blame`. Nothing is
pushed down for any sweep query (no leaf constrains `path`), so the
rows the scan serves are the rows the real scan would.

The sweep's leaves avoid `column = constant`. SQLite propagates such
a top-level `WHERE` conjunct into the other conjuncts and folds what
becomes constant (`WHERE NOT (line_no = 5 AND ERR) AND line_no = 5`
raises in SQLite: `5 = 5` folds away and `ERR` is left), which is the
known constant-folding difference (`_docs/decisions.md`, 2026-09-25,
#51), not evaluation order. Measured: with `=` leaves, 36 of 22,050
generated queries differ from the evaluation-order model for exactly
that reason, and with the leaves below none do.
"""

from __future__ import annotations

import itertools
import os
import sqlite3
from collections.abc import Iterator

import pytest

from historian.catalog import SCAN_FACTORIES
from historian.exec.expression import EvalError
from historian.tables.blame import BLAME_SCHEMA, BlameScan

from differential.conftest import assert_rows_match, load_unfiltered, run_historian, scan_all_rows

ESCAPE_MESSAGE = "ESCAPE expression must be a single character"

ERR = "path LIKE 'a' ESCAPE 'ab'"
AGG_ERR = "max(path) LIKE 'a' ESCAPE 'ab'"


def _sqlite_outcome(conn: sqlite3.Connection, query: str):
    """`(rows, None)`, or `(None, message)` when SQLite raises."""
    try:
        return conn.execute(query).fetchall(), None
    except sqlite3.OperationalError as error:
        return None, str(error)


def _compare(conn, repo, query: str, tables) -> tuple[str, str | None]:
    """Runs *query* on both engines: `(outcome, problem)`, *outcome*
    being SQLite's - `"error"` or `"rows"` - and *problem* `None` when
    historian agrees, a description otherwise. Only the `ESCAPE` error
    is accepted, so a query that is wrong for some other reason cannot
    pass by failing on both sides."""
    sqlite_rows, sqlite_error = _sqlite_outcome(conn, query)
    if sqlite_error is not None:
        if sqlite_error != ESCAPE_MESSAGE:
            return "error", f"oracle raised something else: {sqlite_error}\n  {query}"
        try:
            run_historian(query, repo, tables)
        except EvalError as error:
            if str(error) == sqlite_error:
                return "error", None
            return "error", f"historian raised {error!s}, sqlite {sqlite_error}\n  {query}"
        return "error", f"sqlite raised, historian did not\n  {query}"
    try:
        _, historian_rows = run_historian(query, repo, tables)
    except EvalError as error:
        return "rows", f"historian raised {error!s}, sqlite returned {sqlite_rows!r}\n  {query}"
    try:
        assert_rows_match(sqlite_rows, historian_rows)
    except AssertionError as error:
        return "rows", f"{error}\n  {query}"
    return "rows", None


def _assert_same_outcome(conn, repo, query: str, tables=SCAN_FACTORIES) -> str:
    """`_compare`, asserting agreement; returns SQLite's outcome, for
    callers that also pin which one it is."""
    outcome, problem = _compare(conn, repo, query, tables)
    assert problem is None, problem
    return outcome


def _assert_all_agree(conn, repo, queries: list[str], tables) -> None:
    """`_compare` over every query in one sweep group, reporting every
    disagreement at once rather than stopping at the first."""
    assert queries
    problems = [problem for _, problem in (_compare(conn, repo, q, tables) for q in queries) if problem]
    assert not problems, f"{len(problems)} of {len(queries)} disagree:\n" + "\n".join(problems[:20])


@pytest.fixture(scope="module")
def tiny_conn(tiny_repo):
    conn = load_unfiltered(BlameScan, tiny_repo, BLAME_SCHEMA, "blame")
    yield conn
    conn.close()


# --- The issue's own lists ----------------------------------------------
#
# Each case carries the outcome the issue recorded for SQLite (and
# `_assert_same_outcome` then checks historian against the oracle
# itself, not against this column). `tiny`'s `blame` rows are
# `feature/thing.py` 1, `src/utils.py` 1, `src/utils.py` 2.

_AND5 = f"(line_no = 5 AND {ERR})"

#: Value context: SQLite evaluates every operand and raises.
VALUE_CONTEXT_CASES = [
    f"SELECT path, {_AND5} FROM blame",
    f"SELECT path, (line_no >= 1 OR {ERR}) FROM blame",
    f"SELECT NOT {_AND5} FROM blame",
    f"SELECT 1 FROM blame ORDER BY {_AND5}",
    f"SELECT {_AND5} FROM blame ORDER BY 1",
    f"SELECT {_AND5} AS x FROM blame ORDER BY x",
    f"SELECT count(*) FROM blame GROUP BY {_AND5}",
    f"SELECT sum{_AND5} FROM blame",
    f"SELECT DISTINCT {_AND5} FROM blame",
    f"SELECT line_no + {_AND5} FROM blame",
    f"SELECT path FROM blame WHERE {_AND5} = 0",
    f"SELECT path FROM blame WHERE {_AND5} IS NOT NULL",
    f"SELECT path FROM blame WHERE {_AND5} + 0 = 0",
    f"SELECT path FROM blame WHERE {_AND5} || 'x' = '0x'",
    f"SELECT path FROM blame WHERE {_AND5} LIKE '0'",
    f"SELECT path FROM blame WHERE {_AND5} BETWEEN 0 AND 1",
    f"SELECT path FROM blame WHERE +{_AND5}",
    f"SELECT path FROM blame WHERE -{_AND5}",
    f"SELECT path FROM blame WHERE line_no IN (7, {_AND5})",
    "SELECT count(*) FROM blame GROUP BY path "
    f"HAVING (count(*) > 5 AND {AGG_ERR}) IS NOT NULL",
    f"SELECT line_no BETWEEN 10 AND ({ERR}) FROM blame",
]

#: Condition context: SQLite stops before `ERR` and returns rows.
CONDITION_CONTEXT_CASES = [
    f"SELECT path, line_no FROM blame WHERE line_no = NULL AND {ERR}",
    f"SELECT path, line_no FROM blame WHERE NOT (line_no = NULL OR {ERR})",
    f"SELECT path, line_no FROM blame WHERE NOT (NOT (line_no = NULL AND {ERR}))",
    f"SELECT path, line_no FROM blame WHERE (line_no = NULL AND line_no = 1) AND {ERR}",
    f"SELECT path FROM blame GROUP BY path HAVING max(line_no) = NULL AND {AGG_ERR}",
    f"SELECT path FROM blame GROUP BY path HAVING NOT (max(line_no) = NULL OR {AGG_ERR})",
    f"SELECT path, line_no FROM blame WHERE line_no BETWEEN 10 AND ({ERR})",
    f"SELECT path, line_no FROM blame WHERE line_no BETWEEN NULL AND ({ERR})",
    f"SELECT path, line_no FROM blame WHERE NOT (line_no BETWEEN 10 AND ({ERR}))",
    f"SELECT path, line_no FROM blame WHERE line_no IN (line_no, {ERR})",
    f"SELECT path, line_no FROM blame WHERE line_no NOT IN (line_no, {ERR})",
    f"SELECT path, line_no FROM blame WHERE NOT (line_no IN (line_no, {ERR}))",
    f"SELECT path, line_no FROM blame WHERE line_no IN (NULL, line_no, {ERR})",
    f"SELECT line_no IN (line_no, {ERR}) FROM blame",
    f"SELECT line_no NOT IN (line_no, {ERR}) FROM blame",
    f"SELECT line_no IN (1, {ERR}) FROM blame WHERE line_no = 1",
]

#: Both engines agreed before #111, and must keep agreeing.
ALREADY_AGREED_CASES = [
    (f"SELECT path FROM blame WHERE line_no = 5 AND {ERR}", "rows"),
    (f"SELECT path FROM blame WHERE line_no >= 1 OR {ERR}", "rows"),
    (f"SELECT path FROM blame WHERE NOT (line_no = 5 AND {ERR})", "rows"),
    (f"SELECT path FROM blame WHERE {ERR} AND line_no = 5", "error"),
    (f"SELECT path FROM blame WHERE line_no = NULL OR {ERR}", "error"),
    (f"SELECT path FROM blame WHERE NOT (line_no = NULL AND {ERR})", "error"),
    (f"SELECT path FROM blame WHERE line_no NOT BETWEEN 0 AND ({ERR})", "error"),
    (f"SELECT path FROM blame WHERE line_no IN (5, {ERR})", "error"),
    (f"SELECT path FROM blame WHERE NULL IN (1, {ERR})", "error"),
    (f"SELECT path FROM blame WHERE line_no IN (NULL, {ERR})", "error"),
    (f"SELECT path FROM blame WHERE line_no IN ({ERR})", "error"),
    (f"SELECT count(*) FROM blame GROUP BY path HAVING count(*) > 5 AND {AGG_ERR}", "rows"),
]

#: The `IN ()` and empty-input criteria.
EDGE_CASES = [
    # `IN ()` is FALSE (`NOT IN ()` TRUE) without evaluating the left.
    (f"SELECT ({ERR}) IN () FROM blame", "rows"),
    (f"SELECT ({ERR}) NOT IN () FROM blame", "rows"),
    (f"SELECT path FROM blame WHERE ({ERR}) IN ()", "rows"),
    (f"SELECT path FROM blame WHERE ({ERR}) NOT IN ()", "rows"),
    # A raising left side of a one-element `IN` still raises.
    (f"SELECT ({ERR}) IN (1) FROM blame", "error"),
    (f"SELECT path FROM blame WHERE ({ERR}) IN (1)", "error"),
    # Nothing evaluated, nothing observable.
    (f"SELECT {_AND5} FROM blame LIMIT 0", "rows"),
    (
        f"SELECT count(*) FROM blame WHERE line_no = 5 GROUP BY path "
        f"HAVING {AGG_ERR} AND count(*) > 5",
        "rows",
    ),
]


@pytest.mark.parametrize("query", VALUE_CONTEXT_CASES)
def test_value_context_evaluates_every_operand(tiny_repo, tiny_conn, query):
    assert _assert_same_outcome(tiny_conn, tiny_repo, query) == "error"


@pytest.mark.parametrize("query", CONDITION_CONTEXT_CASES)
def test_condition_context_stops_where_sqlite_does(tiny_repo, tiny_conn, query):
    assert _assert_same_outcome(tiny_conn, tiny_repo, query) == "rows"


@pytest.mark.parametrize("query, outcome", ALREADY_AGREED_CASES)
def test_cases_that_already_agreed_still_agree(tiny_repo, tiny_conn, query, outcome):
    assert _assert_same_outcome(tiny_conn, tiny_repo, query) == outcome


@pytest.mark.parametrize("query, outcome", EDGE_CASES)
def test_empty_in_list_and_empty_input(tiny_repo, tiny_conn, query, outcome):
    assert _assert_same_outcome(tiny_conn, tiny_repo, query) == outcome


def test_condition_context_results_are_nonempty_where_expected(tiny_repo, tiny_conn):
    """The condition-context list is not all "no rows": a query that
    returns nothing on both sides would pass even if historian silently
    dropped every row, so pin the three that keep rows."""
    expected_counts = {
        CONDITION_CONTEXT_CASES[8]: 3,  # NOT (line_no BETWEEN 10 AND ERR)
        CONDITION_CONTEXT_CASES[9]: 3,  # line_no IN (line_no, ERR)
        CONDITION_CONTEXT_CASES[12]: 3,  # line_no IN (NULL, line_no, ERR)
        CONDITION_CONTEXT_CASES[13]: 3,  # SELECT line_no IN (line_no, ERR)
        CONDITION_CONTEXT_CASES[15]: 2,  # SELECT line_no IN (1, ERR) ... WHERE line_no = 1
    }
    for query, count in expected_counts.items():
        assert len(tiny_conn.execute(query).fetchall()) == count, query
        assert len(run_historian(query, tiny_repo)[1]) == count, query


# --- The exhaustive sweep -----------------------------------------------
#
# Every formula built from AND, OR and NOT with up to 3 binary
# operators, NOT optional on every operator node and, for formulas of
# up to one operator, on every leaf too. (A NOT directly on a leaf is
# the same as another leaf - NOT TRUE is FALSE, NOT NULL is NULL, NOT
# ERR still raises - so it adds cases only where it is the whole
# formula's shape.) Leaves are TRUE, FALSE, NULL and ERR. Each formula
# runs in five placements: WHERE, HAVING (over aggregate leaves, so no
# term moves to WHERE - #141), the select list, ORDER BY and GROUP BY.
#
# The k = 3 part is 81,920 formulas, about 410,000 queries and five
# minutes of historian time, so it runs only when
# HISTORIAN_SWEEP_OPERATORS=3 is set; the default is 2 (11,560
# queries). See _docs/decisions.md (2026-10-01, #111) for the full
# k <= 3 run against both engines.

_SWEEP_OPERATORS = int(os.environ.get("HISTORIAN_SWEEP_OPERATORS", "2"))

ROW_LEAVES = {
    "T": "line_no >= 1",
    "F": "line_no > 5",
    "N": "line_no < NULL",
    "E": ERR,
}
AGG_LEAVES = {
    "T": "max(line_no) >= 1",
    "F": "max(line_no) > 5",
    "N": "max(line_no) < NULL",
    "E": AGG_ERR,
}


def _shapes(k: int) -> Iterator[object]:
    """Every binary tree with *k* operator nodes, leaves as `None`, in
    a fixed order."""
    if k == 0:
        yield None
        return
    for left_size in range(k):
        for left in _shapes(left_size):
            for right in _shapes(k - 1 - left_size):
                yield (left, right)


def _leaf_count(shape) -> int:
    return 1 if shape is None else _leaf_count(shape[0]) + _leaf_count(shape[1])


def _fill(shape, ops: Iterator[str], nots: Iterator[bool], leaves: Iterator[str], leaf_nots: Iterator[bool]):
    """A formula as nested tuples: `("L", name)`, `("NOT", f)`, or
    `(op, left, right)`, built from *shape* in preorder."""
    if shape is None:
        formula = ("L", next(leaves))
        return ("NOT", formula) if next(leaf_nots) else formula
    op = next(ops)
    negated = next(nots)
    left = _fill(shape[0], ops, nots, leaves, leaf_nots)
    right = _fill(shape[1], ops, nots, leaves, leaf_nots)
    formula = (op, left, right)
    return ("NOT", formula) if negated else formula


def _formula_groups(max_operators: int) -> Iterator[tuple[str, list[tuple]]]:
    """The sweep's formulas, grouped by everything but their leaves:
    one group per tree shape, operator choice and NOT placement on the
    operators, holding every leaf assignment (and, up to one operator,
    every NOT placement on the leaves) for that skeleton. A group is
    one test, so a failure names its skeleton and lists its formulas."""
    for k in range(max_operators + 1):
        for shape in _shapes(k):
            leaf_count = _leaf_count(shape)
            if k <= 1:
                leaf_not_choices = list(itertools.product([False, True], repeat=leaf_count))
            else:
                leaf_not_choices = [(False,) * leaf_count]
            for ops in itertools.product(["AND", "OR"], repeat=k):
                for nots in itertools.product([False, True], repeat=k):
                    formulas = [
                        _fill(shape, iter(ops), iter(nots), iter(leaves), iter(leaf_nots))
                        for leaves in itertools.product("TFNE", repeat=leaf_count)
                        for leaf_nots in leaf_not_choices
                    ]
                    skeleton = _fill(shape, iter(ops), iter(nots), iter("?" * leaf_count), iter((False,) * leaf_count))
                    yield f"k{k}-{_name(skeleton)}", formulas


def _sql(formula, leaves: dict[str, str]) -> str:
    if formula[0] == "L":
        return leaves[formula[1]]
    if formula[0] == "NOT":
        return f"NOT ({_sql(formula[1], leaves)})"
    return f"({_sql(formula[1], leaves)}) {formula[0]} ({_sql(formula[2], leaves)})"


def _name(formula) -> str:
    if formula[0] == "L":
        return formula[1]
    if formula[0] == "NOT":
        return f"NOT({_name(formula[1])})"
    return f"{formula[0]}({_name(formula[1])},{_name(formula[2])})"


_PLACEMENTS = {
    "where": lambda f: f"SELECT path, line_no FROM blame WHERE {_sql(f, ROW_LEAVES)}",
    "having": lambda f: f"SELECT path FROM blame GROUP BY path HAVING {_sql(f, AGG_LEAVES)}",
    "select": lambda f: f"SELECT {_sql(f, ROW_LEAVES)} FROM blame",
    "orderby": lambda f: f"SELECT path, line_no FROM blame ORDER BY {_sql(f, ROW_LEAVES)}",
    "groupby": lambda f: f"SELECT count(*) FROM blame GROUP BY {_sql(f, ROW_LEAVES)}",
}

_SWEEP_GROUPS = [
    pytest.param([_PLACEMENTS[placement](f) for f in formulas], id=f"{placement}-{name}")
    for name, formulas in _formula_groups(_SWEEP_OPERATORS)
    for placement in _PLACEMENTS
]


def _in_between_groups() -> list:
    """`x IN (a[, b[, c]])` and `NOT IN`, the left side from the leaf
    set and each element from the leaf set or the left side itself
    (`X`), grouped by placement, operator, list length and left side;
    `x BETWEEN low AND high` and `NOT BETWEEN`, each of the three from
    the leaf set, grouped by placement, operator and operand. Each in
    WHERE and in the select list."""
    placements = {
        "where": lambda expr: f"SELECT path, line_no FROM blame WHERE {expr}",
        "select": lambda expr: f"SELECT {expr} FROM blame",
    }
    groups = []
    for placement, make_query in placements.items():
        for op in ("IN", "NOT IN"):
            for length in (1, 2, 3):
                for left in "TFNE":
                    queries = []
                    for elements in itertools.product("TFNEX", repeat=length):
                        items = ", ".join(ROW_LEAVES[left if e == "X" else e] for e in elements)
                        queries.append(make_query(f"({ROW_LEAVES[left]}) {op} ({items})"))
                    groups.append(pytest.param(queries, id=f"{placement}-{left} {op} [{length}]"))
        for op in ("BETWEEN", "NOT BETWEEN"):
            for operand in "TFNE":
                queries = [
                    make_query(f"({ROW_LEAVES[operand]}) {op} ({ROW_LEAVES[low]}) AND ({ROW_LEAVES[high]})")
                    for low, high in itertools.product("TFNE", repeat=2)
                ]
                groups.append(pytest.param(queries, id=f"{placement}-{operand} {op}"))
    return groups


_IN_BETWEEN_GROUPS = _in_between_groups()


@pytest.fixture(scope="module")
def tiny_rows(tiny_repo):
    return scan_all_rows(BlameScan, tiny_repo)


@pytest.fixture(scope="module")
def cached_tables(tiny_rows):
    """`run_historian`'s `tables`, serving `tiny`'s unfiltered rows from
    memory. No capabilities, so nothing is pushed and the planner's
    `Filter` sees every row, exactly as with the real scan."""

    class _CachedBlame:
        schema = BLAME_SCHEMA

        def __init__(self, repo) -> None:
            pass

        def capabilities(self) -> set[str]:
            return set()

        def scan(self, pushed=()):
            assert not pushed
            yield from tiny_rows

    return {"blame": _CachedBlame}


def test_sweep_cached_rows_are_tinys_real_rows(tiny_repo, tiny_rows, cached_tables):
    """The cached source is a stand-in for `BlameScan` and nothing
    else: the same rows, through the same pipeline."""
    query = "SELECT * FROM blame"
    assert run_historian(query, tiny_repo, cached_tables)[1] == run_historian(query, tiny_repo)[1]
    assert len(tiny_rows) == 3


def test_sweep_size():
    """The enumeration is what the docstring says it is: 2,312 formulas
    up to two operators (8 + 256 + 2,048), five placements each, and
    1,240 IN plus 128 BETWEEN expressions in two placements each."""
    formulas = sum(len(param.values[0]) for param in _SWEEP_GROUPS) // len(_PLACEMENTS)
    if _SWEEP_OPERATORS == 2:
        assert formulas == 2312
    in_between = sum(len(param.values[0]) for param in _IN_BETWEEN_GROUPS)
    assert in_between == 2 * (1240 + 128)


@pytest.mark.parametrize("queries", _SWEEP_GROUPS)
def test_and_or_not_sweep(tiny_repo, tiny_conn, cached_tables, queries):
    _assert_all_agree(tiny_conn, tiny_repo, queries, cached_tables)


@pytest.mark.parametrize("queries", _IN_BETWEEN_GROUPS)
def test_in_between_sweep(tiny_repo, tiny_conn, cached_tables, queries):
    _assert_all_agree(tiny_conn, tiny_repo, queries, cached_tables)
