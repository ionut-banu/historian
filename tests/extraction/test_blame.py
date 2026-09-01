"""Extraction tests for the `blame` scan (`_docs/spec.md` §4, §2).

The extraction layer proves the git *data* is right, not that the SQL
is - both a future differential test's historian side and its SQLite
side would read the same extracted rows, so a wrong `authored_at`
would be wrong on both and invisible there (spec §4). So `blame` rows
are checked here against `git blame --line-porcelain` itself, on the
`tiny` and `awkward` fixtures (`tests/conftest.py`, built by
`tests/fixtures/build.py`).

Independent oracle, on purpose
-------------------------------

`_oracle_blame` below parses `git blame --line-porcelain` output using
a *different* algorithm from `historian.tables.blame`'s own parser: it
positively matches a commit-info header line with a regular expression
and accumulates header fields into a dict, rather than the production
parser's linear state machine that ends a record at the first
tab-prefixed line it meets. It calls nothing from
`historian.tables.blame` except the `BLAME_SCHEMA` column order (a
schema, not parsing logic). If the production parser and this one
agreed by sharing a bug, this test would not catch it; deliberately not
sharing code is what makes that not the case. `café.py`'s
porcelain-lookalike line - real content whose text, after stripping a
tab, reads exactly like a genuine `author` header - is what a shared
bug would most plausibly be, so `test_lookalike_line_...` below is the
test that most directly earns this independence.
"""

from __future__ import annotations

import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from fixtures.build import AWKWARD_HEAD, AWKWARD_LOOKALIKE_LINE

from historian.schema import Row
from historian.tables import blame
from historian.tables.blame import BLAME_SCHEMA, BlameScan

# --- The independent oracle ------------------------------------------------

_PATH_IDX = BLAME_SCHEMA.index_of("path")
_LINE_NO_IDX = BLAME_SCHEMA.index_of("line_no")
_LINE_IDX = BLAME_SCHEMA.index_of("line")
_COMMIT_HASH_IDX = BLAME_SCHEMA.index_of("commit_hash")
_AUTHOR_NAME_IDX = BLAME_SCHEMA.index_of("author_name")
_AUTHOR_EMAIL_IDX = BLAME_SCHEMA.index_of("author_email")
_AUTHORED_AT_IDX = BLAME_SCHEMA.index_of("authored_at")

# A commit-info line: 40 hex digits, a space, the original line number,
# the final line number, and (only on the first line of a same-commit
# run) a trailing count - e.g. "feb78aa9...5e2cd8 1 1 4" or "...cd8 2 2".
# Matched by pattern, not by position - the production parser instead
# tracks *where* it is in the block; this oracle tracks *what a line
# looks like*.
_HEADER_RE = re.compile(r"^[0-9a-f]{40} \d+ \d+(?: \d+)?$")


