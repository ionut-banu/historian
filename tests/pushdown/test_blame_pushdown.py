"""Work-done tests for `blame`'s `path` pushdown (issue #122, spec §4
"The pushdown layer").

A pushdown that returns the right rows while blaming every file is
invisible to the differential suite, so every case here asserts on the
scan's own record - `BlameScan.blamed_paths` (which paths `git blame`
was run on, in order) and `BlameScan.git_invocations` (every `git`
process started, `ls-tree` included) - and, alongside it, that the
rows still match SQLite loaded from an *unfiltered* scan.

Each query runs through the real pipeline (`tokenize -> parse -> bind
-> plan -> optimize -> rows()`), with a `blame` factory that keeps the
`BlameScan` it built so the record can be read afterwards.

Expected rows were confirmed with `tests/oracle.py` (the oracle);
the one-line setup/query for each is on the issue.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from historian.catalog import SCHEMAS
from historian.exec.operators import Scan, child_of
from historian.plan.optimizer import optimize
from historian.plan.planner import plan
from historian.sql.binder import bind
from historian.sql.lexer import tokenize
from historian.sql.parser import parse
from historian.tables.blame import BLAME_SCHEMA, BlameScan

from differential.conftest import assert_rows_match, load_unfiltered
from fixtures.build import CASEFOLD_PATHS

# `git ls-tree` order (bytewise), which is `feature/` before `src/`.
TINY_PATHS = ["feature/thing.py", "src/utils.py"]


def _run(query: str, repo: Path):
    """Run *query* through the whole pipeline against *repo*; return
    (rows, the `Scan` operator, the `BlameScan` it drove)."""
    built: list[BlameScan] = []

    def factory(r: Path) -> BlameScan:
        source = BlameScan(r)
        built.append(source)
        return source

    tree = optimize(plan(bind(parse(tokenize(query)), catalog=SCHEMAS), repo, tables={"blame": factory}))
    rows = list(tree.rows())
    node = tree
    while not isinstance(node, Scan):
        node = child_of(node)
    assert len(built) == 1
    return rows, node, built[0]


def _sqlite_rows(query: str, repo: Path):
    conn = load_unfiltered(BlameScan, repo, BLAME_SCHEMA, "blame")
    try:
        return conn.execute(query).fetchall()
    finally:
        conn.close()


def _check(query: str, repo: Path, *, blamed: list[str], invocations: int, pushed: int):
    """Assert the work record, the number of pushed terms, and that
    the rows match SQLite's. Returns the rows for further checks."""
    rows, scan_op, source = _run(query, repo)
    assert source.blamed_paths == blamed
    assert source.git_invocations == invocations
    assert len(scan_op.pushed()) == pushed
    assert_rows_match(_sqlite_rows(query, repo), rows)
    return rows


def _where(query: str):
    """The bound `WHERE` expression of *query* - one term to offer
    `accepts()` directly."""
    return bind(parse(tokenize(query)), catalog=SCHEMAS).where


# --- The capability declaration ---------------------------------------


def test_capabilities_name_path_pushdown(tmp_path):
    caps = BlameScan(tmp_path).capabilities()
    assert caps
    assert all("path" in kind for kind in caps)


def test_record_starts_empty_before_any_scan(tmp_path):
    source = BlameScan(tmp_path)
    assert source.blamed_paths == []
    assert source.git_invocations == 0


# --- `path = 'literal'` -------------------------------------------------


def test_eq_blames_exactly_that_path(tiny_repo):
    rows = _check(
        "SELECT path, line_no FROM blame WHERE path = 'src/utils.py'",
        tiny_repo,
        blamed=["src/utils.py"],
        invocations=2,
        pushed=1,
    )
    assert rows and {row[0] for row in rows} == {"src/utils.py"}


