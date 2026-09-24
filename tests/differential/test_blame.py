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
from historian.sql.lexer import LexError
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


# --- #38 round 2 (QA FAIL): a value-shaped operand nested inside AND/OR/NOT
#
# `coerce_to_bool3` was only ever called at Filter's and Project's own
# root call sites - every case directly above sits at exactly one of
# those two roots. A value-shaped operand *nested* under AND, OR or
# NOT - not the root itself - reached `values.and3`/`or3`/`not3` raw
# and raised `TypeError`, confirmed live: `historian "SELECT path FROM
# blame WHERE line_no - line_no AND path = 'AGENTS.md'"` raised
# `TypeError: not a Bool3: 0 of type int`. `tiny_repo`'s `blame` table
# has 3 rows - `('feature/thing.py', 1), ('src/utils.py', 1),
# ('src/utils.py', 2)` - `line_no` is never `0`, so `line_no - line_no`
# is always the falsy value `0` and `line_no` alone is always truthy.


def test_where_and_coerces_a_nested_value_shaped_left_operand(tiny_repo):
    """`sqlite3 :memory: "create table t(n integer, p text); insert
    into t values(1,'a'); select p from t where n-n and p='a';"` -> no
    rows: the falsy left operand drops every row regardless of the
    right operand. QA's own reproduction case for this issue."""
    _assert_differential(tiny_repo, "SELECT path FROM blame WHERE line_no - line_no AND path = 'src/utils.py'")


def test_where_or_coerces_a_nested_value_shaped_operand(tiny_repo):
    """`line_no` alone is always truthy here, so every row survives
    regardless of the right operand. `sqlite3`: `select n or p='zzz'
    from t where true;`-shaped, confirmed via `select 3 or 0;` -> `1`.
    """
    _assert_differential(tiny_repo, "SELECT path FROM blame WHERE line_no OR path = 'zzz'")


def test_where_not_coerces_a_nested_value_shaped_operand(tiny_repo):
    """`NOT (line_no - line_no)` is `NOT (0)` - truthy - for every row.
    `sqlite3`: `select not(5-5);` -> `1`."""
    _assert_differential(tiny_repo, "SELECT path FROM blame WHERE NOT (line_no - line_no)")


def test_where_or_nested_leading_prefix_falsy_text_literal_discriminates_truthiness(tiny_repo):
    """`'0abc'` nested inside `OR`, not at the `WHERE` root: as a bare
    Python string it is truthy (nonempty), so this discriminates a
    real per-operand coercion from bare Python truthiness the same way
    `test_where_leading_digit_text_literal_is_falsy_not_merely_nonempty`
    does at the root - only here the falsy operand is the left side of
    an `OR`, so the result still depends on the right operand.
    `sqlite3`: `select '0abc' or 0;` -> `0`; confirmed the two
    `line_no = 1` rows still match on their own merits."""
    _assert_differential(tiny_repo, "SELECT path FROM blame WHERE '0abc' OR line_no = 1")


def test_select_list_and_coerces_a_nested_value_shaped_operand(tiny_repo):
    """The same hole, in select-list position: `(line_no - line_no)
    AND 1` - the left operand of `AND` is value-shaped and falsy for
    every row, so the whole expression is SQLite's `0` for every row.
    `sqlite3`: `select typeof((n-n) and 1), (n-n) and 1 from t;` ->
    `integer|0` for every row. This is also QA's own reproduction case
    for this issue, in the other root position."""
    _assert_differential(tiny_repo, "SELECT (line_no - line_no) AND 1 FROM blame")


# --- Aggregate (issue #60): count/sum/avg/min/max, whole table only ----
#
# `tables/blame.py` asserts every blame column is non-NULL before a row
# is ever emitted, and `CASE` does not exist yet (no AST node), so a
# real `blame`-backed expression is either NULL for every row (a bare
# NULL literal, tested below) or NULL for no row (any real column) -
# never a mix in the same column. That covers the "all inputs NULL"
# and "zero rows" edge cases below, but not "some NULLs, some real
# values in the same column" (`count(x)`'s and `sum`/`min`/`max`'s core
# NULL-skipping behaviour, or mixed-storage-class `min`/`max`
# ordering) - those are direct unit tests against the `Aggregate`
# operator with synthetic rows instead, in `tests/test_operators.py`'s
# own "Aggregate" section, not differential cases here, because this
# fixture genuinely cannot produce the shape needed.


def test_aggregate_count_star(tiny_repo):
    _assert_differential(tiny_repo, "SELECT count(*) FROM blame")


def test_aggregate_count_star_over_where_matching_some_rows(tiny_repo):
    _assert_differential(tiny_repo, "SELECT count(*) FROM blame WHERE path = 'src/utils.py'")


def test_aggregate_count_star_over_where_matching_zero_rows(tiny_repo):
    """`count(*)` over zero matching rows is `0`, not an empty result -
    an explicit acceptance criterion, distinct from the zero-rows-
    entirely case (`test_aggregate_sum_avg_min_max_over_where_matching_
    zero_rows` below), which needs every other aggregate to answer
    `NULL` for the very same empty input."""
    _assert_differential(tiny_repo, "SELECT count(*) FROM blame WHERE path = 'no-such-file.py'")


def test_aggregate_sum_avg_min_max_over_line_no(tiny_repo):
    """`sum`/`avg`/`min`/`max` over the whole table's `line_no` -
    real, non-NULL INTEGER data, matching some rows implicitly (every
    row, since there is no `WHERE`). historian has no `typeof()` (no
    scalar functions exist yet - spec §1), but `assert_rows_match`'s
    own strict `type(sqlite_cell) is type(historian_cell)` check
    already proves `sum(line_no)` stays a Python `int` here rather
    than being force-promoted to `float`, with no `typeof()` needed."""
    _assert_differential(
        tiny_repo, "SELECT sum(line_no), avg(line_no), min(line_no), max(line_no) FROM blame"
    )


def test_aggregate_sum_avg_min_max_over_where_matching_some_rows(tiny_repo):
    _assert_differential(
        tiny_repo,
        "SELECT sum(line_no), avg(line_no), min(line_no), max(line_no) "
        "FROM blame WHERE path = 'src/utils.py'",
    )


def test_aggregate_sum_avg_min_max_over_where_matching_zero_rows(tiny_repo):
    """The whole-table-with-zero-rows case (spec §3's named "classic
    mistake") still emits exactly one row: `count` is `0`, the other
    four are `NULL` - a different answer than `count(*)` alone gives
    for the same empty input, which is exactly why both cases are
    pinned separately."""
    _assert_differential(
        tiny_repo,
        "SELECT count(line_no), sum(line_no), avg(line_no), min(line_no), max(line_no) "
        "FROM blame WHERE path = 'no-such-file.py'",
    )


