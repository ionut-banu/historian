"""Deterministic fixture repositories for historian's tests.

Implements spec.md §4: fixture repositories that must be byte-identical
on every machine and every run. See _docs/decisions.md, 2026-09-01,
for what was found beyond the spec's original six GIT_* environment
variables and why the fix is "inherit nothing" rather than "override
the settings we know about."

Only `tiny` and `awkward` are built here. `large` is tracked as its own
issue (#27) and is deliberately not implemented in this module - see
that issue for the isolation recipe restated for whoever picks it up.

This module uses `subprocess` and touches git directly. Per AGENTS.md,
that rule applies to src/historian/ (the parser, planner and executor);
this is test infrastructure, so it is expected and lives outside
src/historian/ entirely.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path
from typing import Sequence


class FixtureError(Exception):
    """A fixture builder produced something that does not match what it
    was told to build.

    Spec §4: "The fixture builder asserts what it built. A fixture that
    silently stops containing a merge commit takes a whole class of
    tests with it." Raised by the builder itself, at build time, rather
    than left to surface later as a confusing extraction or differential
    test failure.
    """


# ---------------------------------------------------------------------------
# Isolation
#
# Git hashes derive from author, committer, timestamps and tree - but
# also from whatever ambient git configuration the build inherits.
# _docs/decisions.md (2026-09-01) records what this cost: core.autocrlf
# alone changes a blob's hash, and a compiled-in system config (Apple's
# Command Line Tools git ships one) survives GIT_CONFIG_SYSTEM=/dev/null
# and needs GIT_CONFIG_NOSYSTEM=1 as well. So every git invocation here
# goes through _isolated_env, which inherits nothing rather than
# overriding a hand-maintained list of settings.
# ---------------------------------------------------------------------------


def _isolated_env(overrides: dict[str, str]) -> dict[str, str]:
    """The environment for a git subprocess that inherits no ambient git
    configuration at all.

    Every ambient GIT_* variable is dropped, not just the well-known
    config ones - a leftover GIT_AUTHOR_NAME or GIT_DIR from whatever
    process launched us is exactly the kind of thing "inherit nothing"
    is meant to rule out by construction, rather than by remembering to
    override it too. `overrides` (the three isolation variables below,
    plus the six GIT_AUTHOR_*/GIT_COMMITTER_* for a commit) are applied
    last and always win.
    """
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    env["GIT_CONFIG_SYSTEM"] = "/dev/null"
    # Necessary in addition to GIT_CONFIG_SYSTEM=/dev/null: Apple's
    # Command Line Tools git reads its own hardcoded system-scope config
    # (init.defaultBranch=main, credential.helper=osxkeychain) regardless
    # of where GIT_CONFIG_SYSTEM points. Only NOSYSTEM suppresses it.
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env.update(overrides)
    return env


def _run_git(repo: Path, args: Sequence[str], overrides: dict[str, str] | None = None) -> str:
    """Run a git command with cwd=repo under the isolated environment,
    returning stdout. Raises RuntimeError with stderr on failure."""
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        env=_isolated_env(overrides or {}),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} (in {repo}) failed:\n{result.stderr}")
    return result.stdout


def _run_git_bytes(repo: Path, args: Sequence[str]) -> bytes:
    """Like _run_git, but for commands whose output must be read as raw
    bytes (a blob's content, which may be binary)."""
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        env=_isolated_env({}),
        capture_output=True,
        text=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} (in {repo}) failed:\n{result.stderr.decode(errors='replace')}")
    return result.stdout


