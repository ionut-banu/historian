"""Differential tests over a synthetic table with a `REAL` column
(issue #140).

No catalog table has a `REAL` column yet, so these drive a stub scan
through the harness's two real call sites - `load_unfiltered` for
SQLite and `run_historian` for historian - with a one-table catalog
`t (s TEXT, i INTEGER, r REAL)`.

What they pin is the loader: a plain `REAL` column in SQLite stores a
bound `-0.0` as `0.0` and an `int` as a float, and a column with no
declared type keeps the bits but loses `REAL` affinity. The harness
loads a typeless raw table behind a `CAST(r AS REAL)` view named `t`
(`conftest.load_table_sql`), which keeps both. Measured with Python's
bundled `sqlite3`, by `float.hex()`; `_docs/decisions.md`,
2026-10-02.

Mutant checks (issue #140): making `load_table_sql` return the plain
`create_table_sql` fails `test_bare_select_keeps_every_real_bit_pattern`
(SQLite reads `0.0` where historian has `-0.0`); making it a typeless
table with no view fails `test_real_affinity_survives_the_load`.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

from differential.conftest import (
    NAN_MESSAGE,
    assert_rows_match,
    load_unfiltered,
    run_historian,
)
from historian.schema import Column, ColumnType, Row, Schema

SCHEMA = Schema(
    columns=(
        Column("s", ColumnType.TEXT),
        Column("i", ColumnType.INTEGER),
        Column("r", ColumnType.REAL),
    )
)
CATALOG = {"t": SCHEMA}

INF = float("inf")

ROWS: list[Row] = [
    ("neg zero", 1, -0.0),
    ("zero", 2, 0.0),
    ("one and a half", 3, 1.5),
    ("null", 4, None),
    ("huge", 5, 1e308),
    ("neg huge", 6, -1e308),
    ("denormal", 7, 5e-324),
    ("inf", 8, INF),
    ("neg inf", 9, -INF),
    ("two to the 53", 10, 9007199254740992.0),
]


class _StubSource:
    """A `ScanSource` over fixed rows, with no pushdown."""

    schema = SCHEMA

    def __init__(self, rows: Sequence[Row]) -> None:
        self._rows = list(rows)

    def capabilities(self) -> set[str]:
        return set()

    def scan(self, pushed: Sequence[object] = ()) -> Iterator[Row]:
        yield from self._rows


def _factory(rows: Sequence[Row]):
    return lambda repo: _StubSource(rows)


def _both(query: str, rows: Sequence[Row], tmp_path: Path) -> tuple[list[Row], list[Row]]:
    """Run *query* on both sides over *rows*: SQLite loaded by
    `load_unfiltered`, historian through `run_historian`."""
    factory = _factory(rows)
    conn = load_unfiltered(factory, tmp_path, SCHEMA, "t")
    try:
        sqlite_rows = conn.execute(query).fetchall()
    finally:
        conn.close()
    _schema, historian_rows = run_historian(query, tmp_path, tables={"t": factory}, catalog=CATALOG)
    return sqlite_rows, historian_rows


# --- The load itself ---------------------------------------------------------


def test_bare_select_keeps_every_real_bit_pattern(tmp_path):
    """`SELECT r FROM t` matches exactly for every REAL cell, `-0.0`
    and `0.0` kept distinct and NULL matching NULL. Also pins SQLite's
    side directly by hex, so the test cannot pass by both sides losing
    the sign."""
    sqlite_rows, historian_rows = _both("SELECT r FROM t", ROWS, tmp_path)
    assert_rows_match(sqlite_rows, historian_rows)

    def hexed(rows):
        return sorted(
            (value.hex() if isinstance(value, float) else repr(value)) for (value,) in rows
        )

    expected = sorted(
        (r.hex() if isinstance(r, float) else repr(r)) for (_s, _i, r) in ROWS
    )
    assert hexed(sqlite_rows) == expected
    assert "-0x0.0p+0" in expected and "0x0.0p+0" in expected


def test_loaded_table_reports_the_schema_through_pragma(tmp_path):
    """The view the loader creates for a schema with a `REAL` column
    reports the same declared types through `PRAGMA table_info` as a
    plain table would (the companion of `test_blame.py`'s
    `test_create_table_sql_uses_column_type_value_for_any_schema`), and
    `typeof` reads `real` for every non-NULL REAL cell."""
    conn = load_unfiltered(_factory(ROWS), tmp_path, SCHEMA, "t")
    try:
        info = {row[1]: row[2] for row in conn.execute('PRAGMA table_info("t")').fetchall()}
        assert info == {"s": "TEXT", "i": "INTEGER", "r": "REAL"}
        kinds = {row[0] for row in conn.execute("SELECT typeof(r) FROM t").fetchall()}
        assert kinds == {"real", "null"}
    finally:
        conn.close()


def test_schema_without_real_column_gets_plain_create_table(tmp_path):
    """A schema with no `REAL` column is loaded into a plain table of
    that name, as before #140 - no view, no raw table."""
    schema = Schema(columns=(Column("s", ColumnType.TEXT), Column("i", ColumnType.INTEGER)))
    conn = load_unfiltered(lambda repo: _StubSource([("a", 1)]), tmp_path, schema, "u")
    try:
        objects = conn.execute("SELECT type, name FROM sqlite_master ORDER BY name").fetchall()
        assert objects == [("table", "u")]
        assert conn.execute("SELECT typeof(s), typeof(i) FROM u").fetchall() == [("text", "integer")]
    finally:
        conn.close()


# --- Affinity survives the load ----------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        "SELECT r = '1.5', r < '2', r = 0 FROM t",
        "SELECT s FROM t WHERE r = '1.5'",
        "SELECT s FROM t WHERE r < '2'",
    ],
)
def test_real_affinity_survives_the_load(query, tmp_path):
    sqlite_rows, historian_rows = _both(query, ROWS, tmp_path)
    assert_rows_match(sqlite_rows, historian_rows)


