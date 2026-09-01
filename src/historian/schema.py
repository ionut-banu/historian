"""``Schema`` and ``Row``: the generic shapes every operator's output
takes.

Introduced by issue #11, not #9. #9's filed body originally assumed the
binder would own these ("the binder probably owns Schema, Row and the
table catalog"), but #11 (a scan) is groomed to land first, and per
`_docs/spec.md` §3 "the schema lives on the operator, not in the row" -
a scan cannot produce or describe rows without these existing. #9
imports `blame`'s `Schema` instance from `historian.tables.blame` rather
than redefining it; the multi-table catalog (table name -> `Schema`,
used for `FROM`-clause resolution) remains #9's job, not this module's.

A natural sibling of ``values.py``: foundational, imported everywhere,
and importing nothing from ``sql/``, ``plan/`` or ``exec/`` in either
direction - a scan needs a `Schema` to describe its rows, and nothing
above the scan layer needs to be defined for that to be possible.

Not in this module
-------------------

**The table catalog** (name -> `Schema`, `FROM`-clause resolution) -
#9. **Column affinity** (converting a literal to a column's declared
type for comparison) - `exec/expression.py`, per `values.py`'s own
"Not in this module" section; `Column.type` here is the declared type
affinity resolves *against*, not the affinity logic itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .values import Value

__all__ = ["Column", "ColumnType", "Row", "Schema"]

#: A row is a plain tuple of values, one per column, in schema order.
#: Per spec §3: "The schema lives on the operator, not in the row, so
#: rows stay cheap and column references resolve to integer offsets at
#: bind time rather than by name at runtime." A ``Row`` therefore
#: carries no column names or types of its own - always read it
#: alongside the ``Schema`` that describes it.
Row = tuple[Value, ...]


class ColumnType(Enum):
    """The three SQLite storage-class affinities `_docs/spec.md` §2
    restricts every table's declared column types to.

    Each member's value is the affinity's own SQLite spelling
    (``"TEXT"``, not an arbitrary label), because a declared column
    type exists to be compared against or fed to SQLite - the
    differential harness's `CREATE TABLE` (M3) and the affinity
    coercion in `exec/expression.py` both want the SQLite keyword
    itself, not a translation step back to one.
    """

    TEXT = "TEXT"
    INTEGER = "INTEGER"
    REAL = "REAL"


@dataclass(frozen=True)
class Column:
    """One column of a `Schema`: its name and declared type."""

    name: str
    type: ColumnType


@dataclass(frozen=True)
class Schema:
    """The ordered column list an operator's rows conform to.

    Frozen and order-preserving: a `Row`'s values line up with
    `columns` positionally, so the order is part of the schema's
    identity, not an incidental detail of how it was built.
    """

    columns: tuple[Column, ...]

    def index_of(self, name: str) -> int:
        """The 0-based position of the column named *name*.

        This is the "integer offsets at bind time" spec §3 describes -
        the mechanism a future binder/`exec/expression.py` uses to turn
        a `ColumnRef` into a position in a `Row`, looked up once rather
        than by name on every row. Raises ``KeyError`` for an unknown
        name; there is no silent fallback, the same way a dict raises
        for a missing key rather than returning something guessed.
        """
        for position, column in enumerate(self.columns):
            if column.name == name:
                return position
        raise KeyError(name)

    @property
    def names(self) -> tuple[str, ...]:
        """The column names in schema order, e.g. for a header row."""
        return tuple(column.name for column in self.columns)
