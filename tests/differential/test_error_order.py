"""Which error is reported when a statement has several, against
SQLite (issue #115, spec §3 "Errors").

Every case here fails at prepare time in SQLite, so the SQLite side is
an empty `blame` table declared with the harness's own
`create_table_sql`, and the historian side is `run_historian`, which
raises `BindError` from `bind()` before any scan exists - no git work.
A case passes when historian raises `BindError` whose message is
SQLite's exactly (no such table/column/function, wrong number of
arguments, aggregate in GROUP BY, term out of range), or, for the
kinds whose wording is historian's own, whose kind is SQLite's (every
`misuse of aggregate ...`, and `HAVING clause on a non-aggregate
query`). The SQLite side is computed live, never hard-coded.

The order, re-measured against the oracle (Python's `sqlite3` module,
SQLite 3.45.1 - see #117) with the pair/triple sweep below: the FROM
table and an unknown `x.*` qualifier; LIMIT then OFFSET (a column
reference outside any aggregate call at once; an error inside an
aggregate call only after both, the last one found winning); the
select list left to right; HAVING on a non-aggregate query; HAVING;
WHERE; ORDER BY; GROUP BY; then an aggregate call in the WHERE of an
aggregate query or the ORDER BY of a non-aggregate one. Within one
ORDER BY/GROUP BY clause: name errors, then an out-of-range ordinal,
then (GROUP BY) an aggregate key. See `_docs/decisions.md`,
2026-10-02.

Two parts: the issue's own table, and the sweep - every pair and
every triple of fragments from distinct clauses of a catalog of 27
erroring fragments in 7 clauses, each spliced into three base queries
(grouped, plain and aggregate-by-select-list). Pairs always run; the
triples (5,242 queries) run only with HISTORIAN_ERROR_SWEEP=full, since
they add little the pairs and the table do not already pin.
"""

from __future__ import annotations

import itertools
import os
import re
import sqlite3

import pytest

from historian.sql.binder import BindError
from historian.tables.blame import BLAME_SCHEMA

from differential.conftest import create_table_sql, run_historian

# --- Comparing two errors -------------------------------------------------

#: Kinds whose message must equal SQLite's byte for byte.
_EXACT_KINDS = {"table", "column", "function", "arity", "group_agg", "range"}

_PATTERNS = [
    ("table", re.compile(r"no such table: (.+)")),
    ("column", re.compile(r"no such column: (.+)")),
    ("function", re.compile(r"no such function: (.+)")),
    ("arity", re.compile(r"wrong number of arguments to function (.+)\(\)")),
    ("group_agg", re.compile(r"aggregate functions are not allowed in the GROUP BY clause")),
    ("range", re.compile(r"\d+(st|nd|rd|th) (GROUP|ORDER) BY term out of range - should be between 1 and \d+")),
    ("misuse", re.compile(r"misuse of (aggregate|aliased aggregate)\b.*")),
    ("having_nonagg", re.compile(r"HAVING clause on a non-aggregate query|HAVING requires an aggregate query\b.*")),
]


def _kind(message: str) -> str:
    for kind, pattern in _PATTERNS:
        if pattern.fullmatch(message):
            return kind
    return "other"


@pytest.fixture(scope="module")
def empty_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute(create_table_sql("blame", BLAME_SCHEMA))
    yield conn
    conn.close()


def _sqlite_error(conn: sqlite3.Connection, query: str) -> str:
    try:
        conn.execute(query)
    except sqlite3.OperationalError as error:
        return str(error)
    raise AssertionError(f"the oracle accepted a query the case expects to fail:\n  {query}")


def _compare(conn, repo, query: str) -> str | None:
    """`None` when historian reports the error SQLite reports, else a
    description of the difference."""
    expected = _sqlite_error(conn, query)
    kind = _kind(expected)
    if kind == "other":
        return f"oracle raised an error this file does not classify: {expected}\n  {query}"
    try:
        run_historian(query, repo)
    except BindError as error:
        got = str(error)
    except Exception as error:  # noqa: BLE001 - reported, not swallowed
        return f"historian raised {type(error).__name__}: {error}, sqlite {expected}\n  {query}"
    else:
        return f"historian accepted it, sqlite raised {expected}\n  {query}"
    if kind in _EXACT_KINDS:
        if got != expected:
            return f"historian: {got}\n  sqlite:    {expected}\n  {query}"
    elif _kind(got) != kind:
        return f"historian: {got} (kind {_kind(got)})\n  sqlite:    {expected} (kind {kind})\n  {query}"
    return None


