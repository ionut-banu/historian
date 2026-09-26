"""Guard tests for the layering rule `AGENTS.md` states: "The parser,
the planner and the executor are plain Python with no git and no
subprocess imports. Only scan operators touch git, so everything
above them is testable against in-memory rows."

Issue #35 (folding in #52). Before this issue, `sql/binder.py` and
`plan/planner.py` each hardcoded a real-table default (`catalog=
TABLES` / `tables=TABLES`) built by importing `historian.tables.blame`
directly - which imports `subprocess` at module level - so merely
*importing* the binder or the planner, without ever running a query,
put `subprocess` into `sys.modules`. `historian/catalog.py` is now the
one place that import happens; `sql/binder.py` and `plan/planner.py`
never import it, `historian.tables.*`, or `subprocess`, directly or
indirectly, and their `catalog=`/`tables=` parameters are required.

Every check here runs in a **fresh interpreter**
(`subprocess.run([sys.executable, ...])`), never in-process: pytest's
own process has already imported most of this package by the time any
in-process test runs (transitively, via other test modules and
`conftest.py` fixtures), so an in-process `'subprocess' not in
sys.modules` assertion would prove nothing - it would pass or fail
based on import order accidents unrelated to the code under test.
`sys.executable`, never a bare `python`/`python3` - see
`_docs/process.md`'s worktree-hazard note, which applies here for the
same reason it applies to a subagent probe.
"""

from __future__ import annotations

import subprocess
import sys

# --- The whole pipeline, imported together ---------------------------------


def _run_import_check(*modules: str) -> subprocess.CompletedProcess:
    import_stmt = "; ".join(f"import {module}" for module in modules)
    code = (
        f"{import_stmt}; import sys; "
        "assert 'subprocess' not in sys.modules, sorted(sys.modules)"
    )
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
    )


def test_pipeline_modules_together_do_not_import_subprocess():
    """The whole pipeline `AGENTS.md` names - lexer, parser, binder,
    planner, the two exec modules, and `values` - imported together in
    one fresh interpreter, must never pull in `subprocess`."""
    result = _run_import_check(
        "historian.sql.lexer",
        "historian.sql.parser",
        "historian.sql.binder",
        "historian.plan.planner",
        "historian.exec.expression",
        "historian.exec.operators",
        "historian.values",
    )
    assert result.returncode == 0, result.stderr


def test_values_and_schema_alone_do_not_import_subprocess():
    """Already true before this issue - stays true. A control case:
    if this one ever fails, the regression is not in the binder/
    planner catalog-wiring this issue fixes."""
    result = _run_import_check("historian.values", "historian.schema")
    assert result.returncode == 0, result.stderr


# --- Per-module checks, so a regression names which chain broke ------------


def test_binder_alone_does_not_import_subprocess():
    """`sql/binder.py` on its own - the chain this issue's Background
    section traced to `from historian.tables.blame import BLAME_SCHEMA`
    feeding a hardcoded `TABLES` default."""
    result = _run_import_check("historian.sql.binder")
    assert result.returncode == 0, result.stderr


def test_planner_alone_does_not_import_subprocess():
    """`plan/planner.py` on its own - the second, independent chain
    this issue's Background section traced to `from historian.tables.
    blame import BlameScan` feeding its own hardcoded `TABLES`
    default. Fixing the binder's chain alone does not fix this one."""
    result = _run_import_check("historian.plan.planner")
    assert result.returncode == 0, result.stderr


def test_exec_expression_alone_does_not_import_subprocess():
    """`exec/expression.py` imports nothing under `historian.tables`
    itself - it inherits the violation solely by importing
    `BoundColumnRef` from `historian.sql.binder`, which is enough to
    execute that module's body. Fixing the binder's chain fixes this
    one too, but it is checked independently so a future regression
    that reintroduces just this one is still caught."""
    result = _run_import_check("historian.exec.expression")
    assert result.returncode == 0, result.stderr


def test_exec_operators_alone_does_not_import_subprocess():
    """Same reasoning as `exec/expression.py` above - `exec/
    operators.py`'s own module docstring is explicit that it
    deliberately never imports `tables/blame.py`, adapting `Scan` to
    anything shaped like its `ScanSource` protocol instead."""
    result = _run_import_check("historian.exec.operators")
    assert result.returncode == 0, result.stderr


# --- #52: the binder's and planner's views can't diverge -------------------


def test_schemas_and_scan_factories_have_the_same_keys():
    """`historian.catalog.SCHEMAS` (what `sql/binder.py`'s `catalog=`
    parameter wants) and `historian.catalog.SCAN_FACTORIES` (what
    `plan/planner.py`'s `tables=` parameter wants) are dict
    comprehensions over one shared `TABLES.items()`, not two
    hand-maintained dicts - so they cannot list different table names
    by construction. Trivially true given that construction, but
    worth asserting so a future refactor that breaks it is caught
    immediately, per #52."""
    from historian.catalog import SCAN_FACTORIES, SCHEMAS

    assert SCHEMAS.keys() == SCAN_FACTORIES.keys()


def test_catalog_has_exactly_phase_1s_tables():
    """`_docs/spec.md` §2 lists exactly one phase 1 table, `blame` -
    `commits`, `commit_files`, `refs`, and `tree` are phase 2 and out
    of scope for this issue. An added phase-2 table that forgets a
    `historian/catalog.py` entry should fail here, not as a bare
    `KeyError` reaching a user (the failure mode #52 and #49 both
    describe)."""
    from historian.catalog import TABLES

    assert set(TABLES) == {"blame"}
