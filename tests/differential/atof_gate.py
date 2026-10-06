"""The pinned text-to-REAL vectors, and the platform gate built on them
(issue #134).

historian converts numeric text to a REAL with `historian.atof.
text_to_real`, a port of SQLite 3.50.4's `sqlite3AtoF`, which is not
correctly rounded. The one conversion model historian follows is the
oracle as pinned by #117 on the maintainer's platform, macOS arm64
(`_docs/decisions.md`, 2026-10-06). Every expected hex below is that
oracle's `SELECT ? * 1.0` with the text bound.

`PINNED_VECTORS` serves two consumers:

- `tests/test_atof.py` unit-tests `text_to_real` against every vector
  directly. Those tests are never gated: the vectors are a fixed
  property of the port, identical on every platform.
- `atof_oracle_gate` asks the running oracle for every vector, once
  per session. If any answer differs, the oracle on this machine
  takes a different path from the reference model, so the tests that
  compare historian against the live oracle on non-correctly-rounded
  text (the differential cases in `test_blame.py` and the sampling
  test in `test_atof.py`) are skipped, naming the platform and #176,
  rather than reported as historian bugs. They are still collected.

Not a test module: no `test_` prefix, nothing for pytest to collect.
"""

from __future__ import annotations

import functools
import platform
import sqlite3

import pytest

__all__ = [
    "PINNED_VECTORS",
    "atof_oracle_gate",
    "oracle_real_hex",
    "skip_unless_atof_gate_open",
]

#: `(text, the oracle's hex, Python's float(text).hex() where it
#: differs, else None)`. Taken from #134's acceptance criteria, each
#: re-run against the oracle (SQLite 3.50.4, macOS arm64).
PINNED_VECTORS: tuple[tuple[str, str, str | None], ...] = (
    # Not correctly rounded: SQLite's answer is one ULP from float()'s.
    ("18823239210196293635", "0x1.05399454f5f45p+64", "0x1.05399454f5f46p+64"),
    ("1946072321543227441181", "0x1.a5fcb706d78f5p+70", "0x1.a5fcb706d78f6p+70"),
    ("190662126541657448781964697", "0x1.3b6c8d2e437a1p+87", "0x1.3b6c8d2e437a2p+87"),
    ("191794978794.7906036428205", "0x1.653ef8ff56532p+37", "0x1.653ef8ff56533p+37"),
    ("188331652056.1147003912624", "0x1.5ecb879ec0eaep+37", "0x1.5ecb879ec0eafp+37"),
    ("0.000000000000000018459166118821173147", "0x1.5482f28793c41p-56", "0x1.5482f28793c42p-56"),
    ("0.00000019744293957374857342", "0x1.a8016768baeb7p-23", "0x1.a8016768baeb8p-23"),
    ("1.9066468827644174e235", "0x1.7fc7f843c02f0p+781", "0x1.7fc7f843c02efp+781"),
    ("8.1642477890804245e291", "0x1.a2e175265d301p+969", "0x1.a2e175265d302p+969"),
    ("9.3765082324807016e-255", "0x1.1993c1a2da6e1p-844", "0x1.1993c1a2da6e2p-844"),
    ("1.2256557583185709e-285", "0x1.754333ae3f702p-947", "0x1.754333ae3f701p-947"),
    # Not a rounding difference: SQLite does not round up at the
    # bottom of the subnormal range.
    ("2.4703282292062328e-324", "0x0.0p+0", "0x0.0000000000001p-1022"),
    # Agree with float(), and must keep agreeing.
    ("1769249608144521505243376208", "0x1.6ddf4b5c38ddfp+90", None),
    ("2.0679993363717427e264", "0x1.06b2491991532p+878", None),
    ("0.1", "0x1.999999999999ap-4", None),
    ("5e-324", "0x0.0000000000001p-1022", None),
    ("2.2250738585072014e-308", "0x1.0000000000000p-1022", None),
    ("9" * 20, "0x1.5af1d78b58c40p+66", None),
    # Signs and limits.
    ("-0.0", "-0x0.0p+0", None),
    ("-0e5", "-0x0.0p+0", None),
    ("-1e-400", "-0x0.0p+0", None),
    ("-2.4703282292062328e-324", "-0x0.0p+0", "-0x0.0000000000001p-1022"),
    ("1e-400", "0x0.0p+0", None),
    ("1.7976931348623159e308", "inf", None),
    ("1e400", "inf", None),
    ("9" * 320, "inf", None),
    ("-1e400", "-inf", None),
    ("1e9999999999999999999", "inf", None),
    ("1e-9999999999999999999", "0x0.0p+0", None),
)


def oracle_real_hex(text: str) -> str:
    """The oracle's `SELECT ? * 1.0` for *text* bound as TEXT, as
    `float.hex()`. A bound value is not parsed as SQL, so this is the
    text-to-REAL conversion alone."""
    conn = sqlite3.connect(":memory:")
    try:
        value = conn.execute("SELECT ? * 1.0", (text,)).fetchone()[0]
    finally:
        conn.close()
    return value.hex()


@functools.cache
def atof_oracle_gate() -> tuple[bool, str]:
    """`(open, reason)`: open when the running oracle gives every
    `PINNED_VECTORS` hex, i.e. it converts text the way the reference
    platform does. Computed once per session. *reason* names the first
    differing vector, the platform and #176 when the gate is closed."""
    for text, expected, _float_hex in PINNED_VECTORS:
        actual = oracle_real_hex(text)
        if actual != expected:
            return False, (
                f"the oracle (sqlite3 {sqlite3.sqlite_version} on "
                f"{platform.system()} {platform.machine()}) converts {text[:40]!r} to "
                f"{actual}, not the reference model's {expected} (macOS arm64); "
                "historian follows the reference model, see #176"
            )
    return True, "the oracle matches every pinned text-to-REAL vector"


def skip_unless_atof_gate_open() -> None:
    """Skip the calling test when `atof_oracle_gate` is closed."""
    is_open, reason = atof_oracle_gate()
    if not is_open:
        pytest.skip(reason)
