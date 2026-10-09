"""A `WHERE` term with no column reference is decided once, before any
row, against SQLite (issue #171, spec §3 "`WHERE` terms with no column
reference").

SQLite splits the `WHERE` - after constant propagation (#142) - into
terms on its top-level `AND`s, nested `AND`s flattened, and evaluates
every term that has no column reference once, in `WHERE` order, before
it reads the first row: the first that is not `TRUE` ends the query
over zero rows, and one that raises raises whatever the input is. A
`HAVING` term that moves below the aggregate (#141) and has no column
joins the list after the `WHERE` ones. A column-free sub-expression
inside a term that has a column is not hoisted.

Only an error is observable: `ERR` is `path LIKE 'a' ESCAPE 'ab'` and
`CONSTERR` is `'a' LIKE 'a' ESCAPE 'ab'`. Each case pins SQLite's own
outcome (`ERROR` for `ESCAPE expression must be a single character`,
or the exact rows) before checking historian against it, so a case
cannot pass by both engines failing for another reason. Every outcome
was measured with the pinned oracle (Python `sqlite3` 3.50.4, #117)
through this harness's own loader: over `tiny` (`blame` rows
`feature/thing.py` 1, `src/utils.py` 1, `src/utils.py` 2) and over an
empty table of `blame`'s schema.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

import pytest

from historian.catalog import SCAN_FACTORIES, SCHEMAS
from historian.exec.expression import EvalError
from historian.plan.planner import plan
from historian.sql.binder import bind
from historian.sql.lexer import tokenize
from historian.sql.parser import parse
from historian.tables.blame import BLAME_SCHEMA, BlameScan

from differential.conftest import (
    assert_rows_match,
    load_table_sql,
    load_unfiltered,
    run_historian,
)

ESCAPE_MESSAGE = "ESCAPE expression must be a single character"
ERROR = "error"

ERR = "path LIKE 'a' ESCAPE 'ab'"
CONSTERR = "'a' LIKE 'a' ESCAPE 'ab'"
S = "SELECT path FROM blame WHERE"
G = "SELECT path FROM blame GROUP BY path HAVING"

ALL = [("feature/thing.py",), ("src/utils.py",), ("src/utils.py",)]


def _run_without_pushdown(query: str, repo, tables):
    """`--no-pushdown`: `plan()` and nothing after it."""
    tree = plan(bind(parse(tokenize(query)), catalog=SCHEMAS), repo, tables=tables)
    return tree.schema, list(tree.rows())


def _check(conn, repo, query: str, expected, tables=SCAN_FACTORIES, *, ordered=False) -> None:
    """SQLite's outcome for *query* is *expected* - `ERROR` or a list of
    rows - and historian's agrees with it, with pushdown and without."""
    try:
        sqlite_rows = conn.execute(query).fetchall()
        sqlite_outcome = sqlite_rows if ordered else sorted(sqlite_rows)
    except sqlite3.OperationalError as error:
        assert str(error) == ESCAPE_MESSAGE, f"oracle raised something else: {error}\n  {query}"
        sqlite_outcome = ERROR
    pinned = expected if expected == ERROR or ordered else sorted(expected)
    assert sqlite_outcome == pinned, f"the oracle no longer gives the pinned outcome\n  {query}"

    for runner in (run_historian, _run_without_pushdown):
        if expected == ERROR:
            with pytest.raises(EvalError) as raised:
                runner(query, repo, tables)
            assert str(raised.value) == ESCAPE_MESSAGE
            continue
        _, historian_rows = runner(query, repo, tables)
        if ordered:
            assert_rows_match(sqlite_rows, historian_rows, ordered=True, key_positions=[0])
        else:
            assert_rows_match(sqlite_rows, historian_rows)


@pytest.fixture(scope="module")
def tiny_conn(tiny_repo):
    conn = load_unfiltered(BlameScan, tiny_repo, BLAME_SCHEMA, "blame")
    yield conn
    conn.close()


class _EmptyBlame:
    """`blame`'s schema, zero rows, no pushdown."""

    schema = BLAME_SCHEMA

    def __init__(self, repo) -> None:
        pass

    def capabilities(self) -> set[str]:
        return set()

    def scan(self, pushed=()) -> Iterator[tuple]:
        assert not pushed
        yield from ()


EMPTY_TABLES = {"blame": _EmptyBlame}


@pytest.fixture(scope="module")
def empty_conn():
    conn = sqlite3.connect(":memory:")
    statements, _insert_into = load_table_sql("blame", BLAME_SCHEMA)
    for statement in statements:
        conn.execute(statement)
    yield conn
    conn.close()


