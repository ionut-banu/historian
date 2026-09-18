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


# --- IS / IS NOT over non-NULL, same-rank operands -----------------------
#
# QA's second-round FAIL on this issue (#59): `is_()` mutated to
# `return _rank(left) == _rank(right)` - dropping the actual value
# comparison and answering from storage-class rank alone - passes the
# entire 767-test suite, unit and differential layers both. It makes
# `IS` agree that any two same-rank values are equal: `'a' IS 'b'`
# becomes TRUE. The two cases directly above only ever compare against
# NULL, so neither puts two non-NULL operands in front of `is_()` at
# all - confirmed by grep, this is the actual gap, not a guess.
#
# The orchestrator scoped round 2 to this one mutation and named three
# others explicitly out of scope: the negated int64-minimum literal,
# IN's FALSE-starting accumulator, and cross-storage-class comparison
# direction. All three are already killed by tests/test_expression.py
# or tests/test_values.py - this is the one killed by nothing, unit or
# differential, until now.
#
# `tiny_repo`'s blame table has 3 rows: ('feature/thing.py', 1),
# ('src/utils.py', 1), ('src/utils.py', 2) - two distinct `path`
# values and two distinct `line_no` values, so `WHERE ... IS <literal>`
# naturally splits rows into matching and non-matching with no
# synthetic table needed. Under the mutant, every row in a same-rank
# comparison agrees regardless of its actual value: a matching case
# would undercount what the mutant reports (it sees all 3, not the
# smaller true count) and a non-matching case would overcount it (it
# sees all 3, not 0) - both directions covered below, for both TEXT
# and INTEGER rank, for both `IS` and `IS NOT`.


def test_where_is_matching_text(tiny_repo):
    """`sqlite3 :memory: "create table blame(path text, line_no
    integer); insert into blame values ('feature/thing.py',1),
    ('src/utils.py',1),('src/utils.py',2); select count(*) from blame
    where path IS 'src/utils.py';"` -> `2`. Under the mutant every row
    is same-rank (TEXT) as the literal, so all 3 would match instead
    of the 2 whose `path` actually equals it."""
    _assert_differential(tiny_repo, "SELECT path FROM blame WHERE path IS 'src/utils.py'")


def test_where_is_non_matching_text(tiny_repo):
    """`sqlite3 :memory: "... where path IS 'no-such-file.py';"` ->
    `0`: no row's `path` is that literal, so `IS` is FALSE everywhere.
    The mutant, which only checks that both sides are TEXT, would say
    TRUE for all 3 rows - the starkest form of the bug, matching
    everything instead of nothing."""
    _assert_differential(tiny_repo, "SELECT path FROM blame WHERE path IS 'no-such-file.py'")


def test_where_is_matching_integer(tiny_repo):
    """`sqlite3 :memory: "... where line_no IS 1;"` -> `2`. Same shape
    as the TEXT case above, over the INTEGER-ranked column instead -
    the mutant's rank check does not care which storage class it was
    fooled on."""
    _assert_differential(tiny_repo, "SELECT path FROM blame WHERE line_no IS 1")


def test_where_is_non_matching_integer(tiny_repo):
    """`sqlite3 :memory: "... where line_no IS 999;"` -> `0`: no row's
    `line_no` is `999`. The mutant would match all 3 rows, since `999`
    and every `line_no` value share the INTEGER rank."""
    _assert_differential(tiny_repo, "SELECT path FROM blame WHERE line_no IS 999")


def test_where_is_not_matching_text(tiny_repo):
    """`sqlite3 :memory: "... where path IS NOT 'src/utils.py';"` ->
    `1`: the one row whose `path` is `'feature/thing.py'`. `is_not` is
    `not is_()`, so the mutant - which says `is_` is TRUE for every
    same-rank pair - would say `is_not` is FALSE for every row here,
    matching 0 instead of 1."""
    _assert_differential(tiny_repo, "SELECT path FROM blame WHERE path IS NOT 'src/utils.py'")


def test_where_is_not_non_matching_text(tiny_repo):
    """`sqlite3 :memory: "... where path IS NOT 'no-such-file.py';"`
    -> `3`: every row's `path` differs from that literal, so `IS NOT`
    holds everywhere. The mutant would say 0 - the complementary miss
    to the case directly above."""
    _assert_differential(tiny_repo, "SELECT path FROM blame WHERE path IS NOT 'no-such-file.py'")


