"""`BoundSelectStatement` -> operator tree: the planner stage of the
pipeline (`_docs/spec.md` §3 - "planner    AST -> operator tree").

Issue #13, closing out M2. Everything below the planner is already
merged: `sql/binder.py` produces a `BoundSelectStatement` with every
column reference resolved to an integer offset, and
`exec/operators.py` (#34) already implements `Scan`, `Filter`,
`Project`. This module's whole job is assembly - deciding which of
those three classes to build, and in what order, from one bound
statement plus a repository path.

One plan representation, not two
----------------------------------

Per §3's own section of that name: v1 has no logical/physical split,
because every logical operation here has exactly one implementation,
so a second tree type plus a translation pass between them would be
ceremony with no decision behind it. `plan()` therefore builds
`exec/operators.py`'s actual `Operator` instances directly - `Scan`,
optionally `Filter`, then `Project` - and returns that tree as-is.
There is no separate optimize/rewrite step (that begins at M4, #13's
own sibling "Scan capability negotiation and predicate splitting" -
unrelated to this issue's number, next milestone's work): the tree
`plan()` returns is exactly what `main()` iterates.

The table -> scan-factory mapping
------------------------------------

Grooming settled this as the one real design decision here, because
phase 2 (`_docs/spec.md` §2: `commits`, `commit_files`, `refs`,
`tree`) repeats it five more times. `TABLES` maps a catalog table name
to a *factory* - `Callable[[Path], ScanSource]` - not to a
pre-constructed scan, because a scan needs the repository path and
that path is not known until `plan()` is called. `plan()` takes this
mapping as a parameter with a default, mirroring `sql/binder.py`'s own
`bind(stmt, catalog=TABLES)` precedent exactly: `tests/test_planner.py`
overrides it with a fake `ScanSource` factory and exercises no git
subprocess at all, while `cli.py` calls `plan()` with no `tables`
argument and gets the real `{"blame": BlameScan}` default. `cli.py`
therefore never imports `tables/blame.py` itself - only this module
does, and only to build that default.

This mirrors, and does not fix, the layering gap #35 already tracks:
importing this module (to reach its own default `TABLES`) transitively
imports `tables/blame.py`, which imports `subprocess` at module level.
`AGENTS.md`'s "no git and no subprocess" promise for the planner is
therefore about *behaviour* (this module never calls a scan's `.scan()`
itself, never shells out, and is fully testable with a fake source and
no repository - see `tests/test_planner.py`), not about the import
graph, which #35 is already the place to fix.

`Scan` gets nothing to negotiate
------------------------------------

`exec/operators.py`'s own `Scan` already documents that pushdown does
not exist yet: every `Scan` calls `source.scan(pushed=())`,
unconditionally, whatever `source.capabilities()` reports. This
module does not change that - it never calls `capabilities()` and
never builds a `Predicate` or `PushdownKind` (neither type exists
yet). Predicate splitting and negotiation is M4 (§6 items 13-14), not
this issue.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from historian.exec.operators import Filter, Operator, Project, Scan, ScanSource
from historian.sql.binder import BoundSelectStatement
from historian.tables.blame import BlameScan

__all__ = ["ScanFactory", "TABLES", "plan"]

#: A table's entry in the catalog: given the repository path, produce
#: the `ScanSource` `Scan` will adapt. A factory rather than a
#: ready-made instance, because the repository path is only known at
#: `plan()` time.
ScanFactory = Callable[[Path], ScanSource]

#: The table catalog: FROM-clause name -> `ScanFactory`. Phase 1 has
#: exactly one table, matching `sql/binder.py`'s own `TABLES`
#: (name -> `Schema`) - the two catalogs are keyed identically by
#: design, but are deliberately two separate dicts (one maps to a
#: `Schema`, this one to a factory), not one dict serving both call
#: sites.
TABLES: dict[str, ScanFactory] = {"blame": BlameScan}


def plan(stmt: BoundSelectStatement, repo: Path, tables: dict[str, ScanFactory] = TABLES) -> Operator:
    """Build the operator tree for *stmt*, a repository at *repo*.

    `tables` maps `stmt.from_table` (already resolved against
    `sql/binder.py`'s own catalog, so the lookup here cannot fail for
    any statement `bind()` actually produced) to the factory that
    builds this query's `ScanSource`. Defaults to this module's own
    `TABLES`, but is a parameter - never hardcoded - so it can be
    swapped for a fake in tests with no repository and no git
    subprocess, per this module's own docstring.

    Returns `Project(Filter(Scan(source), stmt.where), stmt.select_list)`
    when `stmt.where` is present, or `Project(Scan(source),
    stmt.select_list)` when it is `None` - exactly the two shapes
    issue #13's acceptance criteria name, and the only two `plan()`
    ever produces: no new operator or node type, and no separate
    optimize/rewrite step.
    """
    source = tables[stmt.from_table](repo)
    tree: Operator = Scan(source)
    if stmt.where is not None:
        tree = Filter(tree, stmt.where)
    return Project(tree, stmt.select_list)