def _assert_same_error(conn, repo, query: str) -> None:
    problem = _compare(conn, repo, query)
    assert problem is None, problem


# --- The issue's own table -------------------------------------------------
#
# Each row is one query from #115's "Required cases". Unique ghost
# names make the message identify the clause.

TABLE_CASES = [
    # FROM table, qualified star
    "SELECT ghost_s FROM ghost_t WHERE ghost_w = 1 GROUP BY ghost_g HAVING ghost_h = 1 ORDER BY ghost_o LIMIT ghost_l",
    "SELECT ghost.*, ghost_s FROM blame",
    "SELECT ghost_s, ghost.* FROM blame",
    # LIMIT / OFFSET
    "SELECT ghost_s FROM blame LIMIT ghost_l",
    "SELECT path FROM blame LIMIT path",
    "SELECT path AS p FROM blame LIMIT p",
    "SELECT path FROM blame LIMIT 1 OFFSET ghost_o",
    "SELECT path FROM blame LIMIT ghost_l OFFSET ghost_o",
    "SELECT ghost_s FROM blame LIMIT count(*)",
    "SELECT ghost_s FROM blame LIMIT 1+1",
    "SELECT ghost_s FROM blame WHERE ghost_w = 1 GROUP BY ghost_g HAVING ghost_h = 1 ORDER BY ghost_o LIMIT ghost_l",
    # Select list, HAVING on a non-aggregate query
    "SELECT ghost_s FROM blame HAVING path = 'x'",
    "SELECT path FROM blame HAVING ghost_h = 1",
    "SELECT path FROM blame WHERE ghost_w = 1 HAVING path = 'x'",
    "SELECT path FROM blame HAVING path = 'x' ORDER BY ghost_o",
    "SELECT path FROM blame HAVING path = 'x' LIMIT ghost_l",
    "SELECT count(count(*)) FROM blame WHERE ghost_w = 1",
    # HAVING, WHERE, ORDER BY, GROUP BY
    "SELECT count(*) FROM blame WHERE ghost_w = 1 HAVING ghost_h = 1",
    "SELECT path FROM blame WHERE ghost_w = 1 ORDER BY ghost_o",
    "SELECT path FROM blame WHERE ghost_w = 1 GROUP BY ghost_g",
    "SELECT path FROM blame GROUP BY ghost_g HAVING ghost_h = 1",
    "SELECT path FROM blame GROUP BY ghost_g ORDER BY ghost_o",
    "SELECT count(*) FROM blame HAVING ghost_h = 1 ORDER BY ghost_o",
    "SELECT path FROM blame WHERE ghost_w = 1 GROUP BY ghost_g HAVING ghost_h = 1 ORDER BY ghost_o",
    "SELECT path FROM blame WHERE ghost = 1 GROUP BY ghost",
    # Other kinds across clauses
    "SELECT path FROM blame WHERE nofn_w(path) = 1 GROUP BY ghost_g",
    "SELECT path FROM blame GROUP BY ghost_g ORDER BY nofn_o(path)",
    "SELECT path FROM blame WHERE avg() = 1 GROUP BY ghost_g",
    "SELECT path FROM blame GROUP BY 99 ORDER BY ghost_o",
    "SELECT path FROM blame GROUP BY count(*) HAVING ghost_h = 1",
    "SELECT path FROM blame WHERE ghost_w = 1 GROUP BY count(*)",
    "SELECT count(*) AS c FROM blame WHERE ghost_w = 1 HAVING count(c) > 0",
    # Inside one clause
    "SELECT path FROM blame ORDER BY 99, ghost_o",
    "SELECT path FROM blame ORDER BY 99, nofn(path)",
    "SELECT path FROM blame GROUP BY 99, ghost_g",
    "SELECT path FROM blame GROUP BY count(*), 99",
    "SELECT path FROM blame GROUP BY count(*), ghost_g",
    "SELECT path FROM blame GROUP BY count(*), nofn(path)",
    "SELECT path FROM blame WHERE avg() = 1",
    "SELECT count(*) FROM blame WHERE sum(1, 2) > 1",
    "SELECT nofn(path), ghost FROM blame",
    "SELECT ghost, nofn(path) FROM blame",
    # Late aggregate misuse
    "SELECT count(*) FROM blame WHERE count(*) > 1 AND ghost_w = 1",
    "SELECT path FROM blame WHERE count(*) > 1 GROUP BY path ORDER BY ghost_o",
    "SELECT count(*) FROM blame WHERE count(*) > 1 HAVING ghost_h = 1",
    "SELECT count(*) FROM blame WHERE count(*) > 1 LIMIT ghost_l",
    "SELECT path FROM blame WHERE count(*) > 1 AND ghost_w = 1",
    "SELECT path FROM blame WHERE count(*) > 1 ORDER BY ghost_o",
    "SELECT path FROM blame ORDER BY count(*), ghost_o",
    "SELECT path FROM blame ORDER BY count(*), 99",
    "SELECT path FROM blame ORDER BY count(*) LIMIT ghost_l",
    "SELECT path FROM blame WHERE ghost_w = 1 ORDER BY count(*)",
    "SELECT count(*) AS c FROM blame WHERE c > 1 AND ghost_w = 1",
    # Aliases (#32)
    "SELECT path AS p FROM blame WHERE ghost_w = p GROUP BY ghost_g",
    "SELECT path AS p FROM blame WHERE p = 'x' GROUP BY ghost_g",
    "SELECT path AS p FROM blame GROUP BY blame.p ORDER BY ghost_o",
    "SELECT path AS p FROM blame GROUP BY p HAVING ghost_h = 1",
    "SELECT path AS p, p FROM blame WHERE ghost_w = 1",
    # Historian-only rejections run last
    "SELECT path, count(*) FROM blame WHERE ghost_w = 1",
    "SELECT path, count(*) FROM blame HAVING ghost_h = 1",
    "SELECT path, count(*) FROM blame ORDER BY ghost_o",
    "SELECT path, count(*) FROM blame LIMIT ghost_l",
    "SELECT path, count(*) FROM blame WHERE count(*) > 1",
    "SELECT line, count(*) FROM blame GROUP BY path ORDER BY ghost_o",
    "SELECT count(*) FROM blame HAVING path = 'x' ORDER BY ghost_o",
    "SELECT path, count(*) FROM blame GROUP BY path HAVING line = 'x' ORDER BY ghost_o",
    "SELECT DISTINCT path FROM blame ORDER BY line LIMIT ghost_l",
    "SELECT DISTINCT path FROM blame ORDER BY count(*), ghost_o",
]