# Local repo config set immediately after `git init`, as defense in
# depth on top of the environment-level isolation above - not instead
# of it. Chosen from the grooming's own list of settings that were each
# demonstrated to change a hash or silently change what gets committed.
_LOCAL_CONFIG = {
    "core.autocrlf": "false",
    "core.fileMode": "true",
    "core.symlinks": "true",
    "core.ignoreCase": "false",
    "commit.gpgsign": "false",
    "core.safecrlf": "false",
    # Not itself a determinism hazard (git always escapes structurally
    # special characters in a path, quotePath or not), but the awkward
    # fixture's path containing a double quote is far easier to reason
    # about in git's plain output with this off. -z output (used for
    # every machine-parsed listing in this module) is unaffected either
    # way; this is purely for anyone inspecting the fixture by hand.
    "core.quotePath": "false",
}


def _init_repo(path: Path, *, branch: str) -> None:
    """Create a fresh, isolated git repository at path with an explicit
    initial branch name.

    §4's finding made concrete: relying on init.defaultBranch's fallback
    is not portable even with GIT_CONFIG_SYSTEM=/dev/null in place (see
    _isolated_env above), so the branch name is always passed explicitly
    rather than left to any default, isolated or not.
    """
    path.mkdir(parents=True, exist_ok=True)
    _run_git(path, ["init", "--quiet", "-b", branch])
    for key, value in _LOCAL_CONFIG.items():
        _run_git(path, ["config", key, value])


class _Clock:
    """A deterministic, strictly increasing sequence of commit
    timestamps, formatted for GIT_AUTHOR_DATE/GIT_COMMITTER_DATE.

    Real wall-clock time is never used for a fixture commit: two builds
    run seconds (or years) apart would otherwise disagree on every
    commit's timestamp and therefore on every hash from that commit
    onward. The starting point and step are arbitrary but fixed.
    """

    def __init__(self, start: int = 1_700_000_000, step: int = 3600):
        self._next = start
        self._step = step

    def tick(self) -> str:
        value = self._next
        self._next += self._step
        return f"{value} +0000"


def _commit(
    repo: Path,
    message: str,
    *,
    author: tuple[str, str],
    timestamp: str,
    allow_empty_message: bool = False,
) -> str:
    """Commit the index with an explicit author/committer identity and
    timestamp (spec §4's six GIT_* variables), returning the new HEAD.

    Committer is always set identically to author: nothing in any
    fixture needs them to differ, and keeping them equal keeps the
    builder's surface area small.
    """
    name, email = author
    overrides = {
        "GIT_AUTHOR_NAME": name,
        "GIT_AUTHOR_EMAIL": email,
        "GIT_AUTHOR_DATE": timestamp,
        "GIT_COMMITTER_NAME": name,
        "GIT_COMMITTER_EMAIL": email,
        "GIT_COMMITTER_DATE": timestamp,
    }
    args = ["commit", "--quiet", "-m", message]
    if allow_empty_message:
        args.append("--allow-empty-message")
    _run_git(repo, args, overrides)
    return _run_git(repo, ["rev-parse", "HEAD"]).strip()


def _merge(repo: Path, branch: str, message: str, *, author: tuple[str, str], timestamp: str) -> str:
    """Merge branch into the current branch with --no-ff, as an explicit
    two-parent commit with the same deterministic identity as _commit.

    If the merge cannot complete without conflict, `git merge` exits
    nonzero and _run_git raises - which is the proof that a fixture's
    two merge sides never touch the same file, rather than something
    this function needs to check separately.
    """
    name, email = author
    overrides = {
        "GIT_AUTHOR_NAME": name,
        "GIT_AUTHOR_EMAIL": email,
        "GIT_AUTHOR_DATE": timestamp,
        "GIT_COMMITTER_NAME": name,
        "GIT_COMMITTER_EMAIL": email,
        "GIT_COMMITTER_DATE": timestamp,
    }
    _run_git(repo, ["merge", "--no-ff", "--quiet", "-m", message, branch], overrides)
    return _run_git(repo, ["rev-parse", "HEAD"]).strip()