def test_where_is_not_matching_integer(tiny_repo):
    """`sqlite3 :memory: "... where line_no IS NOT 1;"` -> `1`: the
    one row whose `line_no` is `2`. Same INTEGER-rank shape as the
    TEXT `IS NOT` case above."""
    _assert_differential(tiny_repo, "SELECT path FROM blame WHERE line_no IS NOT 1")


def test_where_is_not_non_matching_integer(tiny_repo):
    """`sqlite3 :memory: "... where line_no IS NOT 999;"` -> `3`:
    every row's `line_no` differs from `999`. The mutant would say 0."""
    _assert_differential(tiny_repo, "SELECT path FROM blame WHERE line_no IS NOT 999")


def test_where_like(tiny_repo):
    _assert_differential(tiny_repo, "SELECT path FROM blame WHERE path LIKE 'src/%'")


def test_where_like_single_char_wildcard(tiny_repo):
    """`_` matches exactly one character, distinct from `%`'s "any
    sequence including empty" - `sqlite3 :memory: "select
    'feature/thing.py' like 'feature/th_ng.py';"` -> `1`. The existing
    `%`-only case above cannot by itself tell `_` compiling to the
    right thing (`.`) apart from, say, a literal underscore - a bug
    isolated to `_`'s own translation would pass it silently."""
    _assert_differential(tiny_repo, "SELECT path FROM blame WHERE path LIKE 'feature/th_ng.py'")


def test_where_like_single_char_wildcard_requires_exactly_one_char(tiny_repo):
    """The other half of the pair directly above, needed to rule out
    `_` compiling to `.*` (any sequence, zero included) rather than
    `.` (exactly one) - a bug the positive case above cannot see,
    since `.*` matches everywhere `.` does and then some. `path` is
    `'src/utils.py'`; the pattern below asks for a character between
    the final `s` and the `.` that is not actually there -
    `sqlite3 :memory: "select 'src/utils.py' like
    'src/utils_.py';"` -> `0`. `.` correctly requires and fails to
    find that character; `.*` would match zero characters there and
    wrongly report the row as `1`."""
    _assert_differential(tiny_repo, "SELECT path FROM blame WHERE path LIKE 'src/utils_.py'")


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


# --- Three-valued logic through the engine ------------------------------
#
# QA's FAIL on this issue's first attempt (issue #59): `blame` has no
# nullable column across either fixture (confirmed live - zero NULLs in
# all 7 columns x 15 rows total), so no query over table data alone can
# produce a NULL comparison. Every case below routes a NULL through the
# pipeline via a literal instead - SQLite executes all of them happily,
# per the orchestrator's own list of what would close the gap. Each
# expected shape below is confirmed against `sqlite3` 3.51.0 (see the
# inline invocation in each docstring); `_assert_differential` then
# checks historian agrees, live, rather than pinning a literal value
# here.
#
# The specific mutation this section exists to kill: `_compare`
# (`values.py`) returning `0` instead of `None` when either operand's
# storage-class rank is NULL - turning "any comparison with NULL is
# NULL" into "NULL equals everything", spec §3's "classic bug". Every
# comparison operator (`eq`/`ne`/`lt`/`le`/`gt`/`ge`) routes through
# `_compare`, so a single row reaching any of them with a NULL operand
# is enough to expose it - confirmed by re-running the mutation below.


def test_where_eq_against_null_literal(tiny_repo):
    """`sqlite3 :memory: "create table blame(path text); insert into
    blame values ('a.py'); select count(*) from blame where path =
    NULL;"` -> `0`. A column compared to a NULL literal is never TRUE,
    so no row matches - not "matches every row" (the classic bug) and
    not "matches whichever rows equal NULL under Python's own `==`",
    which would raise before returning a count at all."""
    _assert_differential(tiny_repo, "SELECT path FROM blame WHERE path = NULL")