def _oracle_authored_at(unix_time: int) -> str:
    return datetime.fromtimestamp(unix_time, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _oracle_parse_porcelain(text: str, path: str) -> list[Row]:
    """An independently-written `git blame --line-porcelain` parser.

    Accumulates header fields into a dict keyed by their porcelain
    field name, keyed off a regex match for the commit-info line
    rather than "the line right after the last content line". A
    content line is still identified by a leading tab checked first -
    that part is not a design choice either implementation is free to
    vary, it is simply what the format is - but nothing else about how
    this function locates record boundaries matches the production
    parser's approach.
    """
    rows: list[Row] = []
    fields: dict[str, str] = {}
    commit_hash: str | None = None
    line_no: int | None = None
    for line in text.split("\n"):
        if line.startswith("\t"):
            content = line[1:]
            assert commit_hash is not None and line_no is not None
            rows.append(
                (
                    path,
                    line_no,
                    content,
                    commit_hash,
                    fields["author"],
                    fields["author-mail"].strip("<>"),
                    _oracle_authored_at(int(fields["author-time"])),
                )
            )
            fields = {}
            continue
        if line == "":
            continue
        if _HEADER_RE.match(line):
            parts = line.split(" ")
            commit_hash = parts[0]
            line_no = int(parts[2])
            continue
        if " " in line:
            key, _, value = line.partition(" ")
            fields[key] = value
        # A bare keyword line with no value ("boundary") - ignored,
        # same as the production parser ignores anything it does not
        # need.
    return rows


def _oracle_list_paths(repo: Path) -> list[str]:
    """Independently enumerate paths at HEAD via `ls-tree -z`. This is
    the same flag choice `historian.tables.blame.list_paths` makes,
    because it is the only correct one (the grooming finding that
    unqualified `--name-only` quote-escapes the `awkward` fixture's
    path) - not a parsing algorithm there is a second way to write."""
    raw = subprocess.run(
        ["git", "ls-tree", "-rz", "HEAD", "--name-only"],
        cwd=repo,
        capture_output=True,
        check=True,
    ).stdout
    return [chunk.decode("utf-8", errors="replace") for chunk in raw.split(b"\0") if chunk]


def _oracle_blame(repo: Path, path: str) -> list[Row]:
    raw = subprocess.run(
        ["git", "blame", "--line-porcelain", "HEAD", "--", path],
        cwd=repo,
        capture_output=True,
        check=True,
    ).stdout
    return _oracle_parse_porcelain(raw.decode("utf-8", errors="replace"), path)


def _oracle_scan(repo: Path) -> list[Row]:
    rows: list[Row] = []
    for path in _oracle_list_paths(repo):
        rows.extend(_oracle_blame(repo, path))
    return rows


def _by_path(rows: list[Row]) -> dict[str, list[Row]]:
    grouped: dict[str, list[Row]] = {}
    for row in rows:
        grouped.setdefault(row[_PATH_IDX], []).append(row)
    return grouped


# --- tiny --------------------------------------------------------------


def test_tiny_matches_the_oracle(tiny_repo):
    """The scan's full row set, order aside, equals the independent
    oracle's - the strongest single check available, and it subsumes
    "every column populated" and "line_no is git's own line number"
    for every row `tiny` produces."""
    actual = sorted(BlameScan(tiny_repo).scan())
    expected = sorted(_oracle_scan(tiny_repo))
    assert actual == expected


def test_tiny_yields_exactly_three_rows_over_two_surviving_paths(tiny_repo):
    """`docs/todo.md` (deleted before HEAD) and `src/util.py` (the
    pre-rename path) contribute nothing - matches
    `git ls-tree -r HEAD --name-only` at HEAD."""
    rows = list(BlameScan(tiny_repo).scan())
    grouped = _by_path(rows)
    assert set(grouped) == {"src/utils.py", "feature/thing.py"}
    assert len(grouped["src/utils.py"]) == 2
    assert len(grouped["feature/thing.py"]) == 1
    assert len(rows) == 3


def test_tiny_rename_is_followed_back_to_the_root_commit(tiny_repo):
    """`src/utils.py` was renamed from `src/util.py` with no content
    change (`tests/fixtures/build.py`'s `_verify_tiny` already pins
    this at the fixture-build level). Every line's `commit_hash` must
    therefore be the pre-rename root commit, not the rename commit."""
    root = subprocess.run(
        ["git", "rev-list", "--max-parents=0", "HEAD"],
        cwd=tiny_repo,
        capture_output=True,
        check=True,
        text=True,
    ).stdout.strip()

    rows = [row for row in BlameScan(tiny_repo).scan() if row[_PATH_IDX] == "src/utils.py"]
    assert len(rows) == 2
    assert all(row[_COMMIT_HASH_IDX] == root for row in rows)


def test_tiny_never_blames_a_path_absent_from_ls_tree(tiny_repo, monkeypatch):
    """`docs/todo.md` is tracked in history but not at HEAD - the scan
    must never invoke `git blame` on it. Checked by recording every
    git invocation's argv, per spec §4's pushdown-layer approach:
    "Scans record what they did ... tests read it" (here, the test
    itself is the recorder, via a spy on `subprocess.run`)."""
    calls: list[list[str]] = []
    real_run = subprocess.run

    def spy(args, *a, **kw):
        calls.append(list(args))
        return real_run(args, *a, **kw)

    monkeypatch.setattr(blame.subprocess, "run", spy)

    list(BlameScan(tiny_repo).scan())

    blamed_paths = [call[-1] for call in calls if len(call) >= 2 and call[1] == "blame"]
    assert set(blamed_paths) == {"src/utils.py", "feature/thing.py"}
    assert "docs/todo.md" not in blamed_paths


# --- awkward -------------------------------------------------------------


def test_awkward_matches_the_oracle(awkward_repo):
    actual = sorted(BlameScan(awkward_repo).scan())
    expected = sorted(_oracle_scan(awkward_repo))
    assert actual == expected


def test_awkward_yields_exactly_twelve_rows(awkward_repo):
    rows = list(BlameScan(awkward_repo).scan())
    grouped = _by_path(rows)
    expected_counts = {
        "café.py": 6,
        'a "quoted" name.txt': 1,
        "binary.bin": 1,
        "empty.txt": 0,
        "no_newline.txt": 3,
        "phoenix.txt": 1,
    }
    assert set(grouped) == {path for path, count in expected_counts.items() if count > 0}
    for path, count in expected_counts.items():
        assert len(grouped.get(path, [])) == count, path
    assert len(rows) == 12


def test_awkward_every_row_has_all_seven_columns_populated(awkward_repo):
    """Blame never leaves a surviving line unattributed."""
    for row in BlameScan(awkward_repo).scan():
        assert len(row) == len(BLAME_SCHEMA.columns)
        assert all(value is not None for value in row), row


def test_line_no_is_always_a_python_int_never_a_bool_never_a_str(awkward_repo):
    for row in BlameScan(awkward_repo).scan():
        line_no = row[_LINE_NO_IDX]
        assert isinstance(line_no, int)
        assert not isinstance(line_no, bool)


def test_authored_at_is_iso8601_utc(awkward_repo):
    pattern = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
    for row in BlameScan(awkward_repo).scan():
        assert pattern.match(row[_AUTHORED_AT_IDX]), row[_AUTHORED_AT_IDX]


def test_empty_file_contributes_zero_rows_not_an_error(awkward_repo):
    """Confirmed independently that `git blame --line-porcelain
    empty.txt` exits 0 with empty stdout - so this must not raise and
    must not produce one row with empty content."""
    rows = [row for row in BlameScan(awkward_repo).scan() if row[_PATH_IDX] == "empty.txt"]
    assert rows == []


def test_lookalike_line_is_content_not_a_header(awkward_repo):
    """café.py's line 4 is real file content that reads exactly like a
    porcelain `author` header once its leading tab is stripped. It
    must appear verbatim as that row's `line`, attributed identically
    to lines 1-3 - proving the parser told a real header from content
    by the leading tab, not by matching header keywords against text."""
    cafe_rows = {
        row[_LINE_NO_IDX]: row
        for row in BlameScan(awkward_repo).scan()
        if row[_PATH_IDX] == "café.py"
    }
    assert cafe_rows[4][_LINE_IDX] == AWKWARD_LOOKALIKE_LINE
    first_commit_attribution = (
        cafe_rows[1][_COMMIT_HASH_IDX],
        cafe_rows[1][_AUTHOR_NAME_IDX],
        cafe_rows[1][_AUTHOR_EMAIL_IDX],
    )
    for line_no in (2, 3, 4):
        assert (
            cafe_rows[line_no][_COMMIT_HASH_IDX],
            cafe_rows[line_no][_AUTHOR_NAME_IDX],
            cafe_rows[line_no][_AUTHOR_EMAIL_IDX],
        ) == first_commit_attribution


def test_quoted_path_is_exact_not_reescaped(awkward_repo):
    """`path` must be the literal path (space and double quote intact,
    no backslash escaping), sourced from `ls-tree -z` enumeration - not
    parsed from the porcelain `filename` header, which re-quotes it as
    `"a \\"quoted\\" name.txt"`."""
    rows = [
        row for row in BlameScan(awkward_repo).scan() if row[_PATH_IDX] == 'a "quoted" name.txt'
    ]
    assert len(rows) == 1
    assert rows[0][_PATH_IDX] == 'a "quoted" name.txt'


def test_binary_line_is_a_stable_str(awkward_repo):
    """`binary.bin`'s single row's `line` is a `str` (never `bytes`,
    never raises), decoded as UTF-8 with `errors="replace"`, and the
    same value on every run."""
    first = [row for row in BlameScan(awkward_repo).scan() if row[_PATH_IDX] == "binary.bin"]
    second = [row for row in BlameScan(awkward_repo).scan() if row[_PATH_IDX] == "binary.bin"]
    assert len(first) == 1
    assert isinstance(first[0][_LINE_IDX], str)
    assert first == second


def test_no_newline_file_three_rows_no_injected_newline(awkward_repo):
    rows = sorted(
        (row for row in BlameScan(awkward_repo).scan() if row[_PATH_IDX] == "no_newline.txt"),
        key=lambda row: row[_LINE_NO_IDX],
    )
    assert len(rows) == 3
    assert rows[0][_LINE_IDX] == "first line"
    assert rows[1][_LINE_IDX] == "second line"
    assert rows[2][_LINE_IDX] == "third line without a trailing newline"


def test_phoenix_attributed_to_the_recreation_commit_not_the_original(awkward_repo):
    """`phoenix.txt` was added, deleted, then recreated with different
    content and an empty commit message (the third commit, which is
    also `AWKWARD_HEAD`). Its single row must be attributed to that
    recreation, proving the scan reads content at HEAD rather than
    walking full file history back to the original add."""
    rows = [row for row in BlameScan(awkward_repo).scan() if row[_PATH_IDX] == "phoenix.txt"]
    assert len(rows) == 1
    assert rows[0][_COMMIT_HASH_IDX] == AWKWARD_HEAD


# --- The pushdown-interface stub (M4's real work is #13/#14) -------------


def test_capabilities_is_empty(tiny_repo):
    assert BlameScan(tiny_repo).capabilities() == set()


def test_scan_ignores_pushed_entirely(tiny_repo):
    """No pushdown is attempted: calling `scan()` with a non-empty or
    nonsensical `pushed` argument produces the identical full row set
    as calling it with none - a predicate that cannot push down must
    still produce correct results (spec §4)."""
    baseline = sorted(BlameScan(tiny_repo).scan())
    with_garbage = sorted(BlameScan(tiny_repo).scan(pushed=[object(), "not a predicate", 42]))
    assert with_garbage == baseline


# --- Unit-level: no repository needed -------------------------------------


def test_zero_paths_yields_zero_rows_no_repository_needed():
    """The loop over `paths` in `blame_paths` never runs when `paths`
    is empty, so no git command is ever invoked - a nonexistent repo
    path is fine."""
    rows = list(blame.blame_paths(Path("/nonexistent/not-a-repo"), []))
    assert rows == []


# --- Determinism -----------------------------------------------------------


def test_scanning_twice_is_byte_identical_and_same_order(awkward_repo):
    """Per AGENTS.md: the same repository and the same query always
    produce the same rows in the same order."""
    first = list(BlameScan(awkward_repo).scan())
    second = list(BlameScan(awkward_repo).scan())
    assert first == second
