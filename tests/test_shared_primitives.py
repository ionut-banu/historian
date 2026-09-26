r"""Regression guards for issue #53.

`_ascii_fold`, `_is_ascii_digit`, and SQLite's int64 bounds were each
duplicated across two or three modules before this issue. The fix
gives each exactly one implementation - `is_ascii_digit`/`ascii_fold`
in a new `historian.ascii`, `INT64_MIN`/`INT64_MAX` in
`historian.values` - and every former copy becomes an import of the
shared one. These tests assert that stays true by grepping
`src/historian/` directly, not by importing anything, so a future
change that reintroduces a copy (rather than an import) fails here
regardless of which module it lands in.

The int64-bound guard needs to tell a *definition* (`_INT64_MIN =
...`, the thing this issue removes) from a *use* (`if value >
_INT64_MAX:`, which every consumer still legitimately has after
importing the shared constant) and from unrelated constants that
merely share a substring. The regex below,
`^[A-Z_]*INT64_M(IN|AX)\s*(:[^=]*)?=(?!=)` with `re.MULTILINE`, is
anchored to the start of a line and requires nothing but uppercase
letters/underscores before `INT64_MIN`/`INT64_MAX`, so it matches a
module-level assignment target (`_INT64_MAX =`, bare `INT64_MAX =`,
`_SUM_INT64_MIN =`) but not a use in the middle of a line such as `if
value > _INT64_MAX:` (the line does not start with the name at all).
The trailing `(?!=)` excludes `==` so a stray comparison written at
column 0 is not mistaken for an assignment either - this is a
tightening the orchestrator asked for over the issue's own suggested
`INT64_M(IN|AX)\s*=`, which would have matched `INT64_MAX == x`.  It
also must not match `exec/expression.py`'s
`_INT64_MIN_MAGNITUDE_AS_FLOAT = 9223372036854775808.0` (a distinct
constant, `2**63` as a float, not one of the two bounds): after
`INT64_M` + `MIN`, that name continues with `_MAGNITUDE_AS_FLOAT`
before any `=`, and the regex's only path past the bound name is an
optional group that must start with a literal `:` (a type
annotation), so it cannot swallow `_MAGNITUDE_AS_FLOAT` and the match
fails to reach the `=` at all. `test_int64_regex_examples` below
checks this directly against both names, independent of the
source-scanning tests.
"""

from __future__ import annotations

import pathlib
import re

_SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "historian"

_DEF_ASCII_FOLD = re.compile(r"^def ascii_fold\(", re.MULTILINE)
_DEF_IS_ASCII_DIGIT = re.compile(r"^def is_ascii_digit\(", re.MULTILINE)
_ASSIGN_INT64_MIN = re.compile(r"^[A-Z_]*INT64_MIN\s*(:[^=]*)?=(?!=)", re.MULTILINE)
_ASSIGN_INT64_MAX = re.compile(r"^[A-Z_]*INT64_MAX\s*(:[^=]*)?=(?!=)", re.MULTILINE)


def _py_files() -> list[pathlib.Path]:
    return sorted(_SRC.rglob("*.py"))


def _matches_by_file(pattern: re.Pattern[str]) -> dict[pathlib.Path, int]:
    counts: dict[pathlib.Path, int] = {}
    for path in _py_files():
        text = path.read_text()
        n = len(pattern.findall(text))
        if n:
            counts[path] = n
    return counts


def _assert_exactly_one_in(pattern: re.Pattern[str], home_filename: str) -> None:
    counts = _matches_by_file(pattern)
    total = sum(counts.values())
    assert total == 1, f"expected exactly one match, found {counts}"
    ((path, _),) = counts.items()
    assert path.name == home_filename, f"expected the match in {home_filename}, found it in {path}"


def test_ascii_fold_defined_exactly_once_in_ascii_module():
    _assert_exactly_one_in(_DEF_ASCII_FOLD, "ascii.py")


def test_is_ascii_digit_defined_exactly_once_in_ascii_module():
    _assert_exactly_one_in(_DEF_IS_ASCII_DIGIT, "ascii.py")


def test_int64_min_assigned_exactly_once_in_values_module():
    _assert_exactly_one_in(_ASSIGN_INT64_MIN, "values.py")


def test_int64_max_assigned_exactly_once_in_values_module():
    _assert_exactly_one_in(_ASSIGN_INT64_MAX, "values.py")


def test_int64_regex_examples():
    """The regex machinery itself, isolated from the source tree, so a
    failure in the two tests above points here first if the regex
    itself is wrong rather than the source."""
    assert _ASSIGN_INT64_MIN.search("_INT64_MIN = -9223372036854775808")
    assert _ASSIGN_INT64_MAX.search("INT64_MAX = 9223372036854775807")
    assert _ASSIGN_INT64_MIN.search("_SUM_INT64_MIN = -9223372036854775808")
    # A use, not a definition: does not start the line with the name.
    assert not _ASSIGN_INT64_MAX.search("    if value > _INT64_MAX:")
    assert not _ASSIGN_INT64_MIN.search("        return _INT64_MIN")
    # A different constant that merely shares the `INT64_M` + `IN`/`AX`
    # prefix - must not be mistaken for the bound itself.
    assert not _ASSIGN_INT64_MIN.search(
        "_INT64_MIN_MAGNITUDE_AS_FLOAT = 9223372036854775808.0"
    )
    assert not _ASSIGN_INT64_MAX.search(
        "_INT64_MIN_MAGNITUDE_AS_FLOAT = 9223372036854775808.0"
    )
    # A comparison, not an assignment - the `(?!=)` tightening.
    assert not _ASSIGN_INT64_MAX.search("INT64_MAX == x")