def test_eq_with_literal_on_the_left_is_identical(tiny_repo):
    rows = _check(
        "SELECT path, line_no FROM blame WHERE 'src/utils.py' = path",
        tiny_repo,
        blamed=["src/utils.py"],
        invocations=2,
        pushed=1,
    )
    assert rows and {row[0] for row in rows} == {"src/utils.py"}


def test_table_qualified_path_is_the_same_column(tiny_repo):
    _check(
        "SELECT path FROM blame WHERE blame.path = 'src/utils.py'",
        tiny_repo,
        blamed=["src/utils.py"],
        invocations=2,
        pushed=1,
    )


def test_eq_on_a_path_that_does_not_exist_never_runs_git_blame(tiny_repo):
    rows = _check(
        "SELECT path FROM blame WHERE path = 'does/not/exist.py'",
        tiny_repo,
        blamed=[],
        invocations=1,
        pushed=1,
    )
    assert rows == []


# --- `path LIKE 'prefix%'` ----------------------------------------------


def test_like_prefix_blames_only_paths_under_it(tiny_repo):
    rows = _check(
        "SELECT path FROM blame WHERE path LIKE 'src/%'",
        tiny_repo,
        blamed=["src/utils.py"],
        invocations=2,
        pushed=1,
    )
    assert rows


def test_like_prefix_in_other_case_still_blames_the_match(tiny_repo):
    """SQLite's `LIKE` folds ASCII case: `'src/utils.py' LIKE 'SRC/%'`
    is true, so the scan must not narrow case-sensitively."""
    rows = _check(
        "SELECT path FROM blame WHERE path LIKE 'SRC/%'",
        tiny_repo,
        blamed=["src/utils.py"],
        invocations=2,
        pushed=1,
    )
    assert rows and {row[0] for row in rows} == {"src/utils.py"}


@pytest.mark.parametrize("prefix", ["src/", "SRC/", "Src/", "sRc/"])
def test_like_prefix_folds_ascii_case_across_every_spelling(casefold_repo, prefix):
    """Oracle: `src/a.py`, `SRC/b.py`, `Src/C.py` all match any ASCII
    spelling of `src/%`; `other/a.py` does not. Blamed in `ls-tree`
    order."""
    rows = _check(
        f"SELECT path FROM blame WHERE path LIKE '{prefix}%'",
        casefold_repo,
        blamed=["SRC/b.py", "Src/C.py", "src/a.py"],
        invocations=4,
        pushed=1,
    )
    assert sorted(row[0] for row in rows) == ["SRC/b.py", "Src/C.py", "src/a.py"]


def test_like_prefix_does_not_fold_non_ascii_case(casefold_repo):
    """Oracle: `path LIKE 'straße/%'` matches `straße/a.py` and
    `STRAßE/a.py` (ASCII letters fold, `ß` is left alone) and never
    `STRASSE/a.py`."""
    rows = _check(
        "SELECT path FROM blame WHERE path LIKE 'straße/%'",
        casefold_repo,
        blamed=["STRAßE/a.py", "straße/a.py"],
        invocations=3,
        pushed=1,
    )
    assert sorted(row[0] for row in rows) == ["STRAßE/a.py", "straße/a.py"]


def test_like_prefix_on_a_non_ascii_path(awkward_repo):
    _check(
        "SELECT path FROM blame WHERE path LIKE 'café%'",
        awkward_repo,
        blamed=["café.py"],
        invocations=2,
        pushed=1,
    )


def test_like_prefix_does_not_fold_a_non_ascii_letter_against_its_uppercase(awkward_repo):
    """Oracle: `'café.py' LIKE 'CAFÉ%'` is false - `é`/`É` are not
    folded - so nothing is blamed and nothing is returned."""
    rows = _check(
        "SELECT path FROM blame WHERE path LIKE 'CAFÉ%'",
        awkward_repo,
        blamed=[],
        invocations=1,
        pushed=1,
    )
    assert rows == []


def test_like_bare_percent_is_a_prefix_of_everything(tiny_repo):
    _check(
        "SELECT path FROM blame WHERE path LIKE '%'",
        tiny_repo,
        blamed=TINY_PATHS,
        invocations=3,
        pushed=1,
    )


