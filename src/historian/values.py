"""SQL values, cross-type comparison, and three-valued logic.

This is the foundation every operator is built on, and per `_docs/spec.md`
§3 the largest single source of differential mismatches. Every rule here
is taken from SQLite - checked against the `sqlite3` command-line tool -
rather than invented. Where historian and SQLite disagree, SQLite is
right.

Two representations, both using ``None``
----------------------------------------

``Value`` is ``None | int | float | str``, mirroring SQLite's four
storage classes NULL / INTEGER / REAL / TEXT. ``Bool3`` is
``bool | None``: the result of a predicate, meaning SQL ``TRUE`` /
``FALSE`` / ``NULL``.

``None`` deliberately does double duty - it is both "the value NULL" and
"the predicate NULL". This is intentional, not an oversight. SQL has one
NULL and it propagates through both positions identically, so giving it
two Python spellings would mean a conversion at every boundary between a
comparison's operands and its result, and every such conversion is a
place to get NULL handling wrong. Predicates are ``True`` / ``False`` /
``None`` rather than SQLite's own ``1`` / ``0`` / ``NULL`` integers for
the same reason: an integer predicate is indistinguishable from an
integer value.

``bool`` is not a ``Value``
---------------------------

SQLite has no boolean storage class - ``typeof(true)`` is ``integer`` -
and no table column in §2 is boolean, so ``bool`` is excluded from
``Value`` on purpose. But Python's ``bool`` is a subclass of ``int``:
``isinstance(True, int)`` is ``True`` and ``True == 1`` is ``True``. A
``Bool3`` that leaked into a ``Value`` position would therefore compare
as the integer 1 or 0, pass every type check, and produce quietly wrong
rows.

Every function here validates its arguments and raises ``TypeError`` on a
``bool`` in a ``Value`` position (and, symmetrically, on an ``int`` in a
``Bool3`` position). The cost is one isinstance check per call; the
alternative is a class of bug that never announces itself.

Comparison is not ordering
--------------------------

``eq`` / ``lt`` / ... implement SQL's three-valued comparison: any
operand being NULL makes the result NULL. ``ORDER BY`` needs something
different - a total order with a defined position for NULL - so sorting
uses :func:`order_key` instead. They are separate functions on purpose.

Contract for the future ``Sort`` operator: ``sorted(rows, key=...)`` on
:func:`order_key` gives SQLite's ``ASC`` order, and ``DESC`` is that list
**reversed**, not a negated or complemented key. Confirmed against
``sqlite3`` that ``ORDER BY x DESC`` is exactly the ascending result
reversed, NULLs included - so ``Sort`` needs no NULLS-LAST special case
of its own. (Reversing also reverses the relative order of values that
compare equal; SQLite does not define that order, and historian's
own determinism rule is satisfied as long as the reversal is applied to
an already-deterministic list.)

Grouping is not comparison either
---------------------------------

``GROUP BY`` and ``DISTINCT`` must key rows by the raw ``Value``, using
Python's own ``==`` and ``hash()`` - a plain ``dict`` keyed on the value
is correct. Do **not** key them with :func:`eq`. ``eq(None, None)`` is
``None``, which is falsy, so using it would put every NULL row in its own
group; ``None == None`` is ``True``, which is what SQLite does (a column
of NULL, NULL, 1, 1, 2 gives three groups, and ``SELECT DISTINCT``
returns one NULL row, not two). Python's ``1 == 1.0`` with
``hash(1) == hash(1.0)`` likewise matches SQLite grouping an INTEGER 1
and a REAL 1.0 together. No function is needed for this, which is why
none is provided.

Not in this module
------------------

**Column affinity.** ``5 = '5'`` is ``FALSE``, but
``WHERE line_no = '5'`` is ``TRUE`` against an ``INTEGER`` column,
because SQLite converts the literal to the column's declared type
first. Applying that needs to know which operand came from a column and
how that column was declared - the AST and the schema, neither of which
this module ever sees. It implements value-to-value comparison only.
Affinity belongs to ``exec/expression.py``; see ``_docs/spec.md`` §3 and
the 2026-08-27 entry in ``_docs/decisions.md``.

Also elsewhere: ``LIKE`` / ``IN`` / ``BETWEEN`` / ``CASE``, arithmetic
and ``||`` (all ``exec/expression.py``), and aggregate accumulation (the
``Aggregate`` operator).
"""

__all__ = [
    "Bool3",
    "Value",
    "and3",
    "eq",
    "ge",
    "gt",
    "is_",
    "is_not",
    "is_not_null",
    "is_null",
    "is_true",
    "le",
    "lt",
    "ne",
    "not3",
    "or3",
    "order_key",
]

#: A SQL value: SQLite's NULL, INTEGER, REAL and TEXT storage classes.
#: ``bool`` and ``bytes`` are deliberately excluded - see the module
#: docstring.
Value = None | int | float | str

#: The result of a SQL predicate: ``True``, ``False`` or ``None``,
#: meaning ``TRUE``, ``FALSE`` and ``NULL``.
Bool3 = bool | None

# Storage-class ranks, in SQLite's ordering: NULL < numeric < TEXT.
_RANK_NULL = 0
_RANK_NUMERIC = 1
_RANK_TEXT = 2


def _rank(value: Value) -> int:
    """The storage-class rank of *value*, validating its type.

    Raises ``TypeError`` for anything that is not a ``Value`` - including
    ``bool``, which is checked before ``int`` because it is a subclass of
    it.
    """
    if value is None:
        return _RANK_NULL
    if isinstance(value, bool):
        raise TypeError(
            "bool is not a SQL Value; a Bool3 predicate result has leaked "
            f"into a value position (got {value!r})"
        )
    if isinstance(value, (int, float)):
        return _RANK_NUMERIC
    if isinstance(value, str):
        return _RANK_TEXT
    raise TypeError(
        f"not a SQL Value: {value!r} of type {type(value).__name__}"
    )


