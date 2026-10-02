"""Tests for historian.sql.binder.

Issue #9. Unit-style per spec §4's test-architecture table: asserts
`bind()`'s output and errors directly against constructed
`SelectStatement`/`Schema` values, no repository, no git, no SQLite
process at test time. Every expected value below was independently
checked against the `sqlite3` command-line tool (3.51.0) during this
issue's own work, not merely carried over from the issue's grooming -
the query used is quoted above each group, matching `tests/
test_parser.py`'s convention.

`_bind` binds real parsed SQL against the real `blame` schema, via
`historian.catalog.SCHEMAS` (`{"blame": BLAME_SCHEMA}`) - the real
catalog `bind()`'s required `catalog` parameter is given in
production (issue #35: `sql/binder.py` no longer owns or defaults to
a real catalog itself; `historian/catalog.py` does, and this test
file is one of the few callers allowed to import it, per that issue's
design). A few tests instead bind a hand-built `SelectStatement`
against a synthetic single-column schema (for the ASCII-folding case,
where blame has no non-ASCII column) or a hand-built AST bypassing the
parser entirely (for the defensive `Star`-in-a-bad-position backstop,
since real SQL cannot construct that shape once #31 is fixed - see the
issue's own grooming notes).
"""

import dataclasses

import pytest

from historian.catalog import SCHEMAS
from historian.schema import Column, ColumnType, Schema
from historian.sql.ast import (
    BinaryOp,
    ColumnRef,
    FunctionCall,
    Like,
    Literal,
    Operator,
    OrderDirection,
    SelectItem,
    SelectStatement,
    Star,
    UnaryOp,
    UnaryOperator,
)
from historian.sql.binder import (
    BindError,
    BoundColumnRef,
    BoundSelectStatement,
    _ordinal_value,
    bind,
)
from historian.sql import binder, walk
from historian.sql.lexer import Position, tokenize
from historian.sql.parser import parse
from historian.tables.blame import BLAME_SCHEMA

_POS = Position(line=1, column=1, offset=0)

#: `blame`'s declared column order, per spec §2 - used throughout to
#: assert `*` expansion and "no such column" `available` data.
_BLAME_COLUMNS = ("path", "line_no", "line", "commit_hash", "author_name", "author_email", "authored_at")


def _bind(sql: str) -> BoundSelectStatement:
    return bind(parse(tokenize(sql)), SCHEMAS)


# --- The catalog -------------------------------------------------------------


def test_tables_catalog_is_exactly_blame():
    """`historian.catalog.SCHEMAS` - the catalog `bind()` is actually
    called with in production (`cli.py`), and the one this file's own
    `_bind` helper uses - is seeded with exactly `{"blame":
    BLAME_SCHEMA}`, importing rather than redefining the schema - per
    the issue's own coordination note with #11's grooming, carried
    forward by #35's `historian/catalog.py`."""
    assert SCHEMAS == {"blame": BLAME_SCHEMA}
    assert SCHEMAS["blame"] is BLAME_SCHEMA


# --- FROM-table resolution, case-insensitive and ASCII-only ------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT path FROM blame",
        "SELECT path FROM BLAME",
        "SELECT path FROM BlAmE",
        'SELECT path FROM "BLAME"',
    ],
)
def test_from_table_resolves_case_insensitively(sql):
    """`select path from BLAME` and quoted `"BLAME"` both succeed
    against `create table blame(...)` - confirmed with sqlite3."""
    bound = _bind(sql)
    assert bound.from_table == "blame"


def test_unknown_from_table_raises_no_such_table():
    """`sqlite3`: `select path from ghost;` -> "no such table: ghost"."""
    with pytest.raises(BindError) as exc_info:
        _bind("SELECT path FROM ghost")
    err = exc_info.value
    assert str(err) == "no such table: ghost"
    assert err.available == ("blame",)


def test_no_such_table_error_position_is_select_statement_position():
    """`from_table` is a bare string on the AST with no position of its
    own - the error points at `SelectStatement.position` (the `SELECT`
    keyword) instead. `sqlite3`'s own CLI likewise prints no caret for
    "no such table", confirmed directly, so there is no better position
    to give a future renderer (#18)."""
    stmt = parse(tokenize("SELECT path FROM ghost"))
    with pytest.raises(BindError, match=r"no such table: ghost") as exc_info:
        bind(stmt, SCHEMAS)
    assert exc_info.value.position == stmt.position


def test_unknown_from_table_error_preserves_exact_casing():
    """`sqlite3`: `select path from GhOsT;` -> "no such table: GhOsT"
    (verbatim, not folded)."""
    with pytest.raises(BindError) as exc_info:
        _bind("SELECT path FROM GhOsT")
    assert str(exc_info.value) == "no such table: GhOsT"


# --- Column resolution, bare and qualified, case-insensitive -----------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT PATH FROM blame",
        "SELECT PaTh FROM blame",
        "SELECT blame.PATH FROM blame",
        "SELECT BlAmE.path FROM blame",
        'SELECT "PATH" FROM blame',
    ],
)
def test_column_resolves_case_insensitively(sql):
    """All five spellings resolve to `blame.path`, offset 0."""
    bound = _bind(sql)
    assert len(bound.select_list) == 1
    item = bound.select_list[0].expr
    assert isinstance(item, BoundColumnRef)
    assert item.offset == 0
    assert item.name == "path"


def test_ascii_only_folding_rejects_unicode_fold_of_straße():
    """The one case a Unicode-aware fold gets wrong. `sqlite3` (table
    `t(straße text)`): `select STRAßE from t` succeeds (`ß` is left
    alone), `select STRASSE from t` fails with "no such column:
    STRASSE" - Python's `'straße'.upper() == 'STRASSE'` would wrongly
    match the second. `blame` has no non-ASCII column, so this uses a
    synthetic single-column schema instead."""
    schema = Schema(columns=(Column("straße", ColumnType.TEXT),))
    catalog = {"t": schema}

    ok = bind(parse(tokenize("SELECT STRAßE FROM t")), catalog)
    assert isinstance(ok.select_list[0].expr, BoundColumnRef)
    assert ok.select_list[0].expr.name == "straße"

    with pytest.raises(BindError) as exc_info:
        bind(parse(tokenize("SELECT STRASSE FROM t")), catalog)
    assert str(exc_info.value) == "no such column: STRASSE"


def test_bound_column_ref_carries_integer_offset():
    """A resolved column reference carries a zero-based integer offset,
    computed once via `Schema.index_of` - not a name `exec/
    expression.py` would need to look up per row."""
    bound = _bind("SELECT author_email FROM blame")
    ref = bound.select_list[0].expr
    assert isinstance(ref, BoundColumnRef)
    assert ref.offset == BLAME_SCHEMA.index_of("author_email") == 5


def _all_column_refs(expr: object) -> list[object]:
    """Walk a bound expression tree and collect every leaf that
    references a column, resolved or not - used to confirm no
    unresolved `ColumnRef` survives binding anywhere in a deeply
    nested tree, not only at the top level."""
    if isinstance(expr, (BoundColumnRef, ColumnRef)):
        return [expr]
    found: list[object] = []
    if dataclasses.is_dataclass(expr):
        for field in dataclasses.fields(expr):
            value = getattr(expr, field.name)
            if isinstance(value, tuple):
                for item in value:
                    found.extend(_all_column_refs(item))
            else:
                found.extend(_all_column_refs(value))
    return found


def test_bound_tree_has_no_raw_column_ref_in_a_deeply_nested_where():
    """`dataclasses.replace` threads bound children through every
    composite node type, not only the ones exercised by simpler tests
    above. Confirmed as valid, sensible SQL against sqlite3 (empty
    result on an empty table, no error):

        select 1 from blame where (line_no between 1 and 10)
          and (path like 'src/%' or author_name in ('a','b'))
          and not (commit_hash is null);

    Every one of the five column references inside - in BETWEEN, LIKE,
    IN, and IS NULL, nested under AND/OR/NOT - must come back as a
    `BoundColumnRef` with the right offset, and no plain `ColumnRef`
    may remain anywhere in the tree."""
    bound = _bind(
        "SELECT 1 FROM blame WHERE (line_no BETWEEN 1 AND 10) "
        "AND (path LIKE 'src/%' OR author_name IN ('a', 'b')) "
        "AND NOT (commit_hash IS NULL)"
    )
    refs = _all_column_refs(bound.where)
    assert refs, "expected at least one column reference in the WHERE tree"
    assert all(isinstance(ref, BoundColumnRef) for ref in refs), refs
    by_name = {ref.name: ref.offset for ref in refs}
    assert by_name == {
        "line_no": BLAME_SCHEMA.index_of("line_no"),
        "path": BLAME_SCHEMA.index_of("path"),
        "author_name": BLAME_SCHEMA.index_of("author_name"),
        "commit_hash": BLAME_SCHEMA.index_of("commit_hash"),
    }


# --- Output column naming -----------------------------------------------------


def test_unaliased_column_output_name_is_declared_spelling():
    """`sqlite3 -header`: `select PaTh from blame` headers `path`, not
    `PaTh` - the declared schema spelling, not the user's typed
    casing."""
    bound = _bind("SELECT PaTh FROM blame")
    assert bound.select_list[0].output_name == "path"


def test_explicit_alias_used_verbatim():
    """`sqlite3 -header`: `select path as PaTh from blame` headers
    `PaTh` exactly as written - no folding of an explicit alias."""
    bound = _bind("SELECT path AS PaTh FROM blame")
    item = bound.select_list[0]
    assert item.alias == "PaTh"
    assert item.output_name == "PaTh"


# --- SELECT * / table.* expansion ---------------------------------------------


def test_star_expands_to_declared_column_order():
    """`sqlite3` on a 7-column table shaped like `blame`: `select *`
    returns columns in exactly the declared order."""
    bound = _bind("SELECT * FROM blame")
    assert [item.output_name for item in bound.select_list] == list(_BLAME_COLUMNS)
    assert [item.expr.offset for item in bound.select_list] == list(range(7))


@pytest.mark.parametrize("sql", ["SELECT blame.* FROM blame", "SELECT BlAmE.* FROM blame"])
def test_qualified_star_expands_the_same_way(sql):
    """`blame.*`, and a case-folded qualifier spelling of it, expand
    the same as bare `*` - confirmed against sqlite3."""
    bound = _bind(sql)
    assert [item.output_name for item in bound.select_list] == list(_BLAME_COLUMNS)


def test_qualified_star_wrong_table_raises_no_such_table_not_column():
    """`sqlite3`: `select other.* from blame` (with a real, unrelated
    `other` table) -> "no such table: other", never "no such column" -
    the opposite of a qualified *column* with the same mistake, see
    below."""
    with pytest.raises(BindError) as exc_info:
        bind(parse(tokenize("SELECT other.* FROM blame")), {"blame": BLAME_SCHEMA, "other": BLAME_SCHEMA})
    err = exc_info.value
    assert str(err) == "no such table: other"
    assert set(err.available) == {"blame", "other"}


def test_qualified_star_unknown_table_raises_no_such_table():
    """`sqlite3`: `select ghost.* from blame` -> "no such table: ghost"."""
    with pytest.raises(BindError) as exc_info:
        _bind("SELECT ghost.* FROM blame")
    assert str(exc_info.value) == "no such table: ghost"


# --- Qualified column vs. qualified star: opposite error kinds ---------------


def test_qualified_column_wrong_real_table_raises_no_such_column():
    """`sqlite3`: `select other.path from blame` (real, unrelated
    `other` table) -> "no such column: other.path", never "no such
    table" - confirmed opposite of the qualified-star case above."""
    with pytest.raises(BindError) as exc_info:
        bind(parse(tokenize("SELECT other.path FROM blame")), {"blame": BLAME_SCHEMA, "other": BLAME_SCHEMA})
    assert str(exc_info.value) == "no such column: other.path"


def test_qualified_column_unknown_table_raises_no_such_column():
    """`sqlite3`: `select ghost.path from blame` -> "no such column:
    ghost.path", not "no such table: ghost"."""
    with pytest.raises(BindError) as exc_info:
        _bind("SELECT ghost.path FROM blame")
    err = exc_info.value
    assert str(err) == "no such column: ghost.path"
    assert err.available == _BLAME_COLUMNS


# --- Unknown bare column -------------------------------------------------------


def test_unknown_bare_column_raises_no_such_column():
    """`sqlite3`: `select authr_name from blame` -> "no such column:
    authr_name"."""
    with pytest.raises(BindError) as exc_info:
        _bind("SELECT authr_name FROM blame")
    err = exc_info.value
    assert str(err) == "no such column: authr_name"
    assert err.available == _BLAME_COLUMNS


def test_no_such_column_error_preserves_exact_casing():
    """`sqlite3`: `select AuThR_NaMe from blame` -> "no such column:
    AuThR_NaMe" (verbatim, never folded or matched to the declared
    `author_name`)."""
    with pytest.raises(BindError) as exc_info:
        _bind("SELECT AuThR_NaMe FROM blame")
    assert str(exc_info.value) == "no such column: AuThR_NaMe"


def test_no_such_column_error_carries_column_ref_position():
    """The error's position is the offending `ColumnRef`'s own token
    position, already on the AST from the parser - no new position
    tracking needed here."""
    stmt = parse(tokenize("SELECT authr_name FROM blame"))
    ref = stmt.select_list[0].expr
    with pytest.raises(BindError, match=r"no such column: authr_name") as exc_info:
        bind(stmt, SCHEMAS)
    assert exc_info.value.position == ref.position