#: Found while re-measuring the order (not in the issue's table): the
#: oracle's answer for LIMIT and OFFSET together, nested aggregates in
#: place in every clause, and ordinal suffixes past the first term.
MEASURED_CASES = [
    "SELECT path FROM blame LIMIT avg(1) OFFSET ghost_f",
    "SELECT path FROM blame LIMIT ghost_l OFFSET avg(1)",
    "SELECT ghost_s FROM blame LIMIT 1 OFFSET avg(1)",
    "SELECT ghost_s FROM blame LIMIT avg() OFFSET 1",
    "SELECT path FROM blame LIMIT avg() OFFSET ghost_f",
    "SELECT path FROM blame LIMIT count(ghost_x)",
    "SELECT path FROM blame LIMIT count(ghost_x) OFFSET ghost_f",
    "SELECT path FROM blame LIMIT count(*) + ghost_l OFFSET ghost_f",
    "SELECT ghost_s FROM blame LIMIT ghost_l + 1",
    "SELECT ghost_s FROM blame LIMIT -ghost_l",
    "SELECT ghost_s FROM blame LIMIT blame.path",
    "SELECT path FROM blame LIMIT count(*) OFFSET count(ghost_y)",
    "SELECT count(*) FROM blame WHERE count(count(*)) > 1 AND ghost_w = 1",
    "SELECT count(*) FROM blame WHERE ghost_w = 1 AND count(count(*)) > 1",
    "SELECT count(*) AS c FROM blame WHERE count(c) > 1 AND ghost_w = 1",
    "SELECT path FROM blame ORDER BY count(count(*)), ghost_o",
    "SELECT path FROM blame GROUP BY path ORDER BY 99, count(count(*))",
    "SELECT path FROM blame GROUP BY path HAVING count(count(*)) > 1 AND ghost_h = 1",
    "SELECT path FROM blame GROUP BY path HAVING ghost_h = 1 AND count(count(*)) > 1",
    "SELECT avg(min(line_no)), ghost_s FROM blame",
    "SELECT ghost_s, avg(min(line_no)) FROM blame",
    "SELECT path FROM blame ORDER BY count(*), nofn(1)",
    "SELECT path FROM blame ORDER BY count(*), avg()",
    "SELECT count(*) FROM blame WHERE count(*) > 1 ORDER BY 99",
    "SELECT count(*) FROM blame WHERE count(*) > 1 GROUP BY count(*)",
    "SELECT path FROM blame ORDER BY 1, 99",
    "SELECT path FROM blame ORDER BY 1, 1, 99",
    "SELECT path FROM blame GROUP BY path, path, path, 99",
    "SELECT path FROM blame ORDER BY 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 99",
    "SELECT path FROM blame ORDER BY 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 99",
    "SELECT path FROM blame ORDER BY 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 99",
    "SELECT count(*) AS c FROM blame GROUP BY c, 99",
    "SELECT path FROM blame WHERE sum(*) > 1 GROUP BY ghost_g",
]