def test_select_eq_against_null_literal(tiny_repo):
    """`sqlite3 :memory: "select 1 = NULL;"` -> empty (NULL), for every
    row regardless of `line_no`'s actual value. This is `SELECT`, not
    `WHERE`, so - unlike the case above - a wrong `TRUE` would not be
    filtered out; it would show up directly as `True` in the row
    instead of `None`."""
    _assert_differential(tiny_repo, "SELECT line_no = NULL FROM blame")


def test_null_literal_equals_null_literal(tiny_repo):
    """`sqlite3 :memory: "select (NULL = NULL) is NULL;"` -> `1`
    (TRUE): `NULL = NULL` is `NULL`, not `TRUE`. This is the exact
    shape of the mutation QA found - both operands NULL, not just
    one - and the case the general `path = NULL` test above does not
    by itself guarantee catches every way `_compare` could special-case
    "both sides NULL" differently from "one side NULL"."""
    _assert_differential(tiny_repo, "SELECT NULL = NULL FROM blame")


def test_where_in_with_null_element(tiny_repo):
    """`sqlite3 :memory: "create table blame(line_no integer); insert
    into blame values (1),(1),(2); select count(*) from blame where
    line_no IN (1, NULL);"` -> `2`: a NULL in the list only turns a
    non-match into NULL (excluded), it never turns every row TRUE. The
    two `line_no = 1` rows still match on their own merits."""
    _assert_differential(tiny_repo, "SELECT path FROM blame WHERE line_no IN (1, NULL)")


def test_where_not_in_with_null_element(tiny_repo):
    """`sqlite3 :memory: "create table blame(line_no integer); insert
    into blame values (1),(1),(2); select count(*) from blame where
    line_no NOT IN (1, NULL);"` -> `0`: a NULL anywhere in a `NOT IN`
    list poisons every row, matching or not - `line_no = 1` rows get
    `NOT (TRUE)` = `FALSE`, and the `line_no = 2` row gets
    `NOT (NULL)` = `NULL`. Neither is kept."""
    _assert_differential(tiny_repo, "SELECT path FROM blame WHERE line_no NOT IN (1, NULL)")


def test_where_between_with_null_bound(tiny_repo):
    """`sqlite3 :memory: "create table blame(line_no integer); insert
    into blame values (1),(1),(2); select count(*) from blame where
    line_no BETWEEN 1 AND NULL;"` -> `0`: `line_no >= 1` is TRUE for
    every row here, so each becomes `TRUE AND (line_no <= NULL)` =
    `TRUE AND NULL` = `NULL`, not `TRUE`."""
    _assert_differential(tiny_repo, "SELECT path FROM blame WHERE line_no BETWEEN 1 AND NULL")


def test_where_or_with_null_and_true_operand(tiny_repo):
    """`sqlite3 :memory: "select (NULL OR 1);"` -> `1` (TRUE): `OR`
    short-circuits to TRUE even with a NULL operand present, unlike
    `AND`. `path = NULL` is NULL for every row here, so only the
    `line_no = 1` rows survive via their own `TRUE OR NULL` = `TRUE` -
    the same count as `test_where_in_with_null_element` above, reached
    through `OR` instead of `IN`, to pin that `OR`'s own NULL handling
    (not just `IN`'s) is correct."""
    _assert_differential(tiny_repo, "SELECT path FROM blame WHERE (path = NULL) OR (line_no = 1)")


def test_where_not_of_null_comparison(tiny_repo):
    """`sqlite3 :memory: "select (NOT (1 = NULL)) is NULL;"` -> `1`
    (TRUE): `NOT NULL` is `NULL`, not `TRUE` - so negating a
    NULL-valued comparison does not turn it into a match."""
    _assert_differential(tiny_repo, "SELECT path FROM blame WHERE NOT (path = NULL)")


def test_concatenation_with_null_operand(tiny_repo):
    """`sqlite3 :memory: "select ('a' || NULL) is NULL;"` -> `1`
    (TRUE): `||` propagates NULL from either side rather than treating
    it as an empty string, for every row regardless of `path`'s actual
    value."""
    _assert_differential(tiny_repo, "SELECT path || NULL FROM blame")


# --- Division: truncation toward zero, and division by zero ------------
#
# `blame.line_no` is the only non-TEXT column in phase 1 and the case
# set had no division anywhere, so `_truncating_int_div`
# (`exec/expression.py`) - which exists specifically because Python's
# `//` floors instead of truncating - was entirely unexercised. Same
# root cause as the NULL gap above: an operator historian implements
# but the hand-written cases never reached.


