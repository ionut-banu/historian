"""`historian.atof.text_to_real`, the port of SQLite 3.50.4's
`sqlite3AtoF` (issue #134).

Three kinds of test, and only the last is gated:

- The pinned vectors (`differential.atof_gate.PINNED_VECTORS`), each
  compared with `float.hex()`. They are a property of the port, so
  they hold on every platform and are never skipped.
- Structural pins: the module imports nothing from `historian`, never
  hands the text to `float()`/`int()`, and the parser and
  `_scan_number` no longer call `float()` on SQL-derived text.
- Sampling: for each family of text in #134's measurement table, 2000
  texts from a fixed seed, `text_to_real` against the live oracle's
  `SELECT ? * 1.0`. Gated by `atof_oracle_gate`: on a platform whose
  oracle converts the pinned vectors differently it is skipped, naming
  #176, rather than failed. On the reference platform (macOS arm64) a
  separate test asserts the gate is open, so it cannot close silently
  there.
"""

from __future__ import annotations

import ast
import inspect
import math
import platform
import random
import sqlite3

import pytest

from historian import atof
from historian.atof import text_to_real

from differential.atof_gate import (
    PINNED_VECTORS,
    atof_oracle_gate,
    skip_unless_atof_gate_open,
)

# --- Pinned vectors (never gated) ---------------------------------------


@pytest.mark.parametrize(
    ("text", "expected_hex"),
    [(text, expected) for text, expected, _float_hex in PINNED_VECTORS],
    ids=[text[:32] for text, _expected, _float_hex in PINNED_VECTORS],
)
def test_text_to_real_matches_the_pinned_vector(text, expected_hex):
    result = text_to_real(text)
    assert type(result) is float
    assert result.hex() == expected_hex


@pytest.mark.parametrize(
    ("text", "float_hex"),
    [(text, float_hex) for text, _expected, float_hex in PINNED_VECTORS if float_hex is not None],
    ids=[text[:32] for text, _expected, float_hex in PINNED_VECTORS if float_hex is not None],
)
def test_the_mismatch_vectors_really_differ_from_float(text, float_hex):
    """The table's claim that Python's correctly rounded `float()`
    gives a different double, checked, so the vector test above cannot
    pass against a `float(text)` body."""
    assert float(text).hex() == float_hex


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1e9999999999999999999", math.inf),
        ("-1e9999999999999999999", -math.inf),
        ("1e+9999999999999999999", math.inf),
        ("1e-9999999999999999999", 0.0),
        ("-1e-9999999999999999999", -0.0),
        ("0e9999999999999999999", 0.0),
        ("9" * 5000, math.inf),
        ("-" + "9" * 5000, -math.inf),
        ("0." + "0" * 5000 + "1", 0.0),
        ("0" * 5000 + "5", 5.0),
        ("1" + "0" * 400 + "e-400", 1.0),
    ],
    ids=[
        "huge_exponent",
        "negative_huge_exponent",
        "plus_huge_exponent",
        "huge_negative_exponent",
        "negative_huge_negative_exponent",
        "zero_huge_exponent",
        "nines_5000",
        "negative_nines_5000",
        "tiny_5000_zeros",
        "leading_zeros_5000",
        "digits_cancel_exponent",
    ],
)
def test_text_to_real_never_raises_at_the_limits(text, expected):
    """Overflow is `inf`, underflow a signed zero, and an exponent no
    `int` could hold is clamped the way SQLite clamps it, never
    converted."""
    result = text_to_real(text)
    assert type(result) is float
    assert result.hex() == expected.hex()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("5", 5.0),
        ("+5", 5.0),
        ("-5", -5.0),
        ("5.", 5.0),
        (".5", 0.5),
        ("-.5", -0.5),
        ("1e5", 100000.0),
        ("1E5", 100000.0),
        ("1e+5", 100000.0),
        ("25e-1", 2.5),
        ("0", 0.0),
        ("-0", -0.0),
        ("0.0e0", 0.0),
    ],
)
def test_text_to_real_reads_every_shape_of_number(text, expected):
    """Optional sign, digits with an optional `.`, optional exponent
    with its own optional sign - the grammar the callers isolate."""
    assert text_to_real(text).hex() == expected.hex()


# --- Structure -------------------------------------------------------------


def _atof_tree() -> ast.Module:
    return ast.parse(inspect.getsource(atof))


def test_atof_imports_nothing_from_historian_and_no_number_library():
    """Same standing as `ascii.py`: stdlib only, nothing above it. And
    no `decimal`/`fractions`, which would compute the correctly rounded
    answer rather than SQLite's."""
    imported: set[str] = set()
    for node in ast.walk(_atof_tree()):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert imported <= {"__future__", "struct"}, imported


