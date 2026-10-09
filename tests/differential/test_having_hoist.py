"""`HAVING` terms that move below the aggregate, against SQLite (issue
#141, spec §3 "`HAVING` terms that move below the aggregate").

When a query has a `GROUP BY`, SQLite takes each `AND`-term of
`HAVING` that no group can disagree on - no aggregate call, and every
column reference inside a subexpression that matches a `GROUP BY` key
by shape - and runs it once per input row, in `WHERE`, after the
query's own `WHERE` terms. Only an error and the rows of a group that
the key hides are observable, so each case pins SQLite's own outcome
(`"error"` for `ESCAPE expression must be a single character`, or the
exact rows) before checking historian against the oracle: a case
cannot pass by both engines failing for some other reason.

`ERR` is `path LIKE 'a' ESCAPE 'ab'`, the one expression that raises
at run time, and `AGG_ERR` the same over `max(path)`. `G` is `SELECT
count(*) FROM blame GROUP BY path`. `tiny`'s `blame` rows are
`feature/thing.py` 1, `src/utils.py` 1 and `src/utils.py` 2. Every
outcome was measured with the pinned oracle (Python `sqlite3` 3.50.4,
#117) through this harness's own loader.

No term below is `column = constant` (SQLite propagates such a term
into the others, #142; the leaves use `>`, `>=`, `<` and `IS NULL`
instead), and outside `test_a_literal_zero_term_stays_in_having` no
term is a literal `0`: when this issue was groomed a literal `0`
answered differently on 3.45.1 and 3.50.4, and #117 has since pinned
3.50.4, where an integer literal `0` term stays in `HAVING` - see that
test.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

import pytest

from historian.catalog import SCAN_FACTORIES
from historian.exec.expression import EvalError
from historian.tables.blame import BLAME_SCHEMA, BlameScan

from differential.conftest import assert_rows_match, load_unfiltered, run_historian

ESCAPE_MESSAGE = "ESCAPE expression must be a single character"
ERROR = "error"

ERR = "path LIKE 'a' ESCAPE 'ab'"
AGG_ERR = "max(path) LIKE 'a' ESCAPE 'ab'"
G = "SELECT count(*) FROM blame GROUP BY path"


def _check(conn, repo, query: str, expected, tables=SCAN_FACTORIES, *, key_positions=None) -> None:
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

    if expected == ERROR:
        with pytest.raises(EvalError) as raised:
            run_historian(query, repo, tables)
        assert str(raised.value) == ESCAPE_MESSAGE
        return
    _, historian_rows = run_historian(query, repo, tables)
    if key_positions is None:
        assert_rows_match(sqlite_rows, historian_rows)
    else:
        assert_rows_match(sqlite_rows, historian_rows, ordered=True, key_positions=key_positions)


@pytest.fixture(scope="module")
def tiny_conn(tiny_repo):
    conn = load_unfiltered(BlameScan, tiny_repo, BLAME_SCHEMA, "blame")
    yield conn
    conn.close()


# --- Terms that move: SQLite raises, historian raised nothing before -----

#: Every `ERR`-bearing term here has no aggregate and only key columns,
#: so it moves and runs per row, where `count(*) > 5` would have
#: stopped it per group. Nested `AND`s and parentheses split too.
MOVED_TERM_CASES = [
    f"{G} HAVING count(*) > 5 AND {ERR}",
    f"{G} HAVING count(*) > 5 AND ({ERR}) = 0",
    f"{G} HAVING count(*) > 5 AND NOT ({ERR})",
    f"{G} HAVING count(*) > 5 AND ({ERR}) IS NOT NULL",
    f"{G} HAVING count(*) > 5 AND path IN ({ERR})",
    f"{G} HAVING count(*) > 5 AND (path || 'x') LIKE 'a' ESCAPE 'ab'",
    f"{G} HAVING count(*) > 5 AND ({ERR} AND count(*) > 1)",
    f"{G} HAVING (count(*) > 5 AND {ERR}) AND count(*) > 1",
    f"{G} HAVING count(*) > 5 AND (count(*) > 1 AND {ERR})",
    f"{G} HAVING ((count(*) > 5) AND ({ERR}))",
    f"{G} HAVING count(*) > 5 AND 1 AND {ERR}",
    f"{G} HAVING {ERR}",
]


@pytest.mark.parametrize("query", MOVED_TERM_CASES)
def test_a_term_over_keys_only_moves_and_raises(tiny_repo, tiny_conn, query):
    _check(tiny_conn, tiny_repo, query, ERROR)


#: Expression keys, an ordinal key, an aliased key and two keys: a
#: column counts as a key when the subexpression around it matches a
#: key by shape. (`GROUP BY path, line_no` uses `>=`, not `=`, #142.)
KEY_SHAPE_CASES = [
    "SELECT count(*) FROM blame GROUP BY path || 'x' "
    "HAVING count(*) > 5 AND ((path || 'x') || 'y') LIKE 'a' ESCAPE 'ab'",
    "SELECT count(*) FROM blame GROUP BY path + 0 HAVING count(*) > 5 AND (path + 0) LIKE 'a' ESCAPE 'ab'",
    f"SELECT path, count(*) FROM blame GROUP BY 1 HAVING count(*) > 5 AND {ERR}",
    "SELECT path AS p, count(*) FROM blame GROUP BY p HAVING count(*) > 5 AND p LIKE 'a' ESCAPE 'ab'",
    f"SELECT count(*) FROM blame GROUP BY path, line_no HAVING count(*) > 5 AND ({ERR} OR line_no >= 1)",
]


@pytest.mark.parametrize("query", KEY_SHAPE_CASES)
def test_expression_ordinal_alias_and_two_keys_move_terms(tiny_repo, tiny_conn, query):
    _check(tiny_conn, tiny_repo, query, ERROR)


# --- Terms that stay -----------------------------------------------------

STAYING_TERM_CASES = [
    # An aggregate anywhere in the term keeps it in HAVING, OR branches included.
    (f"{G} HAVING count(*) > 5 AND ({AGG_ERR})", []),
    (f"{G} HAVING count(*) > 5 AND ({ERR} OR count(*) > 1)", []),
    # An OR at the root is one term, with an aggregate in it.
    (f"{G} HAVING count(*) > 5 OR {ERR}", ERROR),
    (f"{G} HAVING {ERR} OR count(*) > 5", ERROR),
    # No GROUP BY: nothing moves, not even a constant.
    ("SELECT count(*) FROM blame HAVING count(*) > 100 AND 'a' LIKE 'a' ESCAPE 'ab'", []),
    ("SELECT count(*) FROM blame WHERE line_no < 0 HAVING count(*) > 100 AND 'a' LIKE 'a' ESCAPE 'ab'", []),
    # Constants that move and keep every row, or none.
    (f"{G} HAVING 1", [(1,), (2,)]),
    (f"{G} HAVING NULL", []),
]


@pytest.mark.parametrize("query, expected", STAYING_TERM_CASES)
def test_terms_that_must_not_move_still_agree(tiny_repo, tiny_conn, query, expected):
    _check(tiny_conn, tiny_repo, query, expected)


# --- Order: after every WHERE term, in HAVING order ----------------------

ORDER_CASES = [
    (f"SELECT count(*) FROM blame WHERE {ERR} GROUP BY path HAVING path > 'zzzz'", ERROR),
    (f"SELECT count(*) FROM blame WHERE line_no > 5 GROUP BY path HAVING {ERR} AND count(*) > 0", []),
    (f"SELECT count(*) FROM blame WHERE line_no < NULL GROUP BY path HAVING {ERR}", []),
    (f"{G} HAVING count(*) > 5 AND {ERR} AND path > 'zzzz'", ERROR),
    (f"{G} HAVING count(*) > 5 AND path > 'zzzz' AND {ERR}", []),
    (f"SELECT count(*) FROM blame WHERE line_no > 5 AND {ERR} GROUP BY path HAVING path > 'zzzz'", []),
]


@pytest.mark.parametrize("query, expected", ORDER_CASES)
def test_moved_terms_run_after_where_in_having_order(tiny_repo, tiny_conn, query, expected):
    _check(tiny_conn, tiny_repo, query, expected)


# --- Nothing reaches the term --------------------------------------------

NO_ROW_CASES = [
    # Every row fails a moved term: no group at all, not a row of zeros.
    "SELECT count(*) FROM blame GROUP BY path HAVING path > 'zzzz'",
    # An earlier moved term removes every row before `ERR` sees one.
    f"{G} HAVING path > 'zzzz' AND {ERR}",
    f"{G} HAVING path < NULL AND {ERR} AND count(*) > 0",
    # `NOT (a AND b)` is one term; it moves whole and stops at `a`.
    f"{G} HAVING count(*) > 5 AND NOT (path > 'zzzz' AND {ERR})",
]


@pytest.mark.parametrize("query", NO_ROW_CASES)
def test_a_moved_term_no_row_reaches_returns_no_rows(tiny_repo, tiny_conn, query):
    _check(tiny_conn, tiny_repo, query, [])


# --- DISTINCT, ORDER BY and LIMIT change nothing --------------------------

CLAUSE_CASES = [
    (f"SELECT DISTINCT count(*) FROM blame GROUP BY path HAVING count(*) > 5 AND {ERR}", ERROR),
    (f"SELECT DISTINCT path FROM blame GROUP BY path HAVING {ERR} AND count(*) > 5", ERROR),
    (f"SELECT DISTINCT path FROM blame GROUP BY path HAVING {ERR} AND count(*) > 5 ORDER BY path", ERROR),
    (f"SELECT DISTINCT path FROM blame GROUP BY path HAVING count(*) > 5 AND {ERR} ORDER BY path", ERROR),
    (f"{G} HAVING count(*) > 5 AND {ERR} ORDER BY path", ERROR),
    (f"{G} HAVING {ERR} LIMIT 0", []),
    (f"{G} HAVING count(*) > 5 AND {ERR} LIMIT 0", []),
]


@pytest.mark.parametrize("query, expected", CLAUSE_CASES)
def test_distinct_order_by_and_limit_change_nothing(tiny_repo, tiny_conn, query, expected):
    _check(tiny_conn, tiny_repo, query, expected)


def test_aggregate_slots_do_not_move(tiny_repo, tiny_conn):
    """A kept aggregate term beside a moved key term, with aggregates in
    the select list and `ORDER BY` too: the same rows as before, in
    order."""
    query = (
        "SELECT path, count(*), max(line_no) FROM blame GROUP BY path "
        "HAVING max(line_no) >= 1 AND path >= '' ORDER BY count(*) DESC"
    )
    _check(
        tiny_conn,
        tiny_repo,
        query,
        [("src/utils.py", 2, 2), ("feature/thing.py", 1, 1)],
        key_positions=(1,),
    )


def test_a_moved_term_is_never_pushed_into_the_scan(tiny_repo, tiny_conn):
    """Through the real `BlameScan`, which would accept `path LIKE
    'zzz%'` from `WHERE`. Pushed, it would blame nothing and `ERR`
    would never run; moved terms are not offered (#172)."""
    _check(tiny_conn, tiny_repo, f"{G} HAVING {ERR} AND path LIKE 'zzz%'", ERROR)


# --- A literal 0 term stays in HAVING (SQLite 3.50.4) ----------------------

#: Measured on the pinned oracle after #117: SQLite does not move a
#: term that is an integer literal `0` (`havingToWhereExprCb` skips an
#: always-false term), so `ERR` beside it still moves and raises,
#: whichever side the `0` is on. Any other constant moves, and a false
#: one in front of `ERR` stops it per row, as in `WHERE`. Behind `ERR`
#: it stops it too: a moved term with no column is decided before any
#: row (#171).
LITERAL_ZERO_CASES = [
    (f"{G} HAVING 0 AND {ERR}", ERROR),
    (f"{G} HAVING {ERR} AND 0", ERROR),
    (f"{G} HAVING (0) AND {ERR}", ERROR),
    (f"{G} HAVING 00 AND {ERR}", ERROR),
    (f"{G} HAVING count(*) > 5 AND 0 AND {ERR}", ERROR),
    (f"{G} HAVING count(*) > 5 AND {ERR} AND 0", ERROR),
    (f"SELECT path FROM blame GROUP BY path HAVING 0 AND {ERR}", ERROR),
    (f"{G} HAVING 0.0 AND {ERR}", []),
    (f"{G} HAVING -0 AND {ERR}", []),
    (f"{G} HAVING NULL AND {ERR}", []),
    (f"{G} HAVING 1 > 2 AND {ERR}", []),
    (f"{G} HAVING {ERR} AND 0.0", []),
    (f"{G} HAVING {ERR} AND -0", []),
    (f"{G} HAVING {ERR} AND NULL", []),
    (f"{G} HAVING {ERR} AND 1 > 2", []),
    (f"{G} HAVING count(*) > 5 AND {ERR} AND 0.0", []),
]


@pytest.mark.parametrize("query, expected", LITERAL_ZERO_CASES)
def test_a_literal_zero_term_stays_in_having(tiny_repo, tiny_conn, query, expected):
    _check(tiny_conn, tiny_repo, query, expected)


# --- In-memory rows: where the per-row answer differs ----------------------


def _blame_row(path, line_no, line) -> tuple:
    return (path, line_no, line, "c" * 40, "Ana", "ana@example.com", "2024-01-01T00:00:00Z")


def _fixed_tables(rows):
    """`run_historian`'s `tables`, serving *rows* with no capabilities,
    as the sweep's `cached_tables` does."""

    class _FixedBlame:
        schema = BLAME_SCHEMA

        def __init__(self, repo) -> None:
            pass

        def capabilities(self) -> set[str]:
            return set()

        def scan(self, pushed=()) -> Iterator[tuple]:
            assert not pushed
            yield from rows

    return {"blame": _FixedBlame}


#: `GROUP BY line + 0` puts `'1'` and `'1.0'` in one group, and a
#: moved term over the key sees each row's own `line`, so it keeps
#: part of the group: `count`/`sum` see only the rows that passed.
#: Filtering the whole group would give `(3, 6)` and no rows.
LINE_PLUS_ZERO_ROWS = [_blame_row("f", 1, "1"), _blame_row("f", 2, "1.0"), _blame_row("f", 3, "1")]

LINE_PLUS_ZERO_CASES = [
    ("SELECT count(*), sum(line_no) FROM blame GROUP BY line + 0 HAVING (line + 0) || 'x' = '1x'", [(2, 4)]),
    ("SELECT count(*), sum(line_no) FROM blame GROUP BY line + 0 HAVING (line + 0) || 'x' = '1.0x'", [(1, 2)]),
    ("SELECT count(*), sum(line_no) FROM blame GROUP BY line + 0", [(3, 6)]),
    ("SELECT line + 0, count(*), sum(line_no) FROM blame GROUP BY line + 0 HAVING (line + 0) || 'x' = '1x'", [(1, 2, 4)]),
    (
        "SELECT line + 0, count(*), sum(line_no) FROM blame GROUP BY line + 0 HAVING (line + 0) || 'x' = '1.0x'",
        [(1.0, 1, 2)],
    ),
]


@pytest.mark.parametrize("query, expected", LINE_PLUS_ZERO_CASES)
def test_a_moved_term_filters_rows_not_groups(tiny_repo, query, expected):
    tables = _fixed_tables(LINE_PLUS_ZERO_ROWS)
    conn = load_unfiltered(tables["blame"], tiny_repo, BLAME_SCHEMA, "blame")
    try:
        _check(conn, tiny_repo, query, expected, tables)
    finally:
        conn.close()


#: A NULL `path` is a group of its own, and a moved term sees it.
NULL_KEY_ROWS = [_blame_row(None, 1, "a"), _blame_row(None, 2, "b"), _blame_row("p", 3, "c"), _blame_row("q", 4, "d")]

NULL_KEY_CASES = [
    ("SELECT path, count(*) FROM blame GROUP BY path HAVING count(*) > 0 AND path IS NULL", [(None, 2)]),
    ("SELECT path, count(*) FROM blame GROUP BY path HAVING count(*) > 0 AND path IS NOT NULL", [("p", 1), ("q", 1)]),
    (f"SELECT path, count(*) FROM blame GROUP BY path HAVING count(*) > 0 AND NOT ({ERR})", ERROR),
    (f"SELECT path, count(*) FROM blame GROUP BY path HAVING count(*) > 0 AND path IS NULL AND NOT ({ERR})", ERROR),
    (f"SELECT path, count(*) FROM blame GROUP BY path HAVING count(*) > 0 AND path < NULL AND NOT ({ERR})", []),
    (f"SELECT path, count(*) FROM blame GROUP BY path HAVING count(*) > 5 AND path IS NULL AND ({ERR}) IS NULL", ERROR),
]


@pytest.mark.parametrize("query, expected", NULL_KEY_CASES)
def test_null_keys(tiny_repo, query, expected):
    tables = _fixed_tables(NULL_KEY_ROWS)
    conn = load_unfiltered(tables["blame"], tiny_repo, BLAME_SCHEMA, "blame")
    try:
        _check(conn, tiny_repo, query, expected, tables)
    finally:
        conn.close()