def test_aggregate_min_max_over_a_text_column(tiny_repo):
    """`min`/`max` over `path` (TEXT), not `line_no` - a separate case
    from the INTEGER cases above since `min`/`max`'s storage-class
    ordering rule only has one class to exercise there; a TEXT column
    exercises SQLite's bytewise text comparison instead."""
    _assert_differential(tiny_repo, "SELECT min(path), max(path) FROM blame")


def test_aggregate_count_star_vs_count_column_over_non_null_data(tiny_repo):
    """`count(*)` and `count(path)` agree over `blame`, since `path` is
    never NULL in real blame data - this fixture cannot by itself tell
    `count(*)` and `count(<col>)` apart (that needs the synthetic unit
    test in `tests/test_operators.py`), but it does confirm both
    compute the fixture's real row count correctly through the whole
    pipeline, planner split included."""
    _assert_differential(tiny_repo, "SELECT count(*), count(path) FROM blame")


def test_aggregate_of_null_literal_over_non_empty_result(tiny_repo):
    """A single non-empty group where every aggregated value is NULL
    (a bare `NULL` literal, not a real column - see the section's own
    note on why): `count(NULL)` is `0`, the other four are `NULL` -
    distinct from the zero-*rows* case above, since `blame` itself has
    rows here, they just all evaluate the argument to NULL."""
    _assert_differential(
        tiny_repo, "SELECT count(NULL), sum(NULL), avg(NULL), min(NULL), max(NULL) FROM blame"
    )


def test_aggregate_call_plus_literal(tiny_repo):
    """`count(*) + 1`: the planner's aggregate/scalar split handles an
    aggregate call embedded in a larger expression, not only a bare
    aggregate as the entire select-list item."""
    _assert_differential(tiny_repo, "SELECT count(*) + 1 FROM blame")


def test_two_aggregate_calls_in_one_expression(tiny_repo):
    """`count(*) + sum(line_no)`: the split handles more than one
    aggregate call in a single select-list expression."""
    _assert_differential(tiny_repo, "SELECT count(*) + sum(line_no) FROM blame")


def test_multiple_aggregate_calls_in_one_select_list(tiny_repo):
    """Several separate select-list items, each its own aggregate call
    - not one expression combining two, the case directly above -
    proving the split's flat, ordered call list lines back up with the
    right select-list item."""
    _assert_differential(tiny_repo, "SELECT count(*), sum(line_no), max(line_no) FROM blame")


def test_count_with_no_parens_content_equals_count_star(tiny_repo):
    """`count()` (no arguments at all, not even `*`) means the same
    thing as `count(*)` - confirmed against `sqlite3`."""
    _assert_differential(tiny_repo, "SELECT count() FROM blame")


# --- Aggregate (issue #60): BindError cases, asserted directly ---------
#
# Unlike the section above, these never reach SQLite at all - historian
# raises `BindError` before either engine would produce a row, the same
# style `test_where_unmatched_name_raises_bind_error` and the "Known
# disagreements" section below already use. `test_where_count_star_
# raises_bind_error` and `test_select_bare_column_with_aggregate_raises_
# bind_error` are genuine SQLite disagreements (deliberate narrowings,
# per `_docs/decisions.md`); the other three are cases where SQLite
# itself also rejects the query (as a `Parse error`), just not with a
# `BindError` historian's oracle comparison could diff against - there
# is nothing to diff either way, since both engines refuse to run it.


def test_where_count_star_raises_bind_error(tiny_repo):
    """`SELECT * FROM blame WHERE count(*) > 1` - confirmed `sqlite3`
    also rejects this ("misuse of aggregate function count()"), so this
    is not a disagreement about semantics - it is one of §3's Errors
    categories (unsupported grammar in this position), asserted
    directly rather than diffed."""
    with pytest.raises(BindError):
        run_historian("SELECT * FROM blame WHERE count(*) > 1", tiny_repo)


def test_select_bare_column_with_aggregate_raises_bind_error(tiny_repo):
    """`SELECT path, count(*) FROM blame` (no `GROUP BY`) - the
    narrowing decision, `_docs/decisions.md` 2026-09-19: SQLite returns
    an arbitrary row's `path` here; historian raises `BindError`
    instead, deliberately, so there is nothing to diff a row result
    against."""
    with pytest.raises(BindError):
        run_historian("SELECT path, count(*) FROM blame", tiny_repo)


def test_sum_with_no_arguments_raises_bind_error(tiny_repo):
    """`SELECT sum() FROM blame` - confirmed a `Parse error` in
    `sqlite3` too (wrong arity), just not one either engine can
    diff a row result for."""
    with pytest.raises(BindError):
        run_historian("SELECT sum() FROM blame", tiny_repo)


def test_count_with_two_arguments_raises_bind_error(tiny_repo):
    """`SELECT count(path, line_no) FROM blame` - confirmed a `Parse
    error` in `sqlite3` too (wrong arity for `count`)."""
    with pytest.raises(BindError):
        run_historian("SELECT count(path, line_no) FROM blame", tiny_repo)


def test_nonexistent_function_raises_bind_error(tiny_repo):
    """`SELECT nonexistent_fn(path) FROM blame` - confirmed a `Parse
    error` in `sqlite3` too (`no such function: nonexistent_fn`). This
    is #60's fix for #45's other half: the message is a real, specific
    `BindError`, not `exec/expression.py`'s old generic `EvalError`."""
    with pytest.raises(BindError):
        run_historian("SELECT nonexistent_fn(path) FROM blame", tiny_repo)


# --- GROUP BY / HAVING (issue #69) --------------------------------------
#
# `NULL`-valued group keys and mixed-storage-class key merging are
# unit-only (`tests/test_operators.py`'s own "Aggregate (issue #69)"
# section) - `tables/blame.py` asserts every column is non-NULL, and
# `CASE` does not exist, so no real blame-backed expression can be
# NULL for some rows and not others. Everything below is reachable
# through `fixtures.build.get_tiny_repo()` and real `blame` data.


def test_group_by_single_real_column(tiny_repo):
    _assert_differential(tiny_repo, "SELECT author_name, count(*) FROM blame GROUP BY author_name")


def test_group_by_a_second_differently_shaped_column(tiny_repo):
    _assert_differential(tiny_repo, "SELECT path, count(*) FROM blame GROUP BY path")


def test_group_by_two_columns_together(tiny_repo):
    _assert_differential(
        tiny_repo,
        "SELECT author_name, path, count(*) FROM blame GROUP BY author_name, path",
    )


def test_group_by_an_expression_not_a_bare_column(tiny_repo):
    """historian has no `%` operator yet (issue #75 - not built by any
    prior issue, and out of this issue's own file list) - `line_no + 1`
    stands in for the same "GROUP BY on an expression" shape the
    issue's own grooming used `line_no % 2` for."""
    _assert_differential(
        tiny_repo, "SELECT line_no + 1, count(*) FROM blame GROUP BY line_no + 1"
    )


