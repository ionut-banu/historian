"""Tests for historian.values.

Every expected value in this file was checked against the `sqlite3`
command-line tool (3.51.0) rather than reasoned about from memory, per
the project rule that SQLite is the definition of correct. The query
used is quoted above each group.
"""

import pytest

from historian.values import (
    Bool3,
    Value,
    and3,
    eq,
    ge,
    gt,
    is_,
    is_not,
    is_not_null,
    is_null,
    is_true,
    le,
    lt,
    ne,
    not3,
    or3,
    order_key,
)

COMPARISONS = [eq, ne, lt, le, gt, ge]


# --- Value representation ------------------------------------------------


def test_value_alias_is_the_four_storage_classes():
    """`Value` is `None | int | float | str` - SQLite's NULL/INTEGER/
    REAL/TEXT. No bool, no bytes."""
    assert Value == (None | int | float | str)


def test_bool3_alias_is_bool_or_none():
    assert Bool3 == (bool | None)


@pytest.mark.parametrize("func", COMPARISONS + [is_, is_not])
@pytest.mark.parametrize("bad", [True, False])
def test_comparisons_reject_bool_operands(func, bad):
    """bool is a subclass of int, so a Bool3 that leaks into a Value
    position would silently compare as 1 or 0 and every isinstance check
    would pass. The guard makes that loud instead."""
    with pytest.raises(TypeError):
        func(bad, 1)
    with pytest.raises(TypeError):
        func(1, bad)


@pytest.mark.parametrize("func", [is_null, is_not_null, order_key])
@pytest.mark.parametrize("bad", [True, False])
def test_unary_value_functions_reject_bool(func, bad):
    with pytest.raises(TypeError):
        func(bad)


@pytest.mark.parametrize("func", COMPARISONS)
def test_comparisons_reject_unsupported_types(func):
    with pytest.raises(TypeError):
        func(b"bytes", 1)


# --- NaN: rejected as a value, not silently ordered ----------------------
#
# sqlite3 has no NaN storage class - `select 0.0/0.0, typeof(0.0/0.0);`
# -> (blank)|null - so a NaN reaching this module is never a value SQLite
# could have produced; it can only be a bug upstream (issue #15). It is
# rejected in `_rank`, the single chokepoint every public function in this
# module routes through, with `ValueError` rather than `TypeError`: NaN is
# the right Python type (`float`) carrying an invalid *value*, which is a
# different failure mode from `bool`/`bytes` being the wrong *type*
# entirely. Infinity is a normal `float` and stays legal - see the
# "NaN does not take infinity down with it" tests below.

NAN = float("nan")
NEGATIVE_NAN = -float("nan")


@pytest.mark.parametrize("func", COMPARISONS)
@pytest.mark.parametrize("nan", [NAN, NEGATIVE_NAN])
def test_comparisons_reject_nan_on_either_side_or_both(func, nan):
    with pytest.raises(ValueError):
        func(nan, 1.0)
    with pytest.raises(ValueError):
        func(1.0, nan)
    with pytest.raises(ValueError):
        func(nan, nan)


def test_gt_of_nan_and_nan_no_longer_returns_true():
    """The issue's own reproduction: `gt(float('nan'), float('nan'))`
    used to return `True` because NaN's numeric rank made it compare
    equal to itself before the value itself was ever examined."""
    with pytest.raises(ValueError):
        gt(NAN, NAN)


@pytest.mark.parametrize("func", [is_, is_not])
@pytest.mark.parametrize("nan", [NAN, NEGATIVE_NAN])
def test_is_and_is_not_reject_nan(func, nan):
    with pytest.raises(ValueError):
        func(nan, nan)
    with pytest.raises(ValueError):
        func(nan, 1.0)
    with pytest.raises(ValueError):
        func(1.0, nan)


@pytest.mark.parametrize("func", [is_null, is_not_null])
@pytest.mark.parametrize("nan", [NAN, NEGATIVE_NAN])
def test_is_null_and_is_not_null_reject_nan(func, nan):
    with pytest.raises(ValueError):
        func(nan)