# --- `path IN (...)` ----------------------------------------------------


def test_in_blames_the_tracked_literals_in_list_order(awkward_repo):
    rows = _check(
        "SELECT path, line_no FROM blame WHERE path IN ('café.py', 'empty.txt', 'nonexistent.txt')",
        awkward_repo,
        blamed=["café.py", "empty.txt"],
        invocations=3,
        pushed=1,
    )
    assert rows and {row[0] for row in rows} == {"café.py"}  # empty.txt has no lines


def test_in_order_follows_the_list_not_ls_tree(tiny_repo):
    rows = _check(
        "SELECT path FROM blame WHERE path IN ('feature/thing.py', 'src/utils.py')",
        tiny_repo,
        blamed=["feature/thing.py", "src/utils.py"],
        invocations=3,
        pushed=1,
    )
    assert rows[0][0] == "feature/thing.py"


def test_empty_in_list_blames_nothing(tiny_repo):
    rows = _check(
        "SELECT path FROM blame WHERE path IN ()",
        tiny_repo,
        blamed=[],
        invocations=1,
        pushed=1,
    )
    assert rows == []


def test_duplicate_in_literal_is_blamed_once(tiny_repo):
    _check(
        "SELECT path FROM blame WHERE path IN ('src/utils.py', 'src/utils.py')",
        tiny_repo,
        blamed=["src/utils.py"],
        invocations=2,
        pushed=1,
    )


def test_in_is_exact_not_case_folded(casefold_repo):
    """`IN` is `=`, and `=` on TEXT is binary: `SRC/b.py` is not
    selected by `'src/b.py'`."""
    rows = _check(
        "SELECT path FROM blame WHERE path IN ('src/a.py', 'src/b.py')",
        casefold_repo,
        blamed=["src/a.py"],
        invocations=2,
        pushed=1,
    )
    assert [row[0] for row in rows] == ["src/a.py"]


# --- Several pushed terms narrow together -----------------------------


def test_two_pushed_terms_intersect(casefold_repo):
    """A two-element `IN`, not `path = 'SRC/b.py'`: since #142 a `path =`
    source makes the `LIKE` beside it a constant, which is not pushed
    (`test_a_like_beside_a_path_source_is_not_pushed`)."""
    _check(
        "SELECT path FROM blame WHERE path LIKE 'src/%' AND path IN ('SRC/b.py', 'nope.py')",
        casefold_repo,
        blamed=["SRC/b.py"],
        invocations=2,
        pushed=2,
    )


def test_in_then_like_keeps_the_in_order_and_intersects(casefold_repo):
    _check(
        "SELECT path FROM blame WHERE path IN ('other/a.py', 'src/a.py', 'Src/C.py') AND path LIKE 'SRC/%'",
        casefold_repo,
        blamed=["src/a.py", "Src/C.py"],
        invocations=3,
        pushed=2,
    )


def test_pushed_path_term_beside_an_unpushable_one(tiny_repo):
    _check(
        "SELECT path FROM blame WHERE line_no = 1 AND path = 'src/utils.py'",
        tiny_repo,
        blamed=["src/utils.py"],
        invocations=2,
        pushed=1,
    )


def test_pushed_path_beside_a_numeric_line_no_filter(numeric_repo):
    """#109: `path` narrows the scan to `long.txt` alone, and `line_no >
    99` - which only holds numerically for 21 of its lines - stays in
    the Filter above it."""
    rows = _check(
        "SELECT path, line_no FROM blame WHERE path = 'long.txt' AND line_no > 99",
        numeric_repo,
        blamed=["long.txt"],
        invocations=2,
        pushed=1,
    )
    assert sorted(rows) == [("long.txt", n) for n in range(100, 121)]


