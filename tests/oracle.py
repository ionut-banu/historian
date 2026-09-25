"""Ad hoc oracle helper (issue #93). Runs a query, and optional setup
SQL, through Python's bundled sqlite3 module - the same module
tests/differential/conftest.py uses to build the harness's SQLite
side. Every float result prints as both repr() and .hex(), so a
check is bit-exact by default instead of depending on the caller
remembering to ask for hex. See _docs/process.md, "The oracle".

A value written into the query or setup text goes through SQLite's
own literal parser, which is not correctly rounded on this platform
for some large-exponent, 17-digit floats (_docs/decisions.md,
2026-09-25). A value passed here as a bind argument is bound with
`?` and reaches SQLite as Python's exact double, untouched - use
bind arguments when the check must see the exact float historian
would compute, not whatever SQLite's parser derives from typing it
out as a literal.

Usage:
    uv run python tests/oracle.py "<query SQL>"
    uv run python tests/oracle.py "<setup SQL>" "<query SQL>"
    uv run python tests/oracle.py "<setup SQL>" "<query SQL>" <bind>...

Not a test: no test_ prefix, no test_* functions, not collected by
pytest.
"""

import sqlite3
import sys


def _coerce(arg: str):
    try:
        return int(arg)
    except ValueError:
        pass
    try:
        return float(arg)
    except ValueError:
        return arg


args = sys.argv[1:]
if len(args) == 1:
    setup, query, binds = None, args[0], ()
else:
    setup, query = args[0], args[1]
    binds = tuple(_coerce(a) for a in args[2:])

con = sqlite3.connect(":memory:")
if setup:
    con.executescript(setup)

for row in con.execute(query, binds):
    for value in row:
        if isinstance(value, float):
            print(f"{value!r} {value.hex()}")
        else:
            print(repr(value))

print(sqlite3.sqlite_version)
