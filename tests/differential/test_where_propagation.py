"""Constant propagation in `WHERE`, against SQLite (issue #142, spec §3
"Constant propagation in `WHERE`").

SQLite rewrites a `WHERE` before it runs: a top-level conjunct
`column = constant` is a source, and every other occurrence of that
column in the `WHERE` becomes the constant, carrying the column's
affinity. The rewrite never changes which rows pass, only which
sub-expressions get evaluated, so what is observable is an error: the
`ESCAPE` error is the one runtime error there is.

`ERR` is `path LIKE 'a' ESCAPE 'ab'` and `ERRA` the same over
`author_name`. A guard such as `NOT (line_no = 5 AND ERRA)` raises per
row only once `line_no` has become `5`: no row of `tiny` has `line_no`
5, so without the rewrite `line_no = 5` is `FALSE` and the `AND` stops
before `ERRA`. Every guard keeps a column (`ERR`, `ERRA`), so it stays
a per-row term after the rewrite - a conjunct with no column left is
#171/#180, and no case here has one.

Each case pins SQLite's own outcome (`ERROR` for `ESCAPE expression
must be a single character`, or the exact rows) before checking
historian against the oracle, so a case cannot pass by both engines
failing for another reason. Every outcome was measured with the
pinned oracle (Python `sqlite3` 3.50.4, #117) through this harness's
own loader. `tiny`'s `blame` rows are `feature/thing.py` 1,
`src/utils.py` 1 and `src/utils.py` 2.

A shape with `path = constant` as a top-level `WHERE` conjunct runs
through an in-memory factory with no capabilities: the real
`BlameScan` pushes it, blames nothing for `'zzz'`, and so hides the
error SQLite raises (#172).

Two source forms the issue lists have no test because the v1 grammar
has no such syntax: `line_no == 5` and a table alias (`FROM blame AS
b`). Both are parse errors in historian today.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence

import pytest

from historian.catalog import SCAN_FACTORIES
from historian.exec.expression import EvalError
from historian.schema import Column, ColumnType, Row, Schema
from historian.tables.blame import BLAME_SCHEMA, BlameScan

from differential.conftest import (
    assert_rows_match,
    create_table_sql,
    load_table_sql,
    load_unfiltered,
    run_historian,
    scan_all_rows,
)

ESCAPE_MESSAGE = "ESCAPE expression must be a single character"
ERROR = "error"

ERR = "path LIKE 'a' ESCAPE 'ab'"
ERRA = "author_name LIKE 'a' ESCAPE 'ab'"
S = "SELECT path FROM blame WHERE"


def _check(conn, repo, query: str, expected, tables=SCAN_FACTORIES, catalog=None, *, key_positions=None) -> None:
    """SQLite's outcome for *query* is *expected* - `ERROR` or a list
    of rows - and historian's agrees with it. With *key_positions*
    the rows are compared in order (`ORDER BY`), ties tolerated."""
    try:
        sqlite_rows = conn.execute(query).fetchall()
        sqlite_outcome = sqlite_rows
    except sqlite3.OperationalError as error:
        assert str(error) == ESCAPE_MESSAGE, f"oracle raised something else: {error}\n  {query}"
        sqlite_outcome = ERROR
    assert sqlite_outcome == expected, f"the oracle no longer gives the pinned outcome\n  {query}"

    kwargs = {} if catalog is None else {"catalog": catalog}
    if expected == ERROR:
        with pytest.raises(EvalError) as raised:
            run_historian(query, repo, tables, **kwargs)
        assert str(raised.value) == ESCAPE_MESSAGE
        return
    _, historian_rows = run_historian(query, repo, tables, **kwargs)
    if key_positions is None:
        assert_rows_match(sqlite_rows, historian_rows)
    else:
        assert_rows_match(sqlite_rows, historian_rows, ordered=True, key_positions=key_positions)


@pytest.fixture(scope="module")
def tiny_conn(tiny_repo):
    conn = load_unfiltered(BlameScan, tiny_repo, BLAME_SCHEMA, "blame")
    yield conn
    conn.close()


@pytest.fixture(scope="module")
def cached_tables(tiny_repo):
    """`tiny`'s unfiltered rows from memory, no capabilities: nothing is
    pushed, so a `path = constant` source cannot hide an error (#172)."""
    rows = scan_all_rows(BlameScan, tiny_repo)

    class _CachedBlame:
        schema = BLAME_SCHEMA

        def __init__(self, repo) -> None:
            pass

        def capabilities(self) -> set[str]:
            return set()

        def scan(self, pushed=()) -> Iterator[tuple]:
            assert not pushed
            yield from rows

    return {"blame": _CachedBlame}


# --- Source forms: SQLite raises, historian returned no rows before -------

GUARD = f"NOT (line_no = 5 AND {ERR})"

SOURCE_FORM_CASES = [
    f"{S} {GUARD} AND line_no = 5",
    f"{S} {GUARD} AND 5 = line_no",
    f"{S} {GUARD} AND line_no IN (5)",
    f"{S} {GUARD} AND (line_no) = 5",
    f"{S} {GUARD} AND line_no = (5)",
    f"{S} {GUARD} AND ((line_no = 5))",
    f"{S} {GUARD} AND line_no = +5",
    f"{S} {GUARD} AND line_no = - -5",
    f"{S} {GUARD} AND line_no = +-+-5",
    f"{S} {GUARD} AND line_no = NULL",
    f"{S} {GUARD} AND blame.line_no = 5",
    f"{S} NOT (blame.line_no = 5 AND {ERR}) AND line_no = 5",
    f"{S} NOT (line_no = -5 AND {ERR}) AND line_no = -5",
]


@pytest.mark.parametrize("query", SOURCE_FORM_CASES)
def test_each_source_form_propagates_and_raises(tiny_repo, tiny_conn, query):
    _check(tiny_conn, tiny_repo, query, ERROR)


def test_a_minus_five_source_does_not_fire_a_five_guard(tiny_repo, tiny_conn):
    """`- -5` is `5`, so a `-5` guard is `5 = -5` after the rewrite."""
    _check(tiny_conn, tiny_repo, f"{S} NOT (line_no = -5 AND {ERR}) AND line_no = - -5", [])


# --- The source's position --------------------------------------------------

POSITION_CASES = [
    # The source first stops each row before the guard, as before.
    (f"{S} line_no = 5 AND {GUARD}", []),
    (f"{S} (line_no = 5) AND {GUARD}", []),
    # A term before the source is rewritten as well as one after it.
    (f"{S} {GUARD} AND line_no = 5", ERROR),
    (f"{S} NOT (line_no = 5 AND {ERRA}) AND line_no = 5 AND {GUARD}", ERROR),
    (f"{S} line_no >= 1 AND NOT (line_no = 5 AND {ERRA}) AND line_no = 5 AND path >= ''", ERROR),
    # Of two sources for one column the last is used and the first is
    # rewritten (to `5 = 5`, true), so the guard between them runs.
    (f"{S} line_no = 5 AND NOT (line_no = 5 AND {ERRA}) AND line_no = 5", ERROR),
    (f"{S} line_no = 5 AND NOT (line_no = 5 AND {ERRA}) AND line_no = 5.0", ERROR),
    (f"{S} line_no = 5.0 AND NOT (line_no = 5 AND {ERRA}) AND line_no = 5", ERROR),
    (f"{S} line_no = '5' AND NOT (line_no || 'x' = '5x' AND {ERRA}) AND line_no = 5", ERROR),
    (f"{S} line_no = 5 AND (NOT (line_no = 5 AND {ERRA}) AND line_no = 5)", ERROR),
    (f"{S} (line_no = 5 AND NOT (line_no = 5 AND {ERRA})) AND line_no = 5", ERROR),
    # Depth: the source inside nested parentheses and ANDs is found.
    (f"{S} {GUARD} AND ((line_no = 5) AND line_no > 0)", ERROR),
]


@pytest.mark.parametrize("query, expected", POSITION_CASES)
def test_the_source_position_and_depth(tiny_repo, tiny_conn, query, expected):
    _check(tiny_conn, tiny_repo, query, expected)


#: Two sources on different columns, both used; `path = constant` in
#: `WHERE`, so in memory (#172).
TWO_COLUMN_CASES = [
    (f"{S} NOT (line_no = 5 AND path = 'zzz' AND {ERRA}) AND line_no = 5 AND path = 'zzz'", ERROR),
    (f"{S} NOT (line_no = 1 AND NOT (path = 'q' AND {ERRA})) AND line_no = 1 AND path = 'q'", ERROR),
    (f"{S} path = 'src/utils.py' AND NOT (line_no = 1 AND path = 'src/utils.py' AND {ERRA})", ERROR),
    (f"{S} NOT (line = line_no AND {ERRA}) AND line = '5' AND line_no = 5", ERROR),
    (f"{S} NOT (line_no = line AND {ERRA}) AND line = '5' AND line_no = 5", ERROR),
]


@pytest.mark.parametrize("query, expected", TWO_COLUMN_CASES)
def test_sources_on_two_columns_are_both_used(tiny_repo, tiny_conn, cached_tables, query, expected):
    _check(tiny_conn, tiny_repo, query, expected, cached_tables)


# --- OR, NOT and arithmetic ---------------------------------------------------

OR_NOT_ARITHMETIC_CASES = [
    # `5 = 5` is TRUE, so the OR stops before ERRA (historian raised before).
    (f"{S} (line_no = 5 OR {ERRA}) AND line_no = 5", []),
    ("SELECT count(*) FROM blame WHERE (line_no = 1 OR " + ERR + ") AND line_no = 1", [(2,)]),
    ("SELECT count(*) FROM blame WHERE (line_no = 5 OR " + ERR + ") AND line_no = 5", [(0,)]),
    # `5 <> 5` is FALSE, so the OR goes on to ERRA.
    (f"{S} (line_no <> 5 OR NOT {ERRA}) AND line_no = 5", ERROR),
    (f"{S} NOT (line_no = 5 AND {ERRA}) AND line_no = 5 AND {ERR}", ERROR),
    # Inside an expression the column has no affinity: `5 + 0 = '5'`
    # and `5 || '' = 5` are FALSE.
    (f"{S} NOT (line_no + 0 = '5' AND {ERRA}) AND line_no = 5", []),
    (f"{S} NOT (line_no || '' = 5 AND {ERRA}) AND line_no = 5", []),
]


@pytest.mark.parametrize("query, expected", OR_NOT_ARITHMETIC_CASES)
def test_or_not_and_arithmetic(tiny_repo, tiny_conn, query, expected):
    _check(tiny_conn, tiny_repo, query, expected)


# --- Not sources --------------------------------------------------------------

GUARD_A = f"NOT (line_no = 5 AND {ERRA})"
ALL_ROWS = [("feature/thing.py",), ("src/utils.py",), ("src/utils.py",)]

NOT_SOURCE_CASES = [
    (f"{S} {GUARD_A} AND line_no IS 5", []),
    (f"{S} {GUARD_A} AND line_no <> 5", ALL_ROWS),
    (f"{S} {GUARD_A} AND NOT (line_no <> 5)", []),
    (f"{S} {GUARD_A} AND line_no BETWEEN 5 AND 5", []),
    (f"{S} {GUARD_A} AND line_no >= 5 AND line_no <= 5", []),
    (f"{S} {GUARD_A} AND line_no IN (5, 5)", []),
    (f"{S} {GUARD_A} AND line_no NOT IN (5)", ALL_ROWS),
    (f"{S} NOT (path = 'zzz' AND {ERRA}) AND path LIKE 'zzz'", []),
    (f"{S} {GUARD_A} AND +line_no = 5", []),
    (f"{S} {GUARD_A} AND line_no + 0 = 5", []),
    (f"{S} {GUARD_A} AND line_no = line_no", ALL_ROWS),
    (f"{S} {GUARD_A} AND line_no = line_no + 5", []),
    (f"{S} {GUARD_A} AND NOT (line_no = 5)", ALL_ROWS),
    (f"{S} {GUARD_A} AND (line_no = 5 OR line_no = 6)", []),
    (f"{S} {GUARD_A} AND line_no = 5 OR line_no = 6", []),
    # A source under NOT or OR is never a source.
    (f"{S} NOT (line_no = 5 AND NOT (line_no = 5 AND {ERR}))", ALL_ROWS),
    (f"{S} (line_no = 5 OR line_no = 6) AND {GUARD}", []),
]


@pytest.mark.parametrize("query, expected", NOT_SOURCE_CASES)
def test_terms_that_are_not_sources(tiny_repo, tiny_conn, query, expected):
    _check(tiny_conn, tiny_repo, query, expected)


# --- The replacement carries the column's affinity ---------------------------


def _guard(column: str, text: str) -> str:
    return f"NOT ({column} || 'x' = '{text}x' AND {ERRA})"


AFFINITY_CASES = [
    # INTEGER: the constant as the column would store it.
    (f"{S} {_guard('line_no', '5')} AND line_no = '05'", ERROR),
    (f"{S} {_guard('line_no', '5')} AND line_no = 5.0", ERROR),
    (f"{S} {_guard('line_no', '5')} AND line_no = ' 5'", ERROR),
    (f"{S} {_guard('line_no', '5')} AND line_no = '5.0'", ERROR),
    (f"{S} {_guard('line_no', '5')} AND line_no = '5e0'", ERROR),
    (f"{S} {_guard('line_no', '05')} AND line_no = '05'", []),
    (f"{S} {_guard('line_no', '5.0')} AND line_no = 5.0", []),
    (f"{S} {_guard('line_no', ' 5')} AND line_no = ' 5'", []),
    (f"{S} {_guard('line_no', '5.5')} AND line_no = 5.5", ERROR),
    (f"{S} {_guard('line_no', '5.5')} AND line_no = '5.5'", ERROR),
    (f"{S} {_guard('line_no', '0')} AND line_no = -0.0", ERROR),
    (f"{S} {_guard('line_no', '0.0')} AND line_no = -0.0", []),
    (f"{S} {_guard('line_no', '9223372036854775807')} AND line_no = 9223372036854775807", ERROR),
    (f"{S} {_guard('line_no', '9.22337203685478e+18')} AND line_no = 9223372036854775808", ERROR),
    (f"{S} {_guard('line_no', '-9223372036854775808')} AND line_no = -9223372036854775808", ERROR),
    # A whole REAL just inside the int64 range becomes an INTEGER. (The
    # boundary itself, `-9223372036854775808.0`, stays REAL in SQLite,
    # but historian reads that literal as the INTEGER int64 minimum
    # everywhere - a literal bug of its own, not propagation's - so it
    # is pinned at the unit level, `tests/test_expression.py`.)
    (f"{S} {_guard('line_no', '-9223372036854774784')} AND line_no = -9223372036854774784.0", ERROR),
    (f"{S} {_guard('line_no', '-9.22337203685477e+18')} AND line_no = -9223372036854774784.0", []),
    # A constant the affinity cannot convert stays as it is.
    (f"{S} {GUARD_A} AND line_no = 'x'", []),
    (f"{S} {_guard('line_no', 'x')} AND line_no = 'x'", ERROR),
    # TEXT.
    (f"{S} {_guard('line', '5')} AND line = 5", ERROR),
    (f"{S} {_guard('line', '5.0')} AND line = 5.0", ERROR),
    (f"{S} {_guard('line', '5')} AND line = 5.0", []),
    (f"{S} {_guard('line', '05')} AND line = '05'", ERROR),
]


@pytest.mark.parametrize("query, expected", AFFINITY_CASES)
def test_the_replacement_has_the_columns_affinity(tiny_repo, tiny_conn, query, expected):
    _check(tiny_conn, tiny_repo, query, expected)


#: The replaced operand keeps the column's affinity as an operand of a
#: comparison, `IN`'s left side, `BETWEEN` and `IS` - but an `IN` list
#: element never has affinity, and `LIKE` reads text either way.
COMPARISON_AFFINITY_CASES = [
    (f"{S} NOT (line_no > '4' AND {ERRA}) AND line_no = 5", ERROR),
    (f"{S} NOT (line_no >= '5' AND {ERRA}) AND line_no = 5", ERROR),
    (f"{S} NOT (line_no IN ('5') AND {ERRA}) AND line_no = 5", ERROR),
    (f"{S} NOT (line_no IN ('5', '6') AND {ERRA}) AND line_no = 5", ERROR),
    (f"{S} NOT (line_no BETWEEN '4' AND '6' AND {ERRA}) AND line_no = 5", ERROR),
    (f"{S} NOT (line_no IS '5' AND {ERRA}) AND line_no = 5", ERROR),
    (f"{S} NOT (line_no < '5' AND {ERRA}) AND line_no = 5", []),
    (f"{S} NOT ('4' < line_no AND {ERRA}) AND line_no = 5", ERROR),
    (f"{S} NOT ('5' = line_no AND {ERRA}) AND line_no = 5", ERROR),
    (f"{S} NOT ('5' IN (line_no) AND {ERRA}) AND line_no = 5", []),
    (f"{S} NOT (line_no LIKE '5' AND {ERRA}) AND line_no = 5", ERROR),
    (f"{S} NOT (line > 4 AND {ERRA}) AND line = '5'", ERROR),
    (f"{S} NOT (line < '10' AND {ERRA}) AND line = 5", []),
]


@pytest.mark.parametrize("query, expected", COMPARISON_AFFINITY_CASES)
def test_the_replacement_keeps_the_columns_affinity_in_comparisons(tiny_repo, tiny_conn, query, expected):
    _check(tiny_conn, tiny_repo, query, expected)


NULL_SOURCE_CASES = [
    (f"{S} NOT (line_no IS NULL AND {ERRA}) AND line_no = NULL", ERROR),
    (f"{S} NOT (line_no > 3 AND {ERRA}) AND line_no = NULL", ERROR),
    (f"{S} NOT (line_no IS NOT NULL AND {ERRA}) AND line_no = NULL", []),
]


@pytest.mark.parametrize("query, expected", NULL_SOURCE_CASES)
def test_a_null_source(tiny_repo, tiny_conn, query, expected):
    _check(tiny_conn, tiny_repo, query, expected)


# --- REAL and INTEGER columns, in memory --------------------------------------
#
# `t (s TEXT, i INTEGER, r REAL)` with no row whose `i` or `r` is 1 or
# 7, so a guard on those raises only when the column was replaced.
# SQLite is loaded into a plain `CREATE TABLE`, not the harness's
# `CAST` view (#140): through the view `r` is `CAST(raw.r AS REAL)`,
# not a column, and SQLite does not propagate it - pinned by
# `test_the_cast_view_hides_a_real_source_from_sqlite`. The rows here
# hold no `-0.0` and no `int` in `r`, the two things the view exists
# to keep.

T_SCHEMA = Schema(
    columns=(
        Column("s", ColumnType.TEXT),
        Column("i", ColumnType.INTEGER),
        Column("r", ColumnType.REAL),
    )
)
T_ROWS: list[Row] = [("a", 2, 2.5), ("b", 3, 3.5)]
ERRS = "s LIKE 'a' ESCAPE 'ab'"


class _TSource:
    schema = T_SCHEMA

    def __init__(self, repo) -> None:
        pass

    def capabilities(self) -> set[str]:
        return set()

    def scan(self, pushed: Sequence[object] = ()) -> Iterator[Row]:
        assert not pushed
        yield from T_ROWS


@pytest.fixture(scope="module")
def t_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute(create_table_sql("t", T_SCHEMA))
    conn.executemany("INSERT INTO t VALUES (?, ?, ?)", T_ROWS)
    yield conn
    conn.close()


def _t_guard(column: str, text: str) -> str:
    return f"NOT ({column} || 'x' = '{text}x' AND {ERRS})"


REAL_INTEGER_CASES = [
    (f"SELECT s FROM t WHERE {_t_guard('r', '1.0')} AND r = 1", ERROR),
    (f"SELECT s FROM t WHERE {_t_guard('r', '1.0')} AND r = '1'", ERROR),
    (f"SELECT s FROM t WHERE {_t_guard('r', '1.0')} AND r = 1.0", ERROR),
    (f"SELECT s FROM t WHERE {_t_guard('r', '1.0')} AND r = ' 1 '", ERROR),
    (f"SELECT s FROM t WHERE {_t_guard('r', '1')} AND r = 1", []),
    (f"SELECT s FROM t WHERE {_t_guard('r', 'y')} AND r = 'y'", ERROR),
    (f"SELECT s FROM t WHERE {_t_guard('r', '9.00719925474099e+15')} AND r = 9007199254740993", ERROR),
    (f"SELECT s FROM t WHERE NOT (r > '0.5' AND {ERRS}) AND r = 1", ERROR),
    (f"SELECT s FROM t WHERE NOT (r = 7 AND {ERRS}) AND r = 7", ERROR),
    (f"SELECT s FROM t WHERE {_t_guard('i', '1')} AND i = 1.0", ERROR),
    (f"SELECT s FROM t WHERE {_t_guard('i', '1')} AND i = '1.0'", ERROR),
    (f"SELECT s FROM t WHERE {_t_guard('i', '1.0')} AND i = 1.0", []),
    (f"SELECT s FROM t WHERE NOT (i = 7 AND {ERRS}) AND i = 7", ERROR),
    # Rows: the replacement is the value the column already has.
    ("SELECT r || 'x' FROM t WHERE r = 2.5 AND s >= ''", [("2.5x",)]),
    ("SELECT s FROM t WHERE r = 2.5 AND r || 'x' = '2.5x'", [("a",)]),
    ("SELECT s FROM t WHERE i = 3.0 AND i || 'x' = '3x'", [("b",)]),
    ("SELECT s FROM t WHERE i = 3.0 AND i || 'x' = '3.0x'", []),
]


@pytest.mark.parametrize("query, expected", REAL_INTEGER_CASES)
def test_real_and_integer_columns(tmp_path, t_conn, query, expected):
    _check(t_conn, tmp_path, query, expected, {"t": _TSource}, {"t": T_SCHEMA})


def test_the_cast_view_hides_a_real_source_from_sqlite():
    """Why the cases above load a plain table: the same query over the
    harness's `CAST` view (#140) does not raise, because SQLite only
    propagates a real column. Oracle-only."""
    query = f"SELECT s FROM t WHERE NOT (r = 7 AND {ERRS}) AND r = 7"
    conn = sqlite3.connect(":memory:")
    try:
        statements, insert_into = load_table_sql("t", T_SCHEMA)
        for statement in statements:
            conn.execute(statement)
        conn.executemany(f'INSERT INTO "{insert_into}" VALUES (?, ?, ?)', T_ROWS)
        assert conn.execute(query).fetchall() == []
    finally:
        conn.close()


# --- Rows, not only errors ----------------------------------------------------

ROW_CASES = [
    (f"{S} line_no = 1.0", [("feature/thing.py",), ("src/utils.py",)]),
    (f"{S} line_no = 1.0 AND line_no || 'x' = '1x'", [("feature/thing.py",), ("src/utils.py",)]),
    (f"{S} line_no = 1.0 AND line_no || 'x' = '1.0x'", []),
    (f"{S} line_no = 1.0 AND path >= ''", [("feature/thing.py",), ("src/utils.py",)]),
    ("SELECT line_no || 'x' FROM blame WHERE line_no = 1.0", [("1x",), ("1x",)]),
    ("SELECT line_no || 'x' FROM blame WHERE line_no = 1.0 AND path >= ''", [("1x",), ("1x",)]),
    ("SELECT max(line_no || 'x') FROM blame WHERE line_no = 1.0 AND path >= ''", [("1x",)]),
    (
        "SELECT line_no || 'x', count(*) FROM blame WHERE line_no = 1.0 AND path >= '' GROUP BY line_no || 'x'",
        [("1x", 2)],
    ),
    ("SELECT count(*) FROM blame WHERE line_no = 1", [(2,)]),
    ("SELECT count(*) FROM blame WHERE line_no = 1 AND line_no = 1.0", [(2,)]),
    (f"{S} line_no = 1 AND line_no = 1.0", [("feature/thing.py",), ("src/utils.py",)]),
    (f"{S} line_no = 1 AND line_no = '1'", [("feature/thing.py",), ("src/utils.py",)]),
    (f"{S} line_no = 1 AND line_no = 2", []),
    # The source-form queries with the guard removed, at 5 and at 1.
    (f"{S} line_no = 5", []),
    (f"{S} 5 = line_no", []),
    (f"{S} line_no IN (5)", []),
    (f"{S} line_no = +5", []),
    (f"{S} line_no = NULL", []),
    (f"{S} blame.line_no = 5", []),
    (f"{S} line_no = -5", []),
    (f"{S} line_no = 1 AND line_no >= 1", [("feature/thing.py",), ("src/utils.py",)]),
    (f"{S} 1 = line_no AND path >= ''", [("feature/thing.py",), ("src/utils.py",)]),
    (f"{S} line_no IN (1) AND line_no < 2", [("feature/thing.py",), ("src/utils.py",)]),
    (f"{S} line_no = 2 AND line_no + 0 = 2 AND line_no IN (1, 2)", [("src/utils.py",)]),
]


@pytest.mark.parametrize("query, expected", ROW_CASES)
def test_rows_are_unchanged_by_the_rewrite(tiny_repo, tiny_conn, query, expected):
    _check(tiny_conn, tiny_repo, query, expected)


def test_order_by_over_a_propagated_column(tiny_repo, tiny_conn):
    _check(
        tiny_conn,
        tiny_repo,
        "SELECT line_no FROM blame WHERE line_no = 1.0 AND path >= '' ORDER BY line_no || 'y'",
        [(1,), (1,)],
        key_positions=(0,),
    )


# --- Empty results and clause interplay -------------------------------------

CLAUSE_CASES = [
    (f"SELECT count(*) FROM blame WHERE {GUARD} AND line_no = 5", ERROR),
    (f"SELECT count(*) FROM blame WHERE {GUARD} AND line_no = 5 GROUP BY path", ERROR),
    ("SELECT count(*) FROM blame WHERE line_no > 5 AND path >= ''", [(0,)]),
    (f"SELECT DISTINCT path FROM blame WHERE {GUARD} AND line_no = 5", ERROR),
    (f"{S} {GUARD} AND line_no = 5 ORDER BY path", ERROR),
    (f"{S} {GUARD} AND line_no = 5 LIMIT 0", []),
    (f"{S} {GUARD} AND line_no = 5 LIMIT 1", ERROR),
]


@pytest.mark.parametrize("query, expected", CLAUSE_CASES)
def test_empty_results_and_other_clauses(tiny_repo, tiny_conn, query, expected):
    _check(tiny_conn, tiny_repo, query, expected)


# --- HAVING is neither a source nor a target --------------------------------
#
# The issue's three shapes use `GROUP BY line_no` with `ERRA`, which
# historian rejects at bind time (`author_name` is not grouped); these
# group by `line_no, path` and use `ERR`, or an aggregate guard. Each
# returns no rows on the oracle.

HAVING_CASES = [
    f"SELECT count(*) FROM blame GROUP BY line_no, path HAVING {GUARD} AND line_no = 5",
    f"SELECT count(*) FROM blame WHERE {GUARD} GROUP BY line_no, path HAVING line_no = 5",
    f"SELECT count(*) FROM blame WHERE line_no = 5 GROUP BY line_no, path HAVING {GUARD}",
    f"SELECT count(*) FROM blame WHERE {GUARD} GROUP BY line_no HAVING line_no = 5",
    "SELECT count(*) FROM blame GROUP BY line_no "
    "HAVING NOT (line_no = 5 AND max(path) LIKE 'a' ESCAPE 'ab') AND line_no = 5",
    "SELECT count(*) FROM blame WHERE line_no = 5 GROUP BY line_no "
    "HAVING NOT (line_no = 5 AND max(path) LIKE 'a' ESCAPE 'ab')",
]


@pytest.mark.parametrize("query", HAVING_CASES)
def test_having_is_not_touched(tiny_repo, tiny_conn, query):
    _check(tiny_conn, tiny_repo, query, [])


# --- With the real scan, a predicate that is never pushed -----------------


def test_a_line_no_source_raises_through_the_real_scan(tiny_repo, tiny_conn):
    """`line_no` is never pushed, so the real `BlameScan` blames every
    path and the guard runs on every row."""
    _check(tiny_conn, tiny_repo, f"{S} {GUARD} AND line_no = 5", ERROR, SCAN_FACTORIES)


def test_the_real_scan_gives_the_same_rows_for_the_pushdown_shapes(tiny_repo, tiny_conn):
    """The shapes `tests/pushdown/test_blame_pushdown.py` counts the
    work of, compared on rows here."""
    for query, expected in [
        ("SELECT count(*) FROM blame WHERE path = 'src/utils.py' AND path LIKE 'src/%'", [(2,)]),
        ("SELECT count(*) FROM blame WHERE path = 'feature/thing.py' AND path = 'src/utils.py'", [(0,)]),
        ("SELECT count(*) FROM blame WHERE path = 'src/utils.py' AND path IN ('a', 'b')", [(0,)]),
    ]:
        _check(tiny_conn, tiny_repo, query, expected)
