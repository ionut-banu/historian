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


# ---------------------------------------------------------------------------
# tiny
#
# A handful of commits, two authors, a rename, a deletion, and a merge.
# See spec.md §4 and the issue #10 grooming comment for exact content.
# ---------------------------------------------------------------------------


def test_tiny_branch_is_main(tmp_path):
    repo = build.build_tiny(tmp_path / "tiny")
    branch = build._run_git(repo, ["branch", "--show-current"]).strip()
    assert branch == "main"


def test_tiny_commit_count_is_minimal(tmp_path):
    """The builder asserts the exact count so a later accidental addition
    or removal fails loudly, per the grooming comment."""
    repo = build.build_tiny(tmp_path / "tiny")
    count = int(build._run_git(repo, ["rev-list", "--count", "HEAD"]).strip())
    assert count == 5


def test_tiny_has_exactly_one_merge_commit_with_two_parents(tmp_path):
    repo = build.build_tiny(tmp_path / "tiny")
    merges = build._run_git(repo, ["log", "--all", "--merges", "--format=%H"]).strip().splitlines()
    assert len(merges) == 1
    parents = build._run_git(repo, ["log", "-1", "--format=%P", merges[0]]).strip().split()
    assert len(parents) == 2


def test_tiny_has_two_distinct_author_identities(tmp_path):
    repo = build.build_tiny(tmp_path / "tiny")
    identities = set(build._run_git(repo, ["log", "--format=%an <%ae>"]).strip().splitlines())
    assert len(identities) == 2


def test_tiny_rename_preserves_blame_attribution_at_head(tmp_path):
    """A line predating the rename is still attributed by git blame on
    the post-rename path at HEAD, to the original (root) commit - the
    case this fixture requirement exists to exercise."""
    repo = build.build_tiny(tmp_path / "tiny")

    root = build._run_git(repo, ["rev-list", "--max-parents=0", "HEAD"]).strip()
    assert "\n" not in root, "expected exactly one root commit"

    porcelain = build._run_git(repo, ["blame", "--line-porcelain", "src/utils.py"])
    commit_hashes = {
        line.split(" ", 1)[0]
        for line in porcelain.splitlines()
        if len(line) >= 40 and all(c in "0123456789abcdef" for c in line[:40]) and line[:1] != "\t"
    }
    assert commit_hashes == {root}


def test_tiny_old_path_absent_and_new_path_present_at_head(tmp_path):
    repo = build.build_tiny(tmp_path / "tiny")
    paths = build._run_git(repo, ["ls-tree", "-r", "--name-only", "HEAD"]).splitlines()
    assert "src/util.py" not in paths
    assert "src/utils.py" in paths


def test_tiny_deleted_file_absent_from_head_but_existed_earlier(tmp_path):
    repo = build.build_tiny(tmp_path / "tiny")
    head_paths = build._run_git(repo, ["ls-tree", "-r", "--name-only", "HEAD"]).splitlines()
    assert "docs/todo.md" not in head_paths

    history = build._run_git(repo, ["log", "--all", "--format=%H", "--", "docs/todo.md"]).strip()
    assert history != "", "docs/todo.md must have existed in some earlier commit"


def test_tiny_is_deterministic_across_independent_builds(tmp_path):
    head_a = build.build_tiny(tmp_path / "a")
    head_b = build.build_tiny(tmp_path / "b")
    assert build._run_git(head_a, ["rev-parse", "HEAD"]) == build._run_git(head_b, ["rev-parse", "HEAD"])

    objects_a = build._run_git(tmp_path / "a", ["rev-list", "--objects", "--all"])
    objects_b = build._run_git(tmp_path / "b", ["rev-list", "--objects", "--all"])
    assert objects_a == objects_b


def test_tiny_head_matches_pinned_hash(tmp_path):
    """The strongest, cheapest regression check available: a determinism
    regression fails this immediately, with no need to reproduce anyone
    else's machine."""
    repo = build.build_tiny(tmp_path / "tiny")
    assert build._run_git(repo, ["rev-parse", "HEAD"]).strip() == build.TINY_HEAD


def test_verify_tiny_raises_when_merge_commit_is_missing(tmp_path):
    """The builder asserts what it built: if a fixture silently stopped
    containing its merge commit, build-time verification must catch it,
    not a downstream test. Simulated here by directly building a
    mutated repo - same commit count, no merge - the way a broken
    build_tiny might, and confirming _verify_tiny names the problem."""
    repo = tmp_path / "no-merge"
    build._init_repo(repo, branch="main")
    clock = build._Clock()
    for i in range(5):
        (repo / f"file{i}.txt").write_text(f"content {i}\n")
        build._run_git(repo, ["add", "-A"])
        build._commit(repo, f"commit {i}", author=("Ana Petrova", "ana@example.com"), timestamp=clock.tick())

    with pytest.raises(build.FixtureError, match="merge"):
        build._verify_tiny(repo)