def test_table_is_the_issues_table():
    assert len(TABLE_CASES) == 68
    assert len(set(TABLE_CASES)) == len(TABLE_CASES)


@pytest.mark.parametrize("query", TABLE_CASES)
def test_issue_table(tiny_repo, empty_conn, query):
    _assert_same_error(empty_conn, tiny_repo, query)


@pytest.mark.parametrize("query", MEASURED_CASES)
def test_measured_cases(tiny_repo, empty_conn, query):
    _assert_same_error(empty_conn, tiny_repo, query)


def test_same_name_twice_reports_the_where_occurrence(tiny_repo, empty_conn):
    """The message is the same either way; the position is the one of
    the clause SQLite reports, WHERE's."""
    query = "SELECT path FROM blame WHERE ghost = 1 GROUP BY ghost"
    _assert_same_error(empty_conn, tiny_repo, query)
    with pytest.raises(BindError) as exc_info:
        run_historian(query, tiny_repo)
    assert (exc_info.value.position.line, exc_info.value.position.column, exc_info.value.position.offset) == (1, 30, 29)


# --- The sweep --------------------------------------------------------------
#
# One erroring fragment per clause, spliced into a base query in place
# of that clause. Unique names (and, for the aggregate-misuse kinds,
# distinct function names) so a reader can tell from SQLite's message
# which fragment it reported. A fragment is used with a base only if
# it errors on its own there (`max(line_no)` in ORDER BY is legal in
# an aggregate query).

