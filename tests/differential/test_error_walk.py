"""Which error SQLite reports when one expression holds several (issue
#144, spec §3 "Errors").

#115 fixed the order of the clauses; this file is about the order
inside one expression tree. SQLite resolves the names of a tree by
walking it, node first and then its children left to right, with one
error slot for the statement that each new error overwrites; whether
the walk goes on after an error depends on the node it meets (a
function call walks its arguments and returns normally, a column that
does not resolve stops the walk up to the nearest enclosing call, and
most other nodes stop at once when an error is already recorded). See
`_docs/decisions.md`, 2026-10-07, and `sql/bind_expr.py`'s docstring.

Every statement here fails at prepare time in SQLite, so the SQLite side
is an empty `blame` and nothing reaches git. The comparison is
`test_error_order.py`'s: exact for `no such column`, `no such
function` and `wrong number of arguments`, by kind for aggregate
misuse, whose wording is historian's own (#102). The oracle's answer
is always asked live. Where a case also names the answer the issue
measured, that is checked too, so a change of the pinned SQLite that
moves an answer fails loudly instead of quietly following it.

Three parts: the issue's table, one hand-written group per rule and
clause, and a generated sweep - random expression trees placed in
eleven statement shapes, the oracle deciding which are rejected.
"""

from __future__ import annotations

import random
import sqlite3

import pytest

from historian.sql.binder import BindError
from historian.tables.blame import BLAME_SCHEMA

from differential.conftest import create_table_sql, run_historian
from differential.test_error_order import _compare, _kind


@pytest.fixture(scope="module")
def empty_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute(create_table_sql("blame", BLAME_SCHEMA))
    yield conn
    conn.close()


def _assert_same_error(conn, repo, query: str) -> None:
    problem = _compare(conn, repo, query)
    assert problem is None, problem


def _oracle_message(conn, query: str) -> str:
    try:
        conn.execute(query)
    except sqlite3.OperationalError as error:
        return str(error)
    raise AssertionError(f"the oracle accepted a query the case expects to fail:\n  {query}")


def _assert_measured(conn, query: str, expected: str) -> None:
    """The oracle still gives the answer the issue measured: the exact
    message, or for a misuse only the kind."""
    actual = _oracle_message(conn, query)
    if expected == "misuse":
        assert _kind(actual) == "misuse", f"{query}: the oracle now says {actual}"
    else:
        assert actual == expected, f"{query}: the oracle now says {actual}"


# --- The issue's table -------------------------------------------------------

TABLE_CASES = [
    ("SELECT nofn(path), ghost FROM blame", "no such function: nofn"),
    ("SELECT ghost, nofn(path) FROM blame", "no such column: ghost"),
    ("SELECT path FROM blame WHERE nofn(path) = 1 AND ghost = 1", "no such function: nofn"),
    ("SELECT nofn(ghost) FROM blame", "no such column: ghost"),
    ("SELECT sum(ghost, 1) FROM blame", "no such column: ghost"),
    ("SELECT nofn(path) + ghost FROM blame", "no such column: ghost"),
    ("SELECT sum() + ghost FROM blame", "no such column: ghost"),
    ("SELECT ghost + nofn(path) FROM blame", "no such column: ghost"),
    ("SELECT ghost + sum() FROM blame", "no such column: ghost"),
    ("SELECT sum(ghost, 1) + nofn(path) FROM blame", "no such function: nofn"),
    ("SELECT nofn(path) + sum(ghost, 1) FROM blame", "no such column: ghost"),
    ("SELECT ghost_s FROM blame LIMIT nofn(1)", "no such function: nofn"),
    ("SELECT path FROM blame LIMIT nofn(1)", "no such function: nofn"),
    ("SELECT path FROM blame LIMIT avg() OFFSET -nosuch_l", "wrong number of arguments to function avg()"),
    ("SELECT path FROM blame LIMIT avg() OFFSET +nosuch_l", "wrong number of arguments to function avg()"),
    ("SELECT path FROM blame LIMIT avg() OFFSET nosuch_l+1", "wrong number of arguments to function avg()"),
    ("SELECT path FROM blame LIMIT avg() OFFSET count(*)+nosuch_l", "wrong number of arguments to function avg()"),
    ("SELECT path FROM blame ORDER BY count(*)", "misuse"),
]