def test_or_of_path_and_numeric_line_no_blames_every_file(numeric_repo):
    """#109: an `OR` cannot push down, so all three files are blamed;
    12 rows from `mid.txt` and 21 from `long.txt`."""
    rows = _check(
        "SELECT path, line_no FROM blame WHERE path = 'mid.txt' OR line_no > 99",
        numeric_repo,
        blamed=["long.txt", "mid.txt", "short.txt"],
        invocations=4,
        pushed=0,
    )
    assert len(rows) == 33


def test_three_term_and_pushes_only_path(tiny_repo):
    _check(
        "SELECT path, line_no FROM blame WHERE path = 'src/utils.py' AND line_no >= 1",
        tiny_repo,
        blamed=["src/utils.py"],
        invocations=2,
        pushed=1,
    )


# --- Nothing pushed: blamed everything, correctly ----------------------


def test_author_name_cannot_push_down(tiny_repo):
    rows = _check(
        "SELECT path, line_no FROM blame WHERE author_name = 'Ana Petrova'",
        tiny_repo,
        blamed=TINY_PATHS,
        invocations=3,
        pushed=0,
    )
    assert rows


def test_no_where_blames_everything(tiny_repo):
    _check("SELECT path FROM blame", tiny_repo, blamed=TINY_PATHS, invocations=3, pushed=0)


def test_path_eq_integer_literal_is_left_to_the_filter(casefold_repo):
    """Oracle: TEXT affinity turns `5` into `'5'`, so `path = 5`
    selects the path spelled `5`. The scan does not carry its own
    copy of affinity conversion, so nothing is pushed, every path is
    blamed, and the `Filter` still returns that row."""
    rows = _check(
        "SELECT path FROM blame WHERE path = 5",
        casefold_repo,
        blamed=list(CASEFOLD_PATHS),
        invocations=1 + len(CASEFOLD_PATHS),
        pushed=0,
    )
    assert rows == [("5",)]


def test_path_eq_null_is_left_to_the_filter(tiny_repo):
    rows = _check(
        "SELECT path FROM blame WHERE path = NULL",
        tiny_repo,
        blamed=TINY_PATHS,
        invocations=3,
        pushed=0,
    )
    assert rows == []


def test_like_with_escape_is_not_pushed(tiny_repo):
    """Oracle: in `'src\\%foo' ESCAPE '\\'` the `%` is literal, so a
    reading that ignored `ESCAPE` would treat this as prefix `src`."""
    _check(
        "SELECT path FROM blame WHERE path LIKE 'src\\%foo' ESCAPE '\\'",
        tiny_repo,
        blamed=TINY_PATHS,
        invocations=3,
        pushed=0,
    )


@pytest.mark.parametrize(
    "where",
    [
        "path LIKE '%src/%'",
        "path LIKE 'src_/%'",
        "path NOT LIKE 'src/%'",
        "path NOT IN ('src/utils.py')",
        "path != 'src/utils.py'",
        "path > 'a'",
        "path IN ('src/utils.py', NULL)",
        "path = 'src/utils.py' OR path = 'feature/thing.py'",
    ],
)
def test_rejected_shapes_blame_everything_and_stay_correct(tiny_repo, where):
    _check(
        f"SELECT path, line_no FROM blame WHERE {where}",
        tiny_repo,
        blamed=TINY_PATHS,
        invocations=3,
        pushed=0,
    )


# --- `accepts()` from shape alone ----------------------------------------


@pytest.mark.parametrize(
    "where",
    [
        "path = 'src/utils.py'",
        "'src/utils.py' = path",
        "blame.path = 'src/utils.py'",
        "path = ''",
        "path IN ('a', 'b')",
        "path IN ()",
        "path LIKE 'src/%'",
        "path LIKE '%'",
        "path LIKE 'straße/%'",
    ],
)
def test_accepts_the_listed_shapes(tmp_path, where):
    assert BlameScan(tmp_path).accepts(_where(f"SELECT path FROM blame WHERE {where}")) is True


