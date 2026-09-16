"""The differential harness (`_docs/spec.md` §4's "The differential
harness"; issue #59, milestone M3 item 10).

For each test query, five steps:

1. Scan the table **with pushdown disabled**, giving the complete set
   of rows.
2. Load those rows into an in-memory SQLite table with the same schema.
3. Run the query through SQLite.
4. Run the query through historian, with pushdown enabled.
5. Compare.

Step 1 is the load-bearing one (spec §4): the obvious implementation
feeds SQLite from the same scan historian uses, which is wrong - a
pushdown bug that wrongly drops rows would remove them from both
sides, both engines would agree, and the bug would be invisible.
Loading SQLite from an unfiltered scan means historian is free to be
clever and any cleverness that changes the answer is caught.

Two genuinely separate call sites, not a flag
----------------------------------------------

Today `exec/operators.py`'s `Scan` always calls `source.scan(pushed=
())` unconditionally, so "pushdown disabled" and "pushdown enabled"
are mechanically identical - M4's negotiation does not exist yet. That
makes it tempting to write one function with an
`if pushdown_enabled: ... else: ...` branch, since right now both
branches would do the same thing. That is exactly the "obvious
implementation" this module refuses to be, because the two steps have
to remain independently true once M4 lands (see the comment on
`load_unfiltered` below):

- Step 1 (`scan_all_rows` / `load_unfiltered`) calls a `ScanSource`
  **directly** - it never imports or calls
  `historian.plan.planner.plan`, never constructs an `exec.operators.
  Scan`, and never runs a query through the SQL pipeline at all. It
  only ever knows how to do one thing: call `.scan()` with nothing
  pushed.
- Step 4 (`run_historian`) always goes through the real
  `tokenize -> parse -> bind -> plan -> tree.rows()` pipeline
  `cli.py:main` uses - never constructing a `ScanSource` or calling
  `.scan()` itself; `plan()` and `Scan` do that internally.

`test_blame.py`'s
`test_loader_and_query_runner_are_two_independent_call_sites` makes
this checkable rather than merely readable: a spy `ScanSource` run
through both functions records exactly two `.scan()` calls, one from
each, both with `pushed=()` - proving the separation holds *by
construction of two independent code paths*, not by coincidence of
one shared call.

Comparison follows §3: sorted multisets unless the query has an
`ORDER BY`, exact order when it does. No query can carry an `ORDER BY`
today - `sql/ast.py`'s `SelectStatement` has no field for it, and
`historian "SELECT path FROM blame ORDER BY path"` is a `ParseError` -
so `assert_rows_match` below only ever implements the sorted-multiset
half. #61 adds `ORDER BY` and, with it, the exact-order branch; the
seam is `assert_rows_match` itself, which should grow an `ordered:
bool` parameter then rather than being rebuilt.

This layer tests the SQL engine, not the extraction (spec §4): both
sides read rows from the same `BlameScan`, so a wrong `authored_at`
would be wrong on both sides and invisible here. That is what
`tests/extraction/` is for.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path

from historian.exec.operators import ScanSource
from historian.plan.planner import ScanFactory
from historian.plan.planner import TABLES as _DEFAULT_TABLES
from historian.plan.planner import plan
from historian.schema import Row, Schema
from historian.sql.binder import bind
from historian.sql.lexer import tokenize
from historian.sql.parser import parse
from historian.values import order_key

__all__ = [
    "assert_rows_match",
    "create_table_sql",
    "load_unfiltered",
    "run_historian",
    "scan_all_rows",
]


# --- Step 1: load SQLite from an unfiltered scan, bypassing plan() --------


def scan_all_rows(factory: ScanFactory, repo: Path) -> list[Row]:
    """Step 1: build *factory*'s `ScanSource` for *repo* and call
    `.scan()` on it with no arguments - i.e. `pushed=()`, its own
    default - giving the table's complete row set.

    This is the half of the harness that must never go through
    `plan()`: it never imports `historian.plan.planner.plan`, never
    constructs an `exec.operators.Scan`, and never runs any part of
    the SQL pipeline. `factory` is typed `ScanFactory` - exactly the
    value type of `plan.planner.plan`'s own `tables: dict[str,
    ScanFactory]` parameter - so this function accepts precisely one
    table's entry from that catalog, not the catalog itself.

    What changes at M4, and what does not
    ----------------------------------------
    M4 adds capability negotiation: `run_historian` below starts
    building real `Predicate`s and `plan()` starts splitting a
    `WHERE` clause and passing some of it into `Scan`, which passes it
    to `source.scan(pushed=...)` with something nonempty. This
    function's own call - `source.scan()`, no arguments - does not
    change, because it never goes through `plan()` or `Scan` at all;
    there is nothing here for negotiation to reach. The separation
    this issue builds needs no rework then - only `run_historian`'s
    call to `plan()` starts carrying real predicates.
    """
    source: ScanSource = factory(repo)
    return list(source.scan())


def create_table_sql(table_name: str, schema: Schema) -> str:
    """The SQLite `CREATE TABLE` for *table_name*, declared from
    *schema* - any `Schema`, not hardcoded to `BLAME_SCHEMA`'s seven
    columns. Each `Column.type.value` is already the exact SQLite
    keyword (`"TEXT"`, `"INTEGER"`, `"REAL"` - `schema.py`'s own
    docstring), so this is a direct translation with no lookup table
    of its own."""
    columns_sql = ", ".join(f'"{column.name}" {column.type.value}' for column in schema.columns)
    return f'CREATE TABLE "{table_name}" ({columns_sql})'


def load_unfiltered(
    factory: ScanFactory, repo: Path, schema: Schema, table_name: str = "blame"
) -> sqlite3.Connection:
    """Steps 1-2 together: `scan_all_rows` (see its own docstring for
    why this never touches `plan()`), then a fresh in-memory SQLite
    database with one table, `table_name`, declared via
    `create_table_sql` and loaded with every row `scan_all_rows`
    produced."""
    rows = scan_all_rows(factory, repo)
    conn = sqlite3.connect(":memory:")
    conn.execute(create_table_sql(table_name, schema))
    if rows:
        placeholders = ", ".join("?" for _ in schema.columns)
        conn.executemany(f'INSERT INTO "{table_name}" VALUES ({placeholders})', rows)
    conn.commit()
    return conn


# --- Step 4: run historian through its real pipeline -----------------------


def run_historian(
    query: str, repo: Path, tables: dict[str, ScanFactory] = _DEFAULT_TABLES
) -> tuple[Schema, list[Row]]:
    """Step 4: reproduce `cli.py:main`'s own pipeline exactly -
    `tokenize -> parse -> bind -> plan -> tree.rows()` - and never
    construct a `ScanSource` or call `.scan()` itself; `plan()` and
    `exec.operators.Scan` do that, the same way `cli.py` leaves it to
    them.

    `tables` defaults to `plan.planner`'s own `TABLES` catalog, exactly
    as `cli.py` calls `plan()` with no `tables` argument. It is a
    parameter (mirroring `plan()`'s own signature) only so
    `test_loader_and_query_runner_are_two_independent_call_sites`
    below can substitute a spy `ScanSource` factory for the
    separation test; every case in this file that runs a real query
    against a real repository uses the default.

    Neither this function nor `scan_all_rows`/`load_unfiltered` above
    catches `LexError`, `ParseError`, `BindError` or `EvalError` -
    an uncaught one fails the test exactly like any other exception,
    which is what lets `test_blame.py` assert on them directly for
    #25/#32/#51. Confirmed during development, not shipped as a case:
    `run_historian("SELECT count(*) FROM blame", repo)` raises
    `EvalError` uncaught (`count(...) is not supported here` -
    `exec/expression.py` has no aggregate registry yet), which is
    exactly why no aggregate-shaped case ships here - see #60.
    """
    tokens = tokenize(query)
    stmt = parse(tokens)
    bound = bind(stmt)
    tree = plan(bound, repo, tables=tables)
    return tree.schema, list(tree.rows())


# --- Step 5: compare -------------------------------------------------------


def _cell_sort_key(value):
    """A sort key for one cell, tolerant of a `bool` that should never
    have reached a `Value` position in the first place.

    `values.order_key` raises `TypeError` on a `bool` ("bool is not a
    SQL Value") by design (`values.py`'s own docstring) - a `Bool3`
    leaking into a `Value` position is a real historian bug (#38, and
    concretely #48: `SELECT 1 = 1 FROM blame` -> Python `True` in the
    output row) that the rest of the codebase is right to refuse
    silently. But that is exactly the shape of row this harness exists
    to compare against SQLite's `1` - if sorting itself raises on it,
    the harness crashes before `assert_rows_match` gets a chance to
    report the mismatch, which is worse than not catching it: a crash
    reads as a broken test, not a caught bug.

    `int(value)` first sidesteps this without changing what
    `values.py` accepts anywhere else: `int(True) == 1`, an entirely
    legitimate `Value`, so `order_key` is only ever handed a `bool` by
    proxy, for sorting purposes, never directly. The comparison in
    `assert_rows_match` below still sees the original, unconverted
    `True` and still reports it as a type mismatch against `1` - this
    function only decides where a row lands in sorted order, never
    whether two cells are equal.
    """
    if isinstance(value, bool):
        return order_key(int(value))
    return order_key(value)


def _row_sort_key(row: Row) -> tuple:
    return tuple(_cell_sort_key(value) for value in row)


def assert_rows_match(sqlite_rows: Sequence[Row], historian_rows: Sequence[Row]) -> None:
    """Step 5, "Comparison follows §3": sorted multisets, since no
    query reachable today carries an `ORDER BY` (see the module
    docstring's seam note for #61).

    Requires equal row counts, sorts both lists with `_row_sort_key`
    (never Python's bare `sorted()`, which raises `TypeError`
    comparing `None` to anything or comparing across storage classes -
    `values.order_key` already implements SQLite's total order and
    handles both), then compares corresponding rows cell-by-cell
    requiring **both** the same Python type and the same value.

    Never bare `==`: `True == 1` in Python, so a comparator using it
    would report `[(True,)]` (historian's actual output for `SELECT
    1 = 1 FROM blame`) as matching `[(1,)]` (SQLite's actual answer)
    and be structurally blind to #48 - see `_docs/decisions.md` for
    why this is a decision, not an implementation detail.
    """
    assert len(sqlite_rows) == len(historian_rows), (
        f"row count mismatch: sqlite produced {len(sqlite_rows)}, "
        f"historian produced {len(historian_rows)}\n"
        f"sqlite:    {sqlite_rows!r}\n"
        f"historian: {historian_rows!r}"
    )

    sorted_sqlite = sorted(sqlite_rows, key=_row_sort_key)
    sorted_historian = sorted(historian_rows, key=_row_sort_key)

    for index, (sqlite_row, historian_row) in enumerate(zip(sorted_sqlite, sorted_historian)):
        assert len(sqlite_row) == len(historian_row), (
            f"row {index} has a different number of columns: "
            f"sqlite={sqlite_row!r} historian={historian_row!r}"
        )
        for col, (sqlite_cell, historian_cell) in enumerate(zip(sqlite_row, historian_row)):
            matches = type(sqlite_cell) is type(historian_cell) and sqlite_cell == historian_cell
            assert matches, (
                f"row {index} column {col} disagrees: "
                f"sqlite={sqlite_cell!r} ({type(sqlite_cell).__name__}) "
                f"historian={historian_cell!r} ({type(historian_cell).__name__})"
            )