def test_division_truncates_toward_zero_negative_dividend(tiny_repo):
    """`sqlite3 :memory: "select -5/2;"` -> `-2`. Python's `-5 // 2` is
    `-3` (floors toward negative infinity); SQLite/C truncate toward
    zero instead."""
    _assert_differential(tiny_repo, "SELECT -5 / 2 FROM blame")


def test_division_truncates_toward_zero_negative_divisor(tiny_repo):
    """`sqlite3 :memory: "select 5/-2;"` -> `-2`, same rule with the
    sign on the other operand - Python's `5 // -2` is `-3`."""
    _assert_differential(tiny_repo, "SELECT 5 / -2 FROM blame")


def test_integer_division_by_zero_is_null(tiny_repo):
    """`sqlite3 :memory: "select (5/0) is NULL;"` -> `1` (TRUE):
    division by zero is NULL, not a `ZeroDivisionError` and not `0`."""
    _assert_differential(tiny_repo, "SELECT 5 / 0 FROM blame")


def test_float_division_by_zero_is_null(tiny_repo):
    """`sqlite3 :memory: "select (5.0/0) is NULL;"` -> `1` (TRUE): the
    same rule holds for a `REAL` operand, which raises
    `ZeroDivisionError` in raw Python rather than producing `inf`."""
    _assert_differential(tiny_repo, "SELECT 5.0 / 0 FROM blame")


# --- Float-to-text: precision and shape must survive a `%.15g` change --
#
# `blame` has no REAL column and the case set had no float literal
# anywhere, so `_format_float` (`exec/expression.py`) - SQLite's
# `%.15g`-based `REAL -> TEXT` algorithm - was entirely unexercised.
# `historian`'s lexer has no exponent-literal syntax (`1e15` lexes as
# `1` followed by the identifier `e15`), so a large float is produced
# via arithmetic overflow instead, exactly the path
# `_int64_bounded` promotes to `float` on overflow
# (`_docs/decisions.md`, 2026-09-01).


def test_float_addition_renders_with_sqlite_precision(tiny_repo):
    """`sqlite3 :memory: "select (0.1+0.2)||'';"` -> `'0.3'`. Python's
    `str(0.1 + 0.2)` is `'0.30000000000000004'` - the same double, a
    different number of significant digits kept. `||` forces the
    REAL -> TEXT path; a bare `SELECT 0.1 + 0.2` would return a Python
    float either side and could match by coincidence of identical
    underlying doubles, never exercising `_format_float` at all."""
    _assert_differential(tiny_repo, "SELECT (0.1 + 0.2) || '' FROM blame")


def test_large_float_renders_in_scientific_notation(tiny_repo):
    """`sqlite3 :memory: "select (9223372036854775807*10)||'';"` ->
    `'9.22337203685478e+19'`: 15 significant digits, and an exponent
    with the `.0` `_format_float` inserts before a bare `e` - not
    Python's `str()`, which keeps 17 digits and a different exponent
    spelling. `9223372036854775807 * 10` overflows `int64`
    (`9223372036854775807` is `int64`'s own max) and is promoted to
    `float` on both engines before `||` renders it."""
    _assert_differential(tiny_repo, "SELECT (9223372036854775807 * 10) || '' FROM blame")


# --- awkward_repo: unicode, quoting, binary content --------------------


def test_unicode_path(awkward_repo):
    _assert_differential(awkward_repo, "SELECT path, line FROM blame WHERE path = 'café.py'")


def test_quoted_and_spaced_path(awkward_repo):
    _assert_differential(
        awkward_repo, """SELECT path FROM blame WHERE path = 'a "quoted" name.txt'"""
    )


def test_binary_file_blamed_line(awkward_repo):
    _assert_differential(awkward_repo, "SELECT path, line FROM blame WHERE path = 'binary.bin'")


# --- #38/#48: SELECT 1 = 1 prints SQLite's 1, not Python's True --------
#
# The case demonstrating the oracle catches a genuine disagreement -
# and the reason the comparator above has to be strict-typed rather
# than bare `==`. Was `xfail(strict=True)` for #48; #38's
# `Project`-side `coerce_to_value` call (`exec/expression.py`,
# `exec/operators.py`) fixes it, so this is now a normal passing case
# - leaving the marker in place would fail the suite on XPASS.