@pytest.mark.parametrize("nan", [NAN, NEGATIVE_NAN])
def test_order_key_rejects_nan(nan):
    with pytest.raises(ValueError):
        order_key(nan)


def test_sorted_by_order_key_raises_on_nan_instead_of_silently_misordering():
    """The exact reproduction from the issue body: NaN's comparisons are
    all False, so it used to sort into an arbitrary position rather than
    raising. `sorted()` propagates whatever `order_key` raises."""
    with pytest.raises(ValueError):
        sorted([3.0, NAN, 1.0, 2.0], key=order_key)


@pytest.mark.parametrize("func", COMPARISONS)
def test_null_does_not_mask_nan_on_the_other_side(func):
    """`_compare` ranks both operands before it looks at NULL, so a NaN
    paired with a NULL must still raise - not be swallowed into the
    ordinary NULL-propagation result of `None`."""
    with pytest.raises(ValueError):
        func(None, NAN)
    with pytest.raises(ValueError):
        func(NAN, None)


def test_nan_raises_valueerror_not_typeerror():
    """Rationale (see the issue #15 grooming comment): NaN is a value of
    the correct Value type (float) that fails a value-level validity
    check, which is a different failure mode from the TypeError raised
    for a wrong-type operand like bool or bytes. Keeping them distinct
    lets a caller tell the two apart without string-matching messages."""
    with pytest.raises(ValueError):
        eq(NAN, 1.0)
    with pytest.raises(TypeError):
        eq(True, 1.0)


# --- NaN does not take infinity down with it ------------------------------

# sqlite3 :memory: "select 9e999, typeof(9e999), -9e999, typeof(-9e999);"
#   -> Inf|real|-Inf|real. Infinity is an ordinary, legal REAL in SQLite;
# only NaN is impossible. A fix that guards against NaN by rejecting any
# "not a finite float" value would wrongly take infinity down with it.


def test_infinity_is_unaffected_by_the_nan_guard():
    # Same rank as any other numeric, payload untouched.
    assert order_key(float("inf")) == (order_key(1.0)[0], float("inf"))
    assert order_key(float("-inf")) == (order_key(1.0)[0], float("-inf"))
    assert gt(float("inf"), 5) is True
    assert lt(float("-inf"), -1000000) is True
    assert eq(float("inf"), float("inf")) is True


def test_infinity_sorts_at_the_ends_via_order_key():
    column = [3.0, float("inf"), 1.0, float("-inf")]
    assert sorted(column, key=order_key) == [
        float("-inf"),
        1.0,
        3.0,
        float("inf"),
    ]


@pytest.mark.parametrize("func", [and3, or3])
@pytest.mark.parametrize("bad", [1, 0, "x", 1.0])
def test_connectives_reject_non_bool3(func, bad):
    """SQLite's own representation of a predicate is 0/1/NULL. Ours is
    False/True/None, and accepting the integers would blur the two."""
    with pytest.raises(TypeError):
        func(bad, True)
    with pytest.raises(TypeError):
        func(True, bad)


@pytest.mark.parametrize("bad", [1, 0, "x", 1.0])
def test_not3_rejects_non_bool3(bad):
    with pytest.raises(TypeError):
        not3(bad)


# --- is_true: the WHERE / HAVING gate ------------------------------------


def test_is_true_keeps_only_true():
    """`WHERE` keeps a row only when the predicate is TRUE; FALSE and
    NULL are both rejected, and confusing the two is the classic bug."""
    assert is_true(True) is True
    assert is_true(False) is False
    assert is_true(None) is False


# --- Comparisons: NULL propagation ---------------------------------------

# sqlite3 :memory: "select quote(null = null), quote(null < 1),
#   quote(1 < null), quote(null <> 1), quote(null <= 1),
#   quote(null >= 1), quote(null > 1), quote(null != 1);"
#   -> NULL|NULL|NULL|NULL|NULL|NULL|NULL|NULL


