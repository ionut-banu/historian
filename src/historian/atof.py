"""Text to REAL exactly as SQLite does it: a port of `sqlite3AtoF` from
SQLite 3.50.4 (`src/util.c`, public domain).

Issue #134. Python's `float(text)` is correctly rounded. SQLite's
conversion is not: it reads at most about 19 significant digits into a
64-bit integer, then scales it by powers of ten in double-double
arithmetic (Dekker's algorithm), and the result is sometimes one ULP
from the correctly rounded double. `18823239210196293635` is
`0x1.05399454f5f45p+64` in SQLite and `0x1.05399454f5f46p+64` from
`float()`; `2.4703282292062328e-324` is `0.0` in SQLite, not the
smallest subnormal. Where historian and SQLite disagree, SQLite is
right (`_docs/spec.md` §1), so every place SQL-derived text becomes a
REAL calls `text_to_real`: TEXT operands and affinity
(`exec/expression.py`'s `_scan_number`) and the parser's decimal and
past-int64 integer literals (`sql/parser.py`).

The model is the oracle pinned by #117 (SQLite 3.50.4) on macOS arm64,
checked by sampling against it (`tests/test_atof.py`). 3.50.4's
`sqlite3AtoF` uses only IEEE binary64 operations - no `long double` -
so the port is plain double arithmetic plus u64 integer arithmetic,
each step rounded as the C's `volatile` doubles are. Other platforms'
oracles are #176.

The shape follows the C on purpose (`AGENTS.md`: portable to Rust).
The digits are read one character at a time into a u64; the text is
never handed to `float()`, `int()`, `decimal` or `fractions`. The one
`float()` call is the C's `(double)s` of a u64, and the one bit-level
step (`_high_half`) is the C's `memcpy` into a u64 and back, which is
`f64::to_bits`/`f64::from_bits` in Rust.

Same standing as `ascii.py`: stdlib only, imports nothing from
`historian`, so the lexer-level parser and the evaluator can both use
it.
"""

from __future__ import annotations

import struct

__all__ = ["text_to_real"]

#: `LARGEST_UINT64` in the C.
_LARGEST_UINT64 = 0xFFFF_FFFF_FFFF_FFFF

#: Once the significand reaches this, further digits cannot be added
#: without overflowing a u64; they only shift the decimal exponent.
_SIGNIFICAND_LIMIT = (_LARGEST_UINT64 - 9) // 10

#: The bound below which a positive exponent is folded into the
#: significand (`s *= 10`) before scaling.
_SCALE_LIMIT = (_LARGEST_UINT64 - 0x7FF) // 10

#: The C clamps a parsed exponent to this rather than overflow an int.
_EXPONENT_CAP = 10000

#: The largest double that converts to u64 without overflow.
_LARGEST_U64_DOUBLE = 18446744073709549568.0

#: Clears the low 26 bits of a double's representation: Dekker's split
#: of a 53-bit significand into a high part whose products are exact.
_SPLIT_MASK = 0xFFFF_FFFF_FC00_0000


def _high_half(x: float) -> float:
    """*x* with the low 26 bits of its IEEE representation cleared -
    the C's `memcpy(&m, &x, 8); m &= 0xfffffffffc000000; memcpy(&hx,
    &m, 8)`."""
    (bits,) = struct.unpack("<Q", struct.pack("<d", x))
    (high,) = struct.unpack("<d", struct.pack("<Q", bits & _SPLIT_MASK))
    return high


def _dekker_mul2(x0: float, x1: float, y: float, yy: float) -> tuple[float, float]:
    """`dekkerMul2`: the double-double `(x0, x1)` times `(y, yy)`,
    returned as a new double-double. Every intermediate is a double,
    rounded at each operation, evaluated left to right as in the C."""
    hx = _high_half(x0)
    tx = x0 - hx
    hy = _high_half(y)
    ty = y - hy
    p = hx * hy
    q = hx * ty + tx * hy
    c = p + q
    cc = p - c + q + tx * ty
    cc = x0 * yy + x1 * y + cc
    r0 = c + cc
    r1 = c - r0
    r1 += cc
    return r0, r1