def test_group_by_ordinal_matches_the_named_column_form(tiny_repo):
    _assert_differential(tiny_repo, "SELECT author_name, count(*) FROM blame GROUP BY 1")


def test_group_by_ordinal_pointing_at_a_non_aggregate_expression(tiny_repo):
    """Ordinal resolution is positional, not name-based - `GROUP BY 1`
    here groups by the first select-list item's own expression
    (`line_no + 1`), the non-aggregate-expression analogue of the
    bare-column ordinal case above."""
    _assert_differential(
        tiny_repo, "SELECT line_no + 1, count(*) FROM blame GROUP BY 1"
    )


def test_having_filters_a_grouped_result_true(tiny_repo):
    _assert_differential(
        tiny_repo,
        "SELECT author_name, count(*) FROM blame GROUP BY author_name HAVING count(*) > 1",
    )


def test_having_filters_a_grouped_result_to_zero_rows(tiny_repo):
    _assert_differential(
        tiny_repo,
        "SELECT author_name, count(*) FROM blame GROUP BY author_name HAVING count(*) > 1000000",
    )


def test_having_references_a_select_list_alias(tiny_repo):
    """The direct analogue of #32's confirmed `sqlite3` transcript for
    this issue."""
    _assert_differential(
        tiny_repo,
        "SELECT author_name, count(*) AS c FROM blame GROUP BY author_name HAVING c > 1",
    )


def test_having_references_an_aggregate_not_in_the_select_list(tiny_repo):
    """`HAVING`'s own aggregate/scalar split is not limited to
    aggregates the select list already introduced."""
    _assert_differential(
        tiny_repo, "SELECT author_name FROM blame GROUP BY author_name HAVING sum(line_no) > 0"
    )


def test_group_by_after_where_matching_zero_rows_is_zero_groups(tiny_repo):
    """A different answer than the ungrouped `WHERE`-matches-nothing
    case (#60: one row, `count(*) = 0`) - grouping zero input rows
    produces zero groups, not one row with a zero count."""
    _assert_differential(
        tiny_repo,
        "SELECT count(*) FROM blame WHERE path = 'no-such-file.py' GROUP BY author_name",
    )


# --- GROUP BY / HAVING (issue #69): BindError cases, asserted directly -


def test_group_by_ordinal_pointing_at_an_aggregate_raises_bind_error(tiny_repo):
    """Orchestrator's correction: confirmed against `sqlite3 3.51.0`
    that an ordinal resolving to an aggregate call is rejected
    identically to the direct and aliased forms below, not "ludicrous
    but legal"."""
    with pytest.raises(BindError):
        run_historian("SELECT path, count(*) FROM blame GROUP BY 2", tiny_repo)


def test_group_by_direct_aggregate_call_raises_bind_error(tiny_repo):
    with pytest.raises(BindError):
        run_historian("SELECT path FROM blame GROUP BY count(*)", tiny_repo)


def test_group_by_aggregate_via_alias_raises_bind_error(tiny_repo):
    with pytest.raises(BindError):
        run_historian("SELECT count(*) AS c FROM blame GROUP BY c", tiny_repo)


def test_group_by_ordinal_out_of_range_raises_bind_error(tiny_repo):
    with pytest.raises(BindError):
        run_historian("SELECT path FROM blame GROUP BY 2", tiny_repo)


def test_group_by_ordinal_zero_raises_bind_error(tiny_repo):
    with pytest.raises(BindError):
        run_historian("SELECT path FROM blame GROUP BY 0", tiny_repo)


def test_grouped_select_item_not_a_key_or_aggregate_raises_bind_error(tiny_repo):
    """Extends #60's narrowing to the grouped case: `path` is neither
    the `GROUP BY` key (`author_name`) nor an aggregate call."""
    with pytest.raises(BindError):
        run_historian(
            "SELECT path, author_name, count(*) FROM blame GROUP BY author_name", tiny_repo
        )


def test_having_with_no_group_by_and_no_aggregate_anywhere_raises_bind_error(tiny_repo):
    """Confirmed against `sqlite3 3.51.0`: `select path from t having
    path = 'x'` -> "HAVING clause on a non-aggregate query". Neither
    `GROUP BY` nor an aggregate call anywhere (select list or HAVING
    itself) is present here."""
    with pytest.raises(BindError):
        run_historian("SELECT path FROM blame HAVING path = 'src/utils.py'", tiny_repo)


def test_having_aggregate_only_in_having_itself_still_raises_bind_error(tiny_repo):
    """Confirmed against `sqlite3`: an aggregate call written in
    HAVING itself does not by itself make the query aggregate -
    `select path from t having count(*) > 1` still raises "HAVING
    clause on a non-aggregate query"."""
    with pytest.raises(BindError):
        run_historian("SELECT path FROM blame HAVING count(*) > 1", tiny_repo)


def test_having_legal_with_aggregate_only_in_the_select_list(tiny_repo):
    """Confirmed against `sqlite3`: `select count(*) from t having 1`
    succeeds - the select list's own aggregate is enough, even though
    HAVING's own predicate has no aggregate call in it at all."""
    _assert_differential(tiny_repo, "SELECT count(*) FROM blame HAVING 1")


def test_having_bare_column_with_no_group_by_raises_bind_error(tiny_repo):
    """Orchestrator correction: `select count(*) from t having
    path = 'x'` returns a row in `sqlite3` (evaluating `path` against
    an arbitrary row) - historian raises `BindError` instead, the
    same "grouped but not a key" narrowing #69's own decisions.md
    entry already gives for the select list, extended to HAVING."""
    with pytest.raises(BindError):
        run_historian("SELECT count(*) FROM blame HAVING path = 'src/utils.py'", tiny_repo)


def test_having_bare_column_not_a_group_key_raises_bind_error(tiny_repo):
    """`select a, count(*) from t group by a having path = 'z'`
    returns a row in `sqlite3`; historian raises `BindError` - `path`
    is neither the `GROUP BY` key (`line_no`) nor inside an aggregate
    call."""
    with pytest.raises(BindError):
        run_historian(
            "SELECT line_no, count(*) FROM blame GROUP BY line_no "
            "HAVING path = 'src/utils.py'",
            tiny_repo,
        )


def test_having_expression_matching_an_expression_group_key(tiny_repo):
    """`GROUP BY line_no + 1 HAVING line_no + 1 > 2` - an expression
    key matched by shape, confirmed legal against `sqlite3` before
    implementing."""
    _assert_differential(
        tiny_repo,
        "SELECT line_no + 1, count(*) FROM blame GROUP BY line_no + 1 HAVING line_no + 1 > 2",
    )


def test_having_bare_column_matching_a_group_key(tiny_repo):
    _assert_differential(
        tiny_repo, "SELECT path, count(*) FROM blame GROUP BY path HAVING path = 'src/utils.py'"
    )