# --- Agree already, and must keep agreeing -----------------------------------

AGREE_ALREADY_CASES = [
    (f"{S} {ERR} AND 1", ERROR),
    (f"{S} {ERR} AND 1=1", ERROR),
    (f"{S} {CONSTERR} AND {ERR}", ERROR),
    (f"{S} {CONSTERR}", ERROR),
    (f"{S} {CONSTERR} AND 0", ERROR),
    (f"{S} {ERR} OR NULL", ERROR),
    (f"{S} line_no < 0 OR {CONSTERR}", ERROR),
    (f"{S} NULL AND {ERR}", []),
    (f"{S} 0 AND {ERR}", []),
    (f"{S} 1=0 AND {CONSTERR}", []),
    (f"{S} NULL AND {CONSTERR}", []),
    (f"{S} line_no < 0 AND 1 IN (1, {CONSTERR})", []),
]


@pytest.mark.parametrize("query, expected", AGREE_ALREADY_CASES)
def test_cases_that_already_agreed(tiny_repo, tiny_conn, query, expected):
    _check(tiny_conn, tiny_repo, query, expected)


# --- A constant term that raises, though no row reaches it --------------------

RAISES_CASES = [
    f"{S} line_no < 0 AND {CONSTERR}",
    f"{S} line_no < 0 AND 1 AND {CONSTERR}",
    f"{S} line_no < 0 AND {CONSTERR} AND 1=0",
    f"{S} line_no < 0 AND {CONSTERR} AND 0",
    f"{S} line_no < 0 AND ({CONSTERR} AND 1 = 0)",
    f"{S} line_no < 0 AND NOT {CONSTERR}",
    f"{S} line_no < 0 AND ({CONSTERR} IS NULL)",
    f"{S} line_no < 0 AND 1 IN (2, {CONSTERR})",
    f"{S} line_no < 0 AND 1 BETWEEN 2 AND {CONSTERR}",
    f"{S} line_no < 0 AND (NULL OR {CONSTERR})",
    f"{S} line_no < 0 AND NULL IS NULL AND {CONSTERR}",
    f"SELECT count(*) FROM blame WHERE line_no < 0 AND {CONSTERR}",
    # Found while implementing: a constant OR whose literal does not
    # decide it (#189) still runs its other operand.
    f"{S} line_no < 0 AND ({CONSTERR} OR 0)",
]


@pytest.mark.parametrize("query", RAISES_CASES)
def test_a_constant_term_raises_before_any_row(tiny_repo, tiny_conn, query):
    _check(tiny_conn, tiny_repo, query, ERROR)


# --- A false or NULL constant term stops the query before a per-row term -------

NO_ROWS_CASES = [
    f"{S} {ERR} AND NULL",
    f"{S} {ERR} AND 0",
    f"{S} {ERR} AND 1=0",
    f"{S} {ERR} AND NOT 1",
    f"{S} {ERR} AND 'a' LIKE 'b'",
    f"{S} {ERR} AND 'a' NOT LIKE 'a'",
    f"{S} {ERR} AND (NULL)",
    f"{S} {ERR} AND (1=0)",
    f"{S} {ERR} AND 1=0 AND 1",
    f"{S} {ERR} AND 1 AND 1=0",
    f"{S} {ERR} AND 0.0",
    f"{S} {ERR} AND '0'",
    f"{S} {ERR} AND ''",
    f"{S} {ERR} AND 'a'",
    f"{S} {ERR} AND 1 IS NULL",
    f"{S} {ERR} AND 1 IN ()",
    f"{S} {ERR} AND 1 IN (2,3)",
    f"{S} {ERR} AND 1 BETWEEN 2 AND 3",
    f"{S} {ERR} AND 1 + 1 = 3",
    f"{S} {ERR} AND -0",
    f"{S} {ERR} AND (0)",
    f"{S} {ERR} AND 00",
    f"{S} {ERR} AND NULL = NULL",
    f"{S} {ERR} AND NULL IS NOT NULL",
    f"{S} {ERR} AND (1 AND NULL)",
    f"{S} {ERR} AND (NULL OR NULL)",
    f"{S} {ERR} AND (NULL OR 0)",
    f"{S} {ERR} AND (0 OR 0)",
    f"{S} {ERR} AND (0 AND {CONSTERR})",
    f"{S} {ERR} AND 1=0 AND {CONSTERR}",
    f"{S} {ERR} AND NOT 1 AND {ERR}",
    f"{S} {ERR} AND NOT 1 > 0",
    # Found while implementing: #189's simplification inside a guard
    # term (`NOT (CONSTERR OR 1)` is `NOT 1`), and a `0 OR NULL`.
    f"{S} {ERR} AND NOT ({CONSTERR} OR 1)",
    f"{S} {ERR} AND (0 OR NULL)",
    f"{S} {ERR} AND ({CONSTERR} OR 1) AND 0",
    f"{S} {ERR} AND (1 OR {CONSTERR}) AND 1=0",
]