@pytest.mark.parametrize("func", COMPARISONS)
@pytest.mark.parametrize("other", [None, 1, 1.5, "", "abc", 0])
def test_every_comparison_is_null_when_either_operand_is_null(func, other):
    assert func(None, other) is None
    assert func(other, None) is None


# --- Comparisons: same storage class -------------------------------------

# sqlite3 :memory: "select 1 = 1.0, 1 < 1.0, 1 <= 1.0, 2 > 1.9999,
#   -0.0 = 0;" -> 1|0|1|1|1


def test_int_and_float_compare_by_numeric_value():
    assert eq(1, 1.0) is True
    assert ne(1, 1.0) is False
    assert lt(1, 1.0) is False
    assert le(1, 1.0) is True
    assert gt(1, 1.0) is False
    assert ge(1, 1.0) is True
    assert gt(2, 1.9999) is True
    assert eq(-0.0, 0) is True


def test_large_int_versus_real_compares_exactly():
    """sqlite3 "select 9007199254740993 = 9007199254740992.0,
    ... < ..., ... > ...;" -> 0|0|1. SQLite compares an INTEGER against
    a REAL exactly rather than casting the int to double, and Python's
    int/float comparison does the same, so no special-casing is needed.
    """
    big, real = 9007199254740993, 9007199254740992.0
    assert eq(big, real) is False
    assert lt(big, real) is False
    assert gt(big, real) is True


def test_ints_and_floats_compare_normally():
    assert lt(1, 2) is True
    assert lt(2, 1) is False
    assert eq(1, 1) is True
    assert ne(1, 2) is True
    assert le(2, 2) is True
    assert ge(2, 3) is False


# sqlite3 :memory: "select 'a' < 'b', 'Z' < 'a', 'abc' < 'abd',
#   'é' < 'z', 'z' < 'é', '' < 'a';" -> 1|1|1|0|1|1


def test_text_compares_bytewise():
    assert lt("a", "b") is True
    assert lt("Z", "a") is True
    assert lt("abc", "abd") is True
    assert lt("", "a") is True
    assert eq("abc", "abc") is True
    assert ne("abc", "abd") is True


def test_text_comparison_is_bytewise_for_non_ascii():
    """UTF-8 byte order coincides with code point order for every
    string historian actually produces - not "for all of Unicode" in
    general, which is false for a lone surrogate (issue #20: a string
    containing one sorts inconsistently with its own UTF-8 bytes).
    What makes the narrower claim true is historian's own decoding
    policy, not a fact about Unicode: git bytes are decoded as UTF-8
    with `errors="replace"` (`tables/blame.py`, settled by #20's
    grooming), which can never produce a lone surrogate, so no string
    this module ever compares can exhibit the mismatch. Python's
    default string ordering therefore matches SQLite's BINARY
    collation for every value in play here. Pinned down rather than
    assumed."""
    assert lt("é", "z") is False
    assert lt("z", "é") is True
    assert gt("é", "z") is True


# --- Comparisons: across storage classes ---------------------------------

# sqlite3 :memory: "select 999 < 'abc', 999 < '5', '5' < 999,
#   999 = '999', 999 <> '999', -1 < '', '' < -1;"
#   -> 1|1|0|0|1|1|0


def test_every_number_orders_before_every_text():
    assert lt(999, "abc") is True
    assert lt(999, "5") is True
    assert lt("5", 999) is False
    assert gt("5", 999) is True
    assert lt(-1, "") is True
    assert lt("", -1) is False


def test_cross_class_equality_is_false_not_null():
    assert eq(999, "999") is False
    assert ne(999, "999") is True
    assert eq("5", 5) is False


