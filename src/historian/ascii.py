"""ASCII-only text predicates SQLite uses instead of Python's Unicode
rules: is a character a digit, and is a character an uppercase ASCII
letter to be folded to lowercase.

Issue #53. Before this issue, `is_ascii_digit` (as `_is_ascii_digit`)
was defined separately in `sql/lexer.py` and `exec/expression.py`, and
`ascii_fold` (as `_ascii_fold`) separately in `sql/binder.py` and
`exec/expression.py` - three copies kept in sync by inspection rather
than a shared dependency, because at the time `sql/lexer.py` and
`exec/expression.py` each had a documented reason not to import
`sql/binder.py` (it transitively imports `historian.tables.blame`,
which imports `subprocess`). This module has nothing to do with SQL
grammar, git, or subprocess - it is two pure functions of a single
character or string - so it can sit below all three consumers with
nothing to be free of importing.

Both rules are SQLite's own, not Python's: `str.isdigit()` also
accepts non-ASCII digit-shaped characters (superscripts, Arabic-Indic
digits, ...) that SQLite does not treat as numeric, and
`str.lower()`/`str.casefold()` are Unicode-aware where SQLite's own
case folding only ever moves the 26 ASCII letters `A`-`Z` - confirmed
against `sqlite3` 3.51.0: `select STRASSE from t` (table `t(straße
text)`) fails with "no such column: STRASSE", while `select STRAßE
from t` succeeds, because `ß` is left alone rather than folded to
`SS`, which is exactly what `'straße'.upper() == 'STRASSE'` would
wrongly do in Python. See `_docs/decisions.md`, 2026-09-01, and
`sql/binder.py`'s own module docstring for the same evidence.

Not in this module: SQLite's int64 bounds. Those are a property of
`Value`'s own `int` variant, not a text-classification rule, and live
in `historian/values.py` instead - see that module's docstring and
`_docs/decisions.md`.
"""

from __future__ import annotations

__all__ = [
    "ascii_fold",
    "is_ascii_digit",
]


def is_ascii_digit(char: str) -> bool:
    """Is *char* one of `0`-`9`? Plain codepoint comparison, not
    `str.isdigit()` - see the module docstring."""
    return "0" <= char <= "9"


def ascii_fold(text: str) -> str:
    """Fold only the ASCII letters `A`-`Z` to `a`-`z`; leave every
    other character - including everything outside ASCII - untouched.

    SQLite's own identifier- and `LIKE`-matching rule, not Python's
    Unicode-aware `str.lower()`. See the module docstring for the
    `straße`/`STRASSE`/`STRAßE` evidence this must agree with.
    """
    return "".join(chr(ord(ch) + 32) if "A" <= ch <= "Z" else ch for ch in text)