def test_table_is_the_issues_table():
    assert len(TABLE_CASES) == 18
    assert len({query for query, _ in TABLE_CASES}) == len(TABLE_CASES)


@pytest.mark.parametrize(("query", "expected"), TABLE_CASES)
def test_issue_table(tiny_repo, empty_conn, query, expected):
    _assert_measured(empty_conn, query, expected)
    _assert_same_error(empty_conn, tiny_repo, query)


def test_limit_of_a_builtin_historian_does_not_have_is_still_rejected(tiny_repo, empty_conn):
    """The accepted difference (#183): SQLite accepts `LIMIT abs(2)`;
    historian knows no `abs` and reports it as an unknown function."""
    query = "SELECT path FROM blame LIMIT abs(2)"
    empty_conn.execute(query)
    with pytest.raises(BindError, match=r"^no such function: abs$"):
        run_historian(query, tiny_repo)


# --- One group per rule ------------------------------------------------------
#
# Each is `SELECT <expr> FROM blame` unless it is a whole statement.

RULE_CASES = [
    # Rule 1: a column that does not resolve records and stops the walk.
    ("ghost + nofn(1)", "no such column: ghost"),
    ("SELECT blame.ghost FROM blame", "no such column: blame.ghost"),
    ("SELECT ghost.path FROM blame", "no such column: ghost.path"),
    # Rule 2: a call records its own error before its arguments.
    ("nofn(ghost)", "no such column: ghost"),
    ("sum(ghost, 1)", "no such column: ghost"),
    ("nofn(nofn2(ghost))", "no such column: ghost"),
    ("sum(sum(ghost))", "no such column: ghost"),
    ("count(DISTINCT ghost)", "no such column: ghost"),
    ("nofn(*)", "no such function: nofn"),
    ("sum(*) + ghost", "no such column: ghost"),
    # The argument list stops at the first ABORT, and nothing else does.
    ("nofn(ghost1, ghost2)", "no such column: ghost1"),
    ("nofn(1, ghost)", "no such function: nofn"),
    ("nofn(path, ghost)", "no such column: ghost"),
    ("nofn(nofn2(1), ghost)", "no such column: ghost"),
    ("nofn(-ghost)", "no such function: nofn"),
    ("nofn(ghost + 1)", "no such function: nofn"),
    ("nofn(ghost1) + ghost2", "no such column: ghost2"),
    # Rule 3: LIKE is a call over (pattern, left, escape).
    ("ghost1 LIKE ghost2", "no such column: ghost2"),
    ("ghost1 LIKE ghost2 ESCAPE ghost3", "no such column: ghost2"),
    ("path LIKE 'a' ESCAPE ghost3", "no such column: ghost3"),
    ("nofn(1) + (ghost1 NOT LIKE ghost2)", "no such function: nofn"),
    ("nofn(1) + (ghost1 LIKE ghost2)", "no such column: ghost2"),
    # Rule 4: IS NULL walks its operand whatever is recorded.
    ("nofn(ghost IS NULL)", "no such column: ghost"),
    ("nofn(1) + (ghost IS NOT NULL)", "no such column: ghost"),
    # Rule 5: IS with a bare column on the right resolves it first.
    ("ghost1 IS ghost2", "no such column: ghost2"),
    ("ghost1 IS NOT ghost2", "no such column: ghost2"),
    ("nofn(1) + (path IS ghost2)", "no such column: ghost2"),
    ("nofn(ghost IS path)", "no such function: nofn"),
    ("nofn(1) IS path", "no such function: nofn"),
    ("(nofn(1) IS path) + ghost", "no such function: nofn"),
    ("ghost1 IS (ghost2)", "no such column: ghost2"),
    ("ghost1 IS blame.ghost2", "no such column: ghost1"),
    # Rule 6: every other node stops at once on a recorded error.
    ("nofn(1) + 1 + ghost", "no such function: nofn"),
    ("nofn(1) + path + ghost", "no such column: ghost"),
    ("path BETWEEN ghost1 AND ghost2", "no such column: ghost1"),
    ("nofn(1) + (path IN (ghost1, ghost2))", "no such function: nofn"),
    ("nofn(1) + (NOT ghost)", "no such function: nofn"),
    ("nofn(1) || -ghost", "no such function: nofn"),
    ("nofn(1) AND ghost", "no such column: ghost"),
    ("nofn(1) OR (ghost = 1)", "no such function: nofn"),
]