def test_no_such_column_available_is_blame_columns_in_declared_order():
    """Matches spec §5's worked example for this exact query: `blame
    has: path, line_no, line, commit_hash, author_name, author_email,
    authored_at`."""
    with pytest.raises(BindError, match=r"no such column: authr_name") as exc_info:
        _bind("SELECT authr_name FROM blame")
    assert exc_info.value.available == _BLAME_COLUMNS


# --- Resolution order across clauses -------------------------------------------


def test_from_table_resolved_before_select_list():
    """`sqlite3`: `select authr_name from ghost;` reports the missing
    table, not the missing column - `FROM` is checked first regardless
    of `SelectStatement`'s own field order (select_list, from_table,
    where)."""
    with pytest.raises(BindError) as exc_info:
        _bind("SELECT authr_name FROM ghost")
    assert str(exc_info.value) == "no such table: ghost"


def test_leftmost_select_item_reported_first():
    """`sqlite3`: `select ghost1, ghost2 from blame;` reports `ghost1`."""
    with pytest.raises(BindError) as exc_info:
        _bind("SELECT ghost1, ghost2 FROM blame")
    assert str(exc_info.value) == "no such column: ghost1"


def test_select_list_resolved_before_where():
    """`sqlite3`: `select ghost_select from blame where ghost_where =
    1;` reports `ghost_select`, not `ghost_where`."""
    with pytest.raises(BindError) as exc_info:
        _bind("SELECT ghost_select FROM blame WHERE ghost_where = 1")
    assert str(exc_info.value) == "no such column: ghost_select"


# Issue #115: one test per adjacent step of the cross-clause order -
# FROM (and an unknown `x.*` qualifier), LIMIT/OFFSET, the select
# list, HAVING on a non-aggregate query, HAVING, WHERE, ORDER BY,
# GROUP BY, the late aggregate misuse, then historian's own
# rejections. Each expected message is the oracle's (sqlite3 module
# 3.45.1); `tests/differential/test_error_order.py` checks the same
# order live against it.


def _bind_error(sql: str) -> BindError:
    with pytest.raises(BindError) as exc_info:
        _bind(sql)
    return exc_info.value


def test_order_qualified_star_table_before_select_list_names():
    assert str(_bind_error("SELECT ghost_s, ghost.* FROM blame")) == "no such table: ghost"


def test_order_from_table_before_limit():
    assert str(_bind_error("SELECT path FROM ghost_t LIMIT ghost_l")) == "no such table: ghost_t"


def test_order_limit_before_select_list():
    assert str(_bind_error("SELECT ghost_s FROM blame LIMIT ghost_l")) == "no such column: ghost_l"


def test_order_limit_before_offset():
    assert str(_bind_error("SELECT path FROM blame LIMIT ghost_l OFFSET ghost_o")) == "no such column: ghost_l"


def test_order_offset_column_before_limit_aggregate():
    """An error inside an aggregate call in LIMIT only counts once
    OFFSET has none: the oracle reports `ghost_f` here."""
    assert str(_bind_error("SELECT path FROM blame LIMIT avg(1) OFFSET ghost_f")) == "no such column: ghost_f"


def test_order_limit_aggregate_before_select_list():
    assert str(_bind_error("SELECT ghost_s FROM blame LIMIT count(*)")).startswith(
        "misuse of aggregate function count()"
    )


def test_limit_column_reference_names_the_column():
    assert str(_bind_error("SELECT path FROM blame LIMIT path")) == "no such column: path"
    assert str(_bind_error("SELECT path AS p FROM blame LIMIT p")) == "no such column: p"


def test_limit_non_literal_still_rejected_last():
    assert str(_bind_error("SELECT ghost_s FROM blame LIMIT 1+1")) == "no such column: ghost_s"
    assert str(_bind_error("SELECT path FROM blame LIMIT 1+1")).startswith("LIMIT must be a literal integer")


def test_order_select_list_before_having_non_aggregate():
    assert str(_bind_error("SELECT ghost_s FROM blame HAVING path = 'x'")) == "no such column: ghost_s"


def test_order_having_non_aggregate_before_having_names():
    assert str(_bind_error("SELECT path FROM blame HAVING ghost_h = 1")).startswith("HAVING requires an aggregate query")


def test_order_having_before_where():
    message = str(_bind_error("SELECT count(*) FROM blame WHERE ghost_w = 1 HAVING ghost_h = 1"))
    assert message == "no such column: ghost_h"


def test_order_where_before_order_by():
    assert str(_bind_error("SELECT path FROM blame WHERE ghost_w = 1 ORDER BY ghost_o")) == "no such column: ghost_w"


def test_order_order_by_before_group_by():
    assert str(_bind_error("SELECT path FROM blame GROUP BY ghost_g ORDER BY ghost_o")) == "no such column: ghost_o"


def test_order_where_before_group_by():
    """The issue's own case."""
    assert str(_bind_error("SELECT path FROM blame WHERE ghost_w = 1 GROUP BY ghost_g")) == "no such column: ghost_w"


def test_order_group_by_before_late_where_aggregate():
    message = str(_bind_error("SELECT path FROM blame WHERE count(*) > 1 GROUP BY ghost_g"))
    assert message == "no such column: ghost_g"


def test_order_late_order_by_aggregate_after_everything():
    assert str(_bind_error("SELECT path FROM blame ORDER BY count(*) LIMIT ghost_l")) == "no such column: ghost_l"
    assert str(_bind_error("SELECT path FROM blame ORDER BY count(*), 99")) == (
        "2nd ORDER BY term out of range - should be between 1 and 1"
    )


def test_where_aggregate_in_non_aggregate_query_is_in_place():
    assert str(_bind_error("SELECT path FROM blame WHERE count(*) > 1 ORDER BY ghost_o")).startswith(
        "misuse of aggregate function count()"
    )


def test_order_late_aggregate_before_historian_only_rejection():
    assert str(_bind_error("SELECT path, count(*) FROM blame WHERE count(*) > 1")).startswith("misuse of aggregate")


def test_historian_only_rejections_run_last():
    assert str(_bind_error("SELECT path, count(*) FROM blame WHERE ghost_w = 1")) == "no such column: ghost_w"
    assert str(_bind_error("SELECT line, count(*) FROM blame GROUP BY path ORDER BY ghost_o")) == (
        "no such column: ghost_o"
    )
    assert str(_bind_error("SELECT DISTINCT path FROM blame ORDER BY line LIMIT ghost_l")) == "no such column: ghost_l"


def test_historian_only_rejections_alone_unchanged():
    assert str(_bind_error("SELECT path, count(*) FROM blame")) == (
        "column path must appear in an aggregate function since this query has no GROUP BY"
    )
    assert str(_bind_error("SELECT DISTINCT path FROM blame ORDER BY line")) == (
        "column line must appear in the select list to be used in ORDER BY together with SELECT DISTINCT"
    )


def test_within_order_by_names_before_ordinal():
    assert str(_bind_error("SELECT path FROM blame ORDER BY 99, ghost_o")) == "no such column: ghost_o"


def test_within_group_by_names_then_ordinal_then_aggregate():
    assert str(_bind_error("SELECT path FROM blame GROUP BY 99, ghost_g")) == "no such column: ghost_g"
    assert str(_bind_error("SELECT path FROM blame GROUP BY count(*), 99")) == (
        "2nd GROUP BY term out of range - should be between 1 and 1"
    )
    assert str(_bind_error("SELECT path FROM blame GROUP BY count(*), ghost_g")) == "no such column: ghost_g"


def test_ordinal_suffixes_match_sqlite():
    many = ", ".join(["1"] * 10)
    assert str(_bind_error(f"SELECT path FROM blame ORDER BY {many}, 99")) == (
        "11th ORDER BY term out of range - should be between 1 and 1"
    )
    assert str(_bind_error("SELECT path FROM blame ORDER BY 1, 1, 99")) == (
        "3rd ORDER BY term out of range - should be between 1 and 1"
    )


def test_one_call_arity_before_misuse():
    assert str(_bind_error("SELECT path FROM blame WHERE avg() = 1")) == "wrong number of arguments to function avg()"


def test_same_name_in_two_clauses_carries_the_where_position():
    error = _bind_error("SELECT path FROM blame WHERE ghost = 1 GROUP BY ghost")
    assert str(error) == "no such column: ghost"
    assert error.position == Position(line=1, column=30, offset=29)


# --- Aliases: no cross-item namespace, WHERE fallback (#32) ------------------


def test_alias_not_visible_to_next_select_item():
    """`sqlite3`: `select path as p, p as p2 from blame;` errors "no
    such column: p" on the second item - SQLite evaluates every
    select-list expression against FROM alone, none see each other's
    aliases. Unchanged by #32: the fallback below applies only to
    `WHERE`, never within the select list itself."""
    with pytest.raises(BindError) as exc_info:
        _bind("SELECT path AS p, p AS p2 FROM blame")
    assert str(exc_info.value) == "no such column: p"


def test_select_list_cannot_see_its_own_alias_shape_from_acceptance_criteria():
    """The exact shape #32's acceptance criteria name: `SELECT a AS x,
    x + 1 FROM blame` (`a` standing in for one of `blame`'s own
    columns) - confirmed unchanged against `sqlite3` (`select a as x,
    x + 1 from t` still errors "no such column: x"; the binder does not
    type-check at bind time, so `x + 1` against a `TEXT` column binds
    the same way arithmetic against any column would). A regression
    guard distinct from the test above: this one exercises `x` nested
    inside an arithmetic-shaped expression rather than as a whole
    second select-list item."""
    with pytest.raises(BindError) as exc_info:
        _bind("SELECT path AS x, x + 1 FROM blame")
    assert str(exc_info.value) == "no such column: x"


def test_where_resolves_select_list_alias_as_fallback():
    """#32: `select path as p, line_no from blame where p = 'a.py'`
    succeeds in `sqlite3`, resolving `p` to the alias since no real
    `blame` column is named `p`. The alias's own bound expression
    (`path`, offset 0) is spliced into `WHERE` in place of the
    `ColumnRef` - confirmed by checking the substituted node is a
    `BoundColumnRef` for `path`, not a new kind of reference."""
    bound = _bind("SELECT path AS p FROM blame WHERE p = 'src/utils.py'")
    ref = bound.where.left
    assert isinstance(ref, BoundColumnRef)
    assert ref.name == "path"
    assert ref.offset == BLAME_SCHEMA.index_of("path")


def test_where_real_column_wins_over_alias_of_a_different_column():
    """#32 finding 1: `create table t(a integer, b integer); insert
    into t values(1,10),(2,20); select b as a from t where a = 1;`
    returns `10` - the real column `a` governs the predicate, not the
    alias `a` (which names `b`). Reproduced here against `blame`:
    aliasing `line_no` to `path` must not let `WHERE path = ...`
    resolve to the alias; the real `path` column wins, confirmed by
    checking the bound `WHERE` tree references `path`'s own offset,
    not `line_no`'s."""
    bound = _bind("SELECT line_no AS path FROM blame WHERE path = 'src/utils.py'")
    ref = bound.where.left
    assert isinstance(ref, BoundColumnRef)
    assert ref.name == "path"
    assert ref.offset == BLAME_SCHEMA.index_of("path")


def test_where_duplicate_alias_resolves_to_first_occurrence():
    """#32 finding 4: `select b as x, c as x from t where x > 50`
    resolves `x` to the first item, `b` - confirmed against `sqlite3`
    with discriminating data. Reproduced against `blame`: two items
    both aliased `x`, `WHERE x` must bind to the first (`path`), not
    the second (`author_email`)."""
    bound = _bind("SELECT path AS x, author_email AS x FROM blame WHERE x = 'src/utils.py'")
    ref = bound.where.left
    assert isinstance(ref, BoundColumnRef)
    assert ref.name == "path"
    assert ref.offset == BLAME_SCHEMA.index_of("path")


def test_where_unmatched_name_still_raises_no_such_column():
    """The regression guard for the "safe direction" #9 pinned: a name
    that matches neither a real column nor any select-list alias still
    raises `BindError`, unchanged, now that the alias fallback exists
    alongside real-column resolution."""
    with pytest.raises(BindError) as exc_info:
        _bind("SELECT path AS p FROM blame WHERE ghost = 1")
    assert str(exc_info.value) == "no such column: ghost"


def test_where_alias_reference_is_ascii_case_insensitive():
    """#32 finding 5: `select b as MyAlias from t where MYALIAS = 10`
    succeeds in `sqlite3` - alias matching reuses `_same_name`, the
    same ASCII-only fold the binder already uses for columns and
    tables, no second case-folding implementation."""
    bound = _bind("SELECT path AS MyAlias FROM blame WHERE MYALIAS = 'src/utils.py'")
    ref = bound.where.left
    assert isinstance(ref, BoundColumnRef)
    assert ref.name == "path"


def test_where_qualified_reference_never_falls_back_to_alias():
    """#32: a table-qualified reference is never an alias candidate -
    confirmed against `sqlite3` (`select b as x from t where t.x = 10`
    still raises "no such column: t.x" even though an alias `x`
    exists). `blame.p`, with `p` an alias and no real column of that
    name, must still raise, not silently resolve to the alias."""
    with pytest.raises(BindError) as exc_info:
        _bind("SELECT path AS p FROM blame WHERE blame.p = 'x'")
    assert str(exc_info.value) == "no such column: blame.p"