def test_verify_tiny_raises_when_a_required_file_is_dropped(tmp_path):
    """A different way a broken builder could silently fail: the
    excludes-file hazard from §4 dropping a required fixture file. Here
    a repo has the right shape - 5 commits, one 2-parent merge, two
    authors - but src/utils.py never made it in, the way a hostile
    excludesFile silently dropping a file would look. Verification
    must say so rather than passing an incomplete fixture through."""
    repo = tmp_path / "missing-file"
    build._init_repo(repo, branch="main")
    clock = build._Clock()
    ana = ("Ana Petrova", "ana@example.com")
    bo = ("Bo Lindqvist", "bo@example.com")

    (repo / "f0.txt").write_text("zero\n")
    build._run_git(repo, ["add", "-A"])
    build._commit(repo, "commit 0", author=ana, timestamp=clock.tick())

    (repo / "f1.txt").write_text("one\n")
    build._run_git(repo, ["add", "-A"])
    build._commit(repo, "commit 1", author=bo, timestamp=clock.tick())

    build._run_git(repo, ["checkout", "--quiet", "-b", "feature"])
    (repo / "f2.txt").write_text("two\n")
    build._run_git(repo, ["add", "-A"])
    build._commit(repo, "commit 2", author=ana, timestamp=clock.tick())

    build._run_git(repo, ["checkout", "--quiet", "main"])
    (repo / "f3.txt").write_text("three\n")
    build._run_git(repo, ["add", "-A"])
    build._commit(repo, "commit 3", author=bo, timestamp=clock.tick())

    build._merge(repo, "feature", "merge", author=ana, timestamp=clock.tick())

    with pytest.raises(build.FixtureError, match="utils.py"):
        build._verify_tiny(repo)


# ---------------------------------------------------------------------------
# awkward
#
# "built to break things" - spec.md §4. One item per required property,
# per the issue #10 grooming comment.
# ---------------------------------------------------------------------------


def test_awkward_branch_is_main(tmp_path):
    repo = build.build_awkward(tmp_path / "awkward")
    branch = build._run_git(repo, ["branch", "--show-current"]).strip()
    assert branch == "main"


def test_awkward_commit_count_is_minimal(tmp_path):
    repo = build.build_awkward(tmp_path / "awkward")
    count = int(build._run_git(repo, ["rev-list", "--count", "HEAD"]).strip())
    assert count == 3


def test_awkward_exact_file_list(tmp_path):
    repo = build.build_awkward(tmp_path / "awkward")
    raw = build._run_git(repo, ["ls-files", "-z"])
    paths = set(raw.split("\0")) - {""}
    assert paths == build.AWKWARD_FILES


def test_awkward_unicode_path_is_tracked(tmp_path):
    repo = build.build_awkward(tmp_path / "awkward")
    paths = set(build._run_git(repo, ["ls-files", "-z"]).split("\0")) - {""}
    assert "café.py" in paths


def test_awkward_space_and_quote_path_is_tracked(tmp_path):
    repo = build.build_awkward(tmp_path / "awkward")
    paths = set(build._run_git(repo, ["ls-files", "-z"]).split("\0")) - {""}
    assert any(" " in p and '"' in p for p in paths)


def test_awkward_unicode_author_name_appears(tmp_path):
    repo = build.build_awkward(tmp_path / "awkward")
    authors = build._run_git(repo, ["log", "--format=%an"])
    assert "Zoë Müller" in authors


def test_awkward_two_distinct_author_identities(tmp_path):
    repo = build.build_awkward(tmp_path / "awkward")
    identities = set(build._run_git(repo, ["log", "--format=%an <%ae>"]).strip().splitlines())
    assert len(identities) == 2


def test_awkward_empty_file_is_zero_bytes(tmp_path):
    repo = build.build_awkward(tmp_path / "awkward")
    size = int(build._run_git(repo, ["cat-file", "-s", "HEAD:empty.txt"]).strip())
    assert size == 0


def test_awkward_binary_blob_is_byte_identical_to_source(tmp_path):
    repo = build.build_awkward(tmp_path / "awkward")
    content = build._run_git_bytes(repo, ["cat-file", "-p", "HEAD:binary.bin"])
    assert content == build.AWKWARD_BINARY_CONTENT
    assert content[0:1] == b"\x00", "the NUL must be within the first bytes git samples"


def test_awkward_no_trailing_newline_file_has_none(tmp_path):
    repo = build.build_awkward(tmp_path / "awkward")
    content = build._run_git_bytes(repo, ["cat-file", "-p", "HEAD:no_newline.txt"])
    assert content == build.AWKWARD_NO_NEWLINE_CONTENT
    assert not content.endswith(b"\n")