def _hex40_commit_hashes(porcelain_output: str) -> set[str]:
    """The set of commit hashes appearing in a `git blame --line-porcelain`
    header line (a 40-hex-digit line not starting with a tab)."""
    hashes = set()
    for line in porcelain_output.splitlines():
        if line[:1] == "\t":
            continue
        head = line.split(" ", 1)[0]
        if len(head) == 40 and all(c in "0123456789abcdef" for c in head):
            hashes.add(head)
    return hashes


# ---------------------------------------------------------------------------
# tiny
#
# "a handful of commits, two authors, a rename, a deletion, and a
# merge" - spec.md §4. Exact content and the reasoning behind each
# element is in the issue #10 grooming comment; the git graph built
# here is the smallest one realizing every element without padding:
#
#   C1 (Ana)  add src/util.py, docs/todo.md
#   C2 (Bo)   git mv src/util.py -> src/utils.py   (rename, no edit)
#   C3 (Ana)  on branch "feature": add feature/thing.py
#   C4 (Bo)   on main: remove docs/todo.md
#   C5        merge feature into main (--no-ff, two parents: C4, C3)
#
# C3 and C4 touch different files, so the merge is conflict-free by
# construction - if it weren't, `git merge` would exit nonzero and
# _run_git would raise right there.
# ---------------------------------------------------------------------------

_TINY_ANA = ("Ana Petrova", "ana@example.com")
_TINY_BO = ("Bo Lindqvist", "bo@example.com")

# Filled in once, from this builder's own first deterministic build,
# and then pinned: the strongest and cheapest regression check
# available for byte-identical fixtures. See _docs/decisions.md,
# 2026-09-01. Update in the same commit as any deliberate content
# change to build_tiny.
TINY_HEAD = "a088fde0619f0dd70e70eb47dad70cccf521f80e"


def build_tiny(dest: Path) -> Path:
    """Build the `tiny` fixture at dest, verify it, and return dest.

    Raises FixtureError if the built repository does not match what
    this function was told to build, including a determinism
    regression against the pinned TINY_HEAD.
    """
    if dest.exists():
        shutil.rmtree(dest)
    _init_repo(dest, branch="main")
    clock = _Clock()

    (dest / "src").mkdir()
    (dest / "src" / "util.py").write_text("def util():\n    return 42\n")
    (dest / "docs").mkdir()
    (dest / "docs" / "todo.md").write_text("- write blame\n")
    _run_git(dest, ["add", "-A"])
    _commit(dest, "Add util and todo", author=_TINY_ANA, timestamp=clock.tick())

    _run_git(dest, ["mv", "src/util.py", "src/utils.py"])
    _commit(dest, "Rename util.py to utils.py", author=_TINY_BO, timestamp=clock.tick())

    _run_git(dest, ["checkout", "--quiet", "-b", "feature"])
    (dest / "feature").mkdir()
    (dest / "feature" / "thing.py").write_text("print('feature')\n")
    _run_git(dest, ["add", "-A"])
    _commit(dest, "Add feature/thing.py", author=_TINY_ANA, timestamp=clock.tick())

    _run_git(dest, ["checkout", "--quiet", "main"])
    _run_git(dest, ["rm", "--quiet", "docs/todo.md"])
    _commit(dest, "Remove docs/todo.md", author=_TINY_BO, timestamp=clock.tick())

    _merge(dest, "feature", "Merge feature into main", author=_TINY_ANA, timestamp=clock.tick())
    _run_git(dest, ["branch", "-d", "feature"])

    _verify_tiny(dest)

    head = _run_git(dest, ["rev-parse", "HEAD"]).strip()
    if head != TINY_HEAD:
        raise FixtureError(
            f"tiny: HEAD is {head}, pinned TINY_HEAD is {TINY_HEAD} - "
            "this is a determinism regression, not a content change, "
            "unless build_tiny was deliberately edited (update the "
            "pinned constant in the same commit if so)"
        )
    return dest


