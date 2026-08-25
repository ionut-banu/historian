"""Tests for the historian package itself: version metadata."""

import tomllib
from pathlib import Path

import historian


def test_version_matches_pyproject():
    """historian.__version__ must match the version declared in
    pyproject.toml, not just some hardcoded string that can drift."""
    pyproject = Path(__file__).parent.parent / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text())
    expected = data["project"]["version"]

    assert historian.__version__ == expected
