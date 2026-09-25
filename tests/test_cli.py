"""Tests for historian.cli: the entry point invoked as `uv run historian`.

Issue #13 (spec §6 M2 item 9), superseding the stub test this file
used to hold ("no args prints the version") - grooming marked that
test superseded, not weakened, because the CLI now has a required
`QUERY` positional argument and printing a version with no arguments
is no longer this command's behaviour at all.

Every test calls `cli.main(argv)` in-process, per this issue's own
constraint - no test here spawns a `historian` subprocess - passing a
list explicitly rather than relying on `sys.argv`. `tiny_repo` is the
pinned, session-scoped fixture from `tests/conftest.py` (#10); its
author identity for `src/utils.py` (`Ana Petrova <ana@example.com>`)
is asserted by `tests/fixtures/build.py` itself, so these tests only
assert what that module already guarantees rather than re-deriving it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from historian import cli

#: The author identity `tests/fixtures/build.py` asserts for every
#: line of `src/utils.py` at HEAD in `tiny` - the root commit, "Add
#: util and todo", authored by Ana. See that module's own
#: `_verify_tiny` for the assertion this test data mirrors.
_UTILS_PY_AUTHOR = "Ana Petrova"

#: `tiny`'s second author (`tests/fixtures/build.py`'s `_TINY_BO`).
#: Bo's two commits are a rename and a deletion - no line of content
#: anywhere in the fixture is ever authored by Bo, which is exactly
#: what makes this name useful below: a non-pushable predicate that
#: correctly matches nothing proves the residual filter as directly as
#: one that correctly matches everything.
_OTHER_TINY_AUTHOR = "Bo Lindqvist"

#: This repository's own worktree root - `tests/test_cli.py` is two
#: directories below it. Used only by the test that runs the
#: milestone's own demo query against historian's own history, per
#: this issue's acceptance criteria.
_REPO_ROOT = Path(__file__).resolve().parents[1]


# --- the demo query, end to end against a real repository -----------------


def test_demo_query_against_tiny_fixture(tiny_repo, capsys):
    """`historian -C <tiny_repo> "SELECT path, author_name FROM blame
    WHERE path = 'src/utils.py'"` exits 0 and prints exactly the blame
    rows for that file, every one attributed to Ana - the milestone's
    own demo query, actually running."""
    ret = cli.main(
        ["-C", str(tiny_repo), "SELECT path, author_name FROM blame WHERE path = 'src/utils.py'"]
    )

    assert ret == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    lines = captured.out.splitlines()
    assert lines[0].split() == ["path", "author_name"]
    assert len(lines) > 1
    for line in lines[1:]:
        assert line.startswith("src/utils.py")
        assert line.rstrip().endswith(_UTILS_PY_AUTHOR)


def test_demo_query_against_historians_own_repository(monkeypatch, capsys):
    """Run with historian's own repository as the (default) `-C`
    target - no `-C` flag at all, relying on the current working
    directory, per §5's default. §6's actual demo: at least one row
    named `src/historian/values.py` comes back."""
    monkeypatch.chdir(_REPO_ROOT)

    ret = cli.main(
        ["SELECT path, author_name FROM blame WHERE path = 'src/historian/values.py'"]
    )

    assert ret == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    lines = captured.out.splitlines()
    assert lines[0].split() == ["path", "author_name"]
    assert any(line.startswith("src/historian/values.py") for line in lines[1:])


# --- determinism and the no-isatty rule ------------------------------------


def test_same_query_twice_is_byte_identical(tiny_repo, capsys):
    """Running the same query twice produces byte-identical stdout
    both times (§3 "Determinism and row order")."""
    query = "SELECT path, author_name FROM blame WHERE path = 'src/utils.py'"

    cli.main(["-C", str(tiny_repo), query])
    first = capsys.readouterr().out

    cli.main(["-C", str(tiny_repo), query])
    second = capsys.readouterr().out

    assert first == second
    assert first != ""


# --- predicates: matching nothing is not an error, residual filter works --


def test_predicate_matching_nothing_exits_zero_with_header_only(tiny_repo, capsys):
    """A predicate matching zero rows is not an error: exit 0, the
    header line, and no data rows."""
    ret = cli.main(["-C", str(tiny_repo), "SELECT path FROM blame WHERE path = 'does/not/exist.py'"])

    assert ret == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    lines = captured.out.splitlines()
    assert lines == ["path"]


def test_predicate_on_non_pushable_column_returns_correct_rows(tiny_repo, capsys):
    """`WHERE author_name = '<a real author>'` cannot push down (spec
    §2: only `path` predicates can) - this proves the residual filter
    above the scan still returns exactly the right rows, since every
    predicate takes this path today (pushdown is M4). Checked both
    directions against `tiny`'s known authorship: every line in the
    fixture is authored by Ana (Bo's two commits are a rename and a
    deletion, never content), so filtering by Ana's name must return
    every row the unfiltered scan does, and filtering by Bo's name
    must correctly return none."""
    unfiltered = cli.main(["-C", str(tiny_repo), "SELECT path FROM blame"])
    assert unfiltered == 0
    all_rows = capsys.readouterr().out.splitlines()[1:]
    assert all_rows

    ret_ana = cli.main(
        ["-C", str(tiny_repo), f"SELECT path FROM blame WHERE author_name = '{_UTILS_PY_AUTHOR}'"]
    )
    assert ret_ana == 0
    ana_rows = capsys.readouterr().out.splitlines()[1:]
    assert ana_rows == all_rows

    ret_bo = cli.main(
        ["-C", str(tiny_repo), f"SELECT path FROM blame WHERE author_name = '{_OTHER_TINY_AUTHOR}'"]
    )
    assert ret_bo == 0
    captured = capsys.readouterr()
    assert captured.out.splitlines() == ["path"]


# --- NULL rendering ----------------------------------------------------


def test_null_renders_as_the_literal_word_null(tiny_repo, capsys):
    """`SELECT NULL FROM blame WHERE path = 'src/utils.py'` renders as
    the literal text `NULL`, not an empty string and not Python's
    `None` spelling (spec §5)."""
    ret = cli.main(["-C", str(tiny_repo), "SELECT NULL FROM blame WHERE path = 'src/utils.py'"])

    assert ret == 0
    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert len(lines) > 1
    for line in lines[1:]:
        assert line.strip() == "NULL"


# --- repository errors: exit 3 ------------------------------------------


def test_nonexistent_repo_path_exits_3(tmp_path, capsys):
    missing = tmp_path / "does-not-exist"

    ret = cli.main(["-C", str(missing), "SELECT path FROM blame"])

    assert ret == 3
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.count("\n") == 1
    assert "traceback" not in captured.err.lower()


def test_directory_that_is_not_a_git_repo_exits_3(tmp_path, capsys):
    not_a_repo = tmp_path / "just-a-directory"
    not_a_repo.mkdir()

    ret = cli.main(["-C", str(not_a_repo), "SELECT path FROM blame"])

    assert ret == 3
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.count("\n") == 1
    assert "traceback" not in captured.err.lower()


# --- query errors: exit 1, four distinct failure points --------------------


def test_syntactically_invalid_query_exits_1(tiny_repo, capsys):
    """`ParseError` path: a syntactically invalid query."""
    ret = cli.main(["-C", str(tiny_repo), "SELECT FROM blame"])

    assert ret == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("error: ")
    assert "traceback" not in captured.err.lower()


def test_unknown_column_exits_1(tiny_repo, capsys):
    """`BindError` path: a query naming an unknown column."""
    ret = cli.main(["-C", str(tiny_repo), "SELECT nope FROM blame"])

    assert ret == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("error: ")
    assert "traceback" not in captured.err.lower()


def test_unimplemented_grammar_exits_1(tiny_repo, capsys):
    """Grammar the parser already rejects outright (any `JOIN` is not
    yet implemented, per `sql/parser.py`'s own module docstring -
    `GROUP BY`/`HAVING` were this test's own example until issue #69
    built them, `ORDER BY` until issue #61 built it, `LIMIT` until
    issue #77 built it, and `DISTINCT` until issue #78 built it - see
    `tests/differential/test_blame.py`'s own `LIMIT`/`OFFSET` and
    `DISTINCT` sections for that grammar's coverage now) is a
    `ParseError`, not a traceback."""
    ret = cli.main(
        ["-C", str(tiny_repo), "SELECT path FROM blame JOIN blame ON path = path"]
    )

    assert ret == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("error: ")
    assert "traceback" not in captured.err.lower()


def test_unknown_function_name_exits_1(tiny_repo, capsys):
    """`BindError` path, issue #60: `SELECT nonexistent_fn(path) FROM
    blame` is rejected at bind time now, not the old generic `EvalError`
    this test used to pin (`test_function_call_exits_1_via_eval_error`,
    superseded - #60 closes that half of #45's gap in `sql/binder.py`
    itself, so a real query can no longer reach that path this way)."""
    ret = cli.main(["-C", str(tiny_repo), "SELECT nonexistent_fn(path) FROM blame"])

    assert ret == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("error: ")
    assert "traceback" not in captured.err.lower()


def test_aggregate_query_succeeds_via_cli(tiny_repo, capsys):
    """Issue #60, superseding the old `EvalError` pin above:
    `SELECT count(*) FROM blame` now runs end to end through the real
    CLI pipeline and prints a real answer - `tiny_repo`'s `blame` table
    has exactly 3 rows (see `tests/differential/test_blame.py`'s own
    note on this fixture)."""
    ret = cli.main(["-C", str(tiny_repo), "SELECT count(*) FROM blame"])

    assert ret == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    lines = captured.out.splitlines()
    assert len(lines) == 2
    assert lines[1] == "3"


# --- usage errors: exit 2, argparse's own handling -------------------------


def test_no_query_argument_exits_2(capsys):
    """No query argument at all exits 2 with a usage message on
    stderr, via argparse's own default handling for a missing
    required argument - no REPL starts, because none exists yet."""
    with pytest.raises(SystemExit) as excinfo:
        cli.main([])

    assert excinfo.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err != ""


@pytest.mark.parametrize(
    "flag",
    ["--explain", "--stats", "--no-pushdown", "--format", "-f", "--file"],
)
def test_deferred_flags_are_unrecognized_and_exit_2(flag, capsys):
    """None of §5's other flags are implemented yet - passing any of
    them is an unrecognized argument, not silently accepted or
    ignored."""
    with pytest.raises(SystemExit) as excinfo:
        cli.main([flag, "SELECT path FROM blame"])

    assert excinfo.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err != ""


# --- clean success path: nothing extra printed anywhere --------------------


def test_success_path_prints_nothing_but_header_and_rows(tiny_repo, capsys):
    ret = cli.main(["-C", str(tiny_repo), "SELECT path FROM blame WHERE path = 'src/utils.py'"])

    assert ret == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    lines = captured.out.splitlines()
    assert lines[0] == "path"
    assert all(line == "src/utils.py" for line in lines[1:])
    assert len(lines) > 1


# --- issue #49: the backstop around the pipeline ----------------------


class _InjectedBug(Exception):
    """A test-local exception type, never one of historian's own. Used
    to prove the backstop catches an *arbitrary* internal error rather
    than being satisfied by coincidence of a real bug (see #63, which
    fixes the two live reproductions this issue's body cites - neither
    of those exception types is used here on purpose)."""


#: The fixed diagnostic issue #49 assigns to any exception that isn't
#: one of the four query-error types and isn't a repository-read
#: failure - never `str(exc)`, the exception's class name, or a
#: traceback (spec §3 "none of them tracebacks", §5 "Never a
#: traceback").
_INTERNAL_ERROR_MESSAGE = (
    "error: historian hit an internal error and could not finish this "
    "query - this is a bug in historian, not a mistake in your SQL. "
    "Please report it, with the query that triggered it.\n"
)


def _plan_that_raises_while_materializing(monkeypatch, exc_type):
    """Monkeypatch `historian.cli.plan` (the name as imported into
    `cli.py`) with a wrapper that builds a genuine operator tree via
    the real `plan()` - correct schema, correct everything - and then
    replaces that tree's bound `rows` method with a function raising
    *exc_type*. This is the injection recipe the groomed issue body
    specifies: it doesn't depend on any real internal bug existing, so
    it keeps working regardless of what #63 does to the two live
    reproductions."""
    real_plan = cli.plan

    def _wrapped_plan(bound, repo):
        tree = real_plan(bound, repo)

        def _boom() -> object:
            raise exc_type("injected-bug-marker: should never reach the user")

        tree.rows = _boom
        return tree

    monkeypatch.setattr(cli, "plan", _wrapped_plan)


def test_internal_error_during_materialization_exits_4_with_fixed_message(
    tiny_repo, capsys, monkeypatch
):
    """An arbitrary exception raised while pulling rows from the
    operator tree - not one of the four query-error types, not an
    `OSError`/`RuntimeError` repository failure - is caught by the new
    backstop clause: exit 4, nothing on stdout, exactly the fixed
    diagnostic on stderr, with no trace of the injected exception's own
    message or class name."""
    _plan_that_raises_while_materializing(monkeypatch, _InjectedBug)

    ret = cli.main(["-C", str(tiny_repo), "SELECT path FROM blame"])

    assert ret == 4
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == _INTERNAL_ERROR_MESSAGE
    assert "injected-bug-marker" not in captured.out
    assert "injected-bug-marker" not in captured.err
    assert "_InjectedBug" not in captured.err
    assert "Traceback" not in captured.err


def test_internal_error_during_rendering_exits_4(tiny_repo, capsys, monkeypatch):
    """The guarded region is widened to cover rendering and writing the
    result, not just lex/parse/bind/plan/materialize - an exception
    raised from `_render_table` (before this issue, entirely outside
    the `try`) is caught by the same backstop, not left to traceback."""

    def _raise(schema, rows):
        raise _InjectedBug("injected-bug-marker: should never reach the user")

    monkeypatch.setattr(cli, "_render_table", _raise)

    ret = cli.main(["-C", str(tiny_repo), "SELECT path FROM blame WHERE path = 'src/utils.py'"])

    assert ret == 4
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == _INTERNAL_ERROR_MESSAGE
    assert "injected-bug-marker" not in captured.err


def test_keyboard_interrupt_during_materialization_propagates(tiny_repo, capsys, monkeypatch):
    """`KeyboardInterrupt` is a `BaseException`, not an `Exception`, so
    the new `except Exception` backstop must not swallow it - it
    propagates out of `main` uncaught, exactly like a query error would
    not."""
    _plan_that_raises_while_materializing(monkeypatch, KeyboardInterrupt)

    with pytest.raises(KeyboardInterrupt):
        cli.main(["-C", str(tiny_repo), "SELECT path FROM blame"])


def test_broken_pipe_while_writing_exits_0_and_prints_nothing(tiny_repo, capsys, monkeypatch):
    """`BrokenPipeError` (stdout closed, e.g. piped to `head`) raised
    while rendering/writing the result is an `OSError` subclass, so
    without its own clause it would be silently misreported as exit 3
    by the existing "could not read repository" handling. It gets its
    own clause instead, ordered before both the repository-read clause
    and the new internal-error backstop: `main` returns 0 and nothing
    further reaches stdout or stderr."""

    def _raise(schema, rows):
        raise BrokenPipeError()

    monkeypatch.setattr(cli, "_render_table", _raise)

    ret = cli.main(["-C", str(tiny_repo), "SELECT path FROM blame WHERE path = 'src/utils.py'"])

    assert ret == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