@pytest.mark.parametrize("query", NO_ROWS_CASES)
def test_a_false_or_null_constant_stops_the_query_before_any_row(tiny_repo, tiny_conn, query):
    _check(tiny_conn, tiny_repo, query, [])


# --- A term is a whole top-level AND term; nested ANDs flatten -----------------

TERM_CASES = [
    (f"{S} line_no = 1 AND ({ERR} AND 0)", []),
    (f"{S} line_no = 1 AND ({ERR} AND NULL)", []),
    (f"{S} line_no < 0 AND ({CONSTERR} AND 0)", ERROR),
    (f"{S} {ERR} AND (line_no = 1 AND line_no = 2)", []),
    # Under NOT or OR the AND is inside one term and does not flatten.
    (f"{S} {ERR} AND NOT (line_no = 1 AND line_no = 2)", ERROR),
    (f"{S} {ERR} AND ({CONSTERR} AND 0)", ERROR),
    (f"{S} {ERR} AND NOT ({CONSTERR} AND 0)", ERROR),
    (f"{S} line_no < 0 AND NOT ({CONSTERR} AND 0)", []),
    # A constant term is evaluated as a condition: it stops where a
    # WHERE root stops, and #189 simplifies it first.
    (f"{S} {ERR} AND (1 = 1 OR {CONSTERR})", ERROR),
    (f"{S} {ERR} AND (1 = 0 OR {CONSTERR})", ERROR),
    (f"{S} {ERR} AND ({CONSTERR} OR 1)", ERROR),
    (f"{S} line_no < 0 AND ({CONSTERR} OR 1)", []),
    (f"{S} line_no < 0 AND (1 = 1 OR {CONSTERR})", []),
    # A column anywhere in the term keeps it per row (#189's shapes).
    (f"{S} {ERR} AND NOT (path LIKE 'x' OR 1)", ERROR),
    (f"{S} line_no < 0 AND NOT (path LIKE 'x' OR 1)", []),
]


@pytest.mark.parametrize("query, expected", TERM_CASES)
def test_the_constant_is_a_whole_term(tiny_repo, tiny_conn, query, expected):
    _check(tiny_conn, tiny_repo, query, expected)


# --- A constant inside a term with a column is not hoisted ---------------------

NOT_HOISTED_CASES = [
    (f"{S} line_no > 0 OR {CONSTERR}", ALL),
    (f"{S} line_no > 5 OR {CONSTERR}", ERROR),
    (f"SELECT {CONSTERR} FROM blame", ERROR),
    (f"SELECT path FROM blame ORDER BY {CONSTERR}", ERROR),
]

NOT_HOISTED_EMPTY_CASES = [
    (f"{S} line_no > 0 OR {CONSTERR}", []),
    (f"{S} line_no > 5 OR {CONSTERR}", []),
    (f"SELECT {CONSTERR} FROM blame", []),
    (f"SELECT path FROM blame ORDER BY {CONSTERR}", []),
]


@pytest.mark.parametrize("query, expected", NOT_HOISTED_CASES)
def test_a_constant_inside_a_per_row_term_is_not_hoisted(tiny_repo, tiny_conn, query, expected):
    _check(tiny_conn, tiny_repo, query, expected)


@pytest.mark.parametrize("query, expected", NOT_HOISTED_EMPTY_CASES)
def test_a_constant_inside_a_per_row_term_does_not_raise_on_empty_input(tiny_repo, empty_conn, query, expected):
    _check(empty_conn, tiny_repo, query, expected, EMPTY_TABLES)


# --- Order among constants: WHERE order, the first non-TRUE one stops ----------

ORDER_CASES = [
    (f"{S} 1=0 AND {CONSTERR}", []),
    (f"{S} NULL AND {CONSTERR}", []),
    (f"{S} {CONSTERR} AND 1=0", ERROR),
    (f"{S} {CONSTERR} AND 0", ERROR),
    (f"{S} {ERR} AND {CONSTERR} AND 1=0", ERROR),
    (f"{S} {ERR} AND 1=0 AND {CONSTERR}", []),
    ("SELECT count(*) FROM blame WHERE line_no = 5 AND line_no = 6 AND " + CONSTERR, [(0,)]),
    (f"{S} {CONSTERR} AND line_no = 5 AND line_no = 6", ERROR),
]