def test_select_list_comparison_prints_python_bool_not_sqlite_int(tiny_repo):
    _assert_differential(tiny_repo, "SELECT 1 = 1 FROM blame")


# --- #38: every predicate-shaped node reaching a select-list position --
#
# One case per predicate-shaped node kind: a comparison is covered
# above; this covers IS/IS NOT, LIKE, IN, BETWEEN, AND/OR, and NOT.
# Each goes through the same `coerce_to_value` call, so `type(cell) is
# int` for every row, never `bool` - `assert_rows_match`'s strict-typed
# comparator is what actually checks that, against `sqlite3` directly.


def test_select_list_is_null_prints_sqlite_int(tiny_repo):
    _assert_differential(tiny_repo, "SELECT line_no IS NULL FROM blame")


def test_select_list_in_prints_sqlite_int(tiny_repo):
    _assert_differential(tiny_repo, "SELECT line_no IN (1, 2) FROM blame")


def test_select_list_like_prints_sqlite_int(tiny_repo):
    _assert_differential(tiny_repo, "SELECT path LIKE 'a%' FROM blame")


def test_select_list_between_prints_sqlite_int(tiny_repo):
    _assert_differential(tiny_repo, "SELECT line_no BETWEEN 1 AND 3 FROM blame")


def test_select_list_and_prints_sqlite_int(tiny_repo):
    _assert_differential(tiny_repo, "SELECT line_no > 1 AND path = 'a.py' FROM blame")


def test_select_list_not_prints_sqlite_int(tiny_repo):
    _assert_differential(tiny_repo, "SELECT NOT (line_no = 1) FROM blame")


# --- #38: WHERE over a value-shaped predicate -------------------------
#
# `evaluate()` returns a plain `Value` for these, not a `Bool3` -
# `Filter`'s `coerce_to_bool3` call gives it SQLite's C-style
# truthiness (leading-prefix numeric coercion, `!= 0`) before
# `values.is_true` ever sees it, rather than raising `TypeError`.


def test_where_bare_numeric_column_uses_c_style_truthiness(tiny_repo):
    """`line_no` is 1-based and never `0` in any fixture row, so every
    row is kept - the point is no crash and the right predicate, not
    that anything gets filtered here (issue #38's own acceptance
    criterion for this case)."""
    _assert_differential(tiny_repo, "SELECT path FROM blame WHERE line_no")


def test_where_bare_text_column_uses_leading_prefix_numeric_truthiness(tiny_repo):
    _assert_differential(tiny_repo, "SELECT path FROM blame WHERE path")


def test_where_leading_digit_text_literal_is_falsy_not_merely_nonempty(tiny_repo):
    """Pins the leading-prefix numeric-coercion rule against the
    "nonempty string is truthy" alternative: confirmed against
    `sqlite3`, `create table t(s text); insert into t values('0abc');
    select 'kept' from t where s;` -> no rows. `'0abc'` is digit-
    leading but not purely numeric - the leading-prefix rule reads it
    as `0`, falsy, dropping every row; "nonempty string is truthy"
    would wrongly keep them all."""
    _assert_differential(tiny_repo, "SELECT path FROM blame WHERE '0abc'")


def test_where_computed_value_expression_uses_same_truthiness(tiny_repo):
    """Not a bare column - `line_no - line_no` is always `0`, so every
    row is dropped, confirming the coercion applies to a computed
    value-shaped expression too, not only a `BoundColumnRef`."""
    _assert_differential(tiny_repo, "SELECT path FROM blame WHERE line_no - line_no")


def test_where_null_valued_expression_drops_rows_without_raising(tiny_repo):
    """`author_email` has no `blame` equivalent, so a `NULL`-valued
    value-shaped expression is built from a literal instead:
    `NULL + line_no` is `NULL` for every row (arithmetic propagates
    NULL), and a `NULL` predicate in value position drops the row
    without raising, exactly like any other `NULL` predicate."""
    _assert_differential(tiny_repo, "SELECT path FROM blame WHERE NULL + line_no")


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
