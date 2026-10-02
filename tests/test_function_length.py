"""Guard for the 50-line function limit in the four binder modules
(#162, following the #151 split).

A function's length is `end_lineno - lineno + 1` of its `def` node, so
comments, blank lines and the docstring count. The test reads the
source files with `ast`; it needs no git. Parametrised over the
modules so a failure names the module and the function.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import historian.sql

MAX_FUNCTION_LINES = 50
BINDER_MODULES = ("bind_expr", "bind_clauses", "grouped", "binder")
SQL_DIR = Path(historian.sql.__file__).parent


@pytest.mark.parametrize("module", BINDER_MODULES)
def test_no_binder_function_is_longer_than_50_lines(module):
    path = SQL_DIR / f"{module}.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    too_long = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            length = node.end_lineno - node.lineno + 1
            if length > MAX_FUNCTION_LINES:
                too_long.append(f"{node.name} is {length} lines (line {node.lineno})")
    assert not too_long, f"{module}.py: " + "; ".join(sorted(too_long))
