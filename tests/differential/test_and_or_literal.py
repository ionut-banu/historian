"""A condition `AND`/`OR` with an integer-literal operand, against
SQLite (issue #189, spec §3 "Expression evaluation").

In a condition - a `WHERE` term, the whole `HAVING`, and the operands
of `AND`/`OR`/`NOT` beneath them - SQLite never evaluates the other
operand of an `AND` or `OR` when one operand is an integer literal
that decides it: `x OR 1` is true and `x AND 0` is false without
running `x`. The literal is an unsigned integer literal that fits in
32 bits (`0` to `2147483647`), any parentheses around it, or a nested
`AND`/`OR` that itself simplifies to one. `x AND 1` and `x OR 0` are
`x`, so `x` still runs. In value context nothing is simplified.

`WHERE` is split into terms on its top-level `AND`s first, so the
terms of a `WHERE` are not simplified against each other (a
column-free term is decided before any row, #171). `HAVING` is one condition, top-level
`AND`s included, and a `HAVING` term that moves below the aggregate
(#141) is a `WHERE` term.

Only an error is observable: `ERR` is `path LIKE 'a' ESCAPE 'ab'`,
`CONSTERR` is `'a' LIKE 'a' ESCAPE 'ab'` and `AGG_ERR` is `max(path)
LIKE 'a' ESCAPE 'ab'`. Each case pins SQLite's own outcome (`ERROR`
for `ESCAPE expression must be a single character`, or the exact
rows) before checking historian against the oracle, so a case cannot
pass by both engines failing for another reason. Every outcome was
measured with the pinned oracle (Python `sqlite3` 3.50.4, #117)
through this harness's own loader; `tiny`'s `blame` rows are
`feature/thing.py` 1, `src/utils.py` 1 and `src/utils.py` 2.

Hex literals (`0x1`, `0x7fffffff`) and `1e0` are SQLite syntax the v1
grammar does not have yet (#6): their SQLite outcomes are pinned in
`test_hex_and_exponent_literals_await_issue_6`, which fails once
historian parses them so the cases can move into the lists above.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

import pytest

from historian.catalog import SCAN_FACTORIES
from historian.exec.expression import EvalError
from historian.sql.parser import ParseError
from historian.tables.blame import BLAME_SCHEMA, BlameScan

from differential.conftest import (
    assert_rows_match,
    create_table_sql,
    load_unfiltered,
    run_historian,
)

ESCAPE_MESSAGE = "ESCAPE expression must be a single character"
ERROR = "error"

ERR = "path LIKE 'a' ESCAPE 'ab'"
CONSTERR = "'a' LIKE 'a' ESCAPE 'ab'"
AGG_ERR = "max(path) LIKE 'a' ESCAPE 'ab'"
S = "SELECT path, line_no FROM blame WHERE"
G = "SELECT path, count(*) FROM blame GROUP BY path HAVING"

ALL = [("feature/thing.py", 1), ("src/utils.py", 1), ("src/utils.py", 2)]
LINE_1 = [("feature/thing.py", 1), ("src/utils.py", 1)]
GROUPS = [("feature/thing.py", 1), ("src/utils.py", 2)]


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
    if expected != ERROR and key_positions is None:
        sqlite_outcome = sorted(sqlite_outcome)
        expected = sorted(expected)
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


# --- x OR <true literal>: TRUE, x never runs ---------------------------------

OR_TRUE_CASES = [
    (f"{S} {ERR} OR 1", ALL),
    (f"{S} {ERR} OR 5", ALL),
    (f"{S} {ERR} OR (1)", ALL),
    (f"{S} {ERR} OR (((1)))", ALL),
    (f"{S} {ERR} OR 01", ALL),
    (f"{S} {ERR} OR 2147483647", ALL),
    (f"{S} {ERR} OR 1 OR 0", ALL),
    (f"{S} ({ERR} OR 0) OR 1", ALL),
    (f"{S} ({ERR} AND 1) OR 1", ALL),
    (f"{S} {ERR} OR (1 AND 1)", ALL),
    (f"{S} {ERR} OR (0 OR 1)", ALL),
    (f"{S} {ERR} OR (1 OR {ERR})", ALL),
    (f"{S} {CONSTERR} OR line_no = 1 OR 1", ALL),
    (f"{S} ({ERR} OR 1) AND line_no = 1", LINE_1),
    (f"{S} NOT ({ERR} OR 1)", []),
    # Found while implementing: the rule reaches through any nesting.
    (f"{S} {ERR} OR 1 OR {ERR}", ALL),
    (f"{S} {ERR} OR ({ERR} OR 1)", ALL),
    (f"{S} ({ERR} OR {ERR}) OR 1", ALL),
    (f"{S} {ERR} OR (line_no = 1 AND 0) OR 1", ALL),
    (f"{S} NOT NOT ({ERR} OR 1)", ALL),
]


@pytest.mark.parametrize("query, expected", OR_TRUE_CASES)
def test_or_with_a_true_literal_never_runs_the_other_operand(tiny_repo, tiny_conn, query, expected):
    _check(tiny_conn, tiny_repo, query, expected)


# --- x AND <false literal> below the top-level ANDs: FALSE, x never runs -----

AND_FALSE_CASES = [
    (f"{S} NOT ({ERR} AND 0)", ALL),
    (f"{S} NOT ({ERR} AND 00)", ALL),
    (f"{S} NOT ({ERR} AND 1 AND 0)", ALL),
    (f"{S} NOT ({ERR} AND {CONSTERR} AND 0)", ALL),
    (f"{S} {ERR} AND 0 OR line_no = 1", LINE_1),
    (f"{S} ({ERR} AND 0) OR line_no = 1", LINE_1),
    (f"{S} line_no = 1 OR ({ERR} AND 0)", LINE_1),
    # Found while implementing.
    (f"{S} NOT (NOT ({ERR} AND 0))", []),
    (f"{S} NOT ({ERR} AND (1 OR {ERR}) AND 0)", ALL),
    ("SELECT count(*) FROM blame WHERE NOT (" + ERR + " AND 0)", [(3,)]),
]


@pytest.mark.parametrize("query, expected", AND_FALSE_CASES)
def test_and_with_a_false_literal_never_runs_the_other_operand(tiny_repo, tiny_conn, query, expected):
    _check(tiny_conn, tiny_repo, query, expected)


# --- The literal: unsigned, integer, 32 bits ---------------------------------

#: `2147483647` is the largest literal that simplifies; leading zeros do
#: not count towards the 32 bits.
BOUNDARY_CASES = [
    (f"{S} {ERR} OR 2147483647", ALL),
    (f"{S} {ERR} OR 2147483648", ERROR),
    (f"{S} {ERR} OR 000000000001", ALL),
    (f"{S} {ERR} OR 00000000002147483647", ALL),
    (f"{S} {ERR} OR 4294967297", ERROR),
    (f"{S} {ERR} OR 9223372036854775807", ERROR),
    (f"{S} NOT ({ERR} AND 00)", ALL),
    (f"{S} {ERR} OR 01", ALL),
]


@pytest.mark.parametrize("query, expected", BOUNDARY_CASES)
def test_only_a_32_bit_unsigned_integer_literal_simplifies(tiny_repo, tiny_conn, query, expected):
    _check(tiny_conn, tiny_repo, query, expected)


#: Not literals of this kind - signed, REAL, text, a comparison, `NOT 0`,
#: too wide - or a literal on the side that does not decide: the
#: operand still runs and raises.
STILL_RAISES_CASES = [
    f"{S} {ERR} OR -1",
    f"{S} {ERR} OR +1",
    f"{S} {ERR} OR 1.5",
    f"{S} {ERR} OR 1.0",
    f"{S} {ERR} OR 'a'",
    f"{S} {ERR} OR '1'",
    f"{S} {ERR} OR 1=1",
    f"{S} {ERR} OR NOT 0",
    f"{S} {ERR} OR 2147483648",
    f"{S} {ERR} OR 9223372036854775807",
    f"{S} {ERR} OR 9223372036854775808",
    f"{S} NOT ({ERR} AND -0)",
    f"{S} NOT ({ERR} AND 0.0)",
    f"{S} NOT ({ERR} AND '0')",
    f"{S} NOT ({ERR} AND 1=0)",
    f"{S} NOT ({ERR} AND NULL)",
    f"{S} {ERR} OR NULL",
    f"{S} {ERR} OR 0",
    f"{S} {ERR} OR (0 OR 0)",
    f"{S} {ERR} OR (0 AND 1)",
    f"{S} NOT ({ERR} AND 1)",
    f"{S} NOT ({ERR} OR 0)",
    f"{S} NOT (1 AND {ERR})",
    f"{S} ({ERR} AND 1) OR line_no = 1",
    f"{S} ({ERR} OR 1) AND {ERR}",
]


@pytest.mark.parametrize("query", STILL_RAISES_CASES)
def test_anything_else_still_runs_the_operand(tiny_repo, tiny_conn, query):
    _check(tiny_conn, tiny_repo, query, ERROR)


#: SQLite answers for the hex and exponent spellings, which historian
#: cannot parse until #6.
HEX_AND_EXPONENT_CASES = [
    (f"SELECT count(*) FROM blame WHERE {ERR} OR 0x1", [(3,)]),
    (f"SELECT count(*) FROM blame WHERE {ERR} OR 0x7fffffff", [(3,)]),
    (f"SELECT count(*) FROM blame WHERE {ERR} OR 0x00000000001", [(3,)]),
    (f"SELECT count(*) FROM blame WHERE NOT ({ERR} AND 0x0)", [(3,)]),
    (f"SELECT count(*) FROM blame WHERE {ERR} OR 0x80000000", ERROR),
    (f"SELECT count(*) FROM blame WHERE {ERR} OR 0xFFFFFFFFFFFFFFFF", ERROR),
    (f"SELECT count(*) FROM blame WHERE {ERR} OR 0x0", ERROR),
    (f"SELECT count(*) FROM blame WHERE {ERR} OR 1e0", ERROR),
]


@pytest.mark.parametrize("query, expected", HEX_AND_EXPONENT_CASES)
def test_hex_and_exponent_literals_await_issue_6(tiny_repo, tiny_conn, query, expected):
    """The 32-bit boundary for hex is `0x7fffffff`, as for decimal;
    `0x0` is false. Historian rejects the spelling at parse time today
    - when #6 adds it, this fails and the case joins the lists above."""
    try:
        outcome = tiny_conn.execute(query).fetchall()
    except sqlite3.OperationalError as error:
        assert str(error) == ESCAPE_MESSAGE
        outcome = ERROR
    assert outcome == expected
    with pytest.raises(ParseError):
        run_historian(query, tiny_repo)