# --- Star in the wrong position: defensive backstop ---------------------------
#
# Constructed directly as an AST, bypassing the parser - #31 tracks
# the parser bug that lets some of these through today
# (`SELECT * AS x`, `count(blame.*)`); the third (`*` inside a general
# expression) already fails to parse. This issue's job is only to make
# sure the binder itself never crashes or silently mis-expands if a
# malformed tree reaches it, by whatever means.


def test_star_with_alias_raises_defensive_error():
    """`select * as x from blame` -> `near "as": syntax error` in
    sqlite3; historian's parser currently accepts it (#31). The binder
    rejects a `Star` select-item that carries an alias rather than
    silently aliasing the whole expansion."""
    stmt = SelectStatement(
        select_list=(SelectItem(expr=Star(table=None, position=_POS), alias="x", position=_POS),),
        from_table="blame",
        where=None,
        group_by=(),
        having=None,
        order_by=(),
        limit=None,
        offset=None,
        position=_POS,
    )
    with pytest.raises(
        BindError,
        match=r"\* is only allowed as a whole select-list item or the sole argument to a function call",
    ):
        bind(stmt, SCHEMAS)


def test_star_in_general_expression_position_raises_defensive_error():
    """`select path from blame where blame.* = 1` -> `near "*": syntax
    error` in sqlite3; already rejected by historian's own parser
    today, but the binder is tested directly against a hand-built tree
    so this stays true independent of parser behaviour."""
    where = Star(table="blame", position=_POS)
    stmt = SelectStatement(
        select_list=(SelectItem(expr=Literal(1, _POS), alias=None, position=_POS),),
        from_table="blame",
        where=where,
        group_by=(),
        having=None,
        order_by=(),
        limit=None,
        offset=None,
        position=_POS,
    )
    with pytest.raises(
        BindError,
        match=r"\* is only allowed as a whole select-list item or the sole argument to a function call",
    ):
        bind(stmt, SCHEMAS)


def test_qualified_star_as_function_argument_raises_defensive_error():
    """`select count(blame.*) from blame` -> `near "*": syntax error`
    in sqlite3; historian's parser currently accepts it (#31). A
    *qualified* star as a function's sole argument is not the same
    case as `count(*)` (unqualified, passed through unexpanded below)
    and is rejected here."""
    call = FunctionCall(name="count", args=(Star(table="blame", position=_POS),), position=_POS)
    stmt = SelectStatement(
        select_list=(SelectItem(expr=call, alias=None, position=_POS),),
        from_table="blame",
        where=None,
        group_by=(),
        having=None,
        order_by=(),
        limit=None,
        offset=None,
        position=_POS,
    )
    with pytest.raises(
        BindError,
        match=r"\* is only allowed as a whole select-list item or the sole argument to a function call",
    ):
        bind(stmt, SCHEMAS)


def test_star_as_non_sole_function_argument_raises_defensive_error():
    """`count(*, path)` -> `near ",": syntax error` in sqlite3, already
    rejected by historian's parser too. Checked directly against a
    hand-built tree: a `Star` alongside another argument is not "the
    sole argument" and is rejected rather than silently expanded or
    passed through.

    The error it gets today is the arity check, which `_validate_
    function_call` runs before the `Star` check is reached: every
    aggregate takes at most one argument, so a two-argument call never
    gets as far as "* is only allowed ..." (issue #108's `match=`
    sweep, pinning the message this query actually has)."""
    call = FunctionCall(
        name="count",
        args=(Star(table=None, position=_POS), ColumnRef(table=None, name="path", position=_POS)),
        position=_POS,
    )
    stmt = SelectStatement(
        select_list=(SelectItem(expr=call, alias=None, position=_POS),),
        from_table="blame",
        where=None,
        group_by=(),
        having=None,
        order_by=(),
        limit=None,
        offset=None,
        position=_POS,
    )
    with pytest.raises(BindError, match=r"wrong number of arguments to function count\(\)"):
        bind(stmt, SCHEMAS)


def test_count_star_passed_through_unexpanded():
    """`count(*)` is the one place `Star` legitimately survives into
    the bound tree, unexpanded and unvalidated - `*` there means "no
    columns", not "all columns", and this issue does not check whether
    `count` is a real function."""
    bound = _bind("SELECT count(*) FROM blame")
    call = bound.select_list[0].expr
    assert isinstance(call, FunctionCall)
    assert len(call.args) == 1
    assert isinstance(call.args[0], Star)
    assert call.args[0].table is None


# --- Explicitly out of scope, pinned so the gap is deliberate ----------------


def test_type_affinity_is_not_checked_at_bind_time():
    """`WHERE line_no = '5'` binds successfully: `line_no` is a real
    `INTEGER` column, and whether `'5'` needs coercing to compare
    against it is `exec/expression.py`'s job (#12), not this module's."""
    bound = _bind("SELECT path FROM blame WHERE line_no = '5'")
    assert bound.where is not None


#: Superseded by issue #60's aggregate registry - see the "Aggregate
#: calls (issue #60)" section below. `nonexistent_fn(path)` used to
#: bind successfully (the whole point of #45's complaint: the failure
#: only ever surfaced later, generically, in `exec/expression.py`);
#: it is a `BindError` now, checked directly below.


# --- Aggregate calls (issue #60) ----------------------------------------
#
# `sql/binder.py`'s new function-name/arity registry, the WHERE-rejects-
# aggregates rule, and the bare-column-mixed-with-aggregate narrowing
# (`_docs/decisions.md`, 2026-09-19). Every expected shape below was
# checked against `sqlite3` 3.51.0 during this issue's own grooming -
# see the issue body's "Aggregate edge cases" section.


def test_unknown_function_name_is_a_bind_error():
    """`SELECT nonexistent_fn(path) FROM blame` - confirmed `sqlite3`
    rejects this too (`no such function: nonexistent_fn`, a parse-time
    error there). Historian's message does not need to match sqlite3's
    wording (spec §3's Errors section only requires naming what is
    unknown), but this must be a real, non-generic `BindError` - not
    the old generic `EvalError` `exec/expression.py` used to raise
    (#45's complaint) once evaluation actually reached the call."""
    with pytest.raises(BindError) as exc_info:
        _bind("SELECT nonexistent_fn(path) FROM blame")
    assert "nonexistent_fn" in str(exc_info.value)


def test_sum_with_no_arguments_is_an_arity_error():
    """`SELECT sum() FROM blame` - confirmed a `Parse error` in
    `sqlite3` ("wrong number of arguments to function sum()"); `sum`
    (unlike `count`) always takes exactly one argument."""
    with pytest.raises(BindError, match=r"wrong number of arguments to function sum\(\)"):
        _bind("SELECT sum() FROM blame")


def test_count_with_two_arguments_is_an_arity_error():
    """`SELECT count(path, line_no) FROM blame` - confirmed a `Parse
    error` in `sqlite3`; `count` takes zero, one bare expression, or
    `*`, never two."""
    with pytest.raises(BindError, match=r"wrong number of arguments to function count\(\)"):
        _bind("SELECT count(path, line_no) FROM blame")


def test_sum_of_star_is_not_valid():
    """`sum(*)` is not `sum(<every column>)` - `*` has no meaning for
    any aggregate but `count`."""
    with pytest.raises(BindError, match=r"wrong number of arguments to function sum\(\)"):
        _bind("SELECT sum(*) FROM blame")


def test_count_with_no_arguments_binds_like_count_star():
    """`SELECT count() FROM blame` - confirmed legal in `sqlite3` and
    identical to `count(*)`. Binds successfully; `plan/planner.py`
    (issue #60) is what actually gives the two the same runtime
    meaning, checked there."""
    bound = _bind("SELECT count() FROM blame")
    call = bound.select_list[0].expr
    assert isinstance(call, FunctionCall)
    assert call.args == ()


def test_aggregate_call_in_where_is_a_bind_error():
    """`SELECT * FROM blame WHERE count(*) > 1` - confirmed `sqlite3`
    rejects this too ("misuse of aggregate function count()"). An
    aggregate call is never legal in `WHERE` in v1's grammar (no
    `HAVING` yet for it to belong to - #69)."""
    with pytest.raises(BindError) as exc_info:
        _bind("SELECT * FROM blame WHERE count(*) > 1")
    assert "count" in str(exc_info.value)


def test_aggregate_nested_inside_where_predicate_is_still_a_bind_error():
    """The WHERE-rejection applies at any depth, not only at the
    predicate's root - `ctx.reject_aggregates` threads through every
    recursive `_bind_expr` call unchanged."""
    with pytest.raises(
        BindError,
        match=r"misuse of aggregate function count\(\): aggregate calls are not allowed in WHERE",
    ):
        _bind("SELECT path FROM blame WHERE (count(*) > 1) AND path = 'a.py'")


def test_bare_column_mixed_with_aggregate_is_a_bind_error():
    """`SELECT path, count(*) FROM blame` - the narrowing decision
    (`_docs/decisions.md`, 2026-09-19): a bare, non-aggregated column
    alongside an aggregate call, no `GROUP BY`, is a `BindError` naming
    the offending column - not sqlite3's arbitrary-row answer."""
    with pytest.raises(BindError) as exc_info:
        _bind("SELECT path, count(*) FROM blame")
    assert "path" in str(exc_info.value)


def test_bare_column_inside_an_aggregate_argument_is_not_flagged():
    """`count(path)` is fine: `path` there is the aggregate's own
    argument, not a bare column sitting outside it."""
    bound = _bind("SELECT count(path) FROM blame")
    assert bound.select_list[0].expr is not None


def test_aggregate_free_query_is_unaffected_by_the_narrowing():
    """`SELECT path FROM blame` has no aggregate anywhere in its
    select list, so the narrowing rule never applies - this must keep
    binding exactly as it always has."""
    bound = _bind("SELECT path FROM blame")
    assert isinstance(bound.select_list[0].expr, BoundColumnRef)


def test_count_star_plus_literal_is_not_flagged():
    """`SELECT count(*) + 1 FROM blame` - the surrounding arithmetic
    references no bare column at all, so nothing is flagged."""
    bound = _bind("SELECT count(*) + 1 FROM blame")
    assert bound.select_list[0].expr is not None


def test_two_aggregate_calls_in_one_query_bind_successfully():
    """`SELECT count(*) + sum(line_no) FROM blame` - proves the
    registry and the narrowing check both handle more than one
    aggregate call in a single select list, not only a bare
    `count(*)`."""
    bound = _bind("SELECT count(*) + sum(line_no) FROM blame")
    assert bound.select_list[0].expr is not None


# --- Aggregate DISTINCT (issue #84) ------------------------------------
#
# The settled design needs no new binder logic at all: `_validate_
# function_call`'s existing name/arity checks already cover every case
# `DISTINCT` can interact with, and `call.distinct` passes through
# `_bind_expr`'s existing `dataclasses.replace(expr, args=...)`
# unchanged. These tests confirm that rather than add anything new.


def test_distinct_aggregate_call_binds_successfully_and_keeps_the_flag():
    """`SELECT count(DISTINCT line_no) FROM blame` binds like any other
    aggregate call, and the bound `FunctionCall` still carries
    `distinct=True` - `_bind_expr`'s `dataclasses.replace` only
    touches `args`, so every other field, including `distinct`,
    survives unchanged."""
    bound = _bind("SELECT count(DISTINCT line_no) FROM blame")
    call = bound.select_list[0].expr
    assert isinstance(call, FunctionCall)
    assert call.distinct is True


def test_non_distinct_aggregate_call_keeps_the_flag_false():
    bound = _bind("SELECT count(line_no) FROM blame")
    call = bound.select_list[0].expr
    assert isinstance(call, FunctionCall)
    assert call.distinct is False


def test_count_distinct_two_arguments_is_still_an_arity_error():
    """`count(DISTINCT a, b)` fails via the pre-existing `>1 argument`
    branch, unrelated to `DISTINCT` - confirmed live against `sqlite3`:
    identical rejection (differently worded) to plain `count(a, b)`."""
    with pytest.raises(BindError, match=r"wrong number of arguments to function count\(\)"):
        _bind("SELECT count(DISTINCT path, line_no) FROM blame")


def test_unknown_function_name_with_distinct_is_still_a_bind_error():
    """`nonexistent_fn(DISTINCT path)` - the existing unknown-name
    check fires first, unaffected by whether `DISTINCT` was written,
    exactly like a hypothetical future scalar function would need to
    decide separately (out of this issue's scope, per its own
    grooming)."""
    with pytest.raises(BindError) as exc_info:
        _bind("SELECT nonexistent_fn(DISTINCT path) FROM blame")
    assert "nonexistent_fn" in str(exc_info.value)


def test_min_max_distinct_bind_successfully_with_the_flag_set():
    """`min`/`max(DISTINCT x)` are accepted syntactically at bind time
    even though the accumulator never consults the flag for these two
    - see `exec/operators.py`'s `_Accumulator`."""
    bound = _bind("SELECT min(DISTINCT line_no), max(DISTINCT line_no) FROM blame")
    min_call, max_call = (item.expr for item in bound.select_list)
    assert isinstance(min_call, FunctionCall) and min_call.distinct is True
    assert isinstance(max_call, FunctionCall) and max_call.distinct is True


