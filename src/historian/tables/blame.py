"""The `blame` scan: `_docs/spec.md` §2, phase 1.

One row per line of code alive at `HEAD`. This is the first module in
`src/` allowed to touch git - per `AGENTS.md`, only scan operators do,
and this is one. `subprocess` appears nowhere else under `src/`.

Two commands, per spec §2's "Implementation"
----------------------------------------------

1. ``git ls-tree -rz HEAD --name-only`` enumerates every path tracked
   at `HEAD`. **Not** the unqualified ``--name-only`` spec §2's prose
   shows: verified during grooming that plain `ls-tree` quote-and-
   backslash-escapes any path containing a space or a double quote
   (git's own C-style path quoting, on regardless of `core.quotePath`),
   which corrupts the `awkward` fixture's ``a "quoted" name.txt``. The
   ``-z``, NUL-terminated form prints the raw bytes.
2. ``git blame --line-porcelain HEAD -- <path>`` per surviving path,
   parsed into rows. ``--line-porcelain`` (not plain ``--porcelain``)
   repeats the full commit header on *every* line rather than
   abbreviating repeats for consecutive same-commit lines - the
   property that makes a stateless, per-line parse correct without
   tracking "the last header seen".

`blame.path` is never read back out of the porcelain output's own
``filename`` header line. That header re-quotes exactly the way
`ls-tree` does (``filename "a \\"quoted\\" name.txt"``), with no
unquoted form - verified during grooming. Every row's `path` instead
comes from the enumeration in (1), which the scan already knows it
invoked `git blame` on.

Decoding
--------

Git bytes (`path` and a blamed `line`) are decoded as UTF-8 with
``errors="replace"``, not ``surrogateescape``. This closes #20:
`surrogateescape` produces a Python `str` that Python's own `sqlite3`
module cannot insert at all (`UnicodeEncodeError: surrogates not
allowed`), which would break M3's differential harness outright, since
its SQLite side is loaded from these same extracted rows
(`_docs/decisions.md`, 2026-08-24). `errors="replace"` inserts and
round-trips cleanly, and - because it never produces a lone surrogate -
keeps `values.py`'s bytewise-text-ordering assumption true in practice
for every value historian actually produces. The same decode call
handles both `path` and `line`; see the module-level `_decode`.

A newline byte (``0x0A``) is never consumed as part of an invalid
multi-byte sequence's replacement: verified directly that
``b"\\xff\\n\\xfe".decode("utf-8", errors="replace")`` still splits on
that ``\\n``. So the whole `git blame` byte stream is decoded once and
split on ``"\\n"``, rather than decoding line by line - both give the
same result, and decoding once is simpler.

Parsing a porcelain block: leading tab first, never a keyword match
---------------------------------------------------------------------

A content line is identified by a leading tab character on that line,
checked *before* anything else - never by testing whether the line's
text looks like a known header keyword. The `awkward` fixture's
`café.py` contains a line of file content that is itself the text
``"author nobody@nowhere.example claims to be a porcelain header but is
not"`` - verified to genuinely fool a parser that strips the line and
then matches it against known header prefixes, because after stripping
its leading tab the line *is* indistinguishable from a real ``author``
header. Checking for the tab first, and only ever stripping that one
leading character (never a general `.strip()`), is what tells them
apart. `tests/extraction/test_blame.py` has its own, independently
written parser that proves this the same way, on the same fixture.

`path` pushdown (issue #122)
----------------------------

`BlameScan.accepts()` takes exactly three shapes of term on the `path`
column (schema offset 0, matched by `BoundColumnRef.offset`, never by
name), and `scan()` uses them to blame fewer files:

- ``path = 'lit'`` or ``'lit' = path`` - a text literal only;
- ``path IN ('lit', ...)`` - every element a text literal, ``IN ()``
  included; never ``NOT IN``;
- ``path LIKE 'prefix%'`` - no ``NOT``, no ``ESCAPE``, a text-literal
  pattern whose only wildcard is one trailing ``%``.

Anything else is rejected and left to the `Filter` above the scan,
which is never removed. In particular a non-text literal (``path =
5``) is rejected rather than converted: TEXT affinity would turn it
into ``'5'``, and that conversion belongs to `exec/expression.py`,
not to a second copy here.

The candidate set is always cut down from `git ls-tree`'s own output,
never built from the literals, so `git blame` is never run on a path
that is not tracked at `HEAD`. A `LIKE` prefix is matched with
`historian.ascii.ascii_fold` on both sides - the same ASCII-only fold
the evaluator's `LIKE` uses - because SQLite's `LIKE` folds ASCII case
(``'src/a.py' LIKE 'SRC/%'`` is true) and leaves every other letter
alone (``'straße' LIKE 'STRASSE'`` is false). A case-sensitive prefix
check would drop matching rows; `str.lower()` would add non-matching
ones and, worse, disagree with the evaluator about what matches.

Several pushed terms narrow one after another, so the result is their
intersection - still a superset of the rows the whole `WHERE` keeps.
An `IN` list blames in the list's own order (deduplicated); `=` and
`LIKE` keep `ls-tree` order.

Every `scan()` call records what it did on the instance -
`blamed_paths`, `git_invocations`, `tracked_path_count` - reset at the
start of the call, so a test (and later `--stats`, #42) can assert
the work was actually avoided rather than only that the rows are
right (spec §4, "The pushdown layer").
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..ascii import ascii_fold
from ..schema import Column, ColumnType, Row, Schema
from ..sql.ast import BinaryOp, Expr, In, Like, Literal, Operator
from ..sql.binder import BoundColumnRef

__all__ = [
    "BLAME_SCHEMA",
    "BlameScan",
    "PATH_EQ",
    "PATH_IN",
    "PATH_LIKE_PREFIX",
    "blame_paths",
    "list_paths",
]

#: `blame`'s seven columns (spec §2), in column order. Imported by #9
#: (the table catalog) and #12 (the `Scan` operator) rather than either
#: redefining it - see the grooming decision recorded on both issues.
BLAME_SCHEMA = Schema(
    columns=(
        Column("path", ColumnType.TEXT),
        Column("line_no", ColumnType.INTEGER),
        Column("line", ColumnType.TEXT),
        Column("commit_hash", ColumnType.TEXT),
        Column("author_name", ColumnType.TEXT),
        Column("author_email", ColumnType.TEXT),
        Column("authored_at", ColumnType.TEXT),
    )
)

#: `path`'s offset in `BLAME_SCHEMA` - how a pushed term's
#: `BoundColumnRef` is recognised as naming it.
_PATH_OFFSET = 0

#: The pushdown kinds `BlameScan.capabilities()` declares (#122): one
#: per accepted `path` shape. Labels only; the optimizer never reads
#: them.
PATH_EQ = "path_eq"
PATH_IN = "path_in"
PATH_LIKE_PREFIX = "path_like_prefix"


def _decode(data: bytes) -> str:
    """Decode raw git bytes as UTF-8 with `errors="replace"` - the
    decoding policy settled by grooming (#20) for both `path` and
    `line`. See the module docstring for why."""
    return data.decode("utf-8", errors="replace")


def _run_git_bytes(repo: Path, args: Sequence[str]) -> bytes:
    """Run a git command with cwd=repo, returning raw stdout bytes.

    Always an explicit argv list, never `shell=True` and never a
    formatted command string - the `awkward` fixture's quoted-and-
    spaced path is a real shell-injection-shaped hazard here, matching
    the convention `tests/fixtures/build.py` already established.
    Output is read as bytes, not decoded by `subprocess` itself
    (`text=False`, the default): a blamed line's content can be
    invalid UTF-8 (`binary.bin`), and decoding is this module's own
    policy (`_decode`), not the standard library's default of
    surrogate-escaping or raising.
    """
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} (in {repo}) failed:\n{_decode(result.stderr)}"
        )
    return result.stdout


def list_paths(repo: Path) -> list[str]:
    """Enumerate every path tracked at `HEAD`, via
    ``git ls-tree -rz HEAD --name-only`` (see the module docstring for
    why ``-z`` and not the unqualified form)."""
    raw = _run_git_bytes(repo, ["ls-tree", "-rz", "HEAD", "--name-only"])
    return [_decode(chunk) for chunk in raw.split(b"\0") if chunk]


def _authored_at(unix_time: int) -> str:
    """ISO-8601 UTC of the form ``YYYY-MM-DDTHH:MM:SSZ``, from the
    porcelain ``author-time`` field (a Unix timestamp, already UTC).

    Deliberately not `author-tz`: `author-tz` is a display offset, and
    using it would need this scan to interpret it correctly for
    `authored_at` to be right - `author-time` alone is enough, and
    keeps `authored_at > '2026-01-01'` behaving identically in
    historian and SQLite with no conversion layer (spec §2).
    """
    return datetime.fromtimestamp(unix_time, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_porcelain(text: str, path: str) -> Iterator[Row]:
    """Parse one file's ``git blame --line-porcelain`` output (already
    decoded) into rows, in file order.

    Stateful and positional, not keyword-matching: each record starts
    with a commit-info line (``<hash> <orig-line> <final-line>
    [<count>]``, never tab-prefixed), is followed by header fields
    (``author ...``, ``author-mail ...``, ``author-time ...`` and
    others this scan does not need), and ends at the first line that
    starts with a tab - which is the content line, and the *only* thing
    a leading tab is ever checked for. See the module docstring for why
    this order of checks matters for the `awkward` fixture.

    `path` is never read from the porcelain ``filename`` field; it is
    the path the caller already invoked `git blame` on.
    """
    lines = text.split("\n")
    index = 0
    total = len(lines)
    while index < total:
        header = lines[index]
        if header == "":
            # A trailing blank line from the final "\n" before EOF (or
            # the whole file being empty, in which case this loop never
            # starts at all - `_run_git_bytes` returns b"" for
            # `empty.txt` and `text.split("\n")` is then `[""]`).
            index += 1
            continue

        commit_hash, _orig_line, final_line, *_count = header.split(" ")
        line_no = int(final_line)
        index += 1

        author_name: str | None = None
        author_email: str | None = None
        author_time: int | None = None
        while True:
            field = lines[index]
            if field.startswith("\t"):
                content = field[1:]
                index += 1
                break
            if field.startswith("author-mail "):
                author_email = field[len("author-mail ") :].strip("<>")
            elif field.startswith("author-time "):
                author_time = int(field[len("author-time ") :])
            elif field.startswith("author "):
                author_name = field[len("author ") :]
            index += 1

        assert author_name is not None
        assert author_email is not None
        assert author_time is not None
        yield (
            path,
            line_no,
            content,
            commit_hash,
            author_name,
            author_email,
            _authored_at(author_time),
        )


def _blame_file(repo: Path, path: str) -> Iterator[Row]:
    """Blame one file at `HEAD`, yielding its rows in line order."""
    raw = _run_git_bytes(repo, ["blame", "--line-porcelain", "HEAD", "--", path])
    yield from _parse_porcelain(_decode(raw), path)


def blame_paths(repo: Path, paths: Sequence[str]) -> Iterator[Row]:
    """Blame each of `paths` at `HEAD` in `repo`, in the given order,
    streaming rows.

    Split out from `BlameScan.scan` so "zero paths in, zero rows out"
    is testable directly, with no repository required: given an empty
    `paths`, this loop body never runs, so no git command is ever
    invoked - `repo` need not even exist.
    """
    for path in paths:
        yield from _blame_file(repo, path)


# --- `path` pushdown (#122) -----------------------------------------------


@dataclass(frozen=True)
class _PathSelection:
    """Which paths one accepted term can select. Exactly one field is
    set: `literals` for `=`/`IN` (deduplicated, in the term's own
    order), `prefix` for `LIKE 'prefix%'`."""

    literals: tuple[str, ...] | None
    prefix: str | None


def _is_path_column(expr: Expr) -> bool:
    return isinstance(expr, BoundColumnRef) and expr.offset == _PATH_OFFSET


def _is_text_literal(expr: Expr) -> bool:
    """A `Literal` whose value is a `str` - never `NULL`, never a
    number (whose TEXT-affinity conversion is the evaluator's job)."""
    return isinstance(expr, Literal) and isinstance(expr.value, str)


def _like_prefix(pattern: str) -> str | None:
    """The prefix of a `LIKE` pattern of the form ``'prefix%'`` - one
    trailing `%`, no other `%` or `_` - or `None` for any other
    pattern."""
    if len(pattern) == 0 or pattern[-1] != "%":
        return None
    prefix = pattern[:-1]
    if "%" in prefix or "_" in prefix:
        return None
    return prefix


def _path_selection(term: Expr) -> _PathSelection | None:
    """The paths *term* can select, or `None` if it is not one of the
    three accepted shapes. Shape alone, no I/O: this is both
    `BlameScan.accepts()`'s answer and what `scan()` narrows by, so
    the two can never disagree."""
    if isinstance(term, BinaryOp):
        if term.op != Operator.EQ:
            return None
        if _is_path_column(term.left) and _is_text_literal(term.right):
            return _PathSelection(literals=(term.right.value,), prefix=None)
        if _is_text_literal(term.left) and _is_path_column(term.right):
            return _PathSelection(literals=(term.left.value,), prefix=None)
        return None
    if isinstance(term, In):
        if term.negated or not _is_path_column(term.left):
            return None
        literals: list[str] = []
        for value in term.values:
            if not _is_text_literal(value):
                return None
            if value.value not in literals:
                literals.append(value.value)
        return _PathSelection(literals=tuple(literals), prefix=None)
    if isinstance(term, Like):
        if term.negated or term.escape is not None:
            return None
        if not _is_path_column(term.left) or not _is_text_literal(term.pattern):
            return None
        prefix = _like_prefix(term.pattern.value)
        if prefix is None:
            return None
        return _PathSelection(literals=None, prefix=prefix)
    return None


def _narrow(candidates: list[str], selection: _PathSelection) -> list[str]:
    """The entries of *candidates* that *selection* can select.

    Always a sub-list of *candidates* - never a path taken from the
    query's own literals - so a path `ls-tree` did not report is never
    blamed. For literals the order is the literals' own; for a prefix,
    *candidates*' order."""
    narrowed: list[str] = []
    if selection.literals is not None:
        for literal in selection.literals:
            for path in candidates:
                if path == literal:
                    narrowed.append(path)
        return narrowed
    assert selection.prefix is not None
    folded_prefix = ascii_fold(selection.prefix)
    for path in candidates:
        if ascii_fold(path).startswith(folded_prefix):
            narrowed.append(path)
    return narrowed


class BlameScan:
    """The `blame` table's scan operator (spec §2, phase 1; §3's scan
    interface).

    Blame is always at `HEAD` in v1 (spec §2); blaming at another
    revision is out of scope. Rename detection and merge handling are
    left entirely to `git blame` itself - reimplementing either is a
    month of work in a domain nobody is evaluating (spec §2), so this
    class shells out and parses, and does nothing else.
    """

    #: The schema every row from `scan()` conforms to.
    schema = BLAME_SCHEMA

    def __init__(self, repo: Path):
        self._repo = Path(repo)
        # The work record (spec §4, "The pushdown layer"), reset by
        # every `scan()` call. Plain attributes, read by tests and by
        # `--stats` (#42): the paths `git blame` was run on, in order;
        # every `git` process started, `ls-tree` included; and how many
        # paths `ls-tree` reported at `HEAD`.
        self.blamed_paths: list[str] = []
        self.git_invocations = 0
        self.tracked_path_count = 0

    def capabilities(self) -> set[str]:
        """Which pushdown kinds this scan can use: `path` equality,
        `path IN`, and `path LIKE 'prefix%'` (spec §2's `blame`
        table). Every other column is unknown until a file has been
        blamed, so it cannot reduce work."""
        return {PATH_EQ, PATH_IN, PATH_LIKE_PREFIX}

    def accepts(self, term: Expr) -> bool:
        """Whether this scan uses *term* (one conjunctive `WHERE`
        term, bound against `BLAME_SCHEMA`) to blame fewer files.
        True only for the three shapes listed in the module
        docstring; decided from the term's shape alone."""
        return _path_selection(term) is not None

    def scan(self, pushed: Sequence[Expr] = ()) -> Iterator[Row]:
        """Yield the `blame` rows at `HEAD` for every path the
        *pushed* terms can select - every tracked path when nothing is
        pushed - streamed file by file.

        The rows are a superset of those the pushed terms keep: a
        path is dropped only when no row of it could satisfy some
        pushed term. A term `accepts()` would reject narrows nothing.
        Resets the work record now, at the call, not at the first row.
        """
        self.blamed_paths = []
        self.git_invocations = 0
        self.tracked_path_count = 0
        return self._scan_rows(tuple(pushed))

    def _scan_rows(self, pushed: tuple[Expr, ...]) -> Iterator[Row]:
        self.git_invocations += 1
        candidates = list_paths(self._repo)
        self.tracked_path_count = len(candidates)
        for term in pushed:
            selection = _path_selection(term)
            if selection is not None:
                candidates = _narrow(candidates, selection)
        for path in candidates:
            self.blamed_paths.append(path)
            self.git_invocations += 1
            yield from _blame_file(self._repo, path)
