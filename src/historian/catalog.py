"""The table catalog: the one place a table gets wired into the query
engine, for both `sql/binder.py` (which wants a `Schema` per table)
and `plan/planner.py` (which wants a `ScanFactory` per table).

Issue #35, folding in #52. Before this issue, `sql/binder.py` and
`plan/planner.py` each hardcoded their own real-table default -
`bind(stmt, catalog={"blame": BLAME_SCHEMA})`, `plan(stmt, repo,
tables={"blame": BlameScan})` - built by importing `historian.tables.
blame` directly. That put `subprocess` (which `tables/blame.py`
imports at module level to shell out to `git`) into `sys.modules` the
moment *either* module was merely imported, never mind run, violating
`AGENTS.md`'s "the parser, the planner and the executor are plain
Python with no git and no subprocess imports." It also meant the
binder's and the planner's two catalogs were two independently
maintained dicts with nothing to stop them naming a different set of
tables (#52) - a phase-2 table added to one and not the other would
fail as a bare `KeyError` reaching a user, not a clean error (see #49).

This module is the fix for both, at once: `TABLES` is one literal
dict, built from direct imports of each table module, and `SCHEMAS`/
`SCAN_FACTORIES` are dict comprehensions *over* `TABLES.items()` -
views derived from one shared source, not two hand-kept copies. They
cannot list different table names from each other by construction,
which is what a hand-kept pair can never guarantee no matter how
carefully a test checks them today. Adding one of §2's phase 2 tables
(`commits`, `commit_files`, `refs`, `tree`) later is one import line
plus one `TABLES` entry here - both derived views pick it up with no
other change anywhere.

`sql/binder.py` and `plan/planner.py` never import this module,
`historian.tables.*`, or `subprocess`, directly or indirectly -
`historian/catalog.py` is imported only from `cli.py` (the
composition root that wires the real catalog into the pipeline) and
from tests that want the real `blame` table rather than a fake
`ScanSource`. Importing *this* module still imports `subprocess`
transitively, and that is expected, not a bug: something concrete has
to name every table's schema and scan class, and confining that
import to one module - reached only from the one place a real query
actually runs - is the fix, not making the import disappear. See
`_docs/decisions.md` for the two designs considered and rejected
(lazy `import subprocess` inside `tables/blame.py`; scans
self-registering into a catalog by import side effect) and why.

No self-registration, deliberately
------------------------------------

A table module does not add itself to `TABLES` by any mechanism run
at its own import time - this module imports each table module by
name and lists it in `TABLES` explicitly. `AGENTS.md`'s "no
metaclasses, no dynamic dispatch tricks, no clever descriptors...
portable to Rust later" rule rules out a registry populated by import
side effects: a Rust port has no equivalent for "importing a module
has the side effect of registering it into a global table" without
reaching for something like the `inventory` or `ctor` crates,
themselves considered a smell in idiomatic Rust for exactly this
reason. A literal dict built from direct imports translates directly
to a `HashMap` or `match` built once in an equivalent `catalog.rs`.

It also avoids an import-order hazard by construction, not by
convention: `TABLES` is one literal expression, evaluated once, top to
bottom, the first time anything imports this module. There is no
mutable global that table modules append to over time, so there is no
window in which `SCHEMAS` or `SCAN_FACTORIES` could be read only
partially populated.
"""

from __future__ import annotations

from dataclasses import dataclass

from historian.plan.planner import ScanFactory
from historian.schema import Schema
from historian.tables.blame import BLAME_SCHEMA, BlameScan

__all__ = ["SCAN_FACTORIES", "SCHEMAS", "TABLES", "TableDef"]


@dataclass(frozen=True)
class TableDef:
    """One table's full entry: the `Schema` the binder resolves
    columns against, and the `ScanFactory` the planner builds a
    `Scan` from. Pairing them in one `TABLES` value - rather than two
    separate dicts someone keeps in sync by hand - is what makes
    `SCHEMAS` and `SCAN_FACTORIES` below views derived from one source
    instead of copies of it."""

    schema: Schema
    scan_factory: ScanFactory


#: The table catalog: FROM-clause name -> `TableDef`. Phase 1
#: (`_docs/spec.md` §2) has exactly one table. Adding a phase 2 table
#: here is one import line above plus one entry in this dict - nothing
#: else changes, in this module or any caller of `SCHEMAS`/
#: `SCAN_FACTORIES`.
TABLES: dict[str, TableDef] = {
    "blame": TableDef(schema=BLAME_SCHEMA, scan_factory=BlameScan),
}

#: `sql/binder.py`'s view: FROM-clause name -> `Schema`. A
#: comprehension over `TABLES.items()`, not a second hand-maintained
#: dict - see the module docstring.
SCHEMAS: dict[str, Schema] = {name: table.schema for name, table in TABLES.items()}

#: `plan/planner.py`'s view: FROM-clause name -> `ScanFactory`. Same
#: derivation as `SCHEMAS` above, from the same `TABLES.items()`, which
#: is what makes the two unable to diverge (#52): they are always
#: built from identical keys by construction, not merely checked
#: against each other after the fact.
SCAN_FACTORIES: dict[str, ScanFactory] = {name: table.scan_factory for name, table in TABLES.items()}