# --- Value context: nothing is simplified ------------------------------------

VALUE_CONTEXT_CASES = [
    f"SELECT {ERR} OR 1 FROM blame",
    f"SELECT {ERR} AND 0 FROM blame",
    f"SELECT path FROM blame ORDER BY {ERR} OR 1",
    f"SELECT count({ERR} OR 1) FROM blame",
    f"SELECT count(*) FROM blame GROUP BY {ERR} OR 1",
    f"SELECT path FROM blame WHERE ({ERR} OR 1) IS NULL",
    f"SELECT path FROM blame WHERE ({ERR} AND 0) IS NULL",
    f"SELECT path FROM blame WHERE ({ERR} AND 0) = 0",
    f"SELECT path FROM blame WHERE +({ERR} OR 1)",
    f"SELECT path FROM blame WHERE ({ERR} OR 1) BETWEEN 0 AND 5",
    f"SELECT path FROM blame WHERE 1 IN ({ERR} OR 1)",
    f"{G} ({AGG_ERR} OR 1) IS NOT NULL",
]


@pytest.mark.parametrize("query", VALUE_CONTEXT_CASES)
def test_value_context_still_evaluates_everything(tiny_repo, tiny_conn, query):
    _check(tiny_conn, tiny_repo, query, ERROR)