# --- No git, no subprocess needed to exercise this module --------------------
#
# `bind()` and everything it calls are tested entirely against
# in-memory `Schema`/`SelectStatement` values above, with no
# repository present - per `AGENTS.md`'s "only scan operators touch
# git". The real, fresh-interpreter guard for that promise -
# `import historian.sql.binder` must never put `subprocess` into
# `sys.modules` - lives in `tests/test_layering.py`
# (`test_binder_alone_does_not_import_subprocess`), not here: a check
# against `vars(binder_module)` in this same pytest process only ever
# asks "does this module itself write a top-level `import subprocess`
# statement", which stayed true throughout #35's bug (a transitive
# import via `from historian.tables.blame import BLAME_SCHEMA` still
# put `subprocess` in `sys.modules`, just under a different module's
# name in `vars()`) - it would have kept passing with the bug present
# and would keep passing after the fix, either way giving false
# confidence about the property that actually matters.


# --- GROUP BY / HAVING (issue #69) ----------------------------------------


def test_group_by_bare_column_resolves_and_is_legal():
    stmt = _bind("SELECT author_name, count(*) FROM blame GROUP BY author_name")
    assert len(stmt.group_by) == 1
    assert isinstance(stmt.group_by[0], BoundColumnRef)
    assert stmt.group_by[0].name == "author_name"


def test_group_by_ordinal_resolves_to_select_list_position():
    """`GROUP BY 1` groups by the *value* of the first select-list
    item - resolved purely positionally, matching the ordinal's own
    select-list item's already-bound expression."""
    stmt = _bind("SELECT author_name, count(*) FROM blame GROUP BY 1")
    assert stmt.group_by[0] == stmt.select_list[0].expr


def test_group_by_ordinal_not_resolved_through_alias():
    """An ordinal is positional, never name-based - `GROUP BY 1` with
    `path AS x` groups by select-list position 1's value (`path`),
    with no alias lookup involved at all."""
    stmt = _bind("SELECT path AS x, count(*) FROM blame GROUP BY 1")
    assert stmt.group_by[0] == stmt.select_list[0].expr
    assert isinstance(stmt.group_by[0], BoundColumnRef)
    assert stmt.group_by[0].name == "path"


def test_group_by_ordinal_pointing_at_an_aggregate_is_a_bind_error():
    """Orchestrator's correction: an ordinal that resolves to an
    aggregate call is rejected exactly like a direct or aliased one -
    `sqlite3` gives the identical "aggregate functions are not
    allowed in the GROUP BY clause" for all three."""
    with pytest.raises(
        BindError,
        match=r"aggregate functions are not allowed in the GROUP BY clause",
    ):
        _bind("SELECT path, count(*) FROM blame GROUP BY 2")


def test_group_by_direct_aggregate_call_is_a_bind_error():
    with pytest.raises(
        BindError,
        match=r"aggregate functions are not allowed in the GROUP BY clause",
    ):
        _bind("SELECT path FROM blame GROUP BY count(*)")


def test_group_by_aggregate_via_alias_is_a_bind_error():
    """The alias must resolve (issue #108): without alias fallback this
    would still be a `BindError`, but "no such column: c"."""
    with pytest.raises(
        BindError, match=r"aggregate functions are not allowed in the GROUP BY clause"
    ):
        _bind("SELECT count(*) AS c FROM blame GROUP BY c")


def test_group_by_ordinal_zero_is_out_of_range():
    with pytest.raises(
        BindError,
        match=r"1st GROUP BY term out of range - should be between 1 and 1",
    ):
        _bind("SELECT path FROM blame GROUP BY 0")


def test_group_by_ordinal_past_the_end_is_out_of_range():
    with pytest.raises(
        BindError,
        match=r"1st GROUP BY term out of range - should be between 1 and 1",
    ):
        _bind("SELECT path FROM blame GROUP BY 2")


def test_group_by_on_an_expression():
    """`GROUP BY` on an expression, selecting that same expression
    back (legal - it matches the group key by shape) alongside an
    aggregate."""
    stmt = _bind("SELECT line_no + 1, count(*) FROM blame GROUP BY line_no + 1")
    assert len(stmt.group_by) == 1
    assert len(stmt.select_list) == 2


def test_group_by_unknown_column_raises_no_such_column():
    with pytest.raises(BindError, match=r"no such column: ghost_column"):
        _bind("SELECT path FROM blame GROUP BY ghost_column")


# --- The grouped narrowing (issue #60, extended by #69) ---------------------


def test_grouped_select_item_matching_the_group_key_is_legal():
    stmt = _bind("SELECT author_name, count(*) FROM blame GROUP BY author_name")
    assert len(stmt.select_list) == 2


def test_grouped_select_item_not_a_key_and_not_an_aggregate_is_a_bind_error():
    """`SELECT path, author_name, count(*) ... GROUP BY author_name` -
    `path` is neither a group key nor an aggregate, extending #60's
    narrowing to the grouped case."""
    with pytest.raises(
        BindError,
        match=r"column path must appear in the GROUP BY clause or be used in an aggregate function",
    ):
        _bind("SELECT path, author_name, count(*) FROM blame GROUP BY author_name")


def test_group_by_with_no_aggregate_in_select_list_still_narrows():
    """`GROUP BY` alone - no aggregate anywhere - still triggers the
    narrowing: a select-list column that is not the group key is a
    BindError, the grouped analogue of #60's aggregate-only trigger."""
    with pytest.raises(
        BindError,
        match=r"column path must appear in the GROUP BY clause or be used in an aggregate function",
    ):
        _bind("SELECT path FROM blame GROUP BY author_name")


def test_group_by_multiple_columns_both_legal_as_select_items():
    stmt = _bind(
        "SELECT author_name, path, count(*) FROM blame GROUP BY author_name, path"
    )
    assert len(stmt.select_list) == 3


# --- HAVING (issue #69) ------------------------------------------------------


def test_having_references_a_bare_aggregate_call():
    stmt = _bind(
        "SELECT author_name, count(*) FROM blame GROUP BY author_name HAVING count(*) > 1"
    )
    assert stmt.having is not None


def test_having_references_a_select_list_alias():
    stmt = _bind(
        "SELECT author_name, count(*) AS c FROM blame GROUP BY author_name HAVING c > 1"
    )
    assert stmt.having is not None


def test_having_references_an_aggregate_not_in_the_select_list():
    stmt = _bind(
        "SELECT author_name FROM blame GROUP BY author_name HAVING sum(line_no) > 0"
    )
    assert stmt.having is not None


def test_having_without_group_by_binds():
    stmt = _bind("SELECT count(*) FROM blame HAVING count(*) > 1")
    assert stmt.group_by == ()
    assert stmt.having is not None


def test_having_unknown_column_raises_no_such_column():
    with pytest.raises(BindError, match=r"no such column: ghost_column"):
        _bind("SELECT count(*) FROM blame HAVING ghost_column > 1")


def test_having_with_no_group_by_and_no_aggregate_anywhere_is_a_bind_error():
    """Confirmed against `sqlite3 3.51.0`: `select path from t having
    path = 'x'` -> "HAVING clause on a non-aggregate query". Neither
    `GROUP BY` nor an aggregate call anywhere (select list or HAVING
    itself) is present here, so historian rejects it the same way."""
    with pytest.raises(
        BindError,
        match=r"HAVING requires an aggregate query - add GROUP BY or an aggregate function to the select list",
    ):
        _bind("SELECT path FROM blame HAVING path = 'src/utils.py'")


def test_having_legal_with_aggregate_only_in_the_select_list():
    """Confirmed against `sqlite3`: `select count(*) from t having 1`
    succeeds - the select list's own `count(*)` is enough to make this
    an aggregate query, even though HAVING's own predicate (`1`) has
    no aggregate call in it at all."""
    stmt = _bind("SELECT count(*) FROM blame HAVING 1")
    assert stmt.having is not None


def test_having_with_aggregate_only_in_having_itself_is_still_a_bind_error():
    """Confirmed against `sqlite3`: an aggregate call written in
    HAVING itself does *not* by itself make the query an aggregate
    query - `select path from t having count(*) > 1` still raises
    "HAVING clause on a non-aggregate query". Only `GROUP BY` or an
    aggregate call in the select list decides that."""
    with pytest.raises(
        BindError,
        match=r"HAVING requires an aggregate query - add GROUP BY or an aggregate function to the select list",
    ):
        _bind("SELECT path FROM blame HAVING count(*) > 1")


# --- HAVING's own grouped narrowing (orchestrator correction) --------------
#
# A bare column reference in HAVING must be a GROUP BY key (matched by
# shape) or sit inside an aggregate call's arguments - the same
# "grouped but not a key" reasoning `_docs/decisions.md`'s 2026-09-24
# follow-on note already gives for the select list, extended to
# HAVING. sqlite3 instead evaluates the bare column against an
# arbitrary row of the group, confirmed live:
# `select count(*) from t having path = 'x'` -> `3`;
# `select a, count(*) from t group by a having path = 'z'` -> `2|1`.


def test_having_bare_column_with_no_group_by_is_a_bind_error():
    """No `GROUP BY` means no keys at all - every bare column outside
    an aggregate is rejected."""
    with pytest.raises(
        BindError,
        match=r"column path must appear in the GROUP BY clause or be used in an aggregate function",
    ):
        _bind("SELECT count(*) FROM blame HAVING path = 'src/utils.py'")


def test_having_bare_column_not_a_group_key_is_a_bind_error():
    with pytest.raises(
        BindError,
        match=r"column path must appear in the GROUP BY clause or be used in an aggregate function",
    ):
        _bind("SELECT line_no, count(*) FROM blame GROUP BY line_no HAVING path = 'src/utils.py'")


def test_having_bare_aggregate_call_is_legal():
    stmt = _bind("SELECT count(*) FROM blame HAVING count(*) > 1")
    assert stmt.having is not None


def test_having_column_inside_an_aggregate_argument_is_legal():
    stmt = _bind("SELECT count(*) FROM blame HAVING sum(line_no) > 3")
    assert stmt.having is not None


def test_having_bare_column_matching_a_group_key_is_legal():
    stmt = _bind("SELECT path, count(*) FROM blame GROUP BY path HAVING path = 'src/utils.py'")
    assert stmt.having is not None


def test_having_expression_matching_an_expression_group_key_is_legal():
    """`GROUP BY line_no + 1 HAVING line_no + 1 > 2` - an expression
    key matched by shape, not just a bare column - confirmed legal
    against `sqlite3` before implementing (`select a+1, count(*) from
    t group by a+1 having a+1 > 2` -> a row)."""
    stmt = _bind(
        "SELECT line_no + 1, count(*) FROM blame GROUP BY line_no + 1 HAVING line_no + 1 > 2"
    )
    assert stmt.having is not None


def test_having_references_a_select_list_alias_of_an_aggregate_is_legal():
    stmt = _bind(
        "SELECT author_name, count(*) AS c FROM blame GROUP BY author_name HAVING c > 1"
    )
    assert stmt.having is not None


def test_having_references_a_select_list_alias_of_a_group_key_is_legal():
    stmt = _bind(
        "SELECT author_name AS a, count(*) FROM blame GROUP BY author_name HAVING a = 'Ana Petrova'"
    )
    assert stmt.having is not None


# --- GROUP BY / HAVING alias-vs-real-column resolution (issue #69 round 2) -
#
# QA's round-1 FAIL: no test anywhere pinned that GROUP BY/HAVING
# resolve column-first (`alias_first=False`), even though the issue's
# own body commits twice to exactly this test, mirroring #32's
# `test_where_real_column_wins_over_alias_of_a_different_column`.
# Mutation-tested against the real file: flipping either call site's
# `alias_first` to `True` makes the corresponding test below stop
# raising.


def test_group_by_real_column_wins_over_alias_of_a_different_column():
    """`author_name AS path` aliases a *different* column to `path`'s
    name. Column-first (correct): `GROUP BY path` binds to the real
    `path` column, so `author_name` in the select list is neither the
    group key nor an aggregate - `BindError`. Alias-first (the round-1
    gap): `GROUP BY path` would instead resolve through the alias to
    `author_name`, which then trivially matches itself and the query
    would bind without error - a silent, wrong-direction resolution
    this test is built to catch."""
    with pytest.raises(
        BindError,
        match=r"column author_name must appear in the GROUP BY clause or be used in an aggregate function",
    ):
        _bind("SELECT author_name AS path, count(*) FROM blame GROUP BY path")


def test_having_real_column_wins_over_alias_of_a_different_column():
    """`line_no AS path` aliases `line_no` to `path`'s name, with the
    real, unaliased `line_no` as the sole GROUP BY key. Column-first
    (correct): `HAVING path` binds to the real, non-key `path` column
    - `BindError`. Alias-first (the round-1 gap): `HAVING path` would
    instead resolve through the alias to `line_no`, which matches the
    group key by shape and binds legally - a real difference in what
    binds, not just in the error text."""
    with pytest.raises(
        BindError,
        match=r"column path must appear in the GROUP BY clause or be used in an aggregate function",
    ):
        _bind(
            "SELECT line_no AS path, count(*) FROM blame GROUP BY line_no "
            "HAVING path = 'src/utils.py'"
        )


# --- Ordinal-to-aggregate BindError, pinned on its own -----------------
#
# QA's secondary note: with the ordinal-to-aggregate check disabled, a
# query with no other reachable check (no non-key/non-aggregate column
# in the select list to trip the grouped narrowing instead) surfaced an
# `EvalError` rather than `BindError`. This query has no select-list
# item that could be caught by any other check - both items are
# themselves aggregate calls - so it exercises the ordinal-to-aggregate
# rejection in `_bind_group_by` on its own, with nothing else able
# to raise first.