# --- GROUP BY / HAVING alias-vs-real-column resolution (issue #69 round 2) -
#
# QA's round-1 FAIL: the issue's own body commits twice to a
# discriminating alias-vs-column test for GROUP BY/HAVING resolution,
# mirroring #32's `test_where_real_column_wins_over_alias_of_a_different_
# column`, and none existed. Both call sites bind with
# `alias_first=False` (column-first) today, matching `sqlite3`'s own
# `GROUP BY`/`HAVING` resolution rule. Flipping either call site to
# `alias_first=True` makes the query below succeed silently instead of
# raising - that is exactly what makes these tests discriminating: a
# query that is *illegal* under the correct (column-first) reading and
# *legal but silently wrong* under the buggy (alias-first) one.


def test_group_by_real_column_wins_over_alias_of_a_different_column(tiny_repo):
    """`author_name AS path` aliases a *different* column to `path`'s
    own name. Column-first (correct): `GROUP BY path` resolves to the
    real `path` column, so the select list's `author_name` is neither
    the group key nor an aggregate - `BindError`, the same shape as
    #32's `WHERE` finding. Alias-first (the bug QA found uncaught):
    `GROUP BY path` would instead resolve through the select-list
    alias to `author_name`, which then trivially matches its own
    select-list item, and the query would succeed silently, grouping
    by `author_name` while claiming to group by `path`. Confirmed live
    against the real code before this test was written: the unmutated
    branch raises `BindError`, and flipping
    `_bind_group_by_item`'s `alias_first` to `True` makes it return
    rows instead - the discriminating direction this test pins."""
    with pytest.raises(BindError):
        run_historian(
            "SELECT author_name AS path, count(*) FROM blame GROUP BY path", tiny_repo
        )


def test_having_real_column_wins_over_alias_of_a_different_column(tiny_repo):
    """`line_no AS path` aliases the integer `line_no` column to
    `path`'s own name, with `GROUP BY line_no` (the real column,
    unaliased) as the sole key. Column-first (correct): `HAVING path`
    resolves to the real, TEXT `path` column, which is neither the
    group key (`line_no`) nor inside an aggregate call - `BindError`
    via the same "bare column not a group key" narrowing pinned
    elsewhere in this file, just now reached through a name that is
    *also* a select-list alias, which #32's alias fallback could pull
    the wrong way. Alias-first (the bug QA found uncaught): `HAVING
    path` would instead resolve through the select-list alias to
    `line_no`, which matches the group key by shape and binds legally
    - a real behavior difference, not just an error-message
    difference. Confirmed live against the real code: the unmutated
    branch raises `BindError`, and flipping the HAVING call site's
    `alias_first` to `True` makes it bind instead."""
    with pytest.raises(BindError):
        run_historian(
            "SELECT line_no AS path, count(*) FROM blame GROUP BY line_no "
            "HAVING path = 'src/utils.py'",
            tiny_repo,
        )


# --- ORDER BY (issue #61) ------------------------------------------------
#
# `assert_rows_match`'s `ordered=True` mode (`tests/differential/
# conftest.py`) needs the ORDER BY key's output position(s) whenever the
# key is selected, or `None` plus a tie-free-by-construction proof when
# it is not - see that function's own docstring for the design. `_order`
# below is this file's equivalent of `_assert_differential`, scoped to
# `ordered=True` queries.
#
# `awkward_repo` is the fixture of choice here, not `tiny_repo`: it has
# two authors and a path (`café.py`) blamed across six lines, so
# `ORDER BY path` has a genuine, multi-row tie to exercise the
# tie-tolerant comparison - `tiny_repo`'s three rows share no repeated
# value on any real column. NULL ordering and mixed-storage-class
# ordering (also named in this issue's conformance list) are *not*
# reachable here: `tables/blame.py` asserts every blame column is
# non-NULL before a row is ever emitted (same constraint `test_operators.
# py`'s own `Aggregate` section documents), so no real `blame` column can
# ever be NULL for some rows and a real value for others, or mix
# storage classes within one column - `line_no` is always INTEGER,
# every text column always TEXT. Those two shapes are covered at the
# unit level instead: `tests/test_operators.py`'s `Sort` section
# (`test_sort_nulls_first_ascending`, `test_sort_nulls_last_descending`,
# `test_sort_mixed_storage_class_numeric_then_text`) and `tests/
# test_planner.py`'s `test_plan_order_by_multi_key_end_to_end`, which
# runs `values.py`'s own multi-key NULL example through the real
# parser/binder/planner/`Sort`, exactly as that module's docstring
# describes doing.


def _order(repo, query: str, key_positions) -> None:
    """Steps 1-5 for one `ORDER BY` query, `ordered=True`. Mirrors
    `_assert_differential` above; separate rather than adding an
    `ordered=`/`key_positions=` parameter to it, since every one of
    that function's existing callers is unordered and would otherwise
    carry two always-default arguments for no benefit."""
    conn = load_unfiltered(BlameScan, repo, BLAME_SCHEMA, "blame")
    try:
        sqlite_rows = conn.execute(query).fetchall()
    finally:
        conn.close()
    _, historian_rows = run_historian(query, repo)
    assert_rows_match(sqlite_rows, historian_rows, ordered=True, key_positions=key_positions)


def test_order_by_ascending_single_key_with_genuine_ties(awkward_repo):
    """`café.py` is blamed across six lines, so `path` ties six ways -
    the tie-tolerant comparison's central case."""
    _order(awkward_repo, "SELECT path FROM blame ORDER BY path", key_positions=(0,))


def test_order_by_descending_single_key_with_genuine_ties(awkward_repo):
    _order(awkward_repo, "SELECT path FROM blame ORDER BY path DESC", key_positions=(0,))


def test_order_by_multiple_keys_independent_directions(awkward_repo):
    """`ORDER BY path ASC, line_no DESC` - the `values.py` worked
    example's shape, run against real data end to end rather than
    fabricated rows."""
    _order(
        awkward_repo,
        "SELECT path, line_no FROM blame ORDER BY path ASC, line_no DESC",
        key_positions=(0, 1),
    )


def test_order_by_alias_wins_over_real_column_of_the_same_name(awkward_repo):
    """The central risk this issue names, run differentially rather
    than only structurally: `line_no AS path` aliases the INTEGER
    `line_no` column to `path`'s own name. Alias-first (correct):
    `ORDER BY path` sorts by `line_no`'s numeric order. Column-first
    (the bug): it would sort by the real, TEXT `path` column's
    alphabetical order instead - a genuinely different row sequence
    over `awkward_repo`'s data, which is what makes this differential
    case discriminating rather than coincidental (`tests/test_binder.
    py`'s structural version of this same test pins the same risk at
    the binder level alone)."""
    _order(
        awkward_repo,
        "SELECT line_no AS path, path AS real_path FROM blame ORDER BY path",
        key_positions=(0,),
    )