@pytest.mark.parametrize(
    "where",
    [
        # column to column
        "path = author_name",
        "author_name = path",
        # not the path column
        "author_name = 'Ana Petrova'",
        "line_no = 1",
        "commit_hash IN ('a')",
        "line LIKE 'x%'",
        # non-text or NULL literal
        "path = 5",
        "path = 5.0",
        "path = NULL",
        "NULL = path",
        "path IN ('a', NULL)",
        "path IN ('a', 5)",
        "path IN ('a', author_name)",
        "path IN ('a', 'b' || 'c')",
        # other operators
        "path != 'a'",
        "path <> 'a'",
        "path < 'a'",
        "path <= 'a'",
        "path > 'a'",
        "path >= 'a'",
        "path || '' = 'a'",
        # negations and other nodes
        "path NOT IN ('a')",
        "path NOT LIKE 'a%'",
        "NOT path = 'a'",
        "path IS NULL",
        "path IS NOT NULL",
        "path IS 'a'",
        "path BETWEEN 'a' AND 'b'",
        "path = 'a' OR path = 'b'",
        # LIKE shapes outside the one accepted prefix form
        "path LIKE 'src\\%foo' ESCAPE '\\'",
        "path LIKE 'src/%' ESCAPE '!'",
        "path LIKE '%src/%'",
        "path LIKE 'src_/%'",
        "path LIKE 'src/'",
        "path LIKE ''",
        "path LIKE 'src/%%'",
        "path LIKE 'src/%x'",
        "path LIKE '_'",
        "path LIKE 5",
        "path LIKE NULL",
        "path LIKE author_name",
        "'src/utils.py' LIKE path",
        "'src/%' LIKE path",
    ],
)
def test_accepts_rejects_every_other_shape(tmp_path, where):
    assert BlameScan(tmp_path).accepts(_where(f"SELECT path FROM blame WHERE {where}")) is False


# --- The record belongs to one scan() call ------------------------------


def test_record_is_reset_by_each_scan_call(tiny_repo):
    source = BlameScan(tiny_repo)
    term = _where("SELECT path FROM blame WHERE path = 'src/utils.py'")

    list(source.scan(pushed=[term]))
    assert source.blamed_paths == ["src/utils.py"]
    assert source.git_invocations == 2

    list(source.scan(pushed=()))
    assert source.blamed_paths == TINY_PATHS
    assert source.git_invocations == 3


def test_record_is_per_instance_not_shared(tiny_repo):
    a = BlameScan(tiny_repo)
    b = BlameScan(tiny_repo)
    list(a.scan(pushed=[_where("SELECT path FROM blame WHERE path IN ()")]))
    list(b.scan(pushed=()))
    assert a.blamed_paths == [] and a.git_invocations == 1
    assert b.blamed_paths == TINY_PATHS and b.git_invocations == 3


def test_scan_ignores_a_term_it_would_not_accept(tiny_repo):
    """A term `accepts()` rejects never narrows, even if a caller hands
    it to `scan()` anyway - ignoring it keeps the output a superset."""
    source = BlameScan(tiny_repo)
    rows = list(source.scan(pushed=[_where("SELECT path FROM blame WHERE path != 'src/utils.py'")]))
    assert source.blamed_paths == TINY_PATHS
    assert {row[0] for row in rows} == set(TINY_PATHS)


# --- estimate(): how many paths scan() would blame, without blaming (#42) ----


def test_estimate_counts_paths_without_blaming(tiny_repo):
    from historian.sql.ast import Like, Literal
    from historian.sql.binder import BoundColumnRef
    from historian.sql.lexer import Position

    pos = Position(line=1, column=1, offset=0)
    term = Like(
        left=BoundColumnRef(offset=0, name="path", position=pos),
        pattern=Literal("src/%", pos),
        negated=False,
        position=pos,
    )
    scan = BlameScan(tiny_repo)
    estimate = scan.estimate([term])
    assert (estimate.name, estimate.selected, estimate.total) == ("BlameScan", 1, 2)
    assert scan.blamed_paths == []
    assert scan.git_invocations == 1
    assert scan.tracked_path_count == 2
    none_pushed = scan.estimate()
    assert (none_pushed.selected, none_pushed.total) == (2, 2)
    assert scan.git_invocations == 1


