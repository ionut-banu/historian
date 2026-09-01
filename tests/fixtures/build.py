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
