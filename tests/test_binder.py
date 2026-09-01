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
`historian.sql.binder.TABLES` (`{"blame": BLAME_SCHEMA}`) - the same
catalog `bind()` defaults to. A few tests instead bind a hand-built
`SelectStatement` against a synthetic single-column schema (for the
ASCII-folding case, where blame has no non-ASCII column) or a
hand-built AST bypassing the parser entirely (for the defensive
`Star`-in-a-bad-position backstop, since real SQL cannot construct
that shape once #31 is fixed - see the issue's own grooming notes).
"""

import dataclasses

import pytest

from historian.schema import Column, ColumnType, Schema
from historian.sql.ast import (
    ColumnRef,
    FunctionCall,
    Literal,
    SelectItem,
    SelectStatement,
    Star,
)
from historian.sql.binder import (
    TABLES,
    BindError,
    BoundColumnRef,
    BoundSelectStatement,
    bind,
)
from historian.sql.lexer import Position, tokenize
from historian.sql.parser import parse
from historian.tables.blame import BLAME_SCHEMA

_POS = Position(line=1, column=1, offset=0)

#: `blame`'s declared column order, per spec §2 - used throughout to
#: assert `*` expansion and "no such column" `available` data.
_BLAME_COLUMNS = ("path", "line_no", "line", "commit_hash", "author_name", "author_email", "authored_at")


def _bind(sql: str) -> BoundSelectStatement:
    return bind(parse(tokenize(sql)))


# --- The catalog -------------------------------------------------------------


def test_tables_catalog_is_exactly_blame():
    """`sql/binder.py` exposes `TABLES: dict[str, Schema]` seeded with
    exactly `{"blame": BLAME_SCHEMA}`, importing rather than
    redefining the schema - per the issue's own coordination note with
    #11's grooming."""
    assert TABLES == {"blame": BLAME_SCHEMA}
    assert TABLES["blame"] is BLAME_SCHEMA


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
    with pytest.raises(BindError) as exc_info:
        bind(stmt)
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
    with pytest.raises(BindError) as exc_info:
        bind(stmt)
    assert exc_info.value.position == ref.position


def test_no_such_column_available_is_blame_columns_in_declared_order():
    """Matches spec §5's worked example for this exact query: `blame
    has: path, line_no, line, commit_hash, author_name, author_email,
    authored_at`."""
    with pytest.raises(BindError) as exc_info:
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


# --- Aliases: no cross-item namespace, no WHERE fallback (deferred) ----------


def test_alias_not_visible_to_next_select_item():
    """`sqlite3`: `select path as p, p as p2 from blame;` errors "no
    such column: p" on the second item - SQLite evaluates every
    select-list expression against FROM alone, none see each other's
    aliases."""
    with pytest.raises(BindError) as exc_info:
        _bind("SELECT path AS p, p AS p2 FROM blame")
    assert str(exc_info.value) == "no such column: p"


def test_where_referencing_select_list_alias_conservatively_rejected():
    """Real SQLite behaviour (`select path as p, line_no from blame
    where p = 'a.py'` succeeds) that this issue deliberately does not
    implement - see #32. Until that lands, `WHERE p` raises "no such
    column: p", the safe direction: a query SQLite accepts is wrongly
    rejected, never a silently wrong row. Pinned here so the gap is
    deliberate rather than an untested accident."""
    with pytest.raises(BindError) as exc_info:
        _bind("SELECT path AS p FROM blame WHERE p = 'x'")
    assert str(exc_info.value) == "no such column: p"


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
        position=_POS,
    )
    with pytest.raises(BindError):
        bind(stmt)


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
        position=_POS,
    )
    with pytest.raises(BindError):
        bind(stmt)


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
        position=_POS,
    )
    with pytest.raises(BindError):
        bind(stmt)


def test_star_as_non_sole_function_argument_raises_defensive_error():
    """`count(*, path)` -> `near ",": syntax error` in sqlite3, already
    rejected by historian's parser too. Checked directly against a
    hand-built tree: a `Star` alongside another argument is not "the
    sole argument" and is rejected rather than silently expanded or
    passed through."""
    call = FunctionCall(
        name="count",
        args=(Star(table=None, position=_POS), ColumnRef(table=None, name="path", position=_POS)),
        position=_POS,
    )
    stmt = SelectStatement(
        select_list=(SelectItem(expr=call, alias=None, position=_POS),),
        from_table="blame",
        where=None,
        position=_POS,
    )
    with pytest.raises(BindError):
        bind(stmt)


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


def test_function_name_and_arity_are_not_validated():
    """`SELECT nonexistent_fn(path) FROM blame` binds successfully -
    the `path` reference inside resolves normally, and whether
    `nonexistent_fn` is real is left to a future function registry."""
    bound = _bind("SELECT nonexistent_fn(path) FROM blame")
    call = bound.select_list[0].expr
    assert isinstance(call, FunctionCall)
    assert call.name == "nonexistent_fn"
    arg = call.args[0]
    assert isinstance(arg, BoundColumnRef)
    assert arg.name == "path"


# --- No git, no subprocess needed to exercise this module --------------------


def test_binder_module_does_not_import_subprocess_directly():
    """`bind()` and everything it calls are tested entirely against
    in-memory `Schema`/`SelectStatement` values above, with no
    repository present - per `AGENTS.md`'s "only scan operators touch
    git". This module does not itself write `import subprocess` (it
    only transitively imports a module that does, via `BLAME_SCHEMA` -
    see the module docstring's own note on that trade-off), so
    `subprocess` never appears as a name bound directly in its own
    namespace."""
    import historian.sql.binder as binder_module

    assert "subprocess" not in vars(binder_module)