def _check_bool3(result: Bool3) -> Bool3:
    """Validate a ``Bool3``, rejecting SQLite's integer 0/1 spelling."""
    if result is None or result is True or result is False:
        return result
    raise TypeError(
        f"not a Bool3: {result!r} of type {type(result).__name__}; "
        "predicates are True, False or None, not 1, 0 or NULL"
    )


def is_true(result: Bool3) -> bool:
    """Should ``WHERE`` / ``HAVING`` keep this row?

    ``True`` only for ``TRUE``. ``FALSE`` and ``NULL`` are both rejected,
    and confusing the two is the classic bug (spec §3).
    """
    return _check_bool3(result) is True


# --- Comparison ----------------------------------------------------------
#
# All six return NULL if either operand is NULL. Otherwise values of
# different storage classes compare by class rank - every number is less
# than every string, whatever the string looks like ('5' included) - and
# values of the same class compare directly: numerics by numeric value
# regardless of int/float subtype, text bytewise.


def _compare(left: Value, right: Value) -> int | None:
    """Three-way compare: -1, 0, 1, or ``None`` when either side is NULL."""
    left_rank = _rank(left)
    right_rank = _rank(right)
    if left_rank == _RANK_NULL or right_rank == _RANK_NULL:
        return None
    if left_rank != right_rank:
        return -1 if left_rank < right_rank else 1
    if left == right:
        return 0
    return -1 if left < right else 1


def eq(left: Value, right: Value) -> Bool3:
    """SQL ``=``."""
    order = _compare(left, right)
    return None if order is None else order == 0


def ne(left: Value, right: Value) -> Bool3:
    """SQL ``<>`` / ``!=``."""
    order = _compare(left, right)
    return None if order is None else order != 0


def lt(left: Value, right: Value) -> Bool3:
    """SQL ``<``."""
    order = _compare(left, right)
    return None if order is None else order < 0


def le(left: Value, right: Value) -> Bool3:
    """SQL ``<=``."""
    order = _compare(left, right)
    return None if order is None else order <= 0


def gt(left: Value, right: Value) -> Bool3:
    """SQL ``>``."""
    order = _compare(left, right)
    return None if order is None else order > 0


def ge(left: Value, right: Value) -> Bool3:
    """SQL ``>=``."""
    order = _compare(left, right)
    return None if order is None else order >= 0


# --- IS / IS NOT / IS NULL / IS NOT NULL ---------------------------------
#
# These are never NULL. They are storage-class-sensitive, not a loose
# equality: `1 IS 1.0` is TRUE (both numeric, equal value) while
# `'1' IS 1` is FALSE (different storage classes).


def is_(left: Value, right: Value) -> bool:
    """SQL ``IS``. Always a plain ``bool``, never ``None``."""
    left_rank = _rank(left)
    right_rank = _rank(right)
    if left_rank != right_rank:
        return False
    if left_rank == _RANK_NULL:
        return True
    return bool(left == right)


def is_not(left: Value, right: Value) -> bool:
    """SQL ``IS NOT``. Always a plain ``bool``, never ``None``."""
    return not is_(left, right)


def is_null(value: Value) -> bool:
    """SQL ``IS NULL``. ``0``, ``0.0`` and ``''`` are not NULL."""
    return _rank(value) == _RANK_NULL


def is_not_null(value: Value) -> bool:
    """SQL ``IS NOT NULL``. Always a plain ``bool``, never ``None``."""
    return not is_null(value)


# --- Three-valued connectives --------------------------------------------
#
# Kleene logic, matching SQLite. Written as explicit tables rather than
# short-circuiting expressions: this code is meant to be portable and
# boring, and the NULL rows are exactly the ones that get reasoned about
# wrongly.


def and3(left: Bool3, right: Bool3) -> Bool3:
    """SQL ``AND``. ``NULL AND FALSE`` is ``FALSE``; ``NULL AND TRUE``
    and ``NULL AND NULL`` are ``NULL``."""
    _check_bool3(left)
    _check_bool3(right)
    if left is False or right is False:
        return False
    if left is None or right is None:
        return None
    return True


def or3(left: Bool3, right: Bool3) -> Bool3:
    """SQL ``OR``. ``NULL OR TRUE`` is ``TRUE``; ``NULL OR FALSE`` and
    ``NULL OR NULL`` are ``NULL``."""
    _check_bool3(left)
    _check_bool3(right)
    if left is True or right is True:
        return True
    if left is None or right is None:
        return None
    return False


def not3(result: Bool3) -> Bool3:
    """SQL ``NOT``. ``NOT NULL`` is ``NULL``."""
    _check_bool3(result)
    if result is None:
        return None
    return not result


# --- Ordering ------------------------------------------------------------


def order_key(value: Value) -> tuple[int, int | float | str]:
    """A total ordering key for ``ORDER BY``, usable as ``sorted(key=)``.

    Returns ``(class_rank, payload)``. Every value gets a position -
    nothing is ever left out and no part of the key is ``None``, so a
    column of all NULLs sorts as happily as any other. NULL sorts first,
    then numerics, then text, matching SQLite.

    ``DESC`` is this ordering reversed; see the module docstring.
    """
    rank = _rank(value)
    if rank == _RANK_NULL:
        # Any constant will do - all NULLs tie with each other and the
        # rank already separates them from everything else. 0 keeps the
        # payload slot the same type family as the numeric case.
        return (_RANK_NULL, 0)
    return (rank, value)