# --- HAVING terms that move below the aggregate (#141) ---------------------


def test_a_moved_having_term_is_not_pushed(tiny_repo):
    """`path = 'src/utils.py'` in `HAVING` moves below the aggregate but
    is never offered to the scan (#172): every tracked path is blamed,
    with the same three git invocations (`ls-tree` and two `blame`s) as
    before #141. Oracle: `2`."""
    rows = _check(
        "SELECT count(*) FROM blame GROUP BY path HAVING path = 'src/utils.py'",
        tiny_repo,
        blamed=TINY_PATHS,
        invocations=3,
        pushed=0,
    )
    assert rows == [(2,)]


def test_where_is_still_pushed_beside_a_moved_term(tiny_repo):
    """The `WHERE` term is pushed and only `src/utils.py` is blamed; the
    moved `ERR` then raises on its rows, as SQLite does."""
    from historian.exec.expression import EvalError

    built: list[BlameScan] = []

    def factory(r: Path) -> BlameScan:
        source = BlameScan(r)
        built.append(source)
        return source

    query = (
        "SELECT count(*) FROM blame WHERE path = 'src/utils.py' GROUP BY path "
        "HAVING path LIKE 'a' ESCAPE 'ab' AND count(*) > 0"
    )
    tree = optimize(plan(bind(parse(tokenize(query)), catalog=SCHEMAS), tiny_repo, tables={"blame": factory}))
    node = tree
    while not isinstance(node, Scan):
        node = child_of(node)
    assert len(node.pushed()) == 1
    with pytest.raises(EvalError, match="ESCAPE expression must be a single character"):
        list(tree.rows())
    assert built[0].blamed_paths == ["src/utils.py"]
    assert built[0].git_invocations == 2


# --- Constant propagation in WHERE (#142) ------------------------------------
#
# The terms negotiated with the scan are the terms as rewritten: a
# `path` term beside a `path = constant` source has the constant in
# `path`'s place and is not one the scan accepts. Before #142 each
# `path` term below was pushed; the numbers on `main` are in each
# docstring.


def test_a_like_beside_a_path_source_is_not_pushed(tiny_repo):
    """`path LIKE 'src/%'` becomes `'src/utils.py' LIKE 'src/%'`; only
    the source is pushed. The same one path is blamed, in the same two
    git invocations, as on `main` (where both terms were pushed).
    Oracle: `2`."""
    rows = _check(
        "SELECT count(*) FROM blame WHERE path = 'src/utils.py' AND path LIKE 'src/%'",
        tiny_repo,
        blamed=["src/utils.py"],
        invocations=2,
        pushed=1,
    )
    assert rows == [(2,)]


def test_of_two_path_sources_the_last_is_pushed(tiny_repo):
    """SQLite uses the last of two sources for one column and rewrites
    the first into `'src/utils.py' = 'feature/thing.py'`, so only
    `path = 'src/utils.py'` is pushed. That rewritten term has no column
    and is `FALSE`, so since #171 it is decided before the scan is read
    and nothing is blamed (#142 alone blamed `src/utils.py` in two git
    invocations; before #142 both were pushed and one ran). Oracle:
    `0`."""
    rows = _check(
        "SELECT count(*) FROM blame WHERE path = 'feature/thing.py' AND path = 'src/utils.py'",
        tiny_repo,
        blamed=[],
        invocations=0,
        pushed=1,
    )
    assert rows == [(0,)]