@pytest.mark.parametrize("query, expected", ORDER_CASES)
def test_constants_run_in_where_order(tiny_repo, tiny_conn, query, expected):
    _check(tiny_conn, tiny_repo, query, expected)


# --- After constant propagation: a FixedColumnRef is no column (#142) ----------

PROPAGATED_CASES = [
    (f"{S} {ERR} AND line_no = 1 AND line_no = 2", []),
    (f"{S} {ERR} AND line_no = 1 AND line_no > 1", []),
    (f"{S} {ERR} AND line_no = 5 AND line_no < 3", []),
    (f"{S} {ERR} AND line_no = 1 AND 1 = 2", []),
    (f"{S} {ERR} AND line_no = 1 AND line_no >= 1", ERROR),
    (f"{S} line_no = 99 AND line_no LIKE 'a' ESCAPE 'ab'", ERROR),
    (f"{S} line_no LIKE 'a' ESCAPE 'ab' AND line_no = 99", ERROR),
    # `ERR` becomes the constant `'zzz' LIKE 'a' ESCAPE 'ab'`, decided
    # before the scan is read, so pushing `path = 'zzz'` (which blames
    # nothing) cannot hide it. The full contradiction family is #180.
    (f"{S} path = 'zzz' AND {ERR}", ERROR),
    (f"{S} {ERR} AND path = 'zzz'", ERROR),
]


@pytest.mark.parametrize("query, expected", PROPAGATED_CASES)
def test_a_propagated_constant_term_is_decided_before_any_row(tiny_repo, tiny_conn, query, expected):
    _check(tiny_conn, tiny_repo, query, expected)


# --- Empty input ---------------------------------------------------------------

EMPTY_CASES = [
    (f"{S} {CONSTERR}", ERROR),
    (f"SELECT count(*) FROM blame WHERE {CONSTERR}", ERROR),
    ("SELECT count(*) FROM blame WHERE 1=0", [(0,)]),
    ("SELECT count(*) FROM blame WHERE NULL", [(0,)]),
    ("SELECT path, count(*) FROM blame WHERE 1=0 GROUP BY path", []),
    (f"{S} {CONSTERR} LIMIT 0", []),
    ("SELECT path FROM blame LIMIT 0", []),
    (f"SELECT count(*) FROM blame WHERE 1=0 AND {CONSTERR}", [(0,)]),
    (f"SELECT count(*) FROM blame WHERE {CONSTERR} AND 1=0", ERROR),
    (f"{S} {ERR} AND {CONSTERR}", ERROR),
    (f"{S} line_no < 0 GROUP BY path HAVING {CONSTERR}", ERROR),
]


@pytest.mark.parametrize("query, expected", EMPTY_CASES)
def test_empty_input(tiny_repo, empty_conn, query, expected):
    _check(empty_conn, tiny_repo, query, expected, EMPTY_TABLES)


# --- LIMIT 0 never pulls a row, OFFSET or not -----------------------------------

LIMIT_CASES = [
    (f"{S} {CONSTERR} LIMIT 0", []),
    (f"{S} {CONSTERR} LIMIT 1", ERROR),
    (f"{S} {CONSTERR} LIMIT 1 OFFSET 5", ERROR),
    (f"{S} {CONSTERR} LIMIT -1 OFFSET 5", ERROR),
    (f"SELECT count(*) FROM blame WHERE {CONSTERR} LIMIT 0", []),
    (f"SELECT count(*) FROM blame WHERE {CONSTERR} LIMIT 1 OFFSET 1", ERROR),
    # Found while implementing: `Limit` skipped the offset before it
    # looked at the limit, so `LIMIT 0 OFFSET n` read rows.
    (f"{S} {CONSTERR} LIMIT 0 OFFSET 1", []),
    (f"{S} {ERR} LIMIT 0 OFFSET 1", []),
    (f"{S} line_no = 1 AND {CONSTERR} LIMIT 0 OFFSET 2", []),
    ("SELECT path FROM blame LIMIT 0 OFFSET 1", []),
    (f"{S} {ERR} AND 1=0 LIMIT 1", []),
]


@pytest.mark.parametrize("query, expected", LIMIT_CASES)
def test_limit_and_offset(tiny_repo, tiny_conn, query, expected):
    _check(tiny_conn, tiny_repo, query, expected)


