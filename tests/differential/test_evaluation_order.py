"""Where evaluation stops early, against SQLite (issue #111, spec §3
"Expression evaluation").

Only an error is observable: every expression historian evaluates is
pure except `LIKE ... ESCAPE` with an escape that is not one
character, which raises `ESCAPE expression must be a single
character` in both engines. `ERR` below is that expression. A query
passes when the two engines agree on the outcome - the same rows, or
both raising that exact message.

The rule, measured against the oracle:

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
with a factory serving those same rows, so each of its ~34,000
queries costs one parse-to-rows pass and no `git blame`. Nothing is
pushed down for any sweep query (the factory declares no
capabilities, and the `HAVING` leaves that constrain `path` move below
the aggregate, where nothing is negotiated - #141), so the rows the
scan serves are the rows the real scan would.

The main placements' leaves avoid `column = constant`: SQLite
propagates a top-level `WHERE` conjunct of that form into the other
conjuncts (#142, spec §3 "Constant propagation in `WHERE`"), which is
a rewrite, not evaluation order. The `propagated` placement covers it:
`WHERE NOT (<formula>) AND line_no = 5`, over leaves that compare
`line_no`, so every `line_no` in the formula becomes `5` (`WHERE NOT
(line_no = 5 AND ERR) AND line_no = 5` raises: `5 = 5` is `TRUE` and
`ERR` runs). The whole `NOT (...)` is one conjunct and contains `path`
through `ERR`, or else only comparisons that are per row in both
engines, so no conjunct is left with no column. What remains is a
conjunct that does become constant - `WHERE ERR AND line_no = 1 AND
line_no = 2`, or `line_no > 5 AND line_no = 5` - which SQLite decides
before any row (#171, #180).
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
    # `IN ()` is FALSE (`NOT IN ()` TRUE) without evaluating the left in
    # condition context (the WHERE cases below) but, on the pinned
    # oracle, with evaluating it in value context: the select-list
    # cases raise. "rows" here was a SQLite 3.45.1 artefact (#117).
    (f"SELECT ({ERR}) IN () FROM blame", "error"),
    (f"SELECT ({ERR}) NOT IN () FROM blame", "error"),
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
# runs in six placements: WHERE, HAVING over aggregate leaves (no term
# moves below the aggregate), HAVING over key-only leaves (every
# top-level term moves - #141), the select list, ORDER BY and GROUP BY.
#
# A seventh placement, `having_mixed`, is HAVING with each leaf
# independently a key-only leaf or the aggregate leaf of the same truth
# value, so moved and kept terms interleave (#141). Its alphabet is
# eight leaves, not four, so it stays at two operators even when
# HISTORIAN_SWEEP_OPERATORS=3 (8^4 leaf assignments per skeleton would
# make the k = 3 part about 1.3 million queries).
#
# The k = 3 part is 81,920 formulas, about 490,000 queries and several
# minutes of historian time, so it runs only when
# HISTORIAN_SWEEP_OPERATORS=3 is set; the default is 2 (13,872
# queries, plus 17,424 for `having_mixed`). See _docs/decisions.md (2026-10-01, #111) for the full
# k <= 3 run against both engines.
#
# An eighth placement, `propagated` (#142), is `WHERE NOT (<formula>) AND
# line_no = 5` over seven leaves comparing `line_no` (and `ERR`), so
# SQLite's constant propagation replaces every `line_no` in the formula
# with `5`. Its alphabet is seven leaves: 11,774 queries up to two
# operators, and 768,320 more for k = 3 - but each runs over three rows
# with nothing to group, so the k = 3 part is about four minutes and
# it is not capped.
#
# Two more, `where_literal` and `having_literal` (#189), add the
# integer literals `1` and `0` to the leaves, which SQLite simplifies a
# condition `AND`/`OR` by: 7,500 formulas each up to two operators,
# 414,720 more each at three. See `_LITERAL_PLACEMENTS`.

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
#: Leaves over the `GROUP BY path` key only: a term made of them has no
#: aggregate and no other column, so it moves below the aggregate.
KEY_LEAVES = {
    "T": "path >= ''",
    "F": "path > 'zzzz'",
    "N": "path < NULL",
    "E": ERR,
}
#: `propagated`'s alphabet (#142): each `line_no` leaf becomes a
#: comparison of `5` once `line_no = 5` is propagated into the formula -
#: `Q` and `G` and `T` true, `D` and `L` false, `N` NULL - while over a
#: row of `tiny` (`line_no` 1 or 2) they are what they say: `Q`, `D`,
#: `G`, `L`, `T`, `N` are `line_no = 5`, `<> 5`, `> 4`, `< 5`, `>= 1`,
#: `< NULL`.
PROPAGATED_LEAVES = {
    "Q": "line_no = 5",
    "D": "line_no <> 5",
    "G": "line_no > 4",
    "L": "line_no < 5",
    "T": "line_no >= 1",
    "N": "line_no < NULL",
    "E": ERR,
}
#: The `literal` placements' alphabets (#189): the row or aggregate
#: leaves and the integer literals `1` and `0`, which an `AND`/`OR` in a
#: condition is simplified by (`ERR OR 1` never runs `ERR`).
LITERAL_ROW_LEAVES = {**ROW_LEAVES, "1": "1", "0": "0"}
LITERAL_AGG_LEAVES = {**AGG_LEAVES, "1": "1", "0": "0"}
#: `having_mixed`'s alphabet: upper case a key-only leaf, lower case the
#: aggregate leaf with the same truth value.
MIXED_LEAVES = {
    **KEY_LEAVES,
    "t": AGG_LEAVES["T"],
    "f": AGG_LEAVES["F"],
    "n": AGG_LEAVES["N"],
    "e": AGG_LEAVES["E"],
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


def _formula_groups(max_operators: int, alphabet: str = "TFNE") -> Iterator[tuple[str, list[tuple]]]:
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
                        for leaves in itertools.product(alphabet, repeat=leaf_count)
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
    "having_keys": lambda f: f"SELECT path FROM blame GROUP BY path HAVING {_sql(f, KEY_LEAVES)}",
    "select": lambda f: f"SELECT {_sql(f, ROW_LEAVES)} FROM blame",
    "orderby": lambda f: f"SELECT path, line_no FROM blame ORDER BY {_sql(f, ROW_LEAVES)}",
    "groupby": lambda f: f"SELECT count(*) FROM blame GROUP BY {_sql(f, ROW_LEAVES)}",
}

_SWEEP_GROUPS = [
    pytest.param([_PLACEMENTS[placement](f) for f in formulas], id=f"{placement}-{name}")
    for name, formulas in _formula_groups(_SWEEP_OPERATORS)
    for placement in _PLACEMENTS
]

#: The most operators `having_mixed` goes to, whatever
#: HISTORIAN_SWEEP_OPERATORS says (see the comment above).
_HAVING_MIXED_OPERATORS = min(_SWEEP_OPERATORS, 2)

_HAVING_MIXED_GROUPS = [
    pytest.param(
        [f"SELECT path FROM blame GROUP BY path HAVING {_sql(f, MIXED_LEAVES)}" for f in formulas],
        id=f"having_mixed-{name}",
    )
    for name, formulas in _formula_groups(_HAVING_MIXED_OPERATORS, alphabet="TFNEtfne")
]


_PROPAGATED_GROUPS = [
    pytest.param(
        [f"SELECT path, line_no FROM blame WHERE NOT ({_sql(f, PROPAGATED_LEAVES)}) AND line_no = 5" for f in formulas],
        id=f"propagated-{name}",
    )
    for name, formulas in _formula_groups(_SWEEP_OPERATORS, alphabet="QDGLTNE")
]


#: The `literal` placements (#189), over six leaves: four plus `1` and
#: `0`. `where_literal` is `WHERE (<formula>) OR 0`, so the whole
#: `WHERE` is one term: a top-level `AND` of the formula is not split,
#: and a term with no column (a formula of literals only) never sits
#: beside a per-row term that raises - that order is #171's. (The `OR
#: 0` is itself simplified away; it changes no row.) `having_literal`
#: is `HAVING <formula>` over the aggregate leaves: `HAVING` is one
#: condition, and a column-free term moves below the aggregate (#141),
#: where it is the `WHERE`'s only term.
_LITERAL_PLACEMENTS = {
    "where_literal": lambda f: f"SELECT path, line_no FROM blame WHERE ({_sql(f, LITERAL_ROW_LEAVES)}) OR 0",
    "having_literal": lambda f: f"SELECT path FROM blame GROUP BY path HAVING {_sql(f, LITERAL_AGG_LEAVES)}",
}

_LITERAL_GROUPS = [
    pytest.param([_LITERAL_PLACEMENTS[placement](f) for f in formulas], id=f"{placement}-{name}")
    for name, formulas in _formula_groups(_SWEEP_OPERATORS, alphabet="TFNE10")
    for placement in _LITERAL_PLACEMENTS
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
    up to two operators (8 + 256 + 2,048), six placements each;
    17,424 `having_mixed` formulas up to two operators (16 + 1,024 +
    16,384: eight leaves, leaf NOTs up to one operator); and 1,240 IN
    plus 128 BETWEEN expressions in two placements each."""
    assert len(_PLACEMENTS) == 6
    formulas = sum(len(param.values[0]) for param in _SWEEP_GROUPS) // len(_PLACEMENTS)
    if _SWEEP_OPERATORS == 2:
        assert formulas == 2312
    if _SWEEP_OPERATORS >= 2:
        assert len(_HAVING_MIXED_GROUPS) == 1 + 4 + 32
        assert sum(len(param.values[0]) for param in _HAVING_MIXED_GROUPS) == 17424
    in_between = sum(len(param.values[0]) for param in _IN_BETWEEN_GROUPS)
    assert in_between == 2 * (1240 + 128)


