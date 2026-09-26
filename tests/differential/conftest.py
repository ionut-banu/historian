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
`ORDER BY`, exact order when it does. `assert_rows_match` grows two
keyword-only parameters for the `ORDER BY` case (issue #61):
`ordered` and `key_positions` - see its own docstring for the tie-
tolerant design and why it exists.

This layer tests the SQL engine, not the extraction (spec §4): both
sides read rows from the same `BlameScan`, so a wrong `authored_at`
would be wrong on both sides and invisible here. That is what
`tests/extraction/` is for.
"""

from __future__ import annotations

import itertools
import sqlite3
from collections.abc import Sequence
from pathlib import Path

from historian.catalog import SCAN_FACTORIES as _DEFAULT_TABLES
from historian.catalog import SCHEMAS
from historian.exec.operators import ScanSource
from historian.plan.planner import ScanFactory
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

    `tables` defaults to `historian.catalog.SCAN_FACTORIES` (issue
    #35: `plan.planner` no longer has a `TABLES` of its own to default
    to), exactly as `cli.py` passes `tables=SCAN_FACTORIES` to
    `plan()`. It is a parameter (mirroring `plan()`'s own signature)
    only so `test_loader_and_query_runner_are_two_independent_call_sites`
    below can substitute a spy `ScanSource` factory for the
    separation test; every case in this file that runs a real query
    against a real repository uses the default. `bind()`'s own
    `catalog` argument below is always `historian.catalog.SCHEMAS`,
    unconditionally - not threaded through `tables` - because every
    case in this file, spy included, binds against the name "blame"
    (`_SpySource` in `tests/differential/test_blame.py` is shaped
    against `BLAME_SCHEMA` for exactly this reason), the same way
    `cli.py` always calls `bind(stmt, catalog=SCHEMAS)` regardless of
    which `tables` mapping `plan()` then receives.

    Neither this function nor `scan_all_rows`/`load_unfiltered` above
    catches `LexError`, `ParseError`, `BindError` or `EvalError` -
    an uncaught one fails the test exactly like any other exception,
    which is what lets `test_blame.py` assert on them directly for
    #25/#32/#51 and, now, #60's own `BindError` cases (an aggregate call
    in `WHERE`, a bare column mixed with an aggregate, arity/unknown-
    function errors - see that file's "Aggregate (issue #60): BindError
    cases" section). `run_historian("SELECT count(*) FROM blame", repo)`
    used to raise `EvalError` uncaught before issue #60 (no aggregate
    registry existed yet); it now runs to completion like any other
    query, and #60's own aggregate-shaped differential cases (that
    file's "Aggregate (issue #60)" section) go through this same
    function, unchanged, exactly like every other case in it.
    """
    tokens = tokenize(query)
    stmt = parse(tokens)
    bound = bind(stmt, catalog=SCHEMAS)
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


def _assert_row_sequences_match(
    sqlite_rows: Sequence[Row], historian_rows: Sequence[Row], *, context: str = ""
) -> None:
    """Cell-by-cell comparison of two same-length row sequences, in
    the order given - the shared innermost check both comparison modes
    below build on. `context` is prepended to every failure message
    (e.g. `"tied group (('x',),): "`) so a mismatch inside one tied
    group of an `ordered=True` comparison is still easy to place.

    Never bare `==`: `True == 1` in Python, so a comparator using it
    would report `[(True,)]` (historian's actual output for `SELECT
    1 = 1 FROM blame`) as matching `[(1,)]` (SQLite's actual answer)
    and be structurally blind to #48 - see `_docs/decisions.md` for
    why this is a decision, not an implementation detail. Requires
    both the same Python type and the same value, for exactly that
    reason.
    """
    assert len(sqlite_rows) == len(historian_rows), (
        f"{context}row count mismatch: sqlite produced {len(sqlite_rows)}, "
        f"historian produced {len(historian_rows)}\n"
        f"sqlite:    {sqlite_rows!r}\n"
        f"historian: {historian_rows!r}"
    )
    for index, (sqlite_row, historian_row) in enumerate(zip(sqlite_rows, historian_rows)):
        assert len(sqlite_row) == len(historian_row), (
            f"{context}row {index} has a different number of columns: "
            f"sqlite={sqlite_row!r} historian={historian_row!r}"
        )
        for col, (sqlite_cell, historian_cell) in enumerate(zip(sqlite_row, historian_row)):
            matches = type(sqlite_cell) is type(historian_cell) and sqlite_cell == historian_cell
            assert matches, (
                f"{context}row {index} column {col} disagrees: "
                f"sqlite={sqlite_cell!r} ({type(sqlite_cell).__name__}) "
                f"historian={historian_cell!r} ({type(historian_cell).__name__})"
            )


def _assert_multiset_match(
    sqlite_rows: Sequence[Row], historian_rows: Sequence[Row], *, context: str = ""
) -> None:
    """Sorts both lists with `_row_sort_key` (never Python's bare
    `sorted()`, which raises `TypeError` comparing `None` to anything
    or comparing across storage classes - `values.order_key` already
    implements SQLite's total order and handles both), then compares
    corresponding rows via `_assert_row_sequences_match`."""
    sorted_sqlite = sorted(sqlite_rows, key=_row_sort_key)
    sorted_historian = sorted(historian_rows, key=_row_sort_key)
    _assert_row_sequences_match(sorted_sqlite, sorted_historian, context=context)


def _grouped_by_consecutive_key(rows: Sequence[Row], keys: list[tuple]) -> list[tuple[tuple, list[Row]]]:
    """*rows* split into consecutive runs of equal *keys* entries, as
    `(key, rows_in_that_run)` pairs, in first-appearance order - a
    correctly `ORDER BY`-sorted result always has every row sharing a
    key adjacent to each other, so a run-based grouping (rather than a
    dict keyed by value) is also what makes a genuine tie-breaking bug
    visible: if `Sort` ever split one key's rows into two non-adjacent
    runs, this produces *two* groups for that key instead of one, and
    the distinct-key-sequence comparison below then legitimately
    disagrees with the other engine's single run."""
    return [
        (key, [row for _key, row in group])
        for key, group in itertools.groupby(zip(keys, rows), key=lambda pair: pair[0])
    ]


def assert_rows_match(
    sqlite_rows: Sequence[Row],
    historian_rows: Sequence[Row],
    *,
    ordered: bool = False,
    key_positions: Sequence[int] | None = None,
    tie_free_proof: tuple[int, int] | None = None,
) -> None:
    """Step 5, "Comparison follows §3": sorted multisets by default,
    or - when the query has an `ORDER BY` (`ordered=True`) - a
    tie-tolerant exact-order comparison (issue #61, hardened by #80).

    `AGENTS.md` guarantees historian's own row order is deterministic,
    but SQLite makes no such promise for rows that tie on every
    `ORDER BY` key - a naive positional comparison would then report a
    false mismatch whenever the two engines break the same tie
    differently, which neither engine's own contract calls a bug. The
    orchestrator's own correction to this issue's grooming further
    named the shape a positional-*or*-grouped comparison alone cannot
    handle: an `ORDER BY` key that is not itself selected (`SELECT p
    FROM u ORDER BY n`) has no column in the output rows to group by
    at all. So this function takes three independent, keyword-only
    parameters rather than inferring any of them from the rows
    themselves - it never parses or rewrites the query to recover a
    hidden key, which would put a second SQL front end in the oracle:

    - `ordered=False` (the default): sorted-multiset comparison, via
      `_assert_multiset_match` - unaffected by `key_positions` and
      `tie_free_proof`.
    - `ordered=True, key_positions=(<0-based output column index>, ...)`:
      every `ORDER BY` key is selected. Rows are grouped into
      consecutive runs by their values at `key_positions`
      (`_grouped_by_consecutive_key`); the *sequence of distinct key
      tuples* must match exactly, in position, between the two
      engines, and the rows *within* each corresponding tied group are
      compared as a multiset (`_assert_multiset_match` again, scoped
      to that group) rather than requiring one specific sub-order -
      stricter than a plain multiset comparison (it still catches a
      key-ordering bug) and no stricter than what either engine
      actually promises (it never fails over a tie). `tie_free_proof`
      is ignored in this mode.
    - `ordered=True, key_positions=None, tie_free_proof=(total_rows,
      distinct_key_tuples)`: at least one `ORDER BY` key is not
      selected, so the harness cannot see ties in the output rows at
      all (issue #80, closing a gap #61's own `key_positions=None`
      mode left open: nothing forced the calling test to actually
      prove tie-freedom, so a silently-tied case could reach a
      positional comparison unproven). The harness itself asserts
      `total_rows == distinct_key_tuples` - naming both numbers on
      failure - *before* comparing a single row, and only then falls
      back to plain positional `_assert_row_sequences_match`; no
      grouping is possible or needed once the proof holds, since a
      tie-free result has nothing left to tolerate. **`tie_free_proof`
      must be computed from a real SQL query run over the same FROM
      and WHERE as the query under test (`count(*)` vs `count(DISTINCT
      <key>)`, the pattern `tests/differential/test_blame.py`'s own
      "ORDER BY" section uses) - never written by hand as literal
      integers, which would make the proof merely decorative.**
    - `ordered=True` with neither `key_positions` nor `tie_free_proof`:
      raises immediately, before comparing anything - the harness
      never trusts an exact-order comparison it has no way to know is
      sound.

    The harness never parses or runs the test's own SQL anywhere in
    this function, including the `tie_free_proof` path: it only ever
    receives two integers the caller computed elsewhere.
    """
    if ordered and key_positions is None and tie_free_proof is None:
        raise ValueError(
            "assert_rows_match(ordered=True) needs either key_positions "
            "(every ORDER BY key is selected - grouped, tie-tolerant "
            "comparison) or tie_free_proof=(total_rows, distinct_key_tuples) "
            "proving this case has no ties by construction, computed from a "
            "real SQL query over the same FROM/WHERE - see this function's "
            "own docstring."
        )

    assert len(sqlite_rows) == len(historian_rows), (
        f"row count mismatch: sqlite produced {len(sqlite_rows)}, "
        f"historian produced {len(historian_rows)}\n"
        f"sqlite:    {sqlite_rows!r}\n"
        f"historian: {historian_rows!r}"
    )

    if not ordered:
        _assert_multiset_match(sqlite_rows, historian_rows)
        return

    if key_positions is None:
        total, distinct = tie_free_proof
        assert total == distinct, (
            f"tie_free_proof claims this case is tie-free but total_rows="
            f"{total} != distinct_key_tuples={distinct} - a positional "
            "comparison would be unsound here, since neither engine "
            "promises how it breaks a tie; add key_positions instead, or "
            "fix the case (or the fixture) so it is genuinely tie-free"
        )
        _assert_row_sequences_match(sqlite_rows, historian_rows)
        return

    sqlite_keys = [tuple(_cell_sort_key(row[pos]) for pos in key_positions) for row in sqlite_rows]
    historian_keys = [
        tuple(_cell_sort_key(row[pos]) for pos in key_positions) for row in historian_rows
    ]
    sqlite_groups = _grouped_by_consecutive_key(sqlite_rows, sqlite_keys)
    historian_groups = _grouped_by_consecutive_key(historian_rows, historian_keys)

    sqlite_key_sequence = [key for key, _rows in sqlite_groups]
    historian_key_sequence = [key for key, _rows in historian_groups]
    assert sqlite_key_sequence == historian_key_sequence, (
        "ORDER BY key sequence disagrees (ties aside, the two engines "
        "must visit the same distinct key values in the same order):\n"
        f"sqlite:    {sqlite_key_sequence!r}\n"
        f"historian: {historian_key_sequence!r}"
    )

    for (key, sqlite_group_rows), (_key, historian_group_rows) in zip(sqlite_groups, historian_groups):
        _assert_multiset_match(sqlite_group_rows, historian_group_rows, context=f"tied group {key!r}: ")