def _statement(case: str) -> str:
    return case if case.startswith("SELECT ") else f"SELECT {case} FROM blame"


@pytest.mark.parametrize(("case", "expected"), RULE_CASES)
def test_rule(tiny_repo, empty_conn, case, expected):
    query = _statement(case)
    _assert_measured(empty_conn, query, expected)
    _assert_same_error(empty_conn, tiny_repo, query)


# --- Aggregate misuse inside an expression -----------------------------------

MISUSE_CASES = [
    ("SELECT sum(count(*)) + ghost FROM blame", "no such column: ghost"),
    ("SELECT ghost + sum(count(*)) FROM blame", "no such column: ghost"),
    ("SELECT nofn(1) + sum(count(*)) FROM blame", "misuse"),
    ("SELECT sum(count(*)) FROM blame", "misuse"),
    ("SELECT sum(1 + count(*)) FROM blame", "misuse"),
    ("SELECT sum(count(*), 1) FROM blame", "wrong number of arguments to function sum()"),
    ("SELECT path FROM blame WHERE count(*) + ghost > 1", "no such column: ghost"),
    ("SELECT path FROM blame WHERE nofn(1) + count(*) > 1", "misuse"),
    ("SELECT path FROM blame WHERE sum(count(*)) > 1", "misuse"),
    ("SELECT count(*) FROM blame WHERE sum(path) + ghost > 1", "no such column: ghost"),
    ("SELECT count(*) FROM blame WHERE nofn(1) + sum(path) > 1", "no such function: nofn"),
    ("SELECT count(*) FROM blame WHERE sum(count(*)) > 1 AND ghost = 1", "misuse"),
    ("SELECT count(*) FROM blame WHERE ghost = 1 AND sum(count(*)) > 1", "no such column: ghost"),
    ("SELECT count(*) AS c FROM blame HAVING nofn(1) + sum(c) > 1", "misuse"),
    ("SELECT count(*) AS c FROM blame HAVING sum(c) + nofn(1) > 1", "no such function: nofn"),
    ("SELECT count(*) AS c FROM blame HAVING sum(path IS c) > 0", "misuse"),
    ("SELECT count(*) AS c FROM blame HAVING sum(c IS path) > 0", "misuse"),
    ("SELECT count(*) AS c FROM blame WHERE sum(c) + nofn(1) > 1", "no such function: nofn"),
    ("SELECT count(*) AS c FROM blame WHERE nofn(1) + sum(c) > 1", "misuse"),
    ("SELECT count(*) AS c FROM blame ORDER BY sum(c) + ghost", "no such column: ghost"),
    ("SELECT path FROM blame ORDER BY count(*) + ghost", "no such column: ghost"),
    ("SELECT path FROM blame ORDER BY nofn(1) + count(*)", "no such function: nofn"),
    ("SELECT DISTINCT path FROM blame ORDER BY count(*)", "misuse"),
]


@pytest.mark.parametrize(("query", "expected"), MISUSE_CASES)
def test_aggregate_misuse(tiny_repo, empty_conn, query, expected):
    _assert_measured(empty_conn, query, expected)
    _assert_same_error(empty_conn, tiny_repo, query)