def _verify_tiny(repo: Path) -> None:
    """Assert that repo matches what build_tiny is supposed to build.

    Inspects only the finished repository through git itself - not any
    bookkeeping from build_tiny - so it can also be pointed at a repo
    that no longer matches (a mutated builder, a bad checkout) and
    catch that directly, per spec §4: "the fixture builder asserts
    what it built."
    """
    count = int(_run_git(repo, ["rev-list", "--count", "HEAD"]).strip())
    if count != 5:
        raise FixtureError(f"tiny: expected exactly 5 commits, found {count}")

    merges = _run_git(repo, ["log", "--all", "--merges", "--format=%H"]).strip().splitlines()
    merges = [m for m in merges if m]
    if len(merges) != 1:
        raise FixtureError(f"tiny: expected exactly one merge commit, found {len(merges)}")
    parents = _run_git(repo, ["log", "-1", "--format=%P", merges[0]]).strip().split()
    if len(parents) != 2:
        raise FixtureError(f"tiny: merge commit {merges[0]} has {len(parents)} parents, expected 2")

    identities = set(_run_git(repo, ["log", "--format=%an <%ae>"]).strip().splitlines())
    if len(identities) != 2:
        raise FixtureError(f"tiny: expected exactly 2 author identities, found {identities}")

    head_paths = set(_run_git(repo, ["ls-tree", "-r", "--name-only", "HEAD"]).splitlines())
    if "src/util.py" in head_paths:
        raise FixtureError("tiny: src/util.py (pre-rename path) is still present at HEAD")
    if "src/utils.py" not in head_paths:
        raise FixtureError("tiny: src/utils.py (post-rename path) is missing from HEAD")
    if "docs/todo.md" in head_paths:
        raise FixtureError("tiny: docs/todo.md should have been deleted before HEAD")

    deletion_history = _run_git(repo, ["log", "--all", "--format=%H", "--", "docs/todo.md"]).strip()
    if not deletion_history:
        raise FixtureError("tiny: docs/todo.md was never tracked - deletion needs prior history")

    root_lines = _run_git(repo, ["rev-list", "--max-parents=0", "HEAD"]).strip().splitlines()
    if len(root_lines) != 1:
        raise FixtureError(f"tiny: expected exactly one root commit, found {len(root_lines)}")
    root = root_lines[0]

    porcelain = _run_git(repo, ["blame", "--line-porcelain", "src/utils.py"])
    blamed = _hex40_commit_hashes(porcelain)
    if blamed != {root}:
        raise FixtureError(
            "tiny: blame on src/utils.py at HEAD should attribute every "
            f"line to the root commit {root} (the rename must not have "
            f"changed content); got {blamed}"
        )


# ---------------------------------------------------------------------------
# awkward
#
# "built to break things" - spec.md §4. One item per required property,
# per the issue #10 grooming comment. Commit graph, smallest realizing
# every item without padding:
#
#   C1 (Zoë)  add café.py, the space-and-quote path, empty.txt,
#             binary.bin, no_newline.txt, phoenix.txt (original content)
#   C2 (Sam)  edit café.py (append lines - exercises blame's compact
#             header form on both C1's and C2's lines), delete
#             phoenix.txt
#   C3 (Sam)  recreate phoenix.txt with different content, empty
#             commit message
#
# phoenix.txt's create/delete/recreate lifecycle is the one thing that
# structurally needs three separate commits; everything else is
# bundled into those three rather than padding the graph further.
# ---------------------------------------------------------------------------

_AWKWARD_ZOE = ("Zoë Müller", "zoe@example.com")
_AWKWARD_SAM = ("Sam Lee", "sam@example.com")

_AWKWARD_CAFE_PATH = "café.py"
_AWKWARD_QUOTED_PATH = 'a "quoted" name.txt'
_AWKWARD_EMPTY_PATH = "empty.txt"
_AWKWARD_BINARY_PATH = "binary.bin"
_AWKWARD_NO_NEWLINE_PATH = "no_newline.txt"
_AWKWARD_PHOENIX_PATH = "phoenix.txt"