def test_order_by_ordinal_matches_a_plain_column(awkward_repo):
    _order(awkward_repo, "SELECT path, line_no FROM blame ORDER BY 2", key_positions=(1,))


def test_order_by_ordinal_pointing_at_a_count_star_aggregate(awkward_repo):
    """Legal in `ORDER BY`, unlike `GROUP BY`'s own ordinal - confirmed
    against sqlite3 during this issue's grooming."""
    _order(
        awkward_repo,
        "SELECT author_name, count(*) FROM blame GROUP BY author_name ORDER BY 2 DESC",
        key_positions=(1,),
    )


def test_order_by_ordinal_pointing_at_a_sum_aggregate(awkward_repo):
    """The second shape this issue's grooming verified against
    sqlite3 alongside `count(*)`: an ordinal pointing at `sum(...)`."""
    _order(
        awkward_repo,
        "SELECT author_name, sum(line_no) FROM blame GROUP BY author_name ORDER BY 2 DESC",
        key_positions=(1,),
    )


# --- Ordinal detection: arbitrary unary nesting (orchestrator correction) --
#
# `_ordinal_value` (`sql/binder.py`) now unwraps any nesting of unary
# `+`/`-` down to an integer literal in both GROUP BY and ORDER BY,
# not just a bare literal or one level of unary - confirmed against
# sqlite3 3.51.0 (see `_docs/decisions.md` for the full evidence).


def test_order_by_bare_positive_ordinal_matches_named_column(awkward_repo):
    _order(awkward_repo, "SELECT path FROM blame ORDER BY +1", key_positions=(0,))


def test_order_by_double_negative_ordinal_matches_named_column(awkward_repo):
    """`ORDER BY -(-1)` - confirmed against sqlite3: ordinal 1, sorts
    by `path` exactly as `ORDER BY path`/`ORDER BY 1` would."""
    _order(awkward_repo, "SELECT path FROM blame ORDER BY -(-1)", key_positions=(0,))


def test_order_by_double_positive_ordinal_matches_named_column(awkward_repo):
    _order(awkward_repo, "SELECT path FROM blame ORDER BY +(+1)", key_positions=(0,))


def test_group_by_bare_positive_ordinal_matches_named_column(awkward_repo):
    _assert_differential(awkward_repo, "SELECT path, count(*) FROM blame GROUP BY +1")


def test_group_by_double_negative_ordinal_matches_named_column(awkward_repo):
    _assert_differential(awkward_repo, "SELECT path, count(*) FROM blame GROUP BY -(-1)")


def test_group_by_double_positive_ordinal_matches_named_column(awkward_repo):
    _assert_differential(awkward_repo, "SELECT path, count(*) FROM blame GROUP BY +(+1)")


def test_order_by_constant_expression_leaves_rows_in_scan_order(awkward_repo):
    """`ORDER BY 1+0` is a constant expression, not an ordinal -
    confirmed against sqlite3 directly (`_docs/decisions.md`): the
    result comes back in the same order as no `ORDER BY` at all, not
    resorted. `author_name` is the discriminating column - `awkward_
    repo`'s natural (path-ordered) scan interleaves the two authors
    (Zoë's lines, then Sam's two `café.py` lines, then more of Zoë's,
    then Sam's `phoenix.txt` line last), which is neither alphabetical
    order (`Sam` before `Zoë`) nor its reverse, so a query that
    actually sorted by `author_name` would visibly differ from this
    one - checked below, not merely asserted, so this test cannot pass
    by coincidence.

    A direct (non-oracle) comparison against historian's own unsorted
    query, not a differential one against sqlite3: neither engine's
    own contract defines a tie order for a key that ties on every row
    (a constant sorts every row into a single group), so encoding
    sqlite3's specific choice here as a permanent oracle assertion
    would pin an implementation accident rather than a semantic
    guarantee - `assert_rows_match`'s own tie-tolerant mode exists
    precisely to avoid exactly that trap. sqlite3 was still confirmed
    live to behave identically before writing this (`_docs/
    decisions.md`), so the property is real, just checked the
    direct way.
    """
    _, unsorted = run_historian("SELECT author_name FROM blame", awkward_repo)
    _, constant_ordered = run_historian(
        "SELECT author_name FROM blame ORDER BY 1+0", awkward_repo
    )
    _, actually_sorted = run_historian(
        "SELECT author_name FROM blame ORDER BY author_name", awkward_repo
    )

    assert constant_ordered == unsorted
    assert constant_ordered != actually_sorted


def test_order_by_a_group_by_key(awkward_repo):
    _order(
        awkward_repo,
        "SELECT author_name, count(*) FROM blame GROUP BY author_name ORDER BY author_name DESC",
        key_positions=(0,),
    )


def test_order_by_aggregate_not_in_the_select_list(awkward_repo):
    """`select k from g group by k order by count(*) desc` - the
    aggregate is the sort key but is never selected, so the harness
    cannot see ties on it (`assert_rows_match`'s `key_positions=None`
    mode). The case must be tie-free by construction, proven here on
    the SQLite side: `author_name`'s two groups must have distinct
    `count(*)` values, checked directly rather than assumed, so a
    fixture change that later gives two authors an equal line count
    fails this assertion loudly instead of producing a silent flake in
    `_order` below."""
    conn = load_unfiltered(BlameScan, awkward_repo, BLAME_SCHEMA, "blame")
    try:
        counts = [
            row[0]
            for row in conn.execute(
                "SELECT count(*) FROM blame GROUP BY author_name"
            ).fetchall()
        ]
    finally:
        conn.close()
    assert len(counts) == len(set(counts)), (
        f"fixture is no longer tie-free on count(*) per author: {counts!r} - "
        "this differential case needs new data or key_positions instead of None"
    )
    _order(
        awkward_repo,
        "SELECT author_name FROM blame GROUP BY author_name ORDER BY count(*) DESC",
        key_positions=None,
    )


def test_order_by_same_query_twice_gives_identical_order(awkward_repo):
    """The historian-side determinism criterion (`AGENTS.md`, spec
    §3's "Determinism and row order"): the same repository and query
    produce the same row order every time, including the relative
    order among rows that tie on every key - a direct (non-oracle)
    check, run against `path`, the same genuinely non-unique key
    `test_order_by_ascending_single_key_with_genuine_ties` uses."""
    _, first = run_historian("SELECT path, line_no FROM blame ORDER BY path", awkward_repo)
    _, second = run_historian("SELECT path, line_no FROM blame ORDER BY path", awkward_repo)
    assert first == second