def test_propagated_sweep_size():
    """`propagated` (#142): seven leaves - 14 + 784 + 10,976 formulas up
    to two operators (leaf NOTs up to one operator), in 1 + 4 + 32
    groups, and 5 * 8 * 8 * 7**4 = 768,320 more in 320 groups at three."""
    sizes = {2: (1 + 4 + 32, 14 + 784 + 10976), 3: (1 + 4 + 32 + 320, 14 + 784 + 10976 + 768320)}
    if _SWEEP_OPERATORS in sizes:
        groups, queries = sizes[_SWEEP_OPERATORS]
        assert len(_PROPAGATED_GROUPS) == groups
        assert sum(len(param.values[0]) for param in _PROPAGATED_GROUPS) == queries


@pytest.mark.parametrize("queries", _SWEEP_GROUPS)
def test_and_or_not_sweep(tiny_repo, tiny_conn, cached_tables, queries):
    _assert_all_agree(tiny_conn, tiny_repo, queries, cached_tables)


@pytest.mark.parametrize("queries", _HAVING_MIXED_GROUPS)
def test_having_mixed_sweep(tiny_repo, tiny_conn, cached_tables, queries):
    _assert_all_agree(tiny_conn, tiny_repo, queries, cached_tables)


@pytest.mark.parametrize("queries", _PROPAGATED_GROUPS)
def test_propagated_sweep(tiny_repo, tiny_conn, cached_tables, queries):
    _assert_all_agree(tiny_conn, tiny_repo, queries, cached_tables)