def test_group_by_ordinal_to_aggregate_is_a_bind_error_with_no_other_check_reachable():
    with pytest.raises(
        BindError,
        match=r"aggregate functions are not allowed in the GROUP BY clause",
    ):
        _bind("SELECT count(*), sum(line_no) FROM blame GROUP BY 1")


# --- ORDER BY (issue #61) -----------------------------------------------
#
# The central risk named in this issue's grooming: `_resolve_name`'s
# `alias_first` flag (#32) has a caller here that no other clause
# supplies - `ORDER BY` is the one clause where the alias wins over a
# same-named real column, the reverse of WHERE/GROUP BY/HAVING. Written
# and watched fail before `bind()` grew ORDER BY support at all.


def test_order_by_alias_wins_over_real_column_of_the_same_name():
    """`author_name AS path` aliases a *different* column to `path`'s
    own name. Alias-first (correct - the one direction unique to
    `ORDER BY`, confirmed against sqlite3 during this issue's grooming:
    `select a as real_a, b as a from t order by a` sorts by the alias
    `b`, not the real column `a`): `ORDER BY path` resolves through the
    select-list alias to `author_name`'s own bound expression, not the
    real `path` column. Column-first (the bug this test is built to
    catch, and the direction every other clause uses): `ORDER BY path`
    would instead resolve to the real `path` column - a silent,
    wrong-direction resolution. Confirmed live against the real code:
    flipping `bind()`'s ORDER BY call site's `alias_first` to `False`
    makes the offset asserted below stop matching."""
    bound = _bind("SELECT author_name AS path FROM blame ORDER BY path")
    order_expr = bound.order_by[0].expr
    assert isinstance(order_expr, BoundColumnRef)
    assert order_expr.offset == BLAME_SCHEMA.index_of("author_name")
    assert order_expr.offset != BLAME_SCHEMA.index_of("path")


def test_no_order_by_defaults_to_empty_tuple():
    assert _bind("SELECT path FROM blame").order_by == ()


def test_order_by_bare_column_resolves_and_defaults_ascending():
    bound = _bind("SELECT path FROM blame ORDER BY path")
    assert len(bound.order_by) == 1
    item = bound.order_by[0]
    assert isinstance(item.expr, BoundColumnRef)
    assert item.expr.offset == BLAME_SCHEMA.index_of("path")
    assert item.direction is OrderDirection.ASC


def test_order_by_desc_direction_carried_through():
    bound = _bind("SELECT path FROM blame ORDER BY path DESC")
    assert bound.order_by[0].direction is OrderDirection.DESC


def test_order_by_multiple_keys_each_with_own_direction():
    bound = _bind("SELECT path, line_no FROM blame ORDER BY path ASC, line_no DESC")
    assert len(bound.order_by) == 2
    assert bound.order_by[0].direction is OrderDirection.ASC
    assert bound.order_by[1].direction is OrderDirection.DESC


def test_order_by_qualified_reference_never_falls_back_to_alias():
    """`blame.path` is table-qualified - never a candidate for the
    alias fallback, mirroring `_resolve_name`'s existing rule for
    `WHERE`/`GROUP BY`/`HAVING` (confirmed against sqlite3 for `WHERE`
    by #32: `select b as x from t where t.x = 10` still raises "no
    such column: t.x"). `line_no AS path` would otherwise make
    `ORDER BY path` ambiguous with the alias; `ORDER BY blame.path`
    must still resolve to the real column."""
    bound = _bind("SELECT line_no AS path FROM blame ORDER BY blame.path")
    item = bound.order_by[0]
    assert isinstance(item.expr, BoundColumnRef)
    assert item.expr.offset == BLAME_SCHEMA.index_of("path")


def test_order_by_unknown_column_raises_no_such_column():
    with pytest.raises(BindError, match=r"no such column: ghost_column"):
        _bind("SELECT path FROM blame ORDER BY ghost_column")


# --- ORDER BY ordinal (issue #61) ---------------------------------------


def test_order_by_ordinal_resolves_to_select_list_position():
    bound = _bind("SELECT author_name, line_no FROM blame ORDER BY 2")
    item = bound.order_by[0]
    assert isinstance(item.expr, BoundColumnRef)
    assert item.expr.offset == BLAME_SCHEMA.index_of("line_no")


def test_order_by_ordinal_not_resolved_through_alias():
    """An ordinal is a positional reference, never a name - it does
    not go through `_resolve_name`'s alias-vs-column logic at all, so
    `alias_first` cannot affect it either way. `ORDER BY 1` must always
    mean the first select-list item regardless of what it is named."""
    bound = _bind("SELECT author_name AS x FROM blame ORDER BY 1")
    item = bound.order_by[0]
    assert isinstance(item.expr, BoundColumnRef)
    assert item.expr.offset == BLAME_SCHEMA.index_of("author_name")


def test_order_by_ordinal_pointing_at_an_aggregate_is_legal():
    """Unlike `GROUP BY`'s own ordinal (which rejects this shape
    outright), `ORDER BY`'s ordinal may legally point at an aggregate
    call - confirmed against sqlite3: `select k, count(*) from g group
    by k order by 2 desc` succeeds."""
    bound = _bind("SELECT author_name, count(*) FROM blame GROUP BY author_name ORDER BY 2 DESC")
    item = bound.order_by[0]
    assert isinstance(item.expr, FunctionCall)
    assert item.expr.name == "count"


def test_order_by_ordinal_zero_is_out_of_range():
    with pytest.raises(
        BindError,
        match=r"1st ORDER BY term out of range - should be between 1 and 1",
    ):
        _bind("SELECT path FROM blame ORDER BY 0")


def test_order_by_negative_ordinal_is_out_of_range():
    """`ORDER BY -1` - parsed as `UnaryOp(NEG, Literal(1))`, per `sql/
    parser.py`'s own docstring - is still recognised as an ordinal and
    rejected as out of range, confirmed against sqlite3: "1st ORDER BY
    term out of range - should be between 1 and 1" for a single-column
    select list."""
    with pytest.raises(
        BindError,
        match=r"1st ORDER BY term out of range - should be between 1 and 1",
    ):
        _bind("SELECT path FROM blame ORDER BY -1")


def test_order_by_ordinal_past_the_end_is_out_of_range():
    with pytest.raises(
        BindError,
        match=r"1st ORDER BY term out of range - should be between 1 and 1",
    ):
        _bind("SELECT path FROM blame ORDER BY 2")


def test_order_by_explicit_positive_ordinal_still_resolves():
    """`ORDER BY +1` - `UnaryOp(POS, Literal(1))` - is still an
    ordinal, confirmed against sqlite3 (`select k from g order by +1`
    succeeds, same as a bare `1`)."""
    bound = _bind("SELECT path FROM blame ORDER BY +1")
    item = bound.order_by[0]
    assert isinstance(item.expr, BoundColumnRef)
    assert item.expr.offset == BLAME_SCHEMA.index_of("path")


# --- Ordinal detection: arbitrary unary nesting (orchestrator correction) --
#
# SQLite treats any nesting of unary `+`/`-` (and parentheses, which
# vanish at parse time - `sql/parser.py`'s `_parse_primary`) around an
# integer literal as an ordinal, in both GROUP BY and ORDER BY - not
# just a bare `Literal` or one level of unary. `_ordinal_value` is the
# one shared helper both `_bind_group_by` and `_bind_order_by`
# now use. A direct unit test on the helper itself, plus both clauses
# through `bind()`, since the helper is unreachable via `bind()` alone
# for the "not an ordinal" shapes (a `BinaryOp` binds as an ordinary
# expression well before `_ordinal_value` would matter to the caller).


def test_ordinal_value_unwraps_arbitrarily_nested_unary_signs():
    literal_one = Literal(value=1, position=_POS)
    single_neg = UnaryOp(op=UnaryOperator.NEG, operand=literal_one, position=_POS)
    single_pos = UnaryOp(op=UnaryOperator.POS, operand=literal_one, position=_POS)
    double_neg = UnaryOp(op=UnaryOperator.NEG, operand=single_neg, position=_POS)
    double_pos = UnaryOp(op=UnaryOperator.POS, operand=single_pos, position=_POS)

    assert _ordinal_value(literal_one) == 1
    assert _ordinal_value(single_neg) == -1
    assert _ordinal_value(double_neg) == 1
    assert _ordinal_value(double_pos) == 1


def test_ordinal_value_is_none_for_a_binary_expression():
    expr = BinaryOp(op=Operator.ADD, left=Literal(1, _POS), right=Literal(0, _POS), position=_POS)
    assert _ordinal_value(expr) is None


def test_ordinal_value_is_none_for_a_binary_expression_nested_inside_unary():
    """A unary sign wrapping something that is *not* itself an ordinal
    stays not-an-ordinal, whatever is inside it - `-(1+0)` is a
    computed expression, not `-1`."""
    inner = BinaryOp(op=Operator.ADD, left=Literal(1, _POS), right=Literal(0, _POS), position=_POS)
    expr = UnaryOp(op=UnaryOperator.NEG, operand=inner, position=_POS)
    assert _ordinal_value(expr) is None


def test_order_by_double_negative_ordinal_resolves_to_the_positive_position():
    """`ORDER BY -(-1)` - confirmed against sqlite3: ordinal 1, not a
    range error and not a computed constant. Parentheses vanish at
    parse time, so this is `UnaryOp(NEG, UnaryOp(NEG, Literal(1)))`."""
    bound = _bind("SELECT path FROM blame ORDER BY -(-1)")
    item = bound.order_by[0]
    assert isinstance(item.expr, BoundColumnRef)
    assert item.expr.offset == BLAME_SCHEMA.index_of("path")


def test_order_by_double_positive_ordinal_resolves():
    bound = _bind("SELECT path FROM blame ORDER BY +(+1)")
    item = bound.order_by[0]
    assert isinstance(item.expr, BoundColumnRef)
    assert item.expr.offset == BLAME_SCHEMA.index_of("path")


def test_order_by_unary_chain_without_parens_still_an_ordinal():
    """`ORDER BY - -1` - two `MINUS` tokens with no parentheses at
    all - confirmed against sqlite3: ordinal 1."""
    bound = _bind("SELECT path FROM blame ORDER BY - -1")
    item = bound.order_by[0]
    assert isinstance(item.expr, BoundColumnRef)
    assert item.expr.offset == BLAME_SCHEMA.index_of("path")


def test_order_by_constant_expression_is_not_an_ordinal():
    """`ORDER BY 1+0` - confirmed against sqlite3: a constant
    expression, not ordinal 1 - binds as an ordinary (if useless)
    sort key, not a positional reference."""
    bound = _bind("SELECT path FROM blame ORDER BY 1+0")
    item = bound.order_by[0]
    assert isinstance(item.expr, BinaryOp)
    assert item.expr.op is Operator.ADD


def test_group_by_double_negative_ordinal_resolves_to_the_positive_position():
    """`GROUP BY -(-1)` - confirmed against sqlite3: ordinal 1."""
    bound = _bind("SELECT path, count(*) FROM blame GROUP BY -(-1)")
    assert bound.group_by == (
        BoundColumnRef(offset=BLAME_SCHEMA.index_of("path"), name="path", position=bound.group_by[0].position),
    )


def test_group_by_double_positive_ordinal_resolves():
    bound = _bind("SELECT path, count(*) FROM blame GROUP BY +(+1)")
    assert isinstance(bound.group_by[0], BoundColumnRef)
    assert bound.group_by[0].offset == BLAME_SCHEMA.index_of("path")


def test_group_by_bare_positive_ordinal_still_resolves():
    bound = _bind("SELECT path, count(*) FROM blame GROUP BY +1")
    assert isinstance(bound.group_by[0], BoundColumnRef)
    assert bound.group_by[0].offset == BLAME_SCHEMA.index_of("path")


def test_group_by_constant_expression_still_raises_bind_error():
    """`GROUP BY 1+0` must stay a `BindError` - a constant key, so the
    select list's non-key, non-aggregate column stays ungrouped, the
    intended narrowing this fix must not disturb."""
    with pytest.raises(
        BindError,
        match=r"column path must appear in the GROUP BY clause or be used in an aggregate function",
    ):
        _bind("SELECT path, count(*) FROM blame GROUP BY 1+0")


# --- ORDER BY and aggregate legality (issue #61) -------------------------


def test_order_by_aggregate_call_with_no_group_by_and_no_select_aggregate_is_a_bind_error():
    """Confirmed against sqlite3: `select p from u order by count(*)`
    (no GROUP BY, no aggregate in the select list) is "misuse of
    aggregate: count()" - the same rejection WHERE gets, not the
    HAVING-style allowance."""
    with pytest.raises(
        BindError,
        match=r"misuse of aggregate function count\(\): aggregate calls are not allowed in WHERE",
    ):
        _bind("SELECT path FROM blame ORDER BY count(*)")


def test_order_by_aggregate_call_legal_once_select_list_already_aggregates():
    """Confirmed against sqlite3: `select count(*) from u order by
    count(*)` succeeds - the select list's own aggregate is enough to
    make ORDER BY's aggregate call legal, with no GROUP BY at all."""
    bound = _bind("SELECT count(*) FROM blame ORDER BY count(*)")
    assert isinstance(bound.order_by[0].expr, FunctionCall)