# --- LIMIT and OFFSET: one root, LIMIT first ---------------------------------

LIMIT_CASES = [
    ("SELECT path FROM blame LIMIT nofn(1) OFFSET ghost", "no such column: ghost"),
    ("SELECT path FROM blame LIMIT ghost OFFSET nofn(1)", "no such column: ghost"),
    ("SELECT path FROM blame LIMIT 1 OFFSET nofn(1)", "no such function: nofn"),
    ("SELECT path FROM blame LIMIT nofn(1) OFFSET 1", "no such function: nofn"),
    ("SELECT path FROM blame LIMIT -nofn(1)", "no such function: nofn"),
    ("SELECT path FROM blame LIMIT (nofn(1))", "no such function: nofn"),
    ("SELECT path FROM blame LIMIT nofn(path)", "no such column: path"),
    ("SELECT path FROM blame LIMIT sum() OFFSET -1", "wrong number of arguments to function sum()"),
    ("SELECT path FROM blame LIMIT count(*) OFFSET nofn(1)", "no such function: nofn"),
    ("SELECT ghost_s FROM blame LIMIT nofn(1)", "no such function: nofn"),
    ("SELECT path FROM blame LIMIT nofn(1) OFFSET path IS NULL", "no such column: path"),
    ("SELECT path FROM blame LIMIT nofn(1) OFFSET 1 IS path", "no such column: path"),
    ("SELECT path FROM blame LIMIT 1 OFFSET 'a' LIKE ghost", "no such column: ghost"),
    ("SELECT path AS p FROM blame LIMIT nofn(p)", "no such column: p"),
    ("SELECT path FROM blame LIMIT count(*) OFFSET sum(1)", "misuse"),
]


@pytest.mark.parametrize(("query", "expected"), LIMIT_CASES)
def test_limit_offset(tiny_repo, empty_conn, query, expected):
    _assert_measured(empty_conn, query, expected)
    _assert_same_error(empty_conn, tiny_repo, query)


# --- The same shapes in every clause, and a second root ----------------------

CLAUSE_CASES = [
    ("SELECT path FROM blame ORDER BY nofn(1) + ghost", "no such column: ghost"),
    ("SELECT path FROM blame ORDER BY nofn(1) + 1 + ghost", "no such function: nofn"),
    ("SELECT count(*) FROM blame HAVING sum() + ghost > 1", "no such column: ghost"),
    ("SELECT count(*) FROM blame HAVING sum() + 1 + ghost > 1", "wrong number of arguments to function sum()"),
    ("SELECT path FROM blame GROUP BY nofn(ghost)", "no such column: ghost"),
    ("SELECT path FROM blame GROUP BY nofn(1, ghost)", "no such function: nofn"),
    ("SELECT path FROM blame GROUP BY path, nofn(1) + path + ghost", "no such column: ghost"),
    ("SELECT path FROM blame WHERE ghost1 LIKE ghost2", "no such column: ghost2"),
    ("SELECT path FROM blame WHERE nofn(1) + (ghost IS NULL) > 0", "no such column: ghost"),
    # The second select item or ORDER BY term is never reached.
    ("SELECT nofn(1) + 1, ghost FROM blame", "no such function: nofn"),
    ("SELECT path, nofn(1) + ghost FROM blame", "no such column: ghost"),
    ("SELECT nofn(1), nofn2(ghost) FROM blame", "no such function: nofn"),
    ("SELECT path FROM blame ORDER BY nofn(1), ghost", "no such function: nofn"),
    ("SELECT path FROM blame ORDER BY path, nofn(ghost)", "no such column: ghost"),
    ("SELECT path FROM blame GROUP BY nofn(1), ghost", "no such function: nofn"),
]


@pytest.mark.parametrize(("query", "expected"), CLAUSE_CASES)
def test_clause_shapes(tiny_repo, empty_conn, query, expected):
    _assert_measured(empty_conn, query, expected)
    _assert_same_error(empty_conn, tiny_repo, query)