def test_text_to_real_never_hands_the_text_to_float_or_int():
    """The digits are read one character at a time into a u64, as the
    C does. `float()`/`int()` of anything derived from the text would
    be a second, correctly rounded or unbounded, conversion."""
    for node in ast.walk(_atof_tree()):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("float", "int"):
            names = {n.id for arg in node.args for n in ast.walk(arg) if isinstance(n, ast.Name)}
            assert "text" not in names, ast.unparse(node)


def test_parser_has_no_float_call():
    """Both numeric-literal sites (`_int_literal_value` past int64, and
    the REAL literal) go through `text_to_real`."""
    from historian.sql import parser

    calls = [
        ast.unparse(node)
        for node in ast.walk(ast.parse(inspect.getsource(parser)))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "float"
    ]
    assert calls == []


def test_literals_and_text_use_the_same_conversion():
    """A literal and the same digits as TEXT give the same double."""
    from historian.exec.expression import arithmetic_operand
    from historian.sql.parser import parse
    from historian.sql.lexer import tokenize

    for text in ("18823239210196293635", "191794978794.7906036428205"):
        literal = parse(tokenize(f"SELECT {text} FROM blame")).select_list[0].expr
        assert literal.value.hex() == text_to_real(text).hex()
        assert arithmetic_operand(text).hex() == text_to_real(text).hex()


# --- The gate --------------------------------------------------------------


@pytest.mark.skipif(
    not (platform.system() == "Darwin" and platform.machine() == "arm64"),
    reason="the gate is only required open on the reference platform, macOS arm64",
)
def test_the_gate_is_open_on_the_reference_platform():
    is_open, reason = atof_oracle_gate()
    assert is_open, reason


# --- Sampling against the oracle (gated) ------------------------------------

SAMPLES_PER_FAMILY = 2000


def _digits(rng: random.Random, count: int) -> str:
    """*count* random digits, the first nonzero."""
    return str(rng.randint(1, 9)) + "".join(rng.choice("0123456789") for _ in range(count - 1))


def _with_point(rng: random.Random, digits: str) -> str:
    split = rng.randint(1, len(digits) - 1)
    return digits[:split] + "." + digits[split:]


def _scientific(rng: random.Random, low: int, high: int) -> str:
    digits = _digits(rng, 17)
    return f"{digits[0]}.{digits[1:]}e{rng.randint(low, high)}"


def _short(rng: random.Random) -> str:
    digits = _digits(rng, rng.randint(1, 15))
    if len(digits) > 1 and rng.random() < 0.7:
        return _with_point(rng, digits)
    return digits


def _mixed(rng: random.Random) -> str:
    """Anything the grammar allows: a sign, a point anywhere, leading
    zeros, an exponent over the whole double range and past it."""
    sign = rng.choice(["", "-", "+"])
    digits = "0" * rng.randint(0, 3) + _digits(rng, rng.randint(1, 30))
    point = rng.randint(0, len(digits))
    mantissa = digits[:point] + "." + digits[point:] if rng.random() < 0.7 else digits
    exponent = f"e{rng.randint(-345, 330)}" if rng.random() < 0.8 else ""
    return sign + mantissa + exponent


#: #134's measurement table, one generator per row, plus `mixed`.
FAMILIES = {
    "digit_run_19_to_320": lambda rng: _digits(rng, rng.randint(19, 320)),
    "decimal_17_digits": lambda rng: _with_point(rng, _digits(rng, 17)),
    "decimal_25_digits": lambda rng: _with_point(rng, _digits(rng, 25)),
    "decimal_40_digits": lambda rng: _with_point(rng, _digits(rng, 40)),
    "leading_zeros_then_20_digits": lambda rng: "0." + "0" * rng.randint(1, 20) + _digits(rng, 20),
    "exponent_200_to_300": lambda rng: _scientific(rng, 200, 300),
    "exponent_minus_300_to_minus_200": lambda rng: _scientific(rng, -300, -200),
    "exponent_minus_20_to_20": lambda rng: _scientific(rng, -20, 20),
    "up_to_15_digits_plain": _short,
    "mixed": _mixed,
}


@pytest.mark.parametrize("family", list(FAMILIES))
def test_text_to_real_matches_the_oracle_on_sampled_text(family):
    skip_unless_atof_gate_open()
    rng = random.Random(f"issue-134-{family}")
    texts = [FAMILIES[family](rng) for _ in range(SAMPLES_PER_FAMILY)]
    conn = sqlite3.connect(":memory:")
    try:
        mismatches = []
        for text in texts:
            expected = conn.execute("SELECT ? * 1.0", (text,)).fetchone()[0].hex()
            actual = text_to_real(text).hex()
            if actual != expected:
                mismatches.append((text, expected, actual))
    finally:
        conn.close()
    assert mismatches == [], f"{len(mismatches)} of {len(texts)} differ, first: {mismatches[:3]}"