def test_no_column_affinity_coercion():
    """`5 = '5'` is FALSE (sqlite3 -> 0), even though
    `WHERE line_no = '5'` is TRUE against an INTEGER column (sqlite3 ->
    1 row). Affinity needs the AST and the schema, so it lives in
    exec/expression.py; this module implements value-to-value
    comparison only. See _docs/spec.md §3 and _docs/decisions.md
    2026-08-27."""
    assert eq(5, "5") is False
    assert eq("5", 5) is False
    assert ne(5, "5") is True
    assert lt(5, "5") is True


# --- IS / IS NOT / IS NULL / IS NOT NULL ---------------------------------

# sqlite3 :memory: "select null is null, 1 is null, 1 is 1.0, '1' is 1,
#   null is not null, 1 is not 1.0, '1' is not 1;" -> 1|0|1|0|0|0|1


def test_is_and_is_not_are_never_null():
    assert is_(None, None) is True
    assert is_(1, None) is False
    assert is_(None, 1) is False
    assert is_(1, 1.0) is True
    assert is_("1", 1) is False
    assert is_not(None, None) is False
    assert is_not(1, 1.0) is False
    assert is_not("1", 1) is True


@pytest.mark.parametrize(
    "left", [None, 0, 1, 0.0, 1.5, "", "abc", "é"]
)
@pytest.mark.parametrize(
    "right", [None, 0, 1, 0.0, 1.5, "", "abc", "é"]
)
def test_is_and_is_not_return_plain_bool_for_every_pair(left, right):
    result = is_(left, right)
    assert result is True or result is False
    assert is_not(left, right) is (not result)


@pytest.mark.parametrize("value", [0, 0.0, "", -0.0, 1, "abc"])
def test_falsy_but_not_null_values_are_not_null(value):
    """sqlite3 "select 0 is null, 0.0 is null, '' is null;" -> 0|0|0."""
    assert is_null(value) is False
    assert is_not_null(value) is True


def test_is_null_of_null():
    assert is_null(None) is True
    assert is_not_null(None) is False


# --- Three-valued connectives --------------------------------------------

# sqlite3 :memory: "select quote(1 and 1), quote(1 and 0),
#   quote(1 and null), quote(0 and 1), quote(0 and 0), quote(0 and null),
#   quote(null and 1), quote(null and 0), quote(null and null);"
#   -> 1|0|NULL|0|0|0|NULL|0|NULL

AND_TABLE = {
    (True, True): True,
    (True, False): False,
    (True, None): None,
    (False, True): False,
    (False, False): False,
    (False, None): False,
    (None, True): None,
    (None, False): False,
    (None, None): None,
}

# sqlite3 :memory: "select quote(1 or 1), quote(1 or 0), quote(1 or null),
#   quote(0 or 1), quote(0 or 0), quote(0 or null), quote(null or 1),
#   quote(null or 0), quote(null or null);"
#   -> 1|1|1|1|0|NULL|1|NULL|NULL

OR_TABLE = {
    (True, True): True,
    (True, False): True,
    (True, None): True,
    (False, True): True,
    (False, False): False,
    (False, None): None,
    (None, True): True,
    (None, False): None,
    (None, None): None,
}


@pytest.mark.parametrize("operands,expected", sorted(AND_TABLE.items(), key=str))
def test_and3_full_truth_table(operands, expected):
    left, right = operands
    assert and3(left, right) is expected


@pytest.mark.parametrize("operands,expected", sorted(OR_TABLE.items(), key=str))
def test_or3_full_truth_table(operands, expected):
    left, right = operands
    assert or3(left, right) is expected


def test_and3_and_or3_cover_all_nine_combinations():
    assert len(AND_TABLE) == 9
    assert len(OR_TABLE) == 9


def test_not3_full_truth_table():
    """sqlite3 "select quote(not 1), quote(not 0), quote(not null);"
    -> 0|1|NULL."""
    assert not3(True) is False
    assert not3(False) is True
    assert not3(None) is None


# --- Ordering ------------------------------------------------------------