# --- ORDER BY (issue #61): BindError cases, asserted directly ----------
#
# sqlite3 accepts every shape below - see this issue's own grooming for
# the confirmed sqlite3 output. historian rejects them: out-of-range
# ordinals are errors in both engines, but the aggregate-query narrowing
# is a deliberate historian-only divergence, matching #69's own
# GROUP BY/HAVING narrowing precedent - see `_docs/decisions.md`.


def test_order_by_ordinal_zero_raises_bind_error(tiny_repo):
    with pytest.raises(BindError):
        run_historian("SELECT path FROM blame ORDER BY 0", tiny_repo)


def test_order_by_negative_ordinal_raises_bind_error(tiny_repo):
    with pytest.raises(BindError):
        run_historian("SELECT path FROM blame ORDER BY -1", tiny_repo)


def test_order_by_ordinal_past_the_end_raises_bind_error(tiny_repo):
    with pytest.raises(BindError):
        run_historian("SELECT path, line_no FROM blame ORDER BY 3", tiny_repo)


def test_order_by_bare_ungrouped_column_in_an_aggregate_query_raises_bind_error(tiny_repo):
    """Legal in sqlite3 (sorts by an arbitrary row's value per group) -
    historian's own narrowing, extending #69's GROUP BY/HAVING
    precedent to ORDER BY."""
    with pytest.raises(BindError):
        run_historian(
            "SELECT author_name, count(*) FROM blame GROUP BY author_name ORDER BY line_no",
            tiny_repo,
        )


def test_order_by_aggregate_call_illegal_without_group_by_or_select_aggregate(tiny_repo):
    """Legal in `HAVING`, illegal in `ORDER BY` unless the query
    already aggregates - confirmed against sqlite3: "misuse of
    aggregate: count()"."""
    with pytest.raises(BindError):
        run_historian("SELECT path FROM blame ORDER BY count(*)", tiny_repo)


def test_group_by_constant_expression_still_raises_bind_error(tiny_repo):
    """`GROUP BY 1+0` must stay a `BindError` even after widening
    ordinal detection - legal in sqlite3 (one group, an arbitrary
    row's `path` per SQLite's own unspecified per-group choice), but
    `1+0` is a constant expression, not an ordinal, so `path` in the
    select list is neither the (nonexistent) group key nor an
    aggregate - the intended narrowing this fix must not disturb."""
    with pytest.raises(BindError):
        run_historian("SELECT path, count(*) FROM blame GROUP BY 1+0", tiny_repo)


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
    `ParseError`. This is a settled decision, not an open question -
    see `_docs/decisions.md`, 2026-09-18, for the full reasoning."""
    with pytest.raises(ParseError):
        run_historian("SELECT path p FROM blame", tiny_repo)


def test_missing_comma_between_two_columns_is_a_parse_error(tiny_repo):
    """#25: the concrete typo mandatory-`AS` exists to catch. Confirmed
    `sqlite3 :memory: "create table t(a integer, b integer); insert
    into t values(1,99); select a b from t;"` -> a single row, `99`,
    under the header `b` - SQLite does not error on a dropped comma
    between two real column names; it silently reinterprets `a b` as
    `a AS b` and column `a`'s value (`1`) is never reported at all.
    historian's mandatory `AS` turns that same input into a
    `ParseError` instead of a silent wrong answer - see
    `_docs/decisions.md`, 2026-09-18."""
    with pytest.raises(ParseError):
        run_historian("SELECT path line_no FROM blame", tiny_repo)


def test_where_resolves_a_select_list_alias(tiny_repo):
    """#32: SQLite lets `WHERE` reference a select-list alias as a
    fallback when no real column claims the name - confirmed:
    `sqlite3 :memory: "create table blame(path text); insert into
    blame values('a.py'); select path as p from blame where p =
    'a.py';"` -> `a.py`. `p` names no real `blame` column, so it falls
    back to the alias, and `WHERE p = 'src/utils.py'` returns exactly
    the rows for that path - this replaces the previous version of
    this test, which pinned the opposite (`BindError`) as a deliberate,
    documented gap; #32 closes it."""
    _assert_differential(tiny_repo, "SELECT path AS p FROM blame WHERE p = 'src/utils.py'")


def test_where_real_column_wins_over_alias_of_a_different_column(tiny_repo):
    """#32 finding 1: a real column always wins over a same-named
    alias of a *different* column - confirmed: `sqlite3 :memory:
    "create table t(a integer, b integer); insert into t
    values(1,10),(2,20); select b as a from t where a = 1;"` -> `10`,
    not empty (which is what it would be if `a` resolved to the alias,
    testing `b = 1`). Reproduced against `blame`: `commit_hash` is
    aliased to `path`'s name, and `WHERE path = ...` must still resolve
    to the real `path` column, not the alias - a wrong-direction
    resolution would test `commit_hash = 'src/utils.py'`, which never
    matches, and silently return zero rows instead of the real matches."""
    _assert_differential(
        tiny_repo, "SELECT commit_hash AS path FROM blame WHERE path = 'src/utils.py'"
    )


def test_where_unmatched_name_raises_bind_error(tiny_repo):
    """The regression guard for the "safe direction" #9 pinned: a name
    matching neither a real column nor any select-list alias still
    raises `BindError`, now that the alias fallback exists alongside
    real-column resolution - confirmed `sqlite3` also rejects it
    (`no such column: ghost`), so there is nothing to diff a row
    result against; both engines error before producing any rows."""
    with pytest.raises(BindError):
        run_historian("SELECT path AS p FROM blame WHERE ghost = 1", tiny_repo)


def test_where_duplicate_alias_resolves_to_first_occurrence(tiny_repo):
    """#32 finding 4: two select-list items sharing an alias resolve to
    the *first* occurrence - confirmed: `sqlite3 :memory: "create table
    t(a integer, b integer, c integer); insert into t values(1,60,5);
    select b as x, c as x from t where x > 50;"` returns the row
    (`b`=60 satisfies `x > 50`), not empty (which `c`=5 would give).
    Reproduced against `blame`: `path` and `author_email` both aliased
    `x`; `WHERE x = 'src/utils.py'` must resolve to the first, `path`,
    not the second, `author_email` (which never equals a path string,
    so a last-wins bug would silently return zero rows)."""
    _assert_differential(
        tiny_repo,
        "SELECT path AS x, author_email AS x FROM blame WHERE x = 'src/utils.py'",
    )


def test_where_select_list_still_cannot_see_its_own_alias(tiny_repo):
    """#32 finding 3: adding the `WHERE` fallback must not make an
    alias visible to *other select-list items* - confirmed unchanged:
    `sqlite3 :memory: "create table t(a integer); select a as x, x + 1
    from t;"` -> `no such column: x`. Reproduced against `blame`: the
    second select-list item referencing the first item's alias still
    raises `BindError`, the same as before this issue."""
    with pytest.raises(BindError):
        run_historian("SELECT path AS x, x FROM blame", tiny_repo)