def test_order_by_aggregate_not_in_select_list_is_legal_when_grouped():
    """Confirmed against sqlite3: `select k from g group by k order by
    count(*) desc` succeeds - the aggregate need not appear in the
    select list at all once GROUP BY makes the query aggregate."""
    bound = _bind("SELECT author_name FROM blame GROUP BY author_name ORDER BY count(*) DESC")
    assert isinstance(bound.order_by[0].expr, FunctionCall)


def test_order_by_references_a_group_by_key_is_legal():
    bound = _bind(
        "SELECT author_name, count(*) FROM blame GROUP BY author_name ORDER BY author_name DESC"
    )
    item = bound.order_by[0]
    assert isinstance(item.expr, BoundColumnRef)


def test_order_by_bare_column_not_a_group_key_is_a_bind_error():
    """Confirmed against sqlite3 as *legal* there (`select k, count(*)
    from g group by k order by v` sorts by an arbitrary row's `v` per
    group) - historian deliberately narrows this the same way it
    narrows HAVING (`_docs/decisions.md`, 2026-09-19/2026-09-24 and
    this issue's own follow-on): a bare, non-key, non-aggregate column
    in an aggregating query's ORDER BY is a BindError."""
    with pytest.raises(
        BindError,
        match=r"column line_no must appear in the GROUP BY clause or be used in an aggregate function",
    ):
        _bind("SELECT author_name, count(*) FROM blame GROUP BY author_name ORDER BY line_no")


def test_order_by_bare_column_not_a_group_key_is_a_bind_error_whole_table_aggregate():
    """The same narrowing applies to a whole-table aggregate query (no
    GROUP BY at all, so there are zero keys) - every bare column in
    ORDER BY is then a BindError, the same "no keys means every bare
    column is rejected" reasoning HAVING already uses with no GROUP
    BY."""
    with pytest.raises(
        BindError,
        match=r"column path must appear in the GROUP BY clause or be used in an aggregate function",
    ):
        _bind("SELECT count(*) FROM blame ORDER BY path")


def test_order_by_expression_built_purely_from_group_keys_is_legal():
    bound = _bind(
        "SELECT line_no, count(*) FROM blame GROUP BY line_no ORDER BY line_no + 1"
    )
    item = bound.order_by[0]
    assert isinstance(item.expr, BinaryOp)
    assert item.expr.op is Operator.ADD
    assert isinstance(item.expr.left, BoundColumnRef)
    assert item.expr.left.offset == BLAME_SCHEMA.index_of("line_no")


def test_order_by_narrowing_does_not_apply_to_a_non_aggregate_query():
    """No GROUP BY, no aggregate anywhere - `is_aggregate_query` is
    `False`, so the narrowing never triggers and an ordinary bare
    column binds exactly as it would in any other non-aggregate
    query."""
    bound = _bind("SELECT path FROM blame ORDER BY line_no")
    assert isinstance(bound.order_by[0].expr, BoundColumnRef)


def test_order_by_real_column_wins_over_alias_of_a_different_column_when_grouped():
    """Sanity check that the grouped narrowing runs against the
    *alias-resolved* expression, not the written name: `line_no AS
    path` with `GROUP BY line_no` as the sole key - `ORDER BY path`
    resolves (alias-first) to `line_no`, which *is* the group key, so
    this must bind, not raise."""
    bound = _bind(
        "SELECT line_no AS path, count(*) FROM blame GROUP BY line_no ORDER BY path"
    )
    item = bound.order_by[0]
    assert isinstance(item.expr, BoundColumnRef)
    assert item.expr.offset == BLAME_SCHEMA.index_of("line_no")


# --- LIMIT / OFFSET (issue #77) ------------------------------------------
#
# `<n>` narrows to exactly what `_ordinal_value` recognises - a literal
# integer, arbitrarily wrapped in unary +/- - reusing that helper
# rather than a third copy of the same recursive unwrap (issue #77's
# own design, justified in `_docs/decisions.md`). No range check
# (unlike GROUP BY/ORDER BY ordinals): 0 and any negative value bind
# successfully and carry their own runtime meaning, which is
# `plan/planner.py`/`exec/operators.py`'s concern, not this module's.


def test_no_limit_defaults_to_none():
    bound = _bind("SELECT path FROM blame")
    assert bound.limit is None
    assert bound.offset is None


def test_limit_bare_integer_resolves():
    bound = _bind("SELECT path FROM blame LIMIT 3")
    assert bound.limit == 3
    assert bound.offset is None


def test_limit_and_offset_both_resolve():
    bound = _bind("SELECT path FROM blame LIMIT 3 OFFSET 2")
    assert bound.limit == 3
    assert bound.offset == 2


def test_limit_zero_resolves_to_zero_not_an_error():
    bound = _bind("SELECT path FROM blame LIMIT 0")
    assert bound.limit == 0


def test_limit_negative_literal_resolves_to_a_negative_int():
    """`LIMIT -1` - confirmed against sqlite3: a negative LIMIT is
    legal and means "no limit" (issue #77's own design, implemented by
    `exec/operators.py`'s `Limit`, not this module) - the binder's own
    job is only to resolve the literal value, -1, without raising."""
    bound = _bind("SELECT path FROM blame LIMIT -1")
    assert bound.limit == -1


def test_offset_negative_literal_resolves_to_a_negative_int():
    """The clamp-to-zero happens in `exec/operators.py`'s `Limit`, not
    here - the binder resolves the literal value verbatim."""
    bound = _bind("SELECT path FROM blame LIMIT 5 OFFSET -1")
    assert bound.offset == -1


def test_limit_unary_paren_nested_literal_resolves():
    """`LIMIT -(-2)` - the same arbitrary unary/paren nesting
    `_ordinal_value` already handles for `GROUP BY`/`ORDER BY`
    (`2f25756`), reused unchanged here."""
    bound = _bind("SELECT path FROM blame LIMIT -(-2)")
    assert bound.limit == 2


def test_offset_unary_paren_nested_literal_resolves():
    bound = _bind("SELECT path FROM blame LIMIT 5 OFFSET +(+2)")
    assert bound.offset == 2


def test_limit_rejects_arithmetic_expression():
    """`LIMIT 1+1` - legal in sqlite3 (confirmed during this issue's
    grooming), but a `BinaryOp` is never an ordinal shape - deliberate
    narrowing, see `_docs/decisions.md`."""
    with pytest.raises(
        BindError,
        match=r"LIMIT must be a literal integer, optionally wrapped in unary \+/- and parentheses",
    ):
        _bind("SELECT path FROM blame LIMIT 1+1")


def test_limit_rejects_column_reference():
    """`LIMIT line_no` - "no such column: a" in sqlite3 too (LIMIT's
    expression has zero visible columns there), but for a different
    reason: historian rejects every non-ordinal shape uniformly,
    sqlite3 rejects a column reference specifically."""
    with pytest.raises(BindError, match=r"no such column: line_no"):
        _bind("SELECT path FROM blame LIMIT line_no")


def test_limit_rejects_select_list_alias():
    """Confirmed against sqlite3: `select a as n from t order by a
    limit n` still raises "no such column: n" - LIMIT gets no alias
    fallback there either. historian rejects it too, for the uniform
    narrowing reason rather than by replicating that specific rule."""
    with pytest.raises(BindError, match=r"no such column: n"):
        _bind("SELECT path AS n FROM blame LIMIT n")


def test_limit_rejects_text_literal():
    """`LIMIT '2'` - legal in sqlite3 (numeric-affinity TEXT
    coercion), deliberately not adopted here - see `_docs/
    decisions.md`."""
    with pytest.raises(
        BindError,
        match=r"LIMIT must be a literal integer, optionally wrapped in unary \+/- and parentheses",
    ):
        _bind("SELECT path FROM blame LIMIT '2'")


def test_limit_rejects_real_literal_even_with_zero_fractional_part():
    """`LIMIT 2.0` - legal in sqlite3 (`MustBeInt`'s exact-zero-
    fractional-part rule), deliberately not adopted - a REAL `Literal`
    is never an ordinal shape regardless of its value."""
    with pytest.raises(
        BindError,
        match=r"LIMIT must be a literal integer, optionally wrapped in unary \+/- and parentheses",
    ):
        _bind("SELECT path FROM blame LIMIT 2.0")


def test_limit_rejects_null():
    with pytest.raises(
        BindError,
        match=r"LIMIT must be a literal integer, optionally wrapped in unary \+/- and parentheses",
    ):
        _bind("SELECT path FROM blame LIMIT NULL")


def test_limit_rejects_function_call():
    with pytest.raises(
        BindError,
        match=r"LIMIT must be a literal integer, optionally wrapped in unary \+/- and parentheses",
    ):
        _bind("SELECT path FROM blame LIMIT abs(-2)")


def test_offset_rejects_arithmetic_expression():
    with pytest.raises(
        BindError,
        match=r"OFFSET must be a literal integer, optionally wrapped in unary \+/- and parentheses",
    ):
        _bind("SELECT path FROM blame LIMIT 5 OFFSET 1+1")


def test_offset_rejects_text_literal():
    with pytest.raises(
        BindError,
        match=r"OFFSET must be a literal integer, optionally wrapped in unary \+/- and parentheses",
    ):
        _bind("SELECT path FROM blame LIMIT 5 OFFSET '2'")


# --- DISTINCT (issue #78) --------------------------------------------------
#
# `distinct` is carried straight through from `SelectStatement.distinct`
# with no name resolution of its own. The one thing DISTINCT changes
# here is a narrowing on `ORDER BY`: once `stmt.distinct` is set, every
# bare column an ORDER BY key touches must match a select-list item by
# shape (exactly, or be built purely from select-list items) or it is
# a BindError - reusing `_split_for_grouped_check` matched against the
# select list instead of GROUP BY's keys. Justified by oracle
# reliability, not historian's own determinism - see
# `_docs/decisions.md`.


def test_distinct_defaults_to_false():
    bound = _bind("SELECT path FROM blame")
    assert bound.distinct is False


def test_distinct_flag_is_carried_through():
    bound = _bind("SELECT DISTINCT path FROM blame")
    assert bound.distinct is True


def test_distinct_star_expands_normally():
    bound = _bind("SELECT DISTINCT * FROM blame")
    assert bound.distinct is True
    assert [item.output_name for item in bound.select_list] == list(_BLAME_COLUMNS)


def test_distinct_without_order_by_triggers_no_narrowing():
    """No `ORDER BY` at all - the narrowing has nothing to check, and
    `distinct` alone never raises."""
    bound = _bind("SELECT DISTINCT author_name FROM blame")
    assert bound.distinct is True


def test_distinct_order_by_a_selected_column_is_legal():
    bound = _bind("SELECT DISTINCT path, line_no FROM blame ORDER BY line_no")
    assert isinstance(bound.order_by[0].expr, BoundColumnRef)


def test_distinct_order_by_a_column_not_in_the_select_list_is_a_bind_error():
    """The discriminating case this issue's own grooming verified live
    against sqlite3 3.51.0: `create table u2(p,n); insert into u2
    values('x',2),('x',1),('y',1); select distinct p from u2 order by
    n;` returns `y` then `x` - but sqlite3's own answer here has no
    documented, reproducible rule behind it (see `_docs/decisions.md`
    for the full discriminating arithmetic), so historian raises
    `BindError` instead of guessing at it."""
    with pytest.raises(
        BindError,
        match=r"column line_no must appear in the select list to be used in ORDER BY together with SELECT DISTINCT",
    ):
        _bind("SELECT DISTINCT path FROM blame ORDER BY line_no")


def test_distinct_order_by_ordinal_is_always_legal():
    """An ordinal already points at a select-list item verbatim, by
    construction - no extra check needed, and this proves it."""
    bound = _bind("SELECT DISTINCT path, line_no FROM blame ORDER BY 2")
    assert isinstance(bound.order_by[0].expr, BoundColumnRef)
    assert bound.order_by[0].expr.offset == BLAME_SCHEMA.index_of("line_no")


def test_distinct_order_by_select_list_alias_is_legal():
    """A select-list alias reference resolves (alias-first, #61's own
    ORDER BY direction) to that item's own bound expression, which
    trivially shape-matches itself."""
    bound = _bind("SELECT DISTINCT path AS p FROM blame ORDER BY p")
    assert isinstance(bound.order_by[0].expr, BoundColumnRef)
    assert bound.order_by[0].expr.offset == BLAME_SCHEMA.index_of("path")


def test_distinct_order_by_expression_built_purely_from_a_selected_column_is_legal():
    bound = _bind("SELECT DISTINCT line_no FROM blame ORDER BY line_no + 1")
    item = bound.order_by[0]
    assert isinstance(item.expr, BinaryOp)
    assert item.expr.op is Operator.ADD


def test_distinct_order_by_aggregate_matching_select_list_aggregate_is_legal():
    """`DISTINCT` combined with `GROUP BY`/aggregates: `ORDER BY
    count(*)` matches the select list's own `count(*)` by shape."""
    bound = _bind(
        "SELECT DISTINCT author_name, count(*) FROM blame GROUP BY author_name "
        "ORDER BY count(*)"
    )
    assert isinstance(bound.order_by[0].expr, FunctionCall)