# --- What a query returns after a false constant ----------------------------------

RESULT_SHAPE_CASES = [
    (f"SELECT count(*) FROM blame WHERE {ERR} AND 1=0", [(0,)]),
    (f"SELECT count(*), max(path) FROM blame WHERE {ERR} AND 1=0", [(0, None)]),
    (f"SELECT path, count(*) FROM blame WHERE {ERR} AND 1=0 GROUP BY path", []),
    (f"SELECT DISTINCT path FROM blame WHERE {ERR} AND 1=0", []),
    (f"SELECT path FROM blame WHERE {ERR} AND 1=0 ORDER BY path", []),
    ("SELECT path FROM blame WHERE 1=1 AND path = 'src/utils.py'", [("src/utils.py",), ("src/utils.py",)]),
    ("SELECT path FROM blame WHERE path = 'src/utils.py' AND 1=0", []),
]


@pytest.mark.parametrize("query, expected", RESULT_SHAPE_CASES)
def test_result_shapes_after_a_constant(tiny_repo, tiny_conn, query, expected):
    _check(tiny_conn, tiny_repo, query, expected)


# --- Moved HAVING constants (#141) follow the WHERE ones --------------------------

HAVING_CASES = [
    (f"{G} {CONSTERR}", ERROR),
    (f"{G} {CONSTERR} AND 1=0", ERROR),
    (f"{G} count(*) > 5 AND {CONSTERR}", ERROR),
    (f"{G} count(*) > 5 AND {CONSTERR} AND 1=0", ERROR),
    (f"{G} path > 'zzz' AND {CONSTERR}", ERROR),
    (f"{G} 0 AND {CONSTERR}", ERROR),
    (f"{G} {CONSTERR} AND 0", ERROR),
    (f"{G} 1=0 AND {CONSTERR}", []),
    (f"{G} NULL AND {CONSTERR}", []),
    (f"{G} count(*) > 5 AND 1=0 AND {CONSTERR}", []),
    (f"SELECT path FROM blame WHERE 1=0 GROUP BY path HAVING {CONSTERR}", []),
    (f"SELECT path FROM blame WHERE 1=0 AND line_no > 0 GROUP BY path HAVING {CONSTERR}", []),
    (f"SELECT path FROM blame WHERE {CONSTERR} GROUP BY path HAVING 1=0", ERROR),
    (f"SELECT path FROM blame WHERE 1=1 GROUP BY path HAVING {CONSTERR}", ERROR),
    (f"SELECT path FROM blame WHERE line_no < 0 GROUP BY path HAVING {CONSTERR}", ERROR),
    (f"SELECT path FROM blame WHERE {ERR} GROUP BY path HAVING 1=0", []),
    (f"SELECT path FROM blame WHERE {ERR} GROUP BY path HAVING NULL", []),
    (f"{G} {ERR} AND 1=0", []),
    (f"{G} {ERR} AND NULL", []),
    (f"{G} {ERR} AND 0", ERROR),
    (f"{G} 1=0 AND {ERR}", []),
    # No GROUP BY: nothing moves, HAVING runs once after the aggregate.
    (f"SELECT count(*) FROM blame HAVING {CONSTERR}", ERROR),
    (f"SELECT count(*) FROM blame WHERE 1=0 HAVING {CONSTERR}", ERROR),
]

HAVING_EMPTY_CASES = [
    (f"{G} {CONSTERR}", ERROR),
    (f"{G} {CONSTERR} AND 1=0", ERROR),
    (f"{G} count(*) > 5 AND {CONSTERR}", ERROR),
    (f"{G} path > 'zzz' AND {CONSTERR}", ERROR),
    (f"{G} 0 AND {CONSTERR}", ERROR),
    (f"{G} 1=0 AND {CONSTERR}", []),
    (f"{G} NULL AND {CONSTERR}", []),
    (f"SELECT count(*) FROM blame HAVING {CONSTERR}", ERROR),
    (f"SELECT count(*) FROM blame WHERE 1=0 HAVING {CONSTERR}", ERROR),
]


@pytest.mark.parametrize("query, expected", HAVING_CASES)
def test_moved_having_constants(tiny_repo, tiny_conn, query, expected):
    _check(tiny_conn, tiny_repo, query, expected)


@pytest.mark.parametrize("query, expected", HAVING_EMPTY_CASES)
def test_moved_having_constants_on_empty_input(tiny_repo, empty_conn, query, expected):
    _check(empty_conn, tiny_repo, query, expected, EMPTY_TABLES)