AWKWARD_FILES = frozenset(
    {
        _AWKWARD_CAFE_PATH,
        _AWKWARD_QUOTED_PATH,
        _AWKWARD_EMPTY_PATH,
        _AWKWARD_BINARY_PATH,
        _AWKWARD_NO_NEWLINE_PATH,
        _AWKWARD_PHOENIX_PATH,
    }
)

# The porcelain-lookalike line: begins with exactly one of the real
# `git blame --line-porcelain` header keywords followed by a space
# ("author "), and is plain file content - not tab-prefixed in the
# source blob. See the issue #10 grooming comment: this is precisely
# the case a parser keying off `line.startswith("author ")` without
# first checking for a leading tab misparses.
AWKWARD_LOOKALIKE_LINE = "author nobody@nowhere.example claims to be a porcelain header but is not"

_AWKWARD_CAFE_INITIAL = (
    "# café.py - awkward fixture content\n"
    "def greet():\n"
    f'    return "café"\n'
    f"{AWKWARD_LOOKALIKE_LINE}\n"
)
_AWKWARD_CAFE_APPENDED = "# appended by Sam\nprint(greet())\n"

_AWKWARD_QUOTED_CONTENT = "content of the file with a space and a quote in its name\n"

# Starts with a NUL byte, which git's binary-detection heuristic
# samples from the first bytes of a blob - so this is unambiguously
# binary-detected regardless of core.autocrlf. Fixed, not random: a
# fixture must be byte-identical on every run.
AWKWARD_BINARY_CONTENT = (
    bytes([0x00, 0x01, 0x02, 0x03, 0xDE, 0xAD, 0xBE, 0xEF])
    + b"not-a-real-image"
    + bytes([0xFF, 0xFE, 0x00, 0x7F])
)

AWKWARD_NO_NEWLINE_CONTENT = b"first line\nsecond line\nthird line without a trailing newline"

AWKWARD_PHOENIX_ORIGINAL = "phoenix: version one\n"
AWKWARD_PHOENIX_RECREATED = "phoenix: reborn, version two\n"

# Filled in once, from this builder's own first deterministic build,
# and then pinned - see the note on TINY_HEAD above; the same applies
# here.
AWKWARD_HEAD = "c19c831743450c1c0bdf0a5d5096e2c25fa104f1"


def build_awkward(dest: Path) -> Path:
    """Build the `awkward` fixture at dest, verify it, and return dest.

    Raises FixtureError if the built repository does not match what
    this function was told to build, including a determinism
    regression against the pinned AWKWARD_HEAD.
    """
    if dest.exists():
        shutil.rmtree(dest)
    _init_repo(dest, branch="main")
    clock = _Clock()

    (dest / _AWKWARD_CAFE_PATH).write_text(_AWKWARD_CAFE_INITIAL)
    (dest / _AWKWARD_QUOTED_PATH).write_text(_AWKWARD_QUOTED_CONTENT)
    (dest / _AWKWARD_EMPTY_PATH).write_bytes(b"")
    (dest / _AWKWARD_BINARY_PATH).write_bytes(AWKWARD_BINARY_CONTENT)
    (dest / _AWKWARD_NO_NEWLINE_PATH).write_bytes(AWKWARD_NO_NEWLINE_CONTENT)
    (dest / _AWKWARD_PHOENIX_PATH).write_text(AWKWARD_PHOENIX_ORIGINAL)
    _run_git(dest, ["add", "-A"])
    _commit(dest, "Add the awkward fixture's initial files", author=_AWKWARD_ZOE, timestamp=clock.tick())

    with (dest / _AWKWARD_CAFE_PATH).open("a") as f:
        f.write(_AWKWARD_CAFE_APPENDED)
    _run_git(dest, ["rm", "--quiet", _AWKWARD_PHOENIX_PATH])
    _run_git(dest, ["add", "-A"])
    _commit(dest, "Edit café.py, remove phoenix.txt", author=_AWKWARD_SAM, timestamp=clock.tick())

    (dest / _AWKWARD_PHOENIX_PATH).write_text(AWKWARD_PHOENIX_RECREATED)
    _run_git(dest, ["add", "-A"])
    _commit(dest, "", author=_AWKWARD_SAM, timestamp=clock.tick(), allow_empty_message=True)

    _verify_awkward(dest)

    head = _run_git(dest, ["rev-parse", "HEAD"]).strip()
    if head != AWKWARD_HEAD:
        raise FixtureError(
            f"awkward: HEAD is {head}, pinned AWKWARD_HEAD is {AWKWARD_HEAD} - "
            "this is a determinism regression, not a content change, "
            "unless build_awkward was deliberately edited (update the "
            "pinned constant in the same commit if so)"
        )
    return dest