# --- An ordinal SQLite rejects at its own turn --------------------------------
#
# Found by the sweep below, outside the walk: SQLite checks an ORDER BY
# or GROUP BY term that is an integer (through any unary signs and
# parentheses) when it reaches that term, and rejects it there if it is
# below 1 or above 65535; an ordinal in between that is past the end of
# the select list is rejected only after every term's names (#115).
# A value outside a 32-bit int is no ordinal to SQLite at all.

ORDINAL_CASES = [
    ("SELECT path FROM blame ORDER BY 0, ghost", "1st ORDER BY term out of range - should be between 1 and 1"),
    ("SELECT path FROM blame ORDER BY -1, ghost", "1st ORDER BY term out of range - should be between 1 and 1"),
    ("SELECT path FROM blame ORDER BY - - 0, ghost", "1st ORDER BY term out of range - should be between 1 and 1"),
    ("SELECT path FROM blame ORDER BY 65536, ghost", "1st ORDER BY term out of range - should be between 1 and 1"),
    ("SELECT path FROM blame ORDER BY path, 0, ghost", "2nd ORDER BY term out of range - should be between 1 and 1"),
    ("SELECT path FROM blame ORDER BY (-1), count(count(*))", "1st ORDER BY term out of range - should be between 1 and 1"),
    ("SELECT path FROM blame ORDER BY ghost, 0", "no such column: ghost"),
    ("SELECT path FROM blame ORDER BY 65535, ghost", "no such column: ghost"),
    ("SELECT path FROM blame ORDER BY 2, ghost", "no such column: ghost"),
    ("SELECT path FROM blame ORDER BY 2147483648, ghost", "no such column: ghost"),
    ("SELECT path FROM blame GROUP BY 0, ghost", "1st GROUP BY term out of range - should be between 1 and 1"),
    ("SELECT path FROM blame GROUP BY -1, ghost", "1st GROUP BY term out of range - should be between 1 and 1"),
    ("SELECT path FROM blame GROUP BY 70000, ghost", "1st GROUP BY term out of range - should be between 1 and 1"),
    ("SELECT path FROM blame GROUP BY 2, ghost", "no such column: ghost"),
]


@pytest.mark.parametrize(("query", "expected"), ORDINAL_CASES)
def test_ordinal_rejected_at_its_turn(tiny_repo, empty_conn, query, expected):
    _assert_measured(empty_conn, query, expected)
    _assert_same_error(empty_conn, tiny_repo, query)


# --- The generated sweep -----------------------------------------------------
#
# Random expression trees up to three levels deep over the leaves and
# nodes the rule names, each placed in eleven statement shapes: the
# issue's nine (a select item, the first of two, WHERE, HAVING, GROUP
# BY, ORDER BY alone and as the second term, LIMIT with OFFSET, OFFSET
# alone) and two more for the clauses where an aggregate is a late
# error or an alias can name one (WHERE and ORDER BY of an aggregate
# query with `count(*) AS c`). `c` is a leaf everywhere: an alias of an
# aggregate where the shape declares it, an unknown column elsewhere.
# The generator knows nothing of the rule: SQLite decides which
# statements are rejected (prepare only, through EXPLAIN) and what each
# one's error is. Seeded, so the statements are the same on every run.

_LEAVES = ("ghost1", "ghost2", "path", "line_no", "1", "'a'", "c")
_IS_RIGHT = ("ghost1", "ghost2", "path", "line_no", "c")
_CALLS = (
    ("nofn", 0),
    ("nofn", 1),
    ("nofn", 2),
    ("sum", 0),
    ("sum", 1),
    ("sum", 2),
    ("count", 1),
    ("count", "*"),
    ("count", "DISTINCT"),
    ("max", 1),
)
_BINARY = ("+", "*", "||", "=", "AND", "OR")