# --- Already agreeing: a deciding left operand, and the top-level ANDs -------

ALREADY_AGREED_CASES = [
    (f"SELECT path FROM blame WHERE 1 OR {ERR}", [("feature/thing.py",), ("src/utils.py",), ("src/utils.py",)]),
    (f"SELECT path FROM blame WHERE 1 = 1 OR {ERR}", [("feature/thing.py",), ("src/utils.py",), ("src/utils.py",)]),
    (f"SELECT path FROM blame WHERE 0 AND {ERR}", []),
    (f"SELECT path FROM blame WHERE NULL AND {ERR}", []),
    (f"SELECT path FROM blame WHERE NOT (0 AND {ERR})", [("feature/thing.py",), ("src/utils.py",), ("src/utils.py",)]),
    (f"SELECT path FROM blame WHERE (1 OR {ERR}) AND line_no = 2", [("src/utils.py",)]),
    (f"SELECT path FROM blame WHERE line_no = 1 AND {ERR} AND 1", ERROR),
    (f"SELECT path FROM blame WHERE {ERR} OR line_no = 1", ERROR),
    # A column the planner replaced by its constant (#142) is not a
    # literal: SQLite keeps it a column.
    (f"SELECT path FROM blame WHERE line_no = 1 AND ({ERR} OR line_no)", ERROR),
    (f"SELECT path FROM blame WHERE line_no = 0 AND NOT ({ERR} AND line_no)", []),
]