def _verify_awkward(repo: Path) -> None:
    """Assert that repo matches what build_awkward is supposed to build.

    Inspects only the finished repository through git itself, per the
    same reasoning as _verify_tiny above.
    """
    count = int(_run_git(repo, ["rev-list", "--count", "HEAD"]).strip())
    if count != 3:
        raise FixtureError(f"awkward: expected exactly 3 commits, found {count}")

    identities = set(_run_git(repo, ["log", "--format=%an <%ae>"]).strip().splitlines())
    if len(identities) != 2:
        raise FixtureError(f"awkward: expected exactly 2 author identities, found {identities}")
    if not any("Zoë Müller" in identity for identity in identities):
        raise FixtureError(f"awkward: expected an author with a unicode name, found {identities}")

    raw = _run_git(repo, ["ls-files", "-z"])
    paths = {p for p in raw.split("\0") if p}
    missing = AWKWARD_FILES - paths
    if missing:
        raise FixtureError(f"awkward: missing required file(s): {sorted(missing)}")
    extra = paths - AWKWARD_FILES
    if extra:
        raise FixtureError(f"awkward: unexpected tracked file(s): {sorted(extra)}")

    empty_size = int(_run_git(repo, ["cat-file", "-s", f"HEAD:{_AWKWARD_EMPTY_PATH}"]).strip())
    if empty_size != 0:
        raise FixtureError(f"awkward: {_AWKWARD_EMPTY_PATH} is not empty ({empty_size} bytes)")

    binary_content = _run_git_bytes(repo, ["cat-file", "-p", f"HEAD:{_AWKWARD_BINARY_PATH}"])
    if binary_content != AWKWARD_BINARY_CONTENT:
        raise FixtureError(f"awkward: {_AWKWARD_BINARY_PATH}'s committed blob does not match its source bytes")

    no_newline_content = _run_git_bytes(repo, ["cat-file", "-p", f"HEAD:{_AWKWARD_NO_NEWLINE_PATH}"])
    if no_newline_content != AWKWARD_NO_NEWLINE_CONTENT:
        raise FixtureError(f"awkward: {_AWKWARD_NO_NEWLINE_PATH}'s committed blob does not match its source bytes")
    if no_newline_content.endswith(b"\n"):
        raise FixtureError(f"awkward: {_AWKWARD_NO_NEWLINE_PATH} has a trailing newline, expected none")

    phoenix_content = _run_git(repo, ["show", f"HEAD:{_AWKWARD_PHOENIX_PATH}"])
    if phoenix_content != AWKWARD_PHOENIX_RECREATED:
        raise FixtureError(
            f"awkward: {_AWKWARD_PHOENIX_PATH} at HEAD is not the recreation "
            f"(expected {AWKWARD_PHOENIX_RECREATED!r}, got {phoenix_content!r})"
        )
    deletions = _run_git(
        repo, ["log", "--all", "--diff-filter=D", "--format=%H", "--", _AWKWARD_PHOENIX_PATH]
    ).strip()
    if not deletions:
        raise FixtureError(f"awkward: {_AWKWARD_PHOENIX_PATH} was never deleted in history")

    hashes = _run_git(repo, ["log", "--format=%H"]).strip().splitlines()
    messages = [_run_git(repo, ["log", "-1", "--format=%B", h]).strip("\n") for h in hashes]
    if "" not in messages:
        raise FixtureError("awkward: expected one commit with an empty message")

    cafe_source = _run_git(repo, ["cat-file", "-p", f"HEAD:{_AWKWARD_CAFE_PATH}"])
    if AWKWARD_LOOKALIKE_LINE not in cafe_source.splitlines():
        raise FixtureError(f"awkward: the porcelain-lookalike line is missing from {_AWKWARD_CAFE_PATH}")
    for line in cafe_source.splitlines():
        if line == AWKWARD_LOOKALIKE_LINE and line.startswith("\t"):
            raise FixtureError("awkward: the porcelain-lookalike line must not start with a tab in the source")

    porcelain = _run_git(repo, ["blame", "--line-porcelain", _AWKWARD_CAFE_PATH])
    tab_content_lines = [line[1:] for line in porcelain.splitlines() if line.startswith("\t")]
    if AWKWARD_LOOKALIKE_LINE not in tab_content_lines:
        raise FixtureError(
            "awkward: the porcelain-lookalike line did not appear as a "
            "tab-prefixed content line in git blame --line-porcelain output"
        )


