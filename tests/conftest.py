"""Shared pytest fixtures for historian's test suite.

Exposes the `tiny` and `awkward` fixture repositories built by
tests/fixtures/build.py (spec.md §4) as session-scoped pytest
fixtures. Session scope: nothing in v1 mutates a fixture repo once
built - every query historian runs is read-only, per §1's non-goals -
so rebuilding per test would only cost time for no isolation benefit.

`large` (spec.md §4) is tracked as its own issue (#27) and is not
exposed here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fixtures.build import get_awkward_repo, get_tiny_repo


@pytest.fixture(scope="session")
def tiny_repo() -> Path:
    return get_tiny_repo()


@pytest.fixture(scope="session")
def awkward_repo() -> Path:
    return get_awkward_repo()