# sqlite3 :memory: "create table t(x); insert into t
#   values(null),(1),(2),(null),(3); select quote(x) from t order by x;"
#   -> NULL NULL 1 2 3
# ... order by x desc; -> 3 2 1 NULL NULL


def test_order_key_never_returns_none_anywhere():
    for value in [None, 0, 1, 1.5, -3, "", "abc", "é"]:
        key = order_key(value)
        assert isinstance(key, tuple)
        assert all(part is not None for part in key)


def test_nulls_sort_first_ascending():
    column = [None, 1, 2, None, 3]
    assert sorted(column, key=order_key) == [None, None, 1, 2, 3]


def test_descending_is_the_ascending_list_reversed():
    """sqlite3 confirms ORDER BY x DESC is exactly the ascending list
    reversed, NULLs included - so Sort gets DESC by reversing and needs
    no NULLS-LAST rule of its own."""
    column = [None, 1, 2, None, 3]
    ascending = sorted(column, key=order_key)
    assert list(reversed(ascending)) == [3, 2, 1, None, None]


def test_order_key_ranks_null_before_numeric_before_text():
    """sqlite3 :memory: "create table t(x); insert into t
    values('abc'),(null),(2),('5'),(1.5),(''); select quote(x) from t
    order by x;" -> NULL 1.5 2 '' '5' 'abc'."""
    column = ["abc", None, 2, "5", 1.5, ""]
    assert sorted(column, key=order_key) == [None, 1.5, 2, "", "5", "abc"]
    assert list(reversed(sorted(column, key=order_key))) == [
        "abc",
        "5",
        "",
        2,
        1.5,
        None,
    ]


def test_order_key_does_not_compare_int_against_str():
    """A key that put values of different classes in the same tuple slot
    would raise TypeError on mixed columns."""
    assert order_key(1)[0] < order_key("1")[0]
    assert order_key(None)[0] < order_key(1)[0]


def test_order_key_is_deterministic():
    for value in [None, 0, 1.5, "abc"]:
        assert order_key(value) == order_key(value)


# --- Grouping / DISTINCT equality ----------------------------------------

# sqlite3 :memory: "create table t(x); insert into t
#   values(null),(null),(1),(1),(2); select quote(x), count(*) from t
#   group by x;" -> NULL|2, 1|2, 2|1
# select distinct x  -> NULL, 1, 2 (one NULL row, not two)


def test_raw_python_equality_implements_grouping_semantics():
    assert (None == None) is True  # noqa: E711
    assert (1 == 1.0) is True
    assert hash(1) == hash(1.0)
    assert ("1" == 1) is False


def test_group_by_raw_value_produces_three_groups():
    column = [None, None, 1, 1, 2]
    groups: dict[Value, int] = {}
    for value in column:
        groups[value] = groups.get(value, 0) + 1
    assert len(groups) == 3
    assert groups[None] == 2
    assert groups[1] == 2
    assert groups[2] == 1


def test_distinct_over_nulls_yields_exactly_one_null():
    column = [None, None, 1, 1, 2]
    distinct = list(dict.fromkeys(column))
    assert distinct == [None, 1, 2]
    assert distinct.count(None) == 1


def test_grouping_by_the_eq_function_would_be_wrong():
    """The wrong instinct is to key groups with `eq`. eq(NULL, NULL) is
    NULL, which is falsy, so every NULL row would become its own group.
    Asserted so the trap is recorded, not just described."""
    assert eq(None, None) is None
    assert not is_true(eq(None, None))
    assert (None == None) is True  # noqa: E711


def test_grouping_treats_int_and_equal_float_as_one_group():
    """sqlite3 "insert into t values(1),(1.0),('1'); select quote(x),
    count(*) from t group by x;" -> 1|2, '1'|1."""
    column = [1, 1.0, "1"]
    groups: dict[Value, int] = {}
    for value in column:
        groups[value] = groups.get(value, 0) + 1
    assert len(groups) == 2
    assert groups[1] == 2
    assert groups["1"] == 1