# ---------------------------------------------------------------------------
# Caching
#
# tiny and awkward are rebuilt exactly once per change to this file,
# not once per test run - the whole suite is run on every engineer and
# QA pass (_docs/process.md), and rebuilding a handful of commits is
# proven idempotent above, so paying for it every time is pure waste.
#
# Built under a path inside tests/fixtures/ rather than a system temp
# directory: that survives between runs (what makes caching worth
# anything) and can be cd-ed into for manual debugging. Never
# committed as binaries (spec.md §4) - see .gitignore.
# ---------------------------------------------------------------------------

CACHE_DIR = Path(__file__).parent / "_built"


def _builder_digest() -> str:
    """A digest of this file's own source, used to invalidate a cached
    build when build.py itself changes - a version constant bumped by
    hand can be forgotten; hashing the file it would have needed to
    change to be forgotten cannot."""
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _cached(cache_dir: Path, name: str, builder) -> Path:
    repo = cache_dir / name
    digest_file = cache_dir / f"{name}.digest"
    digest = _builder_digest()
    if repo.exists() and digest_file.exists() and digest_file.read_text() == digest:
        return repo
    builder(repo)
    digest_file.parent.mkdir(parents=True, exist_ok=True)
    digest_file.write_text(digest)
    return repo


def get_tiny_repo(cache_dir: Path = CACHE_DIR) -> Path:
    """Return the path to a built `tiny` repository, building (or
    rebuilding, if build.py has changed since the cached one) it first
    if necessary."""
    return _cached(cache_dir, "tiny", build_tiny)


def get_awkward_repo(cache_dir: Path = CACHE_DIR) -> Path:
    """Return the path to a built `awkward` repository, building (or
    rebuilding, if build.py has changed since the cached one) it first
    if necessary."""
    return _cached(cache_dir, "awkward", build_awkward)


if __name__ == "__main__":
    # Manual invocation for debugging: `uv run python -m tests.fixtures.build`
    # (or `python tests/fixtures/build.py` from the repo root) builds
    # both fixtures into the cache directory and prints where they
    # landed, so they can be inspected or cd-ed into by hand.
    for repo_name, getter in (("tiny", get_tiny_repo), ("awkward", get_awkward_repo)):
        path = getter()
        head = _run_git(path, ["rev-parse", "HEAD"]).strip()
        print(f"{repo_name}: {path} (HEAD {head})")