def test_awkward_blame_on_no_trailing_newline_file_reports_final_line(tmp_path):
    repo = build.build_awkward(tmp_path / "awkward")
    porcelain = build._run_git(repo, ["blame", "--line-porcelain", "no_newline.txt"])
    content_lines = [line[1:] for line in porcelain.splitlines() if line.startswith("\t")]
    expected_last = build.AWKWARD_NO_NEWLINE_CONTENT.decode().splitlines()[-1]
    assert content_lines[-1] == expected_last


def test_awkward_phoenix_file_at_head_is_the_recreation_not_the_original(tmp_path):
    repo = build.build_awkward(tmp_path / "awkward")
    content = build._run_git(repo, ["show", "HEAD:phoenix.txt"])
    assert content == build.AWKWARD_PHOENIX_RECREATED
    assert content != build.AWKWARD_PHOENIX_ORIGINAL


def test_awkward_phoenix_file_was_genuinely_deleted_in_history(tmp_path):
    """Not a no-op restore: the file was actually absent from some
    commit's tree, so a stale-cache bug reading ls-tree at HEAD only
    would be exposed, per the grooming's "not a no-op restore" note."""
    repo = build.build_awkward(tmp_path / "awkward")
    deletions = build._run_git(
        repo, ["log", "--all", "--diff-filter=D", "--format=%H", "--", "phoenix.txt"]
    ).strip()
    assert deletions != ""


def test_awkward_has_an_empty_commit_message(tmp_path):
    repo = build.build_awkward(tmp_path / "awkward")
    hashes = build._run_git(repo, ["log", "--format=%H"]).strip().splitlines()
    messages = [build._run_git(repo, ["log", "-1", "--format=%B", h]).strip("\n") for h in hashes]
    assert "" in messages


def test_awkward_porcelain_lookalike_line_is_tab_prefixed_content(tmp_path):
    """Verified against real git blame --line-porcelain output: content
    lines are tab-prefixed, header lines never are. The lookalike line
    must appear as tab-prefixed content, and must not itself start with
    a tab in the source blob."""
    repo = build.build_awkward(tmp_path / "awkward")

    source = build._run_git(repo, ["cat-file", "-p", "HEAD:café.py"])
    assert build.AWKWARD_LOOKALIKE_LINE in source.splitlines()
    for line in source.splitlines():
        if line == build.AWKWARD_LOOKALIKE_LINE:
            assert not line.startswith("\t")

    porcelain = build._run_git(repo, ["blame", "--line-porcelain", "café.py"])
    tab_content_lines = [line[1:] for line in porcelain.splitlines() if line.startswith("\t")]
    assert build.AWKWARD_LOOKALIKE_LINE in tab_content_lines


def test_awkward_naive_header_check_without_tab_falsely_matches_lookalike_line(tmp_path):
    """The acceptance test for #11's extraction parser, run here against
    the fixture directly: a parser that strips whitespace and then
    checks header keyword prefixes - without checking for the leading
    tab first - misidentifies our fixture's content line as a header.
    A parser that checks for the tab first does not."""
    repo = build.build_awkward(tmp_path / "awkward")
    porcelain = build._run_git(repo, ["blame", "--line-porcelain", "café.py"])

    naive_keywords = (
        "author ", "author-mail ", "author-time ", "author-tz ",
        "committer ", "committer-mail ", "committer-time ", "committer-tz ",
        "summary ", "filename ", "previous ",
    )
    naive_false_positives = [
        line for line in porcelain.splitlines()
        if line.startswith("\t") and line.lstrip().startswith(naive_keywords)
    ]
    assert any(
        build.AWKWARD_LOOKALIKE_LINE in line for line in naive_false_positives
    ), "the fixture should make a tab-blind naive parser misfire on this line"

    tab_correct_matches = [
        line for line in porcelain.splitlines()
        if (not line.startswith("\t")) and line.startswith(naive_keywords)
    ]
    assert not any(
        build.AWKWARD_LOOKALIKE_LINE in line for line in tab_correct_matches
    ), "a parser that checks for the tab first must not misidentify the content line as a header"


def test_awkward_is_deterministic_across_independent_builds(tmp_path):
    head_a = build.build_awkward(tmp_path / "a")
    head_b = build.build_awkward(tmp_path / "b")
    assert build._run_git(head_a, ["rev-parse", "HEAD"]) == build._run_git(head_b, ["rev-parse", "HEAD"])

    objects_a = build._run_git(tmp_path / "a", ["rev-list", "--objects", "--all"])
    objects_b = build._run_git(tmp_path / "b", ["rev-list", "--objects", "--all"])
    assert objects_a == objects_b