def test_real_affinity_answers_are_sqlites_real_column_answers(tmp_path):
    """The view gives the answers a stored `REAL` column gives, not a
    typeless column's: `r = '1.5'` is true for `1.5`."""
    sqlite_rows, _ = _both("SELECT s FROM t WHERE r = '1.5'", ROWS, tmp_path)
    assert sqlite_rows == [("one and a half",)]


# --- Derived values, aggregates and ordering ----------------------------------


@pytest.mark.parametrize(
    "query",
    [
        "SELECT -r FROM t",
        "SELECT r + 0 FROM t",
        "SELECT r * 1 FROM t",
        # historian has no abs(), typeof(), CAST or CASE yet (spec
        # §1: scalar functions are added when queries need them), so
        # cast(r AS TEXT) is spelled as a concatenation, which renders
        # a REAL as text the same way, and abs(r)/typeof(r) have no
        # spelling here (issue #140 comment).
        "SELECT r || '' FROM t",
        "SELECT r IS NULL, r < 0, r > 0, r || 'x' FROM t",
        "SELECT s, -r, r + 0, r * 1, r - 0, 0 - r FROM t",
        "SELECT min(r), max(r), count(r) FROM t",
        "SELECT sum(r), count(r) FROM t",
        # historian has no exponent literals; a plain decimal bound
        # selects the finite, non-huge cells instead of abs(r) < 1e300.
        "SELECT sum(r), min(r), max(r) FROM t WHERE r < 1000000.0 AND r > -1000000.0",
        "SELECT sum(r), min(r), max(r), count(r) FROM t WHERE r = 0",
        "SELECT min(r), max(r) FROM t WHERE i <= 2",
        "SELECT sum(r) FROM t WHERE i = 1",
        "SELECT -r FROM t WHERE i = 1",
    ],
)
def test_derived_values_match(query, tmp_path):
    sqlite_rows, historian_rows = _both(query, ROWS, tmp_path)
    assert_rows_match(sqlite_rows, historian_rows)


@pytest.mark.parametrize(
    ("query", "key_positions"),
    [
        ("SELECT r FROM t ORDER BY r", (0,)),
        ("SELECT r, s FROM t ORDER BY r", (0,)),
        ("SELECT r, s FROM t ORDER BY r DESC", (0,)),
        ("SELECT -r, s FROM t ORDER BY -r", (0,)),
        ("SELECT r, count(*) FROM t GROUP BY r ORDER BY r", (0,)),
        ("SELECT r, count(*), min(s) FROM t WHERE i <= 2 GROUP BY r ORDER BY r", (0,)),
    ],
)
def test_ordered_and_grouped_shapes_match(query, key_positions, tmp_path):
    """`0.0` and `-0.0` tie under `order_key`; the harness groups them
    and compares within the group exactly."""
    sqlite_rows, historian_rows = _both(query, ROWS, tmp_path)
    assert_rows_match(sqlite_rows, historian_rows, ordered=True, key_positions=key_positions)


def test_group_by_real_without_order_by(tmp_path):
    sqlite_rows, historian_rows = _both("SELECT r, count(*) FROM t GROUP BY r", ROWS, tmp_path)
    assert_rows_match(sqlite_rows, historian_rows)


# --- Nothing matches ----------------------------------------------------------


@pytest.mark.parametrize(
    ("query", "rows", "expected"),
    [
        ("SELECT r FROM t", [], []),
        ("SELECT r FROM t WHERE r > 1.5 AND r < 0", ROWS, []),
        ("SELECT count(r), sum(r), min(r) FROM t", [], [(0, None, None)]),
        ("SELECT count(r), sum(r), min(r) FROM t WHERE r > 1.5 AND r < 0", ROWS, [(0, None, None)]),
    ],
)
def test_nothing_matches(query, rows, expected, tmp_path):
    sqlite_rows, historian_rows = _both(query, rows, tmp_path)
    assert sqlite_rows == expected
    assert_rows_match(sqlite_rows, historian_rows)


# --- What the loader refuses to hide -------------------------------------------


def test_nan_in_a_scanned_real_cell_fails_the_load(tmp_path):
    """SQLite stores a bound NaN as NULL, which would make historian's
    NaN look like a match against NULL. The loader fails instead,
    naming the NaN."""
    rows = [("nan", 1, float("nan"))]
    with pytest.raises(AssertionError, match="NaN") as excinfo:
        load_unfiltered(_factory(rows), tmp_path, SCHEMA, "t")
    assert NAN_MESSAGE in str(excinfo.value)
    assert "'r'" in str(excinfo.value)


def test_nan_would_otherwise_load_as_null():
    """The behaviour the NaN check exists for, measured directly."""
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE raw (r)")
        conn.execute("INSERT INTO raw VALUES (?)", (float("nan"),))
        assert conn.execute("SELECT r FROM raw").fetchall() == [(None,)]
    finally:
        conn.close()


def test_int_in_a_real_column_is_reported_not_normalised(tmp_path):
    """A scan that puts an `int` in a `REAL` column violates §2; the
    view hands SQLite's side `5.0` (as a stored `REAL` column would)
    while historian keeps the scanned `5`, and the harness reports it."""
    rows = [("int", 1, 5)]
    sqlite_rows, historian_rows = _both("SELECT r FROM t", rows, tmp_path)
    assert sqlite_rows == [(5.0,)]
    assert historian_rows == [(5,)]
    with pytest.raises(AssertionError, match="disagrees"):
        assert_rows_match(sqlite_rows, historian_rows)