@pytest.mark.parametrize("query, expected", ALREADY_AGREED_CASES)
def test_cases_that_already_agreed_still_agree(tiny_repo, tiny_conn, query, expected):
    _check(tiny_conn, tiny_repo, query, expected)


def test_top_level_and_terms_raise_on_both_sides(tiny_repo, tiny_conn):
    """`WHERE CONSTERR AND 0` is two terms; SQLite runs the column-free
    ones once, in order, before any row (#171), so `CONSTERR` raises
    before `0` is seen. Simplifying the top-level `AND` would hide it."""
    _check(tiny_conn, tiny_repo, f"SELECT path FROM blame WHERE {CONSTERR} AND 0", ERROR)
    _check(tiny_conn, tiny_repo, f"SELECT path FROM blame WHERE {ERR} AND {CONSTERR} AND 0", ERROR)
    _check(tiny_conn, tiny_repo, f"SELECT path FROM blame WHERE line_no = 1 AND {CONSTERR} AND 0", ERROR)


def test_where_err_and_0_returns_no_rows_through_the_constant_guard(tiny_repo, tiny_conn):
    """`WHERE ERR AND 0` returns no rows in SQLite because the term `0`
    is decided before any row (#171), not because of this rule: the
    top-level terms are not simplified against each other. The `0` is a
    constant term of its own, so no row reaches `ERR`."""
    _check(tiny_conn, tiny_repo, f"SELECT path FROM blame WHERE {ERR} AND 0", [])


# --- HAVING ------------------------------------------------------------------

HAVING_CASES = [
    # Kept in HAVING (aggregate): the whole HAVING is one condition.
    (f"{G} {AGG_ERR} OR 1", GROUPS),
    (f"{G} count(*) > 0 AND ({AGG_ERR} OR 1)", GROUPS),
    (f"{G} {AGG_ERR} AND 0", []),
    (f"{G} {AGG_ERR} AND (0)", []),
    (f"{G} {AGG_ERR} AND count(*) > 0 AND 0", []),
    (f"{G} {AGG_ERR} AND path > 'a' AND 0", []),
    (f"{G} NOT ({AGG_ERR} AND 0)", GROUPS),
    (f"{G} 0 AND {AGG_ERR}", []),
    (f"{G} {AGG_ERR} AND 1", ERROR),
    (f"{G} {AGG_ERR} AND -0", []),
    # Moved below the aggregate (#141): a WHERE term of its own.
    (f"{G} {ERR} OR 1", GROUPS),
    (f"{G} count(*) > 5 AND ({ERR} OR 1)", []),
    (f"{G} NOT ({ERR} AND 0)", GROUPS),
    (f"{G} ({ERR} AND 0) OR path > 'g'", [("src/utils.py", 2)]),
    (f"{G} {ERR} AND 0", ERROR),
    (f"{G} count(*) > 0 AND {ERR} AND 0", ERROR),
    # No GROUP BY: nothing moves, the whole HAVING is one condition.
    (f"SELECT count(*) FROM blame HAVING {AGG_ERR} AND 0", []),
    (f"SELECT count(*) FROM blame HAVING {CONSTERR} AND 0", []),
    (f"SELECT count(*) FROM blame HAVING count(*) > 0 AND {CONSTERR} AND 0", []),
    (f"SELECT count(*) FROM blame HAVING {CONSTERR} OR 1", [(3,)]),
    (f"SELECT count(*) FROM blame HAVING {AGG_ERR} OR 1", [(3,)]),
    (f"SELECT count(*) FROM blame HAVING {CONSTERR}", ERROR),
]