def _scale(r0: float, r1: float, e: int) -> tuple[float, float]:
    """Multiply the double-double `(r0, r1)` by `10**e`, in the C's
    steps of 100, 10 and 1, each power carried with its own correction
    term."""
    if e > 0:
        while e >= 100:
            e -= 100
            r0, r1 = _dekker_mul2(r0, r1, 1.0e100, -1.5902891109759918046e83)
        while e >= 10:
            e -= 10
            r0, r1 = _dekker_mul2(r0, r1, 1.0e10, 0.0)
        while e >= 1:
            e -= 1
            r0, r1 = _dekker_mul2(r0, r1, 1.0e01, 0.0)
    else:
        while e <= -100:
            e += 100
            r0, r1 = _dekker_mul2(r0, r1, 1.0e-100, -1.99918998026028836196e-117)
        while e <= -10:
            e += 10
            r0, r1 = _dekker_mul2(r0, r1, 1.0e-10, -3.6432197315497741579e-27)
        while e <= -1:
            e += 1
            r0, r1 = _dekker_mul2(r0, r1, 1.0e-01, -5.5511151231257827021e-18)
    return r0, r1


def _digit(char: str) -> int:
    """The value of the ASCII digit *char*, or -1 if it is not one."""
    if "0" <= char <= "9":
        return ord(char) - ord("0")
    return -1


def text_to_real(text: str) -> float:
    """*text* as a REAL, by SQLite 3.50.4's `sqlite3AtoF`.

    *text* is a number the caller has already isolated: an optional
    sign, digits with an optional `.`, and an optional `e`/`E`
    exponent with its own optional sign. No whitespace and no
    validation - like the C, it reads the longest prefix of that shape
    and ignores anything after it. It never raises: overflow is
    `inf`/`-inf`, underflow is `0.0`/`-0.0`, and an exponent of any
    length is clamped (to 10000) rather than converted."""
    n = len(text)
    i = 0
    sign = 1
    s = 0  # the significand, a u64
    d = 0  # decimal-exponent adjustment for digits dropped or after the point
    esign = 1
    e = 0

    if i < n and text[i] == "-":
        sign = -1
        i += 1
    elif i < n and text[i] == "+":
        i += 1

    # Digits before the point; past the u64 limit they only shift `d`.
    while i < n and _digit(text[i]) >= 0:
        s = s * 10 + _digit(text[i])
        i += 1
        if s >= _SIGNIFICAND_LIMIT:
            while i < n and _digit(text[i]) >= 0:
                i += 1
                d += 1

    # Digits after the point, kept only while the significand has room.
    if i < n and text[i] == ".":
        i += 1
        while i < n and _digit(text[i]) >= 0:
            if s < _SIGNIFICAND_LIMIT:
                s = s * 10 + _digit(text[i])
                d -= 1
            i += 1

    if i < n and (text[i] == "e" or text[i] == "E"):
        i += 1
        if i < n and text[i] == "-":
            esign = -1
            i += 1
        elif i < n and text[i] == "+":
            i += 1
        while i < n and _digit(text[i]) >= 0:
            e = e * 10 + _digit(text[i]) if e < _EXPONENT_CAP else _EXPONENT_CAP
            i += 1

    if s == 0:
        return -0.0 if sign < 0 else 0.0

    e = e * esign + d

    # Make the exponent smaller where the significand has room.
    while e > 0 and s < _SCALE_LIMIT:
        s *= 10
        e -= 1
    while e < 0 and s % 10 == 0:
        s //= 10
        e += 1

    # The significand as a double-double: its rounded value, and the
    # integer error of that rounding.
    r0 = float(s)
    if r0 <= _LARGEST_U64_DOUBLE:
        s2 = int(r0)
        r1 = float(s - s2) if s >= s2 else -float(s2 - s)
    else:
        r1 = 0.0

    r0, r1 = _scale(r0, r1, e)
    result = r0 + r1
    if result != result:  # NaN: an overflow inside the double-double
        result = 1e300 * 1e300
    if sign < 0:
        result = -result
    return result