def test_literal_sweep_size():
    """`where_literal` and `having_literal` (#189): six leaves - 12 + 576
    + 6,912 formulas up to two operators (leaf NOTs up to one
    operator), in 1 + 4 + 32 groups per placement, and 5 * 8 * 8 * 6**4
    = 414,720 more in 320 groups at three."""
    sizes = {2: (1 + 4 + 32, 12 + 576 + 6912), 3: (1 + 4 + 32 + 320, 12 + 576 + 6912 + 414720)}
    if _SWEEP_OPERATORS in sizes:
        groups, formulas = sizes[_SWEEP_OPERATORS]
        assert len(_LITERAL_GROUPS) == 2 * groups
        assert sum(len(param.values[0]) for param in _LITERAL_GROUPS) == 2 * formulas


def test_literal_sweep_reaches_both_outcomes(tiny_repo, tiny_conn):
    """The literal placements are not all one outcome: `ERR OR 1` keeps
    every row where `ERR OR 0` raises, in both engines."""
    keeps = _LITERAL_PLACEMENTS["where_literal"](("OR", ("L", "E"), ("L", "1")))
    raises = _LITERAL_PLACEMENTS["where_literal"](("OR", ("L", "E"), ("L", "0")))
    assert _assert_same_outcome(tiny_conn, tiny_repo, keeps) == "rows"
    assert len(tiny_conn.execute(keeps).fetchall()) == 3
    assert _assert_same_outcome(tiny_conn, tiny_repo, raises) == "error"
    group_none = _LITERAL_PLACEMENTS["having_literal"](("AND", ("L", "E"), ("L", "0")))
    assert _assert_same_outcome(tiny_conn, tiny_repo, group_none) == "rows"


@pytest.mark.parametrize("queries", _LITERAL_GROUPS)
def test_literal_sweep(tiny_repo, tiny_conn, cached_tables, queries):
    _assert_all_agree(tiny_conn, tiny_repo, queries, cached_tables)


@pytest.mark.parametrize("queries", _IN_BETWEEN_GROUPS)
def test_in_between_sweep(tiny_repo, tiny_conn, cached_tables, queries):
    _assert_all_agree(tiny_conn, tiny_repo, queries, cached_tables)
