"""The first differential case set, against `blame` (issue #59, spec
§4). Every case runs the same query through SQLite (loaded from an
unfiltered `BlameScan`, per `conftest.py`'s step 1) and through
historian's own pipeline, and asserts the rows agree - except the
known-disagreement cases at the bottom, which assert what historian
does today because there is nothing to diff yet (see each section's
own comment).

`tiny_repo` is the default fixture for general `WHERE`/expression
semantics; `awkward_repo` is reserved for cases that specifically
exercise unicode, quoting, or binary content - not every case runs
against both, which would double the suite for no signal (grooming's
own conclusion, recorded on #59).

Query shapes deliberately absent - not skipped, not xfail - because
the grammar to write them does not parse yet at all (confirmed live
for each, per #59's Spec section): `ORDER BY`, `GROUP BY`, `HAVING`,
`LIMIT`, `OFFSET`, `DISTINCT`, `JOIN`, `CASE`, and any aggregate
(`count`/`sum`/`avg`/`min`/`max`). #60 and #61 add these.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence

import pytest

from historian.exec.operators import ScanSource
from historian.plan.planner import ScanFactory
from historian.schema import Row
from historian.sql.binder import BindError
from historian.sql.parser import ParseError
from historian.tables.blame import BLAME_SCHEMA, BlameScan

from differential.conftest import (
    assert_rows_match,
    create_table_sql,
    load_unfiltered,
    run_historian,
    scan_all_rows,
)


def _assert_differential(repo, query: str) -> None:
    """Steps 1-5 end to end for one query against the real `blame`
    table: load SQLite unfiltered (steps 1-2), run the query through
    both engines (steps 3-4), and compare (step 5)."""
    conn = load_unfiltered(BlameScan, repo, BLAME_SCHEMA, "blame")
    try:
        sqlite_rows = conn.execute(query).fetchall()
    finally:
        conn.close()
    _, historian_rows = run_historian(query, repo)
    assert_rows_match(sqlite_rows, historian_rows)


# --- Harness unit tests -----------------------------------------------
#
# These prove the mechanism itself, per #59's own acceptance criteria,
# rather than any one query shape.


class _SpySource:
    """A minimal `ScanSource` recording every `.scan()` call it
    receives - used only to prove the loader (step 1) and the
    query-runner (step 4) are two independent call sites, each
    reaching `.scan()` exactly once, with nothing pushed. Shaped like
    `tests/test_planner.py`'s own `_FakeSource`, but against
    `BLAME_SCHEMA` so a query naming real `blame` columns binds."""

    schema = BLAME_SCHEMA

    def __init__(self, rows: Sequence[Row]) -> None:
        self._rows = rows
        self.scan_calls: list[Sequence[object]] = []

    def capabilities(self) -> set[str]:
        return set()

    def scan(self, pushed: Sequence[object] = ()) -> Iterator[Row]:
        self.scan_calls.append(pushed)
        yield from self._rows


def test_loader_and_query_runner_are_two_independent_call_sites(tmp_path):
    """The checkable half of #59's pushdown-separation criterion: a
    spy `ScanSource` run through both `scan_all_rows`/`load_unfiltered`
    (step 1) and `run_historian` (step 4) records exactly two `.scan()`
    calls - one from each - both with `pushed=()`. This is what stands
    in today for "pushdown disabled" vs "pushdown enabled": they are
    the same call only because M4's negotiation does not exist yet,
    and this test pins that they are the same call *by construction of
    two independent code paths*, not by coincidence of one shared
    call."""
    spy = _SpySource([("a.py", 1, "line", "hash", "Ana", "ana@x.com", "2020-01-01T00:00:00Z")])
    factory: ScanFactory = lambda repo: spy  # noqa: E731

    load_unfiltered(factory, tmp_path, BLAME_SCHEMA, "blame")
    run_historian("SELECT path FROM blame", tmp_path, tables={"blame": factory})

    assert spy.scan_calls == [(), ()]


def test_create_table_sql_uses_column_type_value_for_any_schema():
    """The `CREATE TABLE` criterion: generated from a `Schema` with one
    column of each `ColumnType`, not hardcoded to `BLAME_SCHEMA` -
    checked via `sqlite3` that the affinities actually match, not just
    that the DDL text looks right."""
    import sqlite3

    from historian.schema import Column, ColumnType, Schema

    schema = Schema(
        columns=(
            Column("t", ColumnType.TEXT),
            Column("i", ColumnType.INTEGER),
            Column("r", ColumnType.REAL),
        )
    )
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute(create_table_sql("t1", schema))

        info = {row[1]: row[2] for row in conn.execute('PRAGMA table_info("t1")').fetchall()}
        assert info == {"t": "TEXT", "i": "INTEGER", "r": "REAL"}

        # A TEXT column keeps a numeric-looking string as TEXT - no
        # coercion surprise - and INTEGER/REAL keep their own types.
        conn.execute('INSERT INTO "t1" VALUES (?, ?, ?)', ("123", 1, 1.5))
        typeof_row = conn.execute('SELECT typeof(t), typeof(i), typeof(r) FROM "t1"').fetchone()
        assert typeof_row == ("text", "integer", "real")
    finally:
        conn.close()


def test_awkward_scan_round_trips_through_sqlite_load(awkward_repo):
    """Loading `BlameScan(awkward_repo).scan()` into the generated
    table and reading every row back round-trips exactly - the
    unicode path, the quoted-and-spaced path, and the binary file's
    blamed line included - compared directly against the original
    scan output, with no query involved yet."""
    original = scan_all_rows(BlameScan, awkward_repo)

    conn = load_unfiltered(BlameScan, awkward_repo, BLAME_SCHEMA, "blame")
    try:
        round_tripped = conn.execute('SELECT * FROM "blame" ORDER BY rowid').fetchall()
    finally:
        conn.close()

    assert round_tripped == original
    round_tripped_paths = {row[0] for row in round_tripped}
    assert "café.py" in round_tripped_paths
    assert 'a "quoted" name.txt' in round_tripped_paths
    assert "binary.bin" in round_tripped_paths


def test_type_strict_comparison_catches_bool_vs_int_mismatch():
    """The type-strictness criterion, load-bearing and checkable on
    its own: `[(True,)]` (historian's actual output shape for `SELECT
    1 = 1 FROM blame`) must be reported as a **mismatch** against
    `[(1,)]` (SQLite's actual answer for the same query) - not a match
    via Python's `True == 1`. A comparator using bare `==` passes this
    pair and is structurally blind to #48; this is the test that
    proves `assert_rows_match` is not that comparator."""
    with pytest.raises(AssertionError):
        assert_rows_match([(1,)], [(True,)])


def test_null_containing_rows_sort_and_compare_without_raising():
    """A second unit test: a `NULL`-containing row list sorts and
    compares without raising - Python's bare `sorted()` raises
    `TypeError` comparing `None` to an `int`, which `values.order_key`
    (routed through by `assert_rows_match`) does not."""
    assert_rows_match([(None, 1), (1, None)], [(1, None), (None, 1)])


# --- tiny_repo: WHERE, expressions, and the #47 affinity asymmetry ----


def test_bare_select_star(tiny_repo):
    _assert_differential(tiny_repo, "SELECT * FROM blame")


def test_explicit_column_list(tiny_repo):
    _assert_differential(tiny_repo, "SELECT path, line_no, author_name FROM blame")


def test_where_eq(tiny_repo):
    _assert_differential(tiny_repo, "SELECT path FROM blame WHERE line_no = 1")


def test_where_ne(tiny_repo):
    _assert_differential(tiny_repo, "SELECT path FROM blame WHERE line_no <> 1")


def test_where_lt(tiny_repo):
    _assert_differential(tiny_repo, "SELECT path FROM blame WHERE line_no < 2")


def test_where_le(tiny_repo):
    _assert_differential(tiny_repo, "SELECT path FROM blame WHERE line_no <= 1")


def test_where_gt(tiny_repo):
    _assert_differential(tiny_repo, "SELECT path FROM blame WHERE line_no > 1")


def test_where_ge(tiny_repo):
    _assert_differential(tiny_repo, "SELECT path FROM blame WHERE line_no >= 2")


def test_where_and(tiny_repo):
    _assert_differential(
        tiny_repo, "SELECT path FROM blame WHERE line_no = 1 AND path = 'src/utils.py'"
    )


def test_where_or(tiny_repo):
    _assert_differential(
        tiny_repo, "SELECT path FROM blame WHERE line_no = 1 OR path = 'feature/thing.py'"
    )


def test_where_not(tiny_repo):
    _assert_differential(tiny_repo, "SELECT path FROM blame WHERE NOT (line_no = 1)")


def test_where_is_null(tiny_repo):
    # No blame row's author_email is ever NULL in this fixture, so
    # both engines agreeing on zero rows is itself the proof - a
    # predicate matching nothing is not an error (spec §1).
    _assert_differential(tiny_repo, "SELECT path FROM blame WHERE author_email IS NULL")


def test_where_is_not_null(tiny_repo):
    _assert_differential(tiny_repo, "SELECT path FROM blame WHERE author_email IS NOT NULL")


def test_where_like(tiny_repo):
    _assert_differential(tiny_repo, "SELECT path FROM blame WHERE path LIKE 'src/%'")


def test_where_in(tiny_repo):
    _assert_differential(tiny_repo, "SELECT path FROM blame WHERE line_no IN (1, 2)")


def test_where_between(tiny_repo):
    _assert_differential(tiny_repo, "SELECT path FROM blame WHERE line_no BETWEEN 1 AND 2")


def test_cross_affinity_comparison(tiny_repo):
    """`line_no = '1'` against an `INTEGER` column: SQLite converts
    the TEXT literal to the column's declared affinity before
    comparing, so this matches the row where `line_no` is `1` -
    confirmed to already return it."""
    _assert_differential(tiny_repo, "SELECT path FROM blame WHERE line_no = '1'")


def test_in_has_no_affinity_of_its_own(tiny_repo):
    """#47: the right-hand side of `IN` with a list has no affinity at
    all - `'1' IN (line_no)` does *not* coerce `line_no` the way `=`
    would, so it matches nothing."""
    _assert_differential(tiny_repo, "SELECT path FROM blame WHERE '1' IN (line_no)")


def test_between_bounds_keep_their_own_affinity(tiny_repo):
    """The other half of the #47 pair, same column, same literal:
    `BETWEEN`'s bounds are independent operands and keep their own
    affinity, so `'1' BETWEEN line_no AND line_no` *does* match -
    asymmetric with the `IN` case directly above on purpose."""
    _assert_differential(tiny_repo, "SELECT path FROM blame WHERE '1' BETWEEN line_no AND line_no")


def test_null_literal_in_select_list(tiny_repo):
    _assert_differential(tiny_repo, "SELECT NULL, path FROM blame")


def test_string_concatenation(tiny_repo):
    _assert_differential(tiny_repo, "SELECT path || ':' || line_no FROM blame")


def test_arithmetic(tiny_repo):
    _assert_differential(tiny_repo, "SELECT line_no + 1 FROM blame")


# --- awkward_repo: unicode, quoting, binary content --------------------


def test_unicode_path(awkward_repo):
    _assert_differential(awkward_repo, "SELECT path, line FROM blame WHERE path = 'café.py'")


def test_quoted_and_spaced_path(awkward_repo):
    _assert_differential(
        awkward_repo, """SELECT path FROM blame WHERE path = 'a "quoted" name.txt'"""
    )


def test_binary_file_blamed_line(awkward_repo):
    _assert_differential(awkward_repo, "SELECT path, line FROM blame WHERE path = 'binary.bin'")


# --- #48: SELECT 1 = 1 prints Python True, not SQLite's 1 --------------
#
# The case demonstrating the oracle catches a genuine disagreement -
# and the reason the comparator above has to be strict-typed rather
# than bare `==`. strict=True so the suite breaks loudly (XPASS) the
# moment #48 is fixed and this marker is left behind.


@pytest.mark.xfail(
    strict=True,
    reason="#48: Project stores the raw Bool3, CLI/row output is Python True/False, not SQLite's 1/0",
)
def test_select_list_comparison_prints_python_bool_not_sqlite_int(tiny_repo):
    _assert_differential(tiny_repo, "SELECT 1 = 1 FROM blame")


# --- Known disagreements that raise before producing rows --------------
#
# #25, #32 and #51 are open design questions ("whether it should stay
# this way"), not confirmed bugs - unlike #48 there is no result to
# diff, since historian raises before either side produces a row. Each
# test below asserts what historian does today; it is not xfail, and
# is exactly what has to change when each issue is decided.


def test_bare_alias_without_as_is_a_parse_error(tiny_repo):
    """#25: SQLite accepts a column alias with `AS` omitted -
    confirmed: `sqlite3 :memory: "create table blame(path text);
    insert into blame values ('a.py'),('b.py'); select path p from
    blame;"` -> `a.py` / `b.py`. historian's grammar makes `AS`
    mandatory (spec §1) and rejects the bare form with a
    `ParseError`."""
    with pytest.raises(ParseError):
        run_historian("SELECT path p FROM blame", tiny_repo)


def test_where_cannot_reference_a_select_list_alias(tiny_repo):
    """#32: SQLite lets `WHERE` reference a select-list alias as a
    fallback - confirmed: `sqlite3 :memory: "create table blame(path
    text); insert into blame values('a.py'); select path as p from
    blame where p = 'a.py';"` -> `a.py`. historian's binder resolves
    `WHERE` before `Project` computes any alias and raises `BindError`
    ("no such column: p") instead."""
    with pytest.raises(BindError):
        run_historian("SELECT path AS p FROM blame WHERE p = 'src/utils.py'", tiny_repo)


def test_like_escape_is_a_parse_error(tiny_repo):
    """#51: SQLite supports `LIKE ... ESCAPE` - confirmed: `sqlite3
    :memory: "select '100%' like '100|%' escape '|';"` -> `1`.
    historian's lexer/parser has no `ESCAPE` clause at all and fails
    with a `ParseError` ("expected FROM, found identifier 'ESCAPE'"),
    not an "unsupported feature" error."""
    with pytest.raises(ParseError):
        run_historian("SELECT path FROM blame WHERE path LIKE '100|%' ESCAPE '|'", tiny_repo)


# --- Known disagreements deliberately not included here ----------------
#
# #22 (a digit run glued to an identifier, `SELECT 3abc FROM blame`) is
# not a case: it is currently masked by #25. Confirmed live that both
# engines already reject it today, for unrelated reasons - historian:
# "expected FROM, found identifier 'abc'" (the bare-alias parse error,
# since `3abc` lexes as `3` followed by the identifier `abc`, which
# without #25 support is read as an alias attempt gone wrong before it
# even reaches whatever #22's own bug would be); SQLite: "unrecognized
# token: \"3abc\"". Two engines erroring for two different reasons is
# not a comparable pair of outcomes - there is nothing to diff yet.
# Add it once #25 resolves and the masking parse error goes away.
#
# #24 (rejecting §1's non-goals by name - CTEs, window functions, and
# the rest of the permanently-out-of-scope grammar) is not included
# anywhere in this file, or this directory. SQLite executes that
# grammar; historian must always reject it. This suite's job is
# proving agreement on supported grammar, and there is never supposed
# to be agreement here - #24 belongs with the parser's or CLI's own
# unit tests, not a SQLite-agreement harness.