@pytest.mark.parametrize("query, expected", HAVING_CASES)
def test_having_follows_the_rule(tiny_repo, tiny_conn, query, expected):
    _check(tiny_conn, tiny_repo, query, expected)


# --- Aliases, row counts, LIMIT, ORDER BY, DISTINCT --------------------------

CLAUSE_CASES = [
    # A select-list alias for a literal is that literal (SQLite copies it).
    (f"SELECT 1 AS one, path FROM blame WHERE {ERR} OR one", [(1, p) for p, _ in ALL]),
    (f"SELECT 0 AS z, path FROM blame WHERE NOT ({ERR} AND z)", [(0, p) for p, _ in ALL]),
    (f"SELECT count(*) FROM blame WHERE {ERR} OR 1", [(3,)]),
    (f"SELECT count(*), max(path) FROM blame WHERE NOT ({ERR} OR 1)", [(0, None)]),
    (f"SELECT path FROM blame WHERE {ERR} OR 1 LIMIT 0", []),
    (f"SELECT DISTINCT path FROM blame WHERE {ERR} OR 1", [("feature/thing.py",), ("src/utils.py",)]),
]


@pytest.mark.parametrize("query, expected", CLAUSE_CASES)
def test_aliases_counts_and_clauses(tiny_repo, tiny_conn, query, expected):
    _check(tiny_conn, tiny_repo, query, expected)


def test_order_by_and_limit_keep_order(tiny_repo, tiny_conn):
    _check(
        tiny_conn,
        tiny_repo,
        f"SELECT path FROM blame WHERE {ERR} OR 1 ORDER BY path LIMIT 2",
        [("feature/thing.py",), ("src/utils.py",)],
        key_positions=[0],
    )


def test_limit_1_returns_one_row(tiny_repo, tiny_conn):
    """No ORDER BY, so which row is unspecified; one row, no error."""
    query = f"SELECT path FROM blame WHERE {ERR} OR 1 LIMIT 1"
    assert len(tiny_conn.execute(query).fetchall()) == 1
    assert len(run_historian(query, tiny_repo)[1]) == 1


# --- Zero rows ---------------------------------------------------------------


@pytest.fixture(scope="module")
def empty_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute(create_table_sql("blame", BLAME_SCHEMA))
    yield conn
    conn.close()


@pytest.fixture(scope="module")
def empty_tables():
    class _EmptyBlame:
        schema = BLAME_SCHEMA

        def __init__(self, repo) -> None:
            pass

        def capabilities(self) -> set[str]:
            return set()

        def scan(self, pushed=()) -> Iterator[tuple]:
            assert not pushed
            yield from ()

    return {"blame": _EmptyBlame}


EMPTY_CASES = [
    (f"SELECT path FROM blame WHERE {ERR} OR 1", []),
    (f"SELECT count(*) FROM blame WHERE {ERR} OR 1", [(0,)]),
    (f"SELECT count(*) FROM blame WHERE NOT ({ERR} AND 0)", [(0,)]),
    (f"SELECT path, count(*) FROM blame GROUP BY path HAVING {AGG_ERR} OR 1", []),
    (f"SELECT count(*) FROM blame HAVING {AGG_ERR} OR 1", [(0,)]),
]


@pytest.mark.parametrize("query, expected", EMPTY_CASES)
def test_zero_rows(tiny_repo, empty_conn, empty_tables, query, expected):
    _check(empty_conn, tiny_repo, query, expected, empty_tables)