def _tree(rnd: random.Random, depth: int) -> str:
    if depth == 0 or rnd.random() < 0.25:
        return rnd.choice(_LEAVES)
    below = depth - 1
    pick = rnd.random()
    if pick < 0.32:
        name, arity = rnd.choice(_CALLS)
        if arity == "*":
            return f"{name}(*)"
        if arity == "DISTINCT":
            return f"{name}(DISTINCT {_tree(rnd, below)})"
        return f"{name}({', '.join(_tree(rnd, below) for _ in range(arity))})"
    if pick < 0.40:
        form = rnd.randrange(3)
        if form == 0:
            return f"({_tree(rnd, below)} LIKE {_tree(rnd, below)})"
        if form == 1:
            return f"({_tree(rnd, below)} NOT LIKE {_tree(rnd, below)})"
        return f"({_tree(rnd, below)} LIKE {_tree(rnd, below)} ESCAPE {_tree(rnd, below)})"
    if pick < 0.47:
        return f"({_tree(rnd, below)} IS {rnd.choice(('', 'NOT '))}NULL)"
    if pick < 0.54:
        right = rnd.choice(_IS_RIGHT) if rnd.random() < 0.7 else _tree(rnd, below)
        return f"({_tree(rnd, below)} IS {rnd.choice(('', 'NOT '))}{right})"
    if pick < 0.60:
        return f"({_tree(rnd, below)} {rnd.choice(('', 'NOT '))}BETWEEN {_tree(rnd, below)} AND {_tree(rnd, below)})"
    if pick < 0.66:
        values = ", ".join(_tree(rnd, below) for _ in range(rnd.randrange(1, 3)))
        return f"({_tree(rnd, below)} {rnd.choice(('', 'NOT '))}IN ({values}))"
    if pick < 0.74:
        return f"({rnd.choice(('-', '+', 'NOT '))}{_tree(rnd, below)})"
    return f"({_tree(rnd, below)} {rnd.choice(_BINARY)} {_tree(rnd, below)})"


SWEEP_SHAPES = (
    "SELECT {t} FROM blame",
    "SELECT {t}, {u} FROM blame",
    "SELECT path FROM blame WHERE {t}",
    "SELECT count(*) AS c FROM blame HAVING {t}",
    "SELECT path FROM blame GROUP BY {t}",
    "SELECT path FROM blame ORDER BY {t}",
    "SELECT path FROM blame ORDER BY {u}, {t}",
    "SELECT path FROM blame LIMIT {t} OFFSET {u}",
    "SELECT path FROM blame LIMIT 1 OFFSET {t}",
    "SELECT count(*) AS c FROM blame WHERE {t}",
    "SELECT count(*) AS c FROM blame ORDER BY {t}",
)

_SWEEP_SEED = 144
_SWEEP_TREES = 3000


def _rejected_sweep() -> list[str]:
    """The sweep's statements SQLite rejects at prepare time, in
    generation order, each once."""
    conn = sqlite3.connect(":memory:")
    conn.execute(create_table_sql("blame", BLAME_SCHEMA))
    rnd = random.Random(_SWEEP_SEED)
    seen: set[str] = set()
    rejected: list[str] = []
    for _ in range(_SWEEP_TREES):
        t = _tree(rnd, 3)
        u = _tree(rnd, 2)
        for shape in SWEEP_SHAPES:
            query = shape.format(t=t, u=u)
            if query in seen:
                continue
            seen.add(query)
            try:
                conn.execute("EXPLAIN " + query)
            except sqlite3.OperationalError:
                rejected.append(query)
    conn.close()
    return rejected


SWEEP = _rejected_sweep()


def test_sweep_size():
    """At least 20,000 rejected statements, as the issue requires."""
    assert len(SWEEP) >= 20_000


@pytest.mark.parametrize("query", SWEEP)
def test_sweep(tiny_repo, empty_conn, query):
    _assert_same_error(empty_conn, tiny_repo, query)
