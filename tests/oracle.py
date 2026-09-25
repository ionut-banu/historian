"""Ad hoc oracle helper (issue #93). Runs a query, and optional setup
SQL, through Python's bundled sqlite3 module - the same module
tests/differential/conftest.py uses to build the harness's SQLite
side. Every float result prints as both repr() and .hex(), so a
check is bit-exact by default instead of depending on the caller
remembering to ask for hex. See _docs/process.md, "The oracle".

Usage:
    uv run python tests/oracle.py "<setup SQL>" "<query SQL>"
    uv run python tests/oracle.py "<query SQL>"

Not a test: no test_ prefix, no test_* functions, not collected by
pytest.
"""

import sqlite3
import sys

args = sys.argv[1:]
setup, query = (None, args[0]) if len(args) == 1 else (args[0], args[1])

con = sqlite3.connect(":memory:")
if setup:
    con.executescript(setup)

for row in con.execute(query):
    for value in row:
        if isinstance(value, float):
            print(f"{value!r} {value.hex()}")
        else:
            print(repr(value))

print(sqlite3.sqlite_version)
