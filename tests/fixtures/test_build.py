"""Tests for tests/fixtures/build.py.

Implements the isolation and determinism testing that spec.md §4 and
_docs/decisions.md (2026-09-01, "Fixture builds isolate from all
ambient git configuration") require: a fixture built twice must be
byte-identical, and a fixture built under a hostile ambient git
configuration must still be byte-identical to one built with no
ambient configuration at all.

This file tests the isolation *mechanism* directly, against small
throwaway repositories, before the full tiny/awkward content-level
tests exercise it through build_tiny/build_awkward.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from fixtures import build


# ---------------------------------------------------------------------------
# Isolation primitives
# ---------------------------------------------------------------------------


def _write_repo(path: Path, *, branch: str = "main") -> str:
    """Build a minimal one-commit repo under full isolation, return HEAD."""
    build._init_repo(path, branch=branch)
    (path / "a.txt").write_text("hello\n")
    build._run_git(path, ["add", "-A"])
    return build._commit(
        path,
        "initial commit",
        author=("Test Author", "author@example.com"),
        timestamp=build._Clock().tick(),
    )


def test_isolated_env_strips_ambient_git_config_pointers(monkeypatch, tmp_path):
    """A hostile GIT_CONFIG_GLOBAL in the parent process must not reach
    the child git process: build._isolated_env always sets its own."""
    hostile = tmp_path / "hostile-global.gitconfig"
    hostile.write_text("[core]\n\tautocrlf = true\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(hostile))

    env = build._isolated_env({})

    assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert env["GIT_CONFIG_SYSTEM"] == "/dev/null"
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"


def test_isolated_env_strips_unrelated_ambient_git_vars(monkeypatch):
    """Any other ambient GIT_* variable (e.g. a leftover GIT_AUTHOR_NAME
    from a parent process) must not leak into the child either."""
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Someone Else")
    monkeypatch.setenv("GIT_DIR", "/nonexistent")

    env = build._isolated_env({})

    assert "GIT_AUTHOR_NAME" not in env
    assert "GIT_DIR" not in env


def test_init_repo_uses_explicit_branch_not_ambient_default(tmp_path):
    """git init must be told -b main explicitly; §4's finding is that
    relying on init.defaultBranch is not portable."""
    repo = tmp_path / "repo"
    build._init_repo(repo, branch="main")

    branch = build._run_git(repo, ["branch", "--show-current"]).strip()
    assert branch == "main"


def test_a_freshly_built_repo_has_no_global_or_system_config(tmp_path):
    """git config --list --show-origin inside a fresh fixture repo shows
    only local config the builder set - nothing from a global or system
    scope. This is the direct acceptance test for isolation."""
    repo = tmp_path / "repo"
    _write_repo(repo)

    origins = build._run_git(repo, ["config", "--list", "--show-origin"])
    for line in origins.splitlines():
        origin = line.split("\t", 1)[0]
        assert origin.startswith("file:") and origin.endswith(".git/config"), (
            f"config value leaked from a non-local scope: {line}"
        )


def test_hostile_ambient_config_does_not_change_the_commit_hash(monkeypatch, tmp_path):
    """Building with a fake global config forcing core.autocrlf=true and
    commit.gpgsign=true, and a fake system config forcing a different
    default branch name, must produce the exact same HEAD as building
    with no config files present at all."""
    clean_head = _write_repo(tmp_path / "clean")

    hostile_global = tmp_path / "hostile-global.gitconfig"
    hostile_global.write_text("[core]\n\tautocrlf = true\n[commit]\n\tgpgsign = true\n")
    hostile_system = tmp_path / "hostile-system.gitconfig"
    hostile_system.write_text("[init]\n\tdefaultBranch = something-else\n")

    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(hostile_global))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(hostile_system))

    hostile_head = _write_repo(tmp_path / "hostile")

    assert hostile_head == clean_head


def test_hostile_ambient_config_does_not_change_the_branch_name(monkeypatch, tmp_path):
    hostile_system = tmp_path / "hostile-system.gitconfig"
    hostile_system.write_text("[init]\n\tdefaultBranch = something-else\n")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(hostile_system))

    repo = tmp_path / "repo"
    build._init_repo(repo, branch="main")

    branch = build._run_git(repo, ["branch", "--show-current"]).strip()
    assert branch == "main"


def test_hostile_excludes_file_does_not_drop_a_fixture_file(monkeypatch, tmp_path):
    """§4's finding: a global core.excludesFile matching a fixture path
    can silently drop it from `git add -A` with no error. Isolation
    must defeat this too."""
    excludes = tmp_path / "hostile-excludes"
    excludes.write_text("*.txt\n")
    hostile_global = tmp_path / "hostile-global.gitconfig"
    hostile_global.write_text(f"[core]\n\texcludesFile = {excludes}\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(hostile_global))

    repo = tmp_path / "repo"
    build._init_repo(repo, branch="main")
    (repo / "keep-me.txt").write_text("must survive add -A\n")
    build._run_git(repo, ["add", "-A"])

    tracked = build._run_git(repo, ["ls-files"]).strip().splitlines()
    assert "keep-me.txt" in tracked


def test_hostile_gpgsign_does_not_break_the_commit(monkeypatch, tmp_path):
    """A contributor with commit.gpgsign=true globally and no signing
    key configured must not see fixture builds fail."""
    hostile_global = tmp_path / "hostile-global.gitconfig"
    hostile_global.write_text("[commit]\n\tgpgsign = true\n[gpg]\n\tprogram = /nonexistent-gpg\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(hostile_global))

    # Must not raise.
    _write_repo(tmp_path / "repo")


def test_building_twice_is_byte_identical(tmp_path):
    """The direct determinism test the issue calls for: build the same
    fixture into two different empty directories and diff everything -
    HEAD, tree, and the full object set."""
    head_a = _write_repo(tmp_path / "a")
    head_b = _write_repo(tmp_path / "b")

    assert head_a == head_b

    objects_a = build._run_git(tmp_path / "a", ["rev-list", "--objects", "--all"])
    objects_b = build._run_git(tmp_path / "b", ["rev-list", "--objects", "--all"])
    assert objects_a == objects_b
