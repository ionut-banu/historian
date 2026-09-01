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
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator, Sequence
from datetime import datetime, timezone
from pathlib import Path

from ..schema import Column, ColumnType, Row, Schema

__all__ = ["BLAME_SCHEMA", "BlameScan", "blame_paths", "list_paths"]

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

    def capabilities(self) -> set[str]:
        """Which pushdown kinds this scan can use.

        Always empty. `blame`'s path/author/time pushdown (spec §2's
        table) is M4's job (#13/#14, "Scan capability negotiation and
        predicate splitting") - this issue shapes `capabilities()`'s
        and `scan()`'s call convention per §2 without inventing the
        `PushdownKind`/`Predicate` types that negotiation needs, per
        the grooming decision on #11. An empty set here is not a
        placeholder pending that work; it is simply true today - this
        scan does not yet know how to use a predicate to do less work.
        """
        return set()

    def scan(self, pushed: Sequence[object] = ()) -> Iterator[Row]:
        """Yield every `blame` row at `HEAD`, streamed file by file.

        `pushed` is accepted per §2's ``scan(pushed)`` call convention
        and is provably ignored: `capabilities()` returns an empty set,
        so a future planner (#13) never has anything accepted to pass
        here, and this method does not inspect `pushed`'s contents at
        all - every path `list_paths` reports is always blamed,
        whatever `pushed` is. Rows are streamed (a generator), never
        materialized as a list, matching spec §1's "no materialized
        copy" and §2's phase-1 implementation note.
        """
        yield from blame_paths(self._repo, list_paths(self._repo))
