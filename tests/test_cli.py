"""Tests for historian.cli: the entry point invoked as `uv run historian`."""

import historian
from historian import cli


def test_main_prints_version_and_returns_zero(capsys):
    """Calling the entry point in-process (not via subprocess) must
    print the version string to stdout and return exit code 0."""
    ret = cli.main()

    captured = capsys.readouterr()
    assert captured.out == f"historian {historian.__version__}\n"
    assert ret == 0