def test_distinct_order_by_group_key_not_in_select_list_is_a_bind_error():
    """Stricter than the plain aggregate narrowing above it: `author_name`
    *is* the GROUP BY key (so the aggregate-query narrowing alone would
    accept it), but it is not itself a select-list item once DISTINCT
    is present - `SELECT DISTINCT count(*)` never selects it - so the
    DISTINCT narrowing rejects it even though the GROUP BY narrowing
    would not."""
    with pytest.raises(
        BindError,
        match=r"column author_name must appear in the select list to be used in ORDER BY together with SELECT DISTINCT",
    ):
        _bind(
            "SELECT DISTINCT count(*) FROM blame GROUP BY author_name "
            "ORDER BY author_name"
        )


def test_distinct_order_by_unselected_aggregate_is_a_bind_error():
    """Issue #103: the gap the M3 milestone review found.
    `_split_for_grouped_check`'s `FunctionCall` branch used to return
    "contains an aggregate, no bad column" for *any* function call,
    unconditionally - correct for the GROUP BY-keyed callers, wrong
    here, where an aggregate call must itself shape-match a
    select-list item like any other ORDER BY key touch. `count(*)` is
    never selected, so this must raise `BindError` the same way a bare
    unselected column already does."""
    with pytest.raises(
        BindError,
        match=r"aggregate count\(\.\.\.\) must appear in the select list to be used in ORDER BY together with SELECT DISTINCT",
    ):
        _bind(
            "SELECT DISTINCT author_name FROM blame GROUP BY author_name, path "
            "ORDER BY count(*) DESC"
        )


def test_distinct_order_by_unselected_aggregate_is_a_bind_error_even_with_always_false_where():
    """The check is bind-time and data-independent (the same discipline
    #102 established for nested-aggregate rejection): it must fire
    before any row is read, whether or not a row would ever reach the
    aggregate."""
    with pytest.raises(
        BindError,
        match=r"aggregate count\(\.\.\.\) must appear in the select list to be used in ORDER BY together with SELECT DISTINCT",
    ):
        _bind(
            "SELECT DISTINCT author_name FROM blame WHERE line_no > 100000 "
            "GROUP BY author_name, path ORDER BY count(*) DESC"
        )


def test_distinct_order_by_unselected_aggregate_nested_in_an_expression_is_a_bind_error():
    """The unselected aggregate does not have to be the whole ORDER BY
    key - `count(*) + 0` still contains it, and the walk must find it
    inside the arithmetic rather than only at the top level."""
    with pytest.raises(
        BindError,
        match=r"aggregate count\(\.\.\.\) must appear in the select list to be used in ORDER BY together with SELECT DISTINCT",
    ):
        _bind(
            "SELECT DISTINCT author_name FROM blame GROUP BY author_name, path "
            "ORDER BY count(*) + 0 DESC"
        )


def test_distinct_order_by_aggregate_alias_expression_built_purely_from_select_list_is_legal():
    """Positive case not covered by any existing binder test before this
    issue: `c + 1` is built purely from the selected aggregate's own
    alias `c`, so it must stay legal, the same "built purely from"
    allowance the bare-column case already gets."""
    bound = _bind(
        "SELECT DISTINCT author_name, count(*) AS c FROM blame "
        "GROUP BY author_name, path ORDER BY c + 1 DESC"
    )
    assert isinstance(bound.order_by[0].expr, BinaryOp)


def test_distinct_order_by_aggregate_matching_select_list_aggregate_case_insensitive_is_legal():
    """Issue #103 round 2 (QA FAIL on comment #5869548584): function
    names are ASCII-case-insensitive in SQLite - `COUNT(*)` in the
    select list and `count(*)` in `ORDER BY` name the same aggregate,
    and the sort key *is* fully determined by the output row. Confirmed
    against the oracle (`tests/oracle.py`, sqlite3 3.45.1) that this
    exact query is accepted and returns 2 rows, no error. Before this
    fix, `sql/walk.py`'s `expr_shape_equal` `FunctionCall` branch compared raw,
    un-folded names, so `strict_function_calls=True` wrongly rejected
    it with `BindError`."""
    bound = _bind(
        "SELECT DISTINCT author_name, COUNT(*) FROM blame GROUP BY author_name, path "
        "ORDER BY count(*) DESC"
    )
    assert isinstance(bound.order_by[0].expr, FunctionCall)


# --- Aggregate DISTINCT flag joins shape equality (issue #131) -----------
#
# `count(path)` and `count(DISTINCT path)` are different aggregates, but
# `sql/walk.py`'s `_same_node_fields` used to compare a `FunctionCall` by folded name
# only, so the strict DISTINCT/ORDER BY match accepted one for the
# other. SQLite itself accepts the rejected queries below (checked with
# `tests/oracle.py`); the `BindError` is #78's deliberate narrowing:
# the ORDER BY key is not a select-list item and is not determined by
# the output row.

_DISTINCT_AGG_ERROR = (
    r"aggregate {name}\(\.\.\.\) must appear in the select list to be used in ORDER BY together with SELECT DISTINCT"
)


def test_distinct_order_by_count_distinct_with_only_plain_count_selected_is_a_bind_error():
    """The QA query from #103's round 2 verdict, verbatim."""
    with pytest.raises(BindError, match=_DISTINCT_AGG_ERROR.format(name="count")):
        _bind(
            "SELECT DISTINCT author_name, COUNT(path) FROM blame GROUP BY author_name, path "
            "ORDER BY count(DISTINCT path) DESC"
        )


def test_distinct_order_by_plain_count_with_only_count_distinct_selected_is_a_bind_error():
    with pytest.raises(BindError, match=_DISTINCT_AGG_ERROR.format(name="count")):
        _bind(
            "SELECT DISTINCT author_name, COUNT(DISTINCT path) FROM blame GROUP BY author_name, path "
            "ORDER BY count(path) DESC"
        )


def test_distinct_order_by_count_distinct_mismatch_is_a_bind_error_with_where_0():
    """Bind-time and data-independent: raised before any row is read.
    The binder never sees a repository, so an empty repository cannot
    change the answer either."""
    with pytest.raises(BindError, match=_DISTINCT_AGG_ERROR.format(name="count")):
        _bind(
            "SELECT DISTINCT author_name, COUNT(path) FROM blame WHERE 0 GROUP BY author_name, path "
            "ORDER BY count(DISTINCT path) DESC"
        )


@pytest.mark.parametrize("name", ["sum", "avg", "min", "max"])
@pytest.mark.parametrize("selected_distinct", [False, True])
def test_distinct_order_by_other_aggregates_differing_only_in_distinct_is_a_bind_error(name, selected_distinct):
    selected = f"{name}(DISTINCT line_no)" if selected_distinct else f"{name}(line_no)"
    ordered = f"{name}(line_no)" if selected_distinct else f"{name}(DISTINCT line_no)"
    with pytest.raises(BindError, match=_DISTINCT_AGG_ERROR.format(name=name)):
        _bind(
            f"SELECT DISTINCT author_name, {selected} FROM blame GROUP BY author_name, path "
            f"ORDER BY {ordered} DESC"
        )


def test_distinct_order_by_count_distinct_nested_in_an_expression_is_a_bind_error():
    with pytest.raises(BindError, match=_DISTINCT_AGG_ERROR.format(name="count")):
        _bind(
            "SELECT DISTINCT author_name, COUNT(path) FROM blame GROUP BY author_name, path "
            "ORDER BY count(DISTINCT path) + 0 DESC"
        )


@pytest.mark.parametrize(
    ("sql", "distinct"),
    [
        (
            "SELECT DISTINCT author_name, COUNT(DISTINCT path) FROM blame GROUP BY author_name, path "
            "ORDER BY count(DISTINCT path) DESC",
            True,
        ),
        (
            "SELECT DISTINCT author_name, count(DISTINCT path) FROM blame GROUP BY author_name, path "
            "ORDER BY COUNT(DISTINCT path) DESC",
            True,
        ),
        (
            "SELECT DISTINCT author_name, COUNT(path) FROM blame GROUP BY author_name, path "
            "ORDER BY count(path) DESC",
            False,
        ),
        (
            "SELECT DISTINCT author_name, sum(DISTINCT line_no) FROM blame GROUP BY author_name, path "
            "ORDER BY SUM(DISTINCT line_no) DESC",
            True,
        ),
    ],
)
def test_distinct_order_by_aggregate_with_matching_distinct_flag_is_legal(sql, distinct):
    """Positive control: equal flags (both set or both clear) still
    match, with #103's name folding intact. Kills the reverse mutant
    that treats every pair of aggregate calls as unequal."""
    bound = _bind(sql)
    key = bound.order_by[0].expr
    assert isinstance(key, FunctionCall)
    assert key.distinct is distinct
    assert bound.select_list[1].expr.distinct is distinct


@pytest.mark.parametrize("order_by", ["c", "2"])
def test_distinct_order_by_plain_count_by_alias_or_ordinal_is_legal(order_by):
    bound = _bind(
        "SELECT DISTINCT author_name, COUNT(path) AS c FROM blame GROUP BY author_name, path "
        f"ORDER BY {order_by}"
    )
    assert len(bound.order_by) == 1


def _bound_count(distinct: bool) -> FunctionCall:
    arg = BoundColumnRef(offset=BLAME_SCHEMA.index_of("path"), name="path", position=_POS)
    return FunctionCall(name="count", args=(arg,), position=_POS, distinct=distinct)


def test_expr_shape_equal_count_differing_only_in_distinct_is_not_equal():
    assert walk.expr_shape_equal(_bound_count(False), _bound_count(True)) is False
    assert walk.expr_shape_equal(_bound_count(True), _bound_count(False)) is False


def test_expr_shape_equal_count_with_equal_distinct_flags_is_equal():
    assert walk.expr_shape_equal(_bound_count(True), _bound_count(True)) is True
    assert walk.expr_shape_equal(_bound_count(False), _bound_count(False)) is True


def test_distinct_without_order_by_and_grouped_binds_normally():
    bound = _bind("SELECT DISTINCT author_name, count(*) FROM blame GROUP BY author_name")
    assert bound.distinct is True
    assert len(bound.group_by) == 1


# --- LIKE ... ESCAPE: escape is bound like left/pattern (issue #51 --------
# --- follow-up) -------------------------------------------------------------
#
# The original grooming for #51 claimed `sql/binder.py` needed no
# change, on the theory that `Like.escape` "is bound the same generic
# way every other Expr field already is" - false: `_bind_expr`'s `Like`
# branch is an explicit `dataclasses.replace(expr, left=..., pattern=
# ...)` that never mentioned `escape` at all, so a column-reference
# escape operand stayed a raw, unbound `ColumnRef` all the way to
# `exec/expression.py`'s `evaluate()`, which has no case for it and
# hits its defensive "unhandled expression node type" `AssertionError`
# - confirmed live on this branch before this fix: `SELECT count(*)
# FROM blame WHERE 'a' LIKE 'a' ESCAPE author_name` raised
# `AssertionError`, not the `BindError`/`EvalError` a real historian
# error is supposed to be. `_bind_expr`'s `Like` branch now binds
# `escape` exactly like `left`/`pattern` (`None` passes through
# unchanged - there is nothing to bind when no `ESCAPE` clause is
# present).


def test_like_escape_column_reference_binds_to_a_bound_column_ref():
    """`ESCAPE author_name` must resolve to a `BoundColumnRef`, exactly
    like `author_name` would anywhere else in the expression tree - not
    stay an unbound `ColumnRef`, which is the shape that used to reach
    `evaluate()` and trip its defensive `AssertionError`."""
    bound = _bind("SELECT path FROM blame WHERE path LIKE 'x' ESCAPE author_name")
    like = bound.where
    assert isinstance(like, Like)
    assert isinstance(like.escape, BoundColumnRef)
    assert like.escape.offset == BLAME_SCHEMA.index_of("author_name")
    assert like.escape.name == "author_name"


def test_like_escape_unknown_column_is_a_bind_error_not_an_assertion_error():
    """`ESCAPE nosuchcol` must raise the binder's own `BindError: no
    such column: nosuchcol` - the same error any other unknown-column
    reference in the query gets - not silently pass through unbound and
    surface later as an `AssertionError` somewhere else entirely."""
    with pytest.raises(BindError) as exc_info:
        _bind("SELECT path FROM blame WHERE path LIKE 'x' ESCAPE nosuchcol")
    assert str(exc_info.value) == "no such column: nosuchcol"


def test_like_without_escape_still_binds_with_escape_none():
    """The ordinary, no-`ESCAPE` case must still bind cleanly with
    `escape` staying `None` - the fix only adds a branch for when
    `expr.escape is not None`, never changes the no-`ESCAPE` path."""
    bound = _bind("SELECT path FROM blame WHERE path LIKE 'x'")
    like = bound.where
    assert isinstance(like, Like)
    assert like.escape is None


def test_like_escape_arbitrary_expression_containing_a_column_still_binds():
    """The escape operand can be more than a bare column reference -
    `_bind_expr` recurses into it the same as any other expression tree,
    so a column reference nested inside a larger escape expression
    (here, concatenated with a literal) still resolves to a
    `BoundColumnRef` at its own position in the tree."""
    bound = _bind("SELECT path FROM blame WHERE path LIKE 'x' ESCAPE author_name || ''")
    like = bound.where
    assert isinstance(like, Like)
    assert isinstance(like.escape, BinaryOp)
    assert isinstance(like.escape.left, BoundColumnRef)
    assert like.escape.left.name == "author_name"