FRAGMENTS = {
    "from-table": ("from", "ghost_t"),
    "select-column": ("select", "ghost_s"),
    "select-function": ("select", "nofn_s(path)"),
    "select-arity": ("select", "sum()"),
    "select-nested": ("select", "avg(min(line_no))"),
    "select-qualified-star": ("select", "ghost_q.*"),
    "where-column": ("where", "ghost_w = 1"),
    "where-function": ("where", "nofn_w(path) = 1"),
    "where-arity": ("where", "avg() = 1"),
    "where-aggregate": ("where", "count(*) > 0"),
    "group-column": ("group", "ghost_g"),
    "group-function": ("group", "nofn_g(path)"),
    "group-arity": ("group", "min()"),
    "group-aggregate": ("group", "count(*)"),
    "group-ordinal": ("group", "99"),
    "having-column": ("having", "ghost_h = 1"),
    "having-function": ("having", "nofn_h(path) = 1"),
    "having-arity": ("having", "max() = 1"),
    "having-nested": ("having", "avg(sum(line_no)) > 0"),
    "order-column": ("order", "ghost_o"),
    "order-function": ("order", "nofn_o(path)"),
    "order-arity": ("order", "count(1, 2)"),
    "order-ordinal": ("order", "99"),
    "order-aggregate": ("order", "max(line_no)"),
    "limit-column": ("limit", "ghost_l"),
    "limit-aggregate": ("limit", "avg(1)"),
    "limit-offset-column": ("limit", "1 OFFSET ghost_f"),
}

BASES = {
    "grouped": {
        "select": "path",
        "from": "blame",
        "where": "line_no = 1",
        "group": "path",
        "having": "count(*) > 0",
        "order": "path",
        "limit": "1",
    },
    "plain": {
        "select": "path",
        "from": "blame",
        "where": "line_no = 1",
        "group": None,
        "having": None,
        "order": "path",
        "limit": "1",
    },
    "aggregate": {
        "select": "count(*)",
        "from": "blame",
        "where": "line_no = 1",
        "group": None,
        "having": "count(*) > 0",
        "order": "1",
        "limit": "1",
    },
}

_FULL_SWEEP = os.environ.get("HISTORIAN_ERROR_SWEEP") == "full"


def _build(base: str, fragment_names) -> str:
    parts = dict(BASES[base])
    for name in fragment_names:
        slot, text = FRAGMENTS[name]
        parts[slot] = text
    query = f"SELECT {parts['select']} FROM {parts['from']}"
    for slot, keyword in (("where", "WHERE"), ("group", "GROUP BY"), ("having", "HAVING"), ("order", "ORDER BY"), ("limit", "LIMIT")):
        if parts[slot] is not None:
            query += f" {keyword} {parts[slot]}"
    return query


def _usable(base: str) -> list[str]:
    conn = sqlite3.connect(":memory:")
    conn.execute(create_table_sql("blame", BLAME_SCHEMA))
    usable = []
    for name in FRAGMENTS:
        try:
            conn.execute(_build(base, [name]))
        except sqlite3.OperationalError:
            usable.append(name)
    conn.close()
    return usable


def _sweep(size: int) -> list:
    cases = []
    for base in BASES:
        for combo in itertools.combinations(_usable(base), size):
            if len({FRAGMENTS[name][0] for name in combo}) < size:
                continue
            cases.append(pytest.param(_build(base, combo), id=f"{base}:{'+'.join(combo)}"))
    return cases


_SINGLES = _sweep(1)
_PAIRS = _sweep(2)
_TRIPLES = _sweep(3) if _FULL_SWEEP else [
    pytest.param("", id="set HISTORIAN_ERROR_SWEEP=full", marks=pytest.mark.skip(reason="triples run with HISTORIAN_ERROR_SWEEP=full"))
]


def test_sweep_size():
    """27 fragments, 26 of them errors on their own in the grouped and
    aggregate bases (`max(line_no)` in ORDER BY is legal there), all
    27 in the plain base."""
    assert len(_SINGLES) == 26 + 27 + 26
    assert len(_PAIRS) == 284 + 306 + 284
    if _FULL_SWEEP:
        assert len(_TRIPLES) == 1682 + 1878 + 1682


@pytest.mark.parametrize("query", _SINGLES)
def test_sweep_singles(tiny_repo, empty_conn, query):
    _assert_same_error(empty_conn, tiny_repo, query)


@pytest.mark.parametrize("query", _PAIRS)
def test_sweep_pairs(tiny_repo, empty_conn, query):
    _assert_same_error(empty_conn, tiny_repo, query)


@pytest.mark.parametrize("query", _TRIPLES)
def test_sweep_triples(tiny_repo, empty_conn, query):
    _assert_same_error(empty_conn, tiny_repo, query)
