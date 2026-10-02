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

from pathlib import Path

import pytest

from fixtures.build import (
    get_awkward_repo,
    get_casefold_repo,
    get_large_repo,
    get_numeric_repo,
    get_tiny_repo,
)


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
