"""Shared pytest fixtures for historian's test suite.

Exposes the `tiny` and `awkward` fixture repositories built by
tests/fixtures/build.py (spec.md §4) as session-scoped pytest
fixtures. Session scope: nothing in v1 mutates a fixture repo once
built - every query historian runs is read-only, per §1's non-goals -
so rebuilding per test would only cost time for no isolation benefit.

`large` (spec.md §4) is a benchmark fixture and is opt-in: the
`large_repo` fixture skips unless pytest is run with `--build-large`
(#27), so a plain `uv run pytest` never pays for building it.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

from fixtures.build import (
    get_awkward_repo,
    get_casefold_repo,
    get_large_repo,
    get_numeric_repo,
    get_tiny_repo,
)


#: The one place the oracle's SQLite version is written down (#117). The
#: oracle is Python's bundled `sqlite3` module (`_docs/process.md`, "The
#: oracle"), and which SQLite that is follows the Python build, so the
#: repository pins a uv-managed CPython (`.python-version` and
#: `python-preference = "only-managed"` in `pyproject.toml`) and the
#: session aborts below if the running module is anything else. Whoever
#: changes the pin changes this constant and re-runs the suite in the
#: same commit. There is deliberately no way to bypass the check.
EXPECTED_ORACLE_SQLITE_VERSION = "3.50.4"


def pytest_sessionstart(session: pytest.Session) -> None:
    """Abort the whole run, before anything is collected, when the
    oracle is not the expected SQLite - a hard abort, not a skip."""
    if sqlite3.sqlite_version != EXPECTED_ORACLE_SQLITE_VERSION:
        raise pytest.UsageError(
            f"the oracle must be sqlite3 {EXPECTED_ORACLE_SQLITE_VERSION}, "
            f"but this Python's sqlite3 module is {sqlite3.sqlite_version}.\n"
            f"  sys.version:    {sys.version}\n"
            f"  sys.executable: {sys.executable}\n"
            "Run `uv sync` (and `uv run pytest`) so the pinned managed Python is "
            'used; see `_docs/process.md`, "The oracle".'
        )


def pytest_report_header(config: pytest.Config) -> str:
    return f"oracle: sqlite3 {sqlite3.sqlite_version} (python {sys.version.split()[0]})"


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--build-large",
        action="store_true",
        default=False,
        help="build the `large` benchmark fixture (hundreds of commits) and run tests that need it",
    )


@pytest.fixture(scope="session")
def tiny_repo() -> Path:
    return get_tiny_repo()


@pytest.fixture(scope="session")
def awkward_repo() -> Path:
    return get_awkward_repo()


@pytest.fixture(scope="session")
def casefold_repo() -> Path:
    """Paths differing only by ASCII or non-ASCII letter case, plus one
    spelled `5` - built for `blame`'s `path` pushdown tests (#122)."""
    return get_casefold_repo()


@pytest.fixture(scope="session")
def numeric_repo() -> Path:
    """`line_no` values of one, two and three digits over two authors
    and three files, so numeric and text order disagree on a bare
    `line_no` column (#109)."""
    return get_numeric_repo()


@pytest.fixture(scope="session")
def large_repo(request: pytest.FixtureRequest) -> Path:
    """The generated benchmark fixture (#27). Skipped unless
    `--build-large` is given. Benchmarks only, never correctness."""
    if not request.config.getoption("--build-large"):
        pytest.skip("the large fixture is opt-in: pass --build-large")
    return get_large_repo()