def test_like_escape_is_a_parse_error(tiny_repo):
    """#51: SQLite supports `LIKE ... ESCAPE` - confirmed: `sqlite3
    :memory: "select '100%' like '100|%' escape '|';"` -> `1`.
    historian's lexer/parser has no `ESCAPE` clause at all and fails
    with a `ParseError` ("expected FROM, found identifier 'ESCAPE'"),
    not an "unsupported feature" error."""
    with pytest.raises(ParseError):
        run_historian("SELECT path FROM blame WHERE path LIKE '100|%' ESCAPE '|'", tiny_repo)


# --- Fixed by this issue: a digit run glued to an identifier (#22) -----
#
# `sql/lexer.py`'s `tokenize` now raises `LexError` for a completed
# digit run immediately followed, with no separator, by a character
# that would otherwise start an identifier - before #25's bare-alias
# `ParseError` ever gets a chance to fire, since `cli.py` calls
# `tokenize` then `parse` in sequence and `parse()` is never reached
# once `tokenize` raises. Each case below is `sqlite3 :memory:`
# "unrecognized token" - see `tests/test_lexer.py` for the full
# character-class rule and its lexer-level regression guards.


def test_digit_glued_to_ascii_identifier_is_a_lex_error(tiny_repo):
    """sqlite3: select 3abc;  -> Error: unrecognized token: "3abc".
    Confirms the fix, not #25: `LexError`, never `ParseError` - `3abc`
    never reaches the parser at all."""
    with pytest.raises(LexError):
        run_historian("SELECT 3abc FROM blame", tiny_repo)


def test_digit_glued_to_non_ascii_symbol_is_a_lex_error(tiny_repo):
    """sqlite3: select 1²;  -> Error: unrecognized token: "1²"."""
    with pytest.raises(LexError):
        run_historian("SELECT 1² FROM blame", tiny_repo)


def test_digit_glued_to_non_ascii_word_is_a_lex_error(tiny_repo):
    """sqlite3: select 3café;  -> Error: unrecognized token: "3café" -
    the trigger is the ASCII 'c' right after the digit run; the
    non-ASCII 'é' later in the same run doesn't need to be the first
    character to matter."""
    with pytest.raises(LexError):
        run_historian("SELECT 3café FROM blame", tiny_repo)


def test_digit_glued_directly_to_a_non_ascii_letter_is_a_lex_error(tiny_repo):
    """sqlite3: select 3é;  -> Error: unrecognized token: "3é" - the
    non-ASCII letter itself directly glued, not buried mid-identifier."""
    with pytest.raises(LexError):
        run_historian("SELECT 3é FROM blame", tiny_repo)


def test_digit_glued_to_a_spacing_accent_mark_is_a_lex_error(tiny_repo):
    """sqlite3: select 3´;  -> Error: unrecognized token: "3´" (U+00B4,
    spacing acute accent, directly glued)."""
    with pytest.raises(LexError):
        run_historian("SELECT 3´ FROM blame", tiny_repo)


def test_leading_zero_digit_run_glued_to_a_letter_is_a_lex_error(tiny_repo):
    """sqlite3: select 0y;  -> Error: unrecognized token: "0y" - a lone
    leading-zero digit run glued to an ordinary letter, not a hex or
    exponent marker."""
    with pytest.raises(LexError):
        run_historian("SELECT 0y FROM blame", tiny_repo)


def test_digit_run_glued_to_a_where_keyword_spelling_is_a_lex_error(tiny_repo):
    """sqlite3: select 3from;  -> Error: unrecognized token: "3from" -
    a digit run glued to a keyword-shaped identifier behaves the same
    as an ordinary one: keyword classification happens after the
    identifier text is scanned, and the glue check sits earlier, at the
    number/identifier boundary, so it never has to special-case
    keywords."""
    with pytest.raises(LexError):
        run_historian("SELECT 3from FROM blame", tiny_repo)


def test_digit_run_glued_to_a_select_keyword_spelling_is_a_lex_error(tiny_repo):
    """sqlite3: select 1select;  -> Error: unrecognized token:
    "1select" - same reasoning as `3from`, a different keyword and a
    different `TokenType`."""
    with pytest.raises(LexError):
        run_historian("SELECT 1select FROM blame", tiny_repo)


# --- Regression guards: deferred glue shapes stay unchanged (#22) ------
#
# `e`/`E`/`x`/`X` (scientific notation and hex markers - issue #6) and
# `_` (SQLite 3.46+'s digit-group separator - issue #70) are excluded
# from the rule above, so these three still lex as two tokens - a
# completed number followed by a separate identifier - and still hit
# #25's bare-alias `ParseError`, exactly as before this issue. A naive
# version of the fix (reject any digit run followed by any identifier-
# start character, no exceptions) would have turned each of these into
# a `LexError` instead; these guards prove it didn't overreach.


def test_scientific_notation_glue_still_hits_the_bare_alias_parse_error(tiny_repo):
    """sqlite3: select 3e2;  -> 300.0, a valid REAL historian does not
    implement (#6) - unaffected by this issue either way. `3e2` still
    lexes as INTEGER '3' + IDENTIFIER 'e2', so the query still fails at
    #25's parser branch, not at the lexer."""
    with pytest.raises(ParseError):
        run_historian("SELECT 3e2 FROM blame", tiny_repo)


def test_hex_glue_still_hits_the_bare_alias_parse_error(tiny_repo):
    """sqlite3: select 0x1f;  -> 31, a valid hex integer historian does
    not implement (#6). `0x1f` still lexes as INTEGER '0' + IDENTIFIER
    'x1f', unaffected by this issue."""
    with pytest.raises(ParseError):
        run_historian("SELECT 0x1f FROM blame", tiny_repo)


def test_digit_group_separator_glue_still_hits_the_bare_alias_parse_error(tiny_repo):
    """sqlite3: select 3_1;  -> 31, SQLite 3.46+'s digit-group
    separator - not implemented, and not tracked anywhere before this
    issue's grooming surfaced it (#70). `3_1` still lexes as INTEGER
    '3' + IDENTIFIER '_1'; excluding `_` from this issue's rule is what
    keeps it that way. The naive version of the fix (reject any digit
    run followed by any identifier-start character) would have turned
    this into a `LexError` instead - a regression against SQLite, not
    an improvement."""
    with pytest.raises(ParseError):
        run_historian("SELECT 3_1 FROM blame", tiny_repo)


# --- Issue #63: Bool3 -> Value reverse coercion at a nested operand site -
#
# #38 fixed the two root boundaries (a select-list root, a WHERE/HAVING
# predicate root). This is one recursion level deeper: a predicate-shaped
# result (a comparison, IS, LIKE, IN, BETWEEN) reaching a *nested* Value-
# requiring operand - a comparison operand, ||'s two sides, LIKE's two
# sides, IN's left operand and each list element, BETWEEN's operand/low/
# high, and arithmetic/unary-minus's operand. Every shape below used to
# raise a bare TypeError; each is now confirmed to match sqlite3 exactly,
# per this issue's own grooming audit and its sqlite3 3.51.0 transcript.