def test_an_in_list_beside_a_path_source_is_not_pushed(tiny_repo):
    """`path IN ('a', 'b')` becomes `'src/utils.py' IN ('a', 'b')`,
    which is not pushed. It has no column and is `FALSE`, so since #171
    the scan is never read (#142 alone blamed `src/utils.py`). Oracle:
    `0`."""
    rows = _check(
        "SELECT count(*) FROM blame WHERE path = 'src/utils.py' AND path IN ('a', 'b')",
        tiny_repo,
        blamed=[],
        invocations=0,
        pushed=1,
    )
    assert rows == [(0,)]


def test_a_line_no_source_leaves_path_pushdown_alone(tiny_repo):
    """`line_no` is never pushed and `path` has no source, so `path =
    'src/utils.py'` is pushed as before."""
    _check(
        "SELECT count(*) FROM blame WHERE path = 'src/utils.py' AND line_no = 1 AND line_no >= 1",
        tiny_repo,
        blamed=["src/utils.py"],
        invocations=2,
        pushed=1,
    )



# --- A constant WHERE term decided before the scan is read (#171) -------
#
# The `ConstantGuard` evaluates the column-free terms on the first pull,
# before it pulls from the `Filter` and so before the scan starts: a
# false or NULL constant makes the scan do no work at all - not even
# `git ls-tree` - exactly as `LIMIT 0` does.

CONSTERR = "'a' LIKE 'a' ESCAPE 'ab'"


@pytest.mark.parametrize("constant", ["1=0", "NULL", "NULL = NULL", "'a' LIKE 'b'"])
def test_a_false_constant_blames_nothing_and_runs_no_git(tiny_repo, constant):
    rows = _check(
        f"SELECT count(*) FROM blame WHERE {constant}", tiny_repo, blamed=[], invocations=0, pushed=0
    )
    assert rows == [(0,)]


def test_a_false_constant_beside_a_pushed_path_term_blames_nothing(tiny_repo):
    """Before #171 this blamed `src/utils.py`: the pushed term still
    narrowed the scan, but the scan ran."""
    rows = _check(
        "SELECT path FROM blame WHERE path = 'src/utils.py' AND 1=0", tiny_repo, blamed=[], invocations=0, pushed=1
    )
    assert rows == []


def test_a_true_constant_leaves_the_work_as_it_was(tiny_repo):
    rows = _check(
        "SELECT path FROM blame WHERE 1=1 AND path = 'src/utils.py'",
        tiny_repo,
        blamed=["src/utils.py"],
        invocations=2,
        pushed=1,
    )
    assert len(rows) == 2


def test_a_raising_constant_runs_no_git_before_it_raises(tiny_repo):
    from historian.exec.expression import EvalError

    built: list[BlameScan] = []

    def factory(r: Path) -> BlameScan:
        built.append(BlameScan(r))
        return built[-1]

    query = f"SELECT path FROM blame WHERE path = 'src/utils.py' AND {CONSTERR}"
    tree = optimize(plan(bind(parse(tokenize(query)), catalog=SCHEMAS), tiny_repo, tables={"blame": factory}))
    with pytest.raises(EvalError):
        list(tree.rows())
    assert built[0].git_invocations == 0
    assert built[0].blamed_paths == []


def _run_no_pushdown(query: str, repo: Path):
    built: list[BlameScan] = []

    def factory(r: Path) -> BlameScan:
        built.append(BlameScan(r))
        return built[-1]

    tree = plan(bind(parse(tokenize(query)), catalog=SCHEMAS), repo, tables={"blame": factory})
    return list(tree.rows()), built[0]


def test_no_pushdown_blames_every_path_only_when_the_guard_passes(tiny_repo):
    rows, source = _run_no_pushdown("SELECT path FROM blame WHERE 1=1 AND path = 'src/utils.py'", tiny_repo)
    assert len(rows) == 2
    assert source.blamed_paths == TINY_PATHS
    rows, source = _run_no_pushdown("SELECT path FROM blame WHERE path = 'src/utils.py' AND 1=0", tiny_repo)
    assert rows == []
    assert source.blamed_paths == []
    assert source.git_invocations == 0