def test_awkward_head_matches_pinned_hash(tmp_path):
    repo = build.build_awkward(tmp_path / "awkward")
    assert build._run_git(repo, ["rev-parse", "HEAD"]).strip() == build.AWKWARD_HEAD


def test_verify_awkward_raises_when_a_file_is_dropped(tmp_path):
    """Simulates exactly the hostile-excludesFile hazard from §4: a
    fixture with the right commit shape (3 commits, 2 authors) but
    missing one required file, the way a silently-dropped file would
    look. Built directly rather than through build_awkward, so the
    commit count stays 3 and the file-presence check is what fires."""
    repo = tmp_path / "missing-file"
    build._init_repo(repo, branch="main")
    clock = build._Clock()
    zoe = ("Zoë Müller", "zoe@example.com")
    sam = ("Sam Lee", "sam@example.com")

    # Every awkward file except empty.txt.
    (repo / "café.py").write_text(build._AWKWARD_CAFE_INITIAL)
    (repo / 'a "quoted" name.txt').write_text(build._AWKWARD_QUOTED_CONTENT)
    (repo / "binary.bin").write_bytes(build.AWKWARD_BINARY_CONTENT)
    (repo / "no_newline.txt").write_bytes(build.AWKWARD_NO_NEWLINE_CONTENT)
    (repo / "phoenix.txt").write_text(build.AWKWARD_PHOENIX_ORIGINAL)
    build._run_git(repo, ["add", "-A"])
    build._commit(repo, "initial", author=zoe, timestamp=clock.tick())

    build._run_git(repo, ["rm", "--quiet", "phoenix.txt"])
    build._commit(repo, "remove phoenix", author=sam, timestamp=clock.tick())

    (repo / "phoenix.txt").write_text(build.AWKWARD_PHOENIX_RECREATED)
    build._run_git(repo, ["add", "-A"])
    build._commit(repo, "", author=sam, timestamp=clock.tick(), allow_empty_message=True)

    with pytest.raises(build.FixtureError, match="empty.txt"):
        build._verify_awkward(repo)


# ---------------------------------------------------------------------------
# Caching
#
# tiny and awkward are cheap enough to build but rebuilding them for
# every test would still add up over a full run repeated on every
# engineer and QA pass. Cached between runs; invalidated when build.py
# itself changes, per the issue #10 grooming comment.
# ---------------------------------------------------------------------------


def test_get_tiny_repo_reuses_a_cached_build(tmp_path):
    cache_dir = tmp_path / "cache"
    repo = build.get_tiny_repo(cache_dir)
    sentinel = repo / ".sentinel"
    sentinel.write_text("still here")

    repo_again = build.get_tiny_repo(cache_dir)

    assert repo_again == repo
    assert sentinel.exists(), "a cached build must not be rebuilt"


def test_get_tiny_repo_rebuilds_when_the_builder_digest_is_stale(tmp_path):
    cache_dir = tmp_path / "cache"
    repo = build.get_tiny_repo(cache_dir)
    sentinel = repo / ".sentinel"
    sentinel.write_text("should be gone after rebuild")
    (cache_dir / "tiny.digest").write_text("a stale digest, not build.py's real one")

    build.get_tiny_repo(cache_dir)

    assert not sentinel.exists(), "a stale builder digest must force a rebuild"


def test_get_awkward_repo_reuses_a_cached_build(tmp_path):
    cache_dir = tmp_path / "cache"
    repo = build.get_awkward_repo(cache_dir)
    sentinel = repo / ".sentinel"
    sentinel.write_text("still here")

    repo_again = build.get_awkward_repo(cache_dir)

    assert repo_again == repo
    assert sentinel.exists(), "a cached build must not be rebuilt"


def test_default_cache_dir_is_gitignored():
    """Fixtures are never committed as binaries (spec.md §4): the
    default cache location must be excluded from the outer historian
    repo, not merely a temp directory nobody happens to add."""
    repo_root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        ["git", "check-ignore", "--quiet", str(build.CACHE_DIR)],
        cwd=repo_root,
    )
    assert result.returncode == 0, f"{build.CACHE_DIR} is not gitignored"


def test_tiny_repo_session_fixture_resolves_to_the_pinned_build(tiny_repo):
    """Wiring test for the session-scoped fixture in tests/conftest.py
    that #11 (and later issues) will consume."""
    assert build._run_git(tiny_repo, ["rev-parse", "HEAD"]).strip() == build.TINY_HEAD


def test_awkward_repo_session_fixture_resolves_to_the_pinned_build(awkward_repo):
    assert build._run_git(awkward_repo, ["rev-parse", "HEAD"]).strip() == build.AWKWARD_HEAD
