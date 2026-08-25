"""The `historian` command-line entry point.

This is a stub: it takes no arguments and prints a version string.
The real command line (`-C`, `-f`, `--format`, `--explain`, `--stats`,
`--no-pushdown`, the REPL) starts at issue #9 and grows through later
issues in M2/M4/M6.
"""

from historian import __version__


def main() -> int:
    print(f"historian {__version__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