# --- LIKE ... ESCAPE: escape joins `sql/walk.py`'s `expr_shape_equal` Like branch --------
# --- (issue #101) ------------------------------------------------------
#
# #51 threaded `escape` through binding (above) and #51/#108's own
# `sql/walk.py`'s `contains_aggregate`/`_split_for_grouped_check` walks already compare
# it. `sql/walk.py`'s `expr_shape_equal` `Like` branch was the one left comparing
# only `negated`/`left`/`pattern` - so a `LIKE ... ESCAPE` select-list
# or HAVING expression that differs from a GROUP BY key only in its
# escape operand silently shape-matched that key. Once `escape` joins
# the comparison, both queries below no longer shape-match their
# GROUP BY key, so they fall into the pre-existing "not a GROUP BY key,
# not an aggregate" narrowing (`_check_grouped_select_list`/HAVING's own
# check) instead of returning wrong rows - not SQLite's own answer,
# which historian deliberately does not reproduce (see the module
# docstring's determinism note): confirmed live, simulating the fix
# in-process against the real binder, that both raise this exact
# `BindError` once only `sql/walk.py`'s `expr_shape_equal` `Like` branch is patched.


def test_like_escape_select_list_differing_only_in_escape_from_group_key_is_a_bind_error():
    with pytest.raises(
        BindError,
        match=r"column path must appear in the GROUP BY clause or be used in an aggregate function",
    ):
        _bind("SELECT path LIKE 'c%' ESCAPE 'c', count(*) FROM blame GROUP BY path LIKE 'c%'")


def test_like_escape_having_differing_only_in_escape_from_group_key_is_a_bind_error():
    with pytest.raises(
        BindError,
        match=r"column path must appear in the GROUP BY clause or be used in an aggregate function",
    ):
        _bind(
            "SELECT count(*) FROM blame GROUP BY path LIKE 'c%' "
            "HAVING path LIKE 'c%' ESCAPE 'c'"
        )


def test_like_escape_identical_on_both_sides_still_matches_its_group_key():
    """Regression guard: the fix must not overcorrect into treating
    every `LIKE ... ESCAPE` as unequal to itself - the identical
    expression (same escape operand on both sides) still shape-matches
    its GROUP BY key and binds without error."""
    bound = _bind(
        "SELECT path LIKE 'c%' ESCAPE 'c', count(*) FROM blame "
        "GROUP BY path LIKE 'c%' ESCAPE 'c'"
    )
    assert len(bound.group_by) == 1


# --- Depth: binding does not recurse per tree level (issue #107) -----------
#
# The parser now rejects any expression taller than SQLite's 1000, but
# the binder's walks must not depend on that - a bound tree can be about
# twice as tall as any parsed one (a select-list alias spliced into
# WHERE), and `bind()` accepts hand-built statements. These trees are
# built directly as ASTs, far past 1000 levels, and bound at the
# interpreter's default recursion limit.

_DEEP = 5000


def _deep_stmt(*, select=None, where=None, group_by=(), having=None, order_by=(), limit=None):
    from historian.sql.ast import OrderByItem

    items = select if select is not None else (SelectItem(expr=ColumnRef(None, "path", _POS), alias=None, position=_POS),)
    return SelectStatement(
        select_list=items,
        from_table="blame",
        where=where,
        group_by=group_by,
        having=having,
        order_by=tuple(OrderByItem(expr=e, direction=OrderDirection.ASC, position=_POS) for e in order_by),
        limit=limit,
        offset=None,
        position=_POS,
    )


def _deep_plus(n: int, leaf):
    node = leaf()
    for _ in range(n - 1):
        node = BinaryOp(op=Operator.ADD, left=node, right=leaf(), position=_POS)
    return node


def _chain_depth(expr) -> int:
    depth = 0
    while isinstance(expr, BinaryOp):
        expr = expr.left
        depth += 1
    return depth


def test_deep_where_chain_binds():
    from historian.sql.ast import And

    term = BinaryOp(op=Operator.EQ, left=ColumnRef(None, "path", _POS), right=Literal("x", _POS), position=_POS)
    where = term
    for _ in range(_DEEP):
        where = And(left=where, right=term, position=_POS)
    bound = bind(_deep_stmt(where=where), SCHEMAS)
    node = bound.where
    count = 0
    while isinstance(node, And):
        assert isinstance(node.right.left, BoundColumnRef)
        node = node.left
        count += 1
    assert count == _DEEP
    assert isinstance(node.left, BoundColumnRef)


def test_deep_alias_spliced_into_where_binds():
    """A select-list alias naming a deep chain, referenced from a deep
    WHERE chain: the bound WHERE is as tall as both together."""
    alias_expr = _deep_plus(_DEEP, lambda: ColumnRef(None, "line_no", _POS))
    select = (SelectItem(expr=alias_expr, alias="c", position=_POS),)
    leaves = iter([ColumnRef(None, "c", _POS)] + [Literal(1, _POS)] * _DEEP)
    where = _deep_plus(_DEEP, lambda: next(leaves))
    bound = bind(_deep_stmt(select=select, where=where), SCHEMAS)
    leftmost = bound.where
    while isinstance(leftmost, BinaryOp):
        leftmost = leftmost.left
    assert isinstance(leftmost, BoundColumnRef) and leftmost.name == "line_no"
    assert _chain_depth(bound.where) == 2 * _DEEP - 2


def test_deep_group_by_having_order_by_bind():
    """The grouped-select check (`_split_for_grouped_check`), the GROUP
    BY aggregate check (`sql/walk.py`'s `contains_aggregate`) and shape matching
    (`sql/walk.py`'s `expr_shape_equal`) over trees past 1000 levels. Shape matching
    is tried at every node against every key, quadratic in the height,
    so this uses 1500 levels rather than 5000."""
    deep = 1500
    key = _deep_plus(deep, lambda: ColumnRef(None, "line_no", _POS))
    select = (
        SelectItem(expr=_deep_plus(deep, lambda: ColumnRef(None, "line_no", _POS)), alias=None, position=_POS),
        SelectItem(expr=FunctionCall("count", (Star(None, _POS),), _POS), alias=None, position=_POS),
    )
    having = BinaryOp(
        op=Operator.GT,
        left=_deep_plus(deep, lambda: FunctionCall("count", (Star(None, _POS),), _POS)),
        right=Literal(0, _POS),
        position=_POS,
    )
    stmt = _deep_stmt(select=select, group_by=(key,), having=having, order_by=(key,))
    bound = bind(stmt, SCHEMAS)
    assert _chain_depth(bound.group_by[0]) == deep - 1
    assert _chain_depth(bound.order_by[0].expr) == deep - 1


def test_deep_bare_column_outside_the_group_key_is_still_found():
    """`_split_for_grouped_check` must still find the first bad bare
    column at the bottom of a deep tree."""
    key = ColumnRef(None, "path", _POS)
    expr = ColumnRef(None, "line_no", _POS)
    for _ in range(_DEEP):
        expr = BinaryOp(op=Operator.ADD, left=expr, right=ColumnRef(None, "path", _POS), position=_POS)
    select = (SelectItem(expr=expr, alias=None, position=_POS),)
    with pytest.raises(BindError, match="column line_no must appear in the GROUP BY clause"):
        bind(_deep_stmt(select=select, group_by=(key,)), SCHEMAS)


def test_deep_aggregate_in_where_is_still_rejected():
    call = FunctionCall("count", (Star(None, _POS),), _POS)
    where = BinaryOp(op=Operator.GT, left=_deep_plus(_DEEP, lambda: Literal(1, _POS)), right=call, position=_POS)
    where = _deep_plus(1, lambda: where)
    with pytest.raises(BindError, match="misuse of aggregate"):
        bind(_deep_stmt(where=where), SCHEMAS)


def test_deep_unknown_column_is_reported_left_to_right():
    """Errors keep their left-to-right order: the first unknown name in
    a deep chain is the one reported."""
    names = iter(["nope_first"] + ["nope_later"] * _DEEP)
    expr = _deep_plus(_DEEP, lambda: ColumnRef(None, next(names), _POS))
    with pytest.raises(BindError, match="no such column: nope_first"):
        bind(_deep_stmt(where=expr), SCHEMAS)


def test_ordinal_value_of_a_deep_unary_chain():
    expr = Literal(1, _POS)
    for _ in range(_DEEP + 1):
        expr = UnaryOp(op=UnaryOperator.NEG, operand=expr, position=_POS)
    assert _ordinal_value(expr) == -1
    expr = UnaryOp(op=UnaryOperator.POS, operand=expr, position=_POS)
    assert _ordinal_value(expr) == -1
    assert _ordinal_value(UnaryOp(op=UnaryOperator.NEG, operand=expr, position=_POS)) == 1


def test_ordinal_value_of_a_deep_unary_chain_over_a_binary_is_none():
    expr = BinaryOp(op=Operator.ADD, left=Literal(1, _POS), right=Literal(1, _POS), position=_POS)
    for _ in range(_DEEP):
        expr = UnaryOp(op=UnaryOperator.NEG, operand=expr, position=_POS)
    assert _ordinal_value(expr) is None


def test_order_by_and_limit_with_999_unary_minus_bind():
    """The review's two shapes, through the real parser: `ORDER BY` with
    999 minus signs is ordinal -1, out of range (a `BindError`); with
    998 it is ordinal 1. `LIMIT` with 998 is `LIMIT 1`."""
    with pytest.raises(BindError, match="ORDER BY term out of range"):
        _bind(f"SELECT path FROM blame ORDER BY {'- ' * 999}1")
    bound = _bind(f"SELECT path FROM blame ORDER BY {'- ' * 998}1")
    assert isinstance(bound.order_by[0].expr, BoundColumnRef)
    assert _bind(f"SELECT path FROM blame LIMIT {'- ' * 998}1").limit == 1


def test_999_operator_not_and_unary_chains_bind():
    from historian.sql.ast import Not

    bound = _bind(f"SELECT {'NOT ' * 999}line_no FROM blame")
    node = bound.select_list[0].expr
    for _ in range(999):
        assert isinstance(node, Not)
        node = node.operand
    assert isinstance(node, BoundColumnRef)
    bound = _bind(f"SELECT {'- ' * 999}line_no FROM blame")
    node = bound.select_list[0].expr
    for _ in range(999):
        assert isinstance(node, UnaryOp)
        node = node.operand
    assert isinstance(node, BoundColumnRef)


# --- M3 review mutants (issue #108) ---------------------------------------
#
# Each test below kills one mutant the M3 milestone review found
# surviving (diff `6517c67..53a9f0d`). Every query here is accepted by
# SQLite - these are historian's deliberate binder narrowings (spec
# §3), so they are unit tests and cannot be differential. Each message
# was observed at this checkout before the test was written, and each
# test was shown to fail with its mutant applied by hand, then reverted.


def test_group_by_key_with_aggregate_only_in_like_escape_is_a_bind_error():
    """A1, `sql/walk.py`'s `contains_aggregate`: the only aggregate call in the GROUP
    BY key sits in `Like.escape`. A walk that does not visit `escape`
    sees no aggregate and accepts the key."""
    with pytest.raises(
        BindError, match=r"aggregate functions are not allowed in the GROUP BY clause"
    ):
        _bind("SELECT path FROM blame GROUP BY path LIKE 'a' ESCAPE count(*)")


def test_ungrouped_aggregate_query_with_bare_column_only_in_like_escape_is_a_bind_error():
    """A1, `_split_for_grouped_check`: the only bare column in the
    select item sits in `Like.escape`. A walk that does not visit
    `escape` finds no bad column and accepts the query."""
    with pytest.raises(
        BindError,
        match=r"column author_name must appear in an aggregate function since this query has no GROUP BY",
    ):
        _bind("SELECT 'a' LIKE 'a' ESCAPE author_name, count(*) FROM blame")


def test_group_by_literal_differing_only_in_int_versus_real_type_is_a_bind_error():
    """A2: `1.0` and `1` are equal as Python values but different
    literals, so `line_no + 1.0` does not shape-match the key `line_no
    + 1`. Positive control: `test_group_by_on_an_expression` binds the
    same query with `+ 1` on both sides."""
    with pytest.raises(
        BindError,
        match=r"column line_no must appear in the GROUP BY clause or be used in an aggregate function",
    ):
        _bind("SELECT line_no + 1.0, count(*) FROM blame GROUP BY line_no + 1")


def test_group_by_is_differing_only_in_negation_is_a_bind_error():
    """A3: `line_no IS NULL` does not shape-match the key `line_no IS
    NOT NULL`."""
    with pytest.raises(
        BindError,
        match=r"column line_no must appear in the GROUP BY clause or be used in an aggregate function",
    ):
        _bind("SELECT line_no IS NULL, count(*) FROM blame GROUP BY line_no IS NOT NULL")


def test_group_by_is_with_matching_negation_binds():
    """A3's positive control: the same `IS NULL` on both sides matches
    its key and binds."""
    bound = _bind("SELECT line_no IS NULL, count(*) FROM blame GROUP BY line_no IS NULL")
    assert len(bound.group_by) == 1


def test_group_by_plain_alias_resolves_to_the_select_items_expression():
    """A4: `GROUP BY p` falls back to the select-list alias `p`, so the
    bound key is the select item's own bound expression (`path`).
    Without alias fallback it would be "no such column: p"."""
    bound = _bind("SELECT path AS p, count(*) FROM blame GROUP BY p")
    assert bound.group_by[0] == bound.select_list[0].expr
    assert isinstance(bound.group_by[0], BoundColumnRef)
    assert bound.group_by[0].name == "path"
