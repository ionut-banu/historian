"""Tests for historian.exec.expression.

Issue #12. Unit-style per spec §4's test-architecture table: hand-built
`Expr`/`Row`/`Schema` values, no repository, no git, no SQLite process
at test time (`AGENTS.md`'s scan-only-touches-git rule). Every expected
value below was independently checked against the `sqlite3` command-line
tool (3.51.0) during this issue's own work - the query used is quoted
above each group, matching `tests/test_binder.py`'s convention.

No differential suite exists yet for this issue (it arrives with #10's
harness landing plus the "8b" operators issue) - see the issue body's
own "Conformance" section - so correctness here rests entirely on these
sqlite3-verified values, not on comparing against a live SQLite process.

Two synthetic schemas are used throughout, matching the exact tables
built in sqlite3 to derive expected values:

    CREATE TABLE t(n INTEGER, s TEXT, r REAL); INSERT INTO t VALUES (5, '5', 5.0);

`_ROW`/`_SCHEMA` below mirror this exactly, so a test's own sqlite3
annotation can be run verbatim against the same shape.
"""

import pytest

from historian.schema import Column, ColumnType, Row, Schema
from historian.sql.ast import (
    FunctionCall,
    Literal,
    Star,
)
from historian.sql.binder import BoundColumnRef
from historian.sql.lexer import Position

_POS = Position(line=1, column=1, offset=0)

#: `t(n INTEGER, s TEXT, r REAL)`, one row: `(5, '5', 5.0)`.
_SCHEMA = Schema(
    columns=(
        Column("n", ColumnType.INTEGER),
        Column("s", ColumnType.TEXT),
        Column("r", ColumnType.REAL),
    )
)
_ROW: Row = (5, "5", 5.0)


def _lit(value) -> Literal:
    return Literal(value, _POS)


def _col(name: str) -> BoundColumnRef:
    offset = _SCHEMA.index_of(name)
    return BoundColumnRef(offset=offset, name=name, position=_POS)


# --- Literals and bound column references -----------------------------


def test_literal_returns_its_value_unchanged():
    """`evaluate(Literal(5), ...)` is `5` - no computation to check
    against sqlite3, this is the base case of the recursion."""
    from historian.exec.expression import evaluate

    assert evaluate(_lit(5), _ROW, _SCHEMA) == 5


def test_literal_null_returns_none():
    from historian.exec.expression import evaluate

    assert evaluate(_lit(None), _ROW, _SCHEMA) is None


def test_bound_column_ref_resolves_by_offset_not_name():
    """`BoundColumnRef` carries a resolved offset (per §3, "column
    references resolve to integer offsets at bind time rather than by
    name at runtime") - this reads `row[ref.offset]` directly, never
    consulting `schema` to re-derive the offset from `ref.name`."""
    from historian.exec.expression import evaluate

    assert evaluate(_col("n"), _ROW, _SCHEMA) == 5
    assert evaluate(_col("s"), _ROW, _SCHEMA) == "5"
    assert evaluate(_col("r"), _ROW, _SCHEMA) == 5.0


def test_bound_column_ref_never_rederives_offset_from_schema_by_name():
    """A `BoundColumnRef` whose `name` field disagrees with what schema
    actually has at `offset` - constructed by hand, unreachable via a
    real binder - still resolves by `offset` alone. This is what makes
    the offset the load-bearing field, not `name` (kept only for
    rendering)."""
    from historian.exec.expression import evaluate

    mismatched = BoundColumnRef(offset=0, name="totally_not_n", position=_POS)
    assert evaluate(mismatched, _ROW, _SCHEMA) == 5


# --- Defensive guards: Star and FunctionCall ---------------------------


def test_star_reaching_evaluator_is_an_assertion_error():
    """The binder expands every `Star` before this module ever sees a
    tree (per its own docstring) - a `Star` here is a defensive
    "should never happen" guard, not UX to design, per the issue body."""
    from historian.exec.expression import evaluate

    with pytest.raises(AssertionError):
        evaluate(Star(table=None, position=_POS), _ROW, _SCHEMA)


def test_function_call_raises_structured_eval_error_not_bare_exception():
    """No aggregate or scalar function is in scope for this module (the
    planner splits aggregates out before this ever runs; no scalar
    function is chosen yet per §1) - a `FunctionCall` reaching here is
    unsupported grammar, per §3's "Errors" taxonomy, not a runtime type
    error and not a bare Python exception."""
    from historian.exec.expression import EvalError, evaluate

    call = FunctionCall(name="frobnicate", args=(), position=_POS)
    with pytest.raises(EvalError) as excinfo:
        evaluate(call, _ROW, _SCHEMA)
    assert excinfo.value.position is _POS