def test_comparison_nested_predicate_operand_eq(tiny_repo):
    """sqlite3: `select (1=1) = 1;` -> 1."""
    _assert_differential(tiny_repo, "SELECT (1=1) = 1 FROM blame")


def test_comparison_nested_predicate_operand_ne(tiny_repo):
    """sqlite3: `select (1=1) <> 1;` -> 0."""
    _assert_differential(tiny_repo, "SELECT (1=1) <> 1 FROM blame")


def test_is_nested_predicate_operand_left_side(tiny_repo):
    """sqlite3: `select (1=1) is 1;` -> 1."""
    _assert_differential(tiny_repo, "SELECT (1=1) IS 1 FROM blame")


def test_is_nested_predicate_operand_right_side(tiny_repo):
    """sqlite3: `select 1 is (1=1);` -> 1."""
    _assert_differential(tiny_repo, "SELECT 1 IS (1=1) FROM blame")


def test_concat_nested_predicate_operand(tiny_repo):
    """sqlite3: `select (1=1) || 'x';` -> '1x'."""
    _assert_differential(tiny_repo, "SELECT (1=1) || 'x' FROM blame")


def test_like_nested_predicate_operand_pattern_side_discriminating(tiny_repo):
    """sqlite3 (`t(n,p)` with `p='1'`): `select p from t where p like
    (1=1);` matches only the row whose TEXT is exactly `'1'` - proving
    the coercion produces SQLite's own `'1'` text spelling of TRUE, not
    a `str(bool)` bug (`str(True)` = `'True'`, which would match
    nothing here and hide the bug). Literal-only, so it does not depend
    on `tiny`'s own `path` values - it runs once per `blame` row and
    every row must agree independently."""
    _assert_differential(tiny_repo, "SELECT '1' LIKE (1=1) FROM blame")


def test_like_nested_predicate_operand_pattern_side_discriminating_false_branch(tiny_repo):
    """sqlite3: `select '0' like (1=2);` -> 1 - the `(1=2)` sibling of
    the discriminating case above."""
    _assert_differential(tiny_repo, "SELECT '0' LIKE (1=2) FROM blame")


def test_like_nested_predicate_operand_discriminates_against_str_bool(tiny_repo):
    """sqlite3: `select 'True' like (1=1);` -> 0 - a `str(bool)` bug
    (`str(True)` = `'True'`) would make this `1`; SQLite's own `'1'`
    text spelling of TRUE does not match the text `'True'`."""
    _assert_differential(tiny_repo, "SELECT 'True' LIKE (1=1) FROM blame")


def test_like_nested_predicate_operand_left_side(tiny_repo):
    """sqlite3: `select (1=1) like '1';` -> 1 - the left side of LIKE,
    not just the pattern side."""
    _assert_differential(tiny_repo, "SELECT (1=1) LIKE '1' FROM blame")


def test_in_nested_predicate_operand_left_side(tiny_repo):
    """sqlite3: `select (1=1) in (1,2);` -> 1."""
    _assert_differential(tiny_repo, "SELECT (1=1) IN (1, 2) FROM blame")


def test_in_nested_predicate_operand_list_element(tiny_repo):
    """sqlite3 (`n` INTEGER, `n=1`): `select n in (1=1, 2);` -> 1."""
    _assert_differential(tiny_repo, "SELECT line_no IN (1=1, 2) FROM blame")


def test_between_nested_predicate_operand_itself(tiny_repo):
    """sqlite3: `select (1=1) between 0 and 2;` -> 1."""
    _assert_differential(tiny_repo, "SELECT (1=1) BETWEEN 0 AND 2 FROM blame")


def test_between_nested_predicate_low_bound(tiny_repo):
    """sqlite3 (`n` INTEGER): `select n between (1=1) and 5;` matches
    the low bound coerced to 1."""
    _assert_differential(tiny_repo, "SELECT line_no BETWEEN (1=1) AND 5 FROM blame")


def test_between_nested_predicate_high_bound(tiny_repo):
    """sqlite3 (`n` INTEGER): `select n between 1 and (1=1);` matches
    the high bound coerced to 1."""
    _assert_differential(tiny_repo, "SELECT line_no BETWEEN 1 AND (1=1) FROM blame")


def test_where_position_nested_predicate_operand(tiny_repo):
    """The same reverse-coercion shape proven from a `WHERE` position,
    not only the select list."""
    _assert_differential(tiny_repo, "SELECT path FROM blame WHERE (line_no = 1) = 1")


def test_having_position_nested_predicate_operand(tiny_repo):
    """`HAVING` is planned as a `Filter`, same as `WHERE` - shares the
    identical `evaluate()` path and the identical gap, per this issue's
    own audit. `line_no` is both the `GROUP BY` key and the operand of
    the nested comparison, so the query binds cleanly."""
    _assert_differential(
        tiny_repo,
        "SELECT line_no, count(*) FROM blame GROUP BY line_no HAVING (line_no = 1) = 1",
    )


def test_comparison_of_a_null_predicate_result_stays_null(tiny_repo):
    """sqlite3: `select (1=NULL) = 1;` -> NULL. Not a defect - `Bool3`
    `NULL` and `Value` `NULL` are already the identical Python `None`
    on both sides of this boundary - but pinned here explicitly as a
    regression guard rather than left to accident, per this issue's own
    acceptance criteria."""
    _assert_differential(tiny_repo, "SELECT (1=NULL) = 1 FROM blame")


def test_arithmetic_on_a_nested_predicate_operand(tiny_repo):
    """sqlite3: `select (1=1)+10, typeof((1=1)+10);` -> 11|integer.
    Correct by explicit `coerce_to_value()` at the arithmetic call site
    now, not merely by Python's `bool` subclassing `int` - see
    `tests/test_expression.py` for the unit-level mutation check that
    actually discriminates the two implementations."""
    _assert_differential(tiny_repo, "SELECT (1=1)+10 FROM blame")


def test_unary_minus_on_a_nested_predicate_operand(tiny_repo):
    """sqlite3: `select -(1=1);` -> -1."""
    _assert_differential(tiny_repo, "SELECT -(1=1) FROM blame")


# --- Known disagreements deliberately not included here ----------------
#
# #24 (rejecting §1's non-goals by name - CTEs, window functions, and
# the rest of the permanently-out-of-scope grammar) is not included
# anywhere in this file, or this directory. SQLite executes that
# grammar; historian must always reject it. This suite's job is
# proving agreement on supported grammar, and there is never supposed
# to be agreement here - #24 belongs with the parser's or CLI's own
# unit tests, not a SQLite-agreement harness.
