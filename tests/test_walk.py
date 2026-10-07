"""Tests for `sql/walk.py`: the shared children table, rebuild, shape
equality, `contains_aggregate` and the aggregate-query predicate
(issue #112).

The first group builds one instance of every concrete `Expr` subclass
by reflection over its dataclass fields, so a field added to a node
fails here until `sql/walk.py` handles it. Reflection is allowed in
tests and not in `src/` (`AGENTS.md`: the source is plain `isinstance`
chains that translate to a Rust `match`).
"""

from __future__ import annotations

import dataclasses
import types
import typing
from dataclasses import dataclass

import pytest

from historian.sql import binder, grouped, walk
from historian.sql.ast import (
    And,
    Between,
    BinaryOp,
    ColumnRef,
    Expr,
    FunctionCall,
    In,
    Is,
    Like,
    Literal,
    Not,
    Operator,
    Or,
    Star,
    UnaryOp,
    UnaryOperator,
)
from historian.sql.lexer import Position
from historian.sql.walk import (
    BoundColumnRef,
    children,
    contains_aggregate,
    expr_shape_equal,
    is_aggregate_query,
    with_children,
)

_POS = Position(line=1, column=1, offset=0)
_OTHER_POS = Position(line=2, column=7, offset=30)


# --- Reflection over the node types (tests only) ---------------------------


def _concrete_expr_types() -> list[type]:
    """Every concrete `Expr` subclass the package defines, found by
    walking `Expr.__subclasses__()` recursively. Classes defined by
    tests (an unknown node below, fakes in other test files) are left
    out by module."""
    found: list[type] = []
    pending = list(Expr.__subclasses__())
    while pending:
        cls = pending.pop()
        pending.extend(cls.__subclasses__())
        if dataclasses.is_dataclass(cls) and cls.__module__.startswith("historian."):
            found.append(cls)
    return sorted(set(found), key=lambda cls: cls.__name__)


_TYPES = _concrete_expr_types()

ONE = "one"  # a field of type Expr
MANY = "many"  # tuple[Expr, ...]
OPTIONAL = "optional"  # Expr | None
PLAIN = "plain"  # anything else


def _is_expr_type(hint) -> bool:
    return isinstance(hint, type) and issubclass(hint, Expr)


def _classify(hint) -> str:
    if _is_expr_type(hint):
        return ONE
    origin = typing.get_origin(hint)
    args = typing.get_args(hint)
    if origin is tuple and len(args) == 2 and args[1] is Ellipsis and _is_expr_type(args[0]):
        return MANY
    if origin in (types.UnionType, typing.Union):
        non_none = [arg for arg in args if arg is not type(None)]
        if len(args) == 2 and len(non_none) == 1 and _is_expr_type(non_none[0]):
            return OPTIONAL
    assert "Expr" not in repr(hint), f"unclassified expression-holding field type {hint!r}"
    return PLAIN


def _fields(cls: type) -> list[tuple[str, str]]:
    """`(name, kind)` for every dataclass field of *cls*, in declaration
    order."""
    hints = typing.get_type_hints(cls)
    return [(field.name, _classify(hints[field.name])) for field in dataclasses.fields(cls)]


#: Two different values for every non-child, non-`position` field of
#: every node type. A field missing from this table fails
#: `_plain_value` loudly rather than being skipped.
_SAMPLES: dict[tuple[type, str], tuple[object, object]] = {
    (Literal, "value"): (1, 2),
    (ColumnRef, "table"): (None, "blame"),
    (ColumnRef, "name"): ("path", "line_no"),
    (BoundColumnRef, "offset"): (0, 1),
    (BoundColumnRef, "name"): ("path", "line_no"),
    (walk.FixedColumnRef, "offset"): (0, 1),
    (walk.FixedColumnRef, "name"): ("path", "line_no"),
    (walk.FixedColumnRef, "value"): (5, "5"),
    (Star, "table"): (None, "blame"),
    (FunctionCall, "name"): ("count", "sum"),
    (FunctionCall, "distinct"): (False, True),
    (UnaryOp, "op"): (UnaryOperator.POS, UnaryOperator.NEG),
    (BinaryOp, "op"): (Operator.ADD, Operator.SUB),
    (Is, "negated"): (False, True),
    (Like, "negated"): (False, True),
    (In, "negated"): (False, True),
    (Between, "negated"): (False, True),
}


def _plain_value(cls: type, name: str, which: int) -> object:
    key = (cls, name)
    assert key in _SAMPLES, f"{cls.__name__}.{name} has no sample values in tests/test_walk.py's _SAMPLES"
    return _SAMPLES[key][which]


class _Sentinels:
    """Hands out distinct `Literal` nodes, each with its own value, so
    every child slot holds a node no other slot holds."""

    def __init__(self, start: int = 1000, position: Position = _POS) -> None:
        self._next = start
        self._position = position

    def __call__(self) -> Literal:
        self._next += 1
        return Literal(self._next, self._position)


def _build(
    cls: type,
    sentinels: _Sentinels,
    *,
    position: Position = _POS,
    plain_overrides: dict[str, int] | None = None,
    many_length: int = 2,
    optional_present: bool = True,
) -> tuple[Expr, tuple[Expr, ...]]:
    """An instance of *cls* with a fresh sentinel in every child slot,
    and the children it should report, in field-declaration order."""
    overrides = plain_overrides or {}
    kwargs: dict[str, object] = {}
    expected: list[Expr] = []
    for name, kind in _fields(cls):
        if name == "position":
            kwargs[name] = position
        elif kind == ONE:
            node = sentinels()
            kwargs[name] = node
            expected.append(node)
        elif kind == MANY:
            nodes = tuple(sentinels() for _ in range(many_length))
            kwargs[name] = nodes
            expected.extend(nodes)
        elif kind == OPTIONAL:
            if optional_present:
                node = sentinels()
                kwargs[name] = node
                expected.append(node)
            else:
                kwargs[name] = None
        else:
            kwargs[name] = _plain_value(cls, name, overrides.get(name, 0))
    return cls(**kwargs), tuple(expected)


def _same_objects(actual: tuple[Expr, ...], expected: tuple[Expr, ...]) -> bool:
    return len(actual) == len(expected) and all(a is e for a, e in zip(actual, expected))


def test_every_node_type_is_found():
    """The reflection above finds the whole hierarchy, `BoundColumnRef`
    included - otherwise every test below would pass vacuously."""
    names = {cls.__name__ for cls in _TYPES}
    assert {
        "And",
        "Between",
        "BinaryOp",
        "BoundColumnRef",
        "ColumnRef",
        "FixedColumnRef",
        "FunctionCall",
        "In",
        "Is",
        "Like",
        "Literal",
        "Not",
        "Or",
        "Star",
        "UnaryOp",
    } <= names


def test_bound_column_ref_is_the_binders_class():
    assert binder.BoundColumnRef is walk.BoundColumnRef


# --- Children and rebuild ----------------------------------------------------


@pytest.mark.parametrize("cls", _TYPES, ids=lambda cls: cls.__name__)
@pytest.mark.parametrize("many_length", [0, 1, 3])
@pytest.mark.parametrize("optional_present", [True, False])
def test_children_are_every_expression_field_in_declaration_order(cls, many_length, optional_present):
    node, expected = _build(cls, _Sentinels(), many_length=many_length, optional_present=optional_present)
    assert _same_objects(children(node), expected), (children(node), expected)


@pytest.mark.parametrize("cls", _TYPES, ids=lambda cls: cls.__name__)
@pytest.mark.parametrize("optional_present", [True, False])
def test_rebuilding_with_the_same_children_gives_an_equal_node(cls, optional_present):
    node, _expected = _build(cls, _Sentinels(), optional_present=optional_present)
    assert with_children(node, children(node)) == node


@pytest.mark.parametrize("cls", _TYPES, ids=lambda cls: cls.__name__)
@pytest.mark.parametrize("many_length", [0, 1, 3])
@pytest.mark.parametrize("optional_present", [True, False])
def test_rebuilding_puts_each_new_child_in_its_own_slot(cls, many_length, optional_present):
    """Rebuilt around a second set of sentinels, the node reports
    exactly those as its children, in order, keeps its type, its
    position and every non-child field, and equals the node built
    directly around them."""
    node, _old = _build(cls, _Sentinels(start=1000), many_length=many_length, optional_present=optional_present)
    direct, new = _build(cls, _Sentinels(start=5000), many_length=many_length, optional_present=optional_present)
    rebuilt = with_children(node, list(new))
    assert type(rebuilt) is cls
    assert _same_objects(children(rebuilt), new)
    assert rebuilt == direct


# --- Shape equality ------------------------------------------------------------


def _plain_fields(cls: type) -> list[str]:
    return [name for name, kind in _fields(cls) if kind == PLAIN and name != "position"]


_PLAIN_CASES = [(cls, name) for cls in _TYPES for name in _plain_fields(cls)]


@pytest.mark.parametrize(
    ("cls", "field"), _PLAIN_CASES, ids=[f"{cls.__name__}.{name}" for cls, name in _PLAIN_CASES]
)
def test_nodes_differing_only_in_one_non_child_field_are_not_shape_equal(cls, field):
    a, _ = _build(cls, _Sentinels())
    b, _ = _build(cls, _Sentinels(), plain_overrides={field: 1})
    assert getattr(a, field) != getattr(b, field)
    assert expr_shape_equal(a, b) is False
    assert expr_shape_equal(b, a) is False


@pytest.mark.parametrize("cls", _TYPES, ids=lambda cls: cls.__name__)
def test_nodes_differing_only_in_position_are_shape_equal(cls):
    a, _ = _build(cls, _Sentinels(), position=_POS)
    b, _ = _build(cls, _Sentinels(), position=_OTHER_POS)
    assert a != b
    assert expr_shape_equal(a, b) is True
    assert expr_shape_equal(b, a) is True


@pytest.mark.parametrize("cls", _TYPES, ids=lambda cls: cls.__name__)
def test_nodes_differing_only_in_a_childs_position_are_shape_equal(cls):
    a, a_children = _build(cls, _Sentinels(position=_POS))
    b, _ = _build(cls, _Sentinels(position=_OTHER_POS))
    assert expr_shape_equal(a, b) is True
    if a_children:
        assert a != b


def _child_slot_cases() -> list[tuple[type, int]]:
    cases = []
    for cls in _TYPES:
        _node, expected = _build(cls, _Sentinels())
        cases.extend((cls, index) for index in range(len(expected)))
    return cases


_CHILD_CASES = _child_slot_cases()


@pytest.mark.parametrize(
    ("cls", "index"), _CHILD_CASES, ids=[f"{cls.__name__}[{index}]" for cls, index in _CHILD_CASES]
)
def test_nodes_differing_only_in_one_child_are_not_shape_equal(cls, index):
    """Every child slot, `Like.escape` and each `IN` value included, is
    compared: change one child's value and the shapes differ."""
    a, a_children = _build(cls, _Sentinels())
    replaced = list(a_children)
    replaced[index] = Literal(-1, _POS)
    b = with_children(a, replaced)
    assert expr_shape_equal(a, b) is False
    assert expr_shape_equal(b, a) is False


@pytest.mark.parametrize("cls", [cls for cls in _TYPES if any(k == OPTIONAL for _n, k in _fields(cls))])
def test_an_optional_child_present_against_absent_is_not_shape_equal(cls):
    present, _ = _build(cls, _Sentinels(), optional_present=True)
    absent, _ = _build(cls, _Sentinels(), optional_present=False)
    assert expr_shape_equal(present, absent) is False
    assert expr_shape_equal(absent, present) is False


@pytest.mark.parametrize("cls", [cls for cls in _TYPES if any(k == MANY for _n, k in _fields(cls))])
def test_a_child_list_of_different_length_is_not_shape_equal(cls):
    two, _ = _build(cls, _Sentinels(), many_length=2)
    three, _ = _build(cls, _Sentinels(), many_length=3)
    assert expr_shape_equal(two, three) is False
    assert expr_shape_equal(three, two) is False


def test_nodes_of_different_types_are_not_shape_equal():
    left, right = Literal(1, _POS), Literal(2, _POS)
    and_node = And(left=left, right=right, position=_POS)
    or_node = Or(left=left, right=right, position=_POS)
    assert expr_shape_equal(and_node, or_node) is False
    assert expr_shape_equal(Not(operand=left, position=_POS), UnaryOp(UnaryOperator.POS, left, _POS)) is False


def test_literals_differing_only_in_int_versus_real_type_are_not_shape_equal():
    """`1 == 1.0` in Python, but they are different literals."""
    assert expr_shape_equal(Literal(1, _POS), Literal(1.0, _POS)) is False
    assert expr_shape_equal(Literal(1.0, _POS), Literal(1, _POS)) is False


def test_function_names_are_compared_ascii_case_insensitively():
    upper = FunctionCall(name="COUNT", args=(), position=_POS)
    lower = FunctionCall(name="count", args=(), position=_OTHER_POS)
    assert expr_shape_equal(upper, lower) is True


def test_shape_equality_without_group_by_keys_matches_nothing():
    """With no `GROUP BY` keys there is nothing to match: the binder's
    `_matches_any_key` over `()` is `False`."""
    assert grouped._matches_any_key(Literal(1, _POS), ()) is False


# --- Every node type handled; an unknown one is not ----------------------------


@pytest.mark.parametrize("cls", _TYPES, ids=lambda cls: cls.__name__)
def test_every_shared_function_handles_every_node_type(cls):
    node, _ = _build(cls, _Sentinels())
    kids = children(node)
    assert with_children(node, kids) == node
    assert expr_shape_equal(node, node) is True


def test_an_unknown_node_type_raises_assertion_error_everywhere():
    @dataclass(frozen=True)
    class Unknown(Expr):
        position: Position

    node = Unknown(_POS)
    with pytest.raises(AssertionError, match="unhandled expression node type Unknown"):
        children(node)
    with pytest.raises(AssertionError, match="unhandled expression node type Unknown"):
        with_children(node, [])
    with pytest.raises(AssertionError, match="unhandled expression node type Unknown"):
        expr_shape_equal(node, node)
    with pytest.raises(AssertionError, match="unhandled expression node type Unknown"):
        contains_aggregate(node)


# --- contains_aggregate ----------------------------------------------------------


def _count() -> FunctionCall:
    return FunctionCall(name="count", args=(Star(None, _POS),), position=_POS)


def _col(offset: int = 0) -> BoundColumnRef:
    return BoundColumnRef(offset=offset, name="path", position=_POS)


def test_contains_aggregate_finds_a_call_in_any_child_slot():
    """Every child slot is looked into. `FunctionCall` is left out: it
    is an aggregate call itself, whatever its arguments."""
    assert contains_aggregate(_build(FunctionCall, _Sentinels())[0]) is True
    for cls, index in _CHILD_CASES:
        if cls is FunctionCall:
            continue
        node, kids = _build(cls, _Sentinels())
        assert contains_aggregate(node) is False
        replaced = list(kids)
        replaced[index] = BinaryOp(op=Operator.ADD, left=_col(), right=_count(), position=_POS)
        assert contains_aggregate(with_children(node, replaced)) is True, (cls, index)


def test_contains_aggregate_rejects_an_unbound_tree():
    with pytest.raises(AssertionError, match="needs a bound tree"):
        contains_aggregate(ColumnRef(None, "path", _POS))


def test_contains_aggregate_on_a_deep_tree():
    node: Expr = _count()
    for _ in range(5000):
        node = BinaryOp(op=Operator.ADD, left=node, right=Literal(1, _POS), position=_POS)
    assert contains_aggregate(node) is True


# --- The aggregate-query predicate ----------------------------------------------


def test_no_group_by_and_no_aggregate_is_not_an_aggregate_query():
    assert is_aggregate_query((), []) is False
    assert is_aggregate_query((), [_col(), Literal(1, _POS)]) is False


def test_group_by_alone_is_an_aggregate_query():
    assert is_aggregate_query((_col(),), []) is True
    assert is_aggregate_query((_col(),), [_col()]) is True


def test_a_select_list_aggregate_is_an_aggregate_query():
    assert is_aggregate_query((), [_col(), _count()]) is True
    nested = Like(
        left=Literal("a", _POS), pattern=Literal("a", _POS), negated=False, position=_POS, escape=_count()
    )
    assert is_aggregate_query((), [nested]) is True


def test_an_aggregate_only_in_having_or_order_by_is_not_on_the_binders_select_list_call():
    """The binder passes the select list alone. An aggregate written
    only in `HAVING` or `ORDER BY` does not make the query aggregate
    there - the planner, passing all three clauses, would see it."""
    select_exprs = [_col()]
    having = BinaryOp(op=Operator.GT, left=_count(), right=Literal(1, _POS), position=_POS)
    order_key = _count()
    assert is_aggregate_query((), select_exprs) is False
    assert is_aggregate_query((), [*select_exprs, having]) is True
    assert is_aggregate_query((), [*select_exprs, order_key]) is True


# --- split_conjuncts, join_conjuncts, references_only_keys (#141) -------------
#
# Reached as `walk.<name>` rather than imported by name, so that this
# module still collects when one of them is missing.


def _and(left: Expr, right: Expr) -> And:
    return And(left=left, right=right, position=_POS)


def test_split_conjuncts_is_the_one_the_optimizer_uses():
    from historian.plan import optimizer

    assert optimizer.split_conjuncts is walk.split_conjuncts


def test_split_conjuncts_splits_every_and_left_to_right():
    a, b, c, d = (Literal(index, _POS) for index in range(4))
    assert walk.split_conjuncts(_and(_and(a, b), _and(c, d))) == [a, b, c, d]
    assert walk.split_conjuncts(_and(a, _and(b, _and(c, d)))) == [a, b, c, d]
    whole = Or(left=_and(a, b), right=c, position=_POS)
    assert walk.split_conjuncts(whole) == [whole]
    negated = Not(operand=_and(a, b), position=_POS)
    assert walk.split_conjuncts(negated) == [negated]


def test_join_conjuncts_builds_a_left_deep_chain_in_order():
    a, b, c = (Literal(index, _POS) for index in range(3))
    joined = walk.join_conjuncts([a, b, c])
    assert isinstance(joined, And) and isinstance(joined.left, And)
    assert joined.left.left is a and joined.left.right is b and joined.right is c
    got = walk.split_conjuncts(joined)
    assert len(got) == 3 and all(x is y for x, y in zip(got, [a, b, c]))


def test_join_conjuncts_of_one_term_is_that_term():
    a = Literal(1, _POS)
    assert walk.join_conjuncts([a]) is a


def test_join_conjuncts_of_nothing_is_an_error():
    with pytest.raises(AssertionError):
        walk.join_conjuncts([])


def test_split_and_join_are_iterative_over_a_deep_chain():
    terms = [Literal(index, _POS) for index in range(20000)]
    joined = walk.join_conjuncts(terms)
    got = walk.split_conjuncts(joined)
    assert len(got) == len(terms) and all(x is y for x, y in zip(got, terms))


_KEY = BoundColumnRef(offset=0, name="path", position=_POS)
_NOT_KEY = BoundColumnRef(offset=1, name="line_no", position=_POS)


@pytest.mark.parametrize("cls", [cls for cls in _TYPES if cls is not ColumnRef], ids=lambda cls: cls.__name__)
def test_references_only_keys_handles_every_node_type(cls):
    """Built around literal sentinels, every node type but a column
    reference has no column at all, so it references only keys."""
    node, _ = _build(cls, _Sentinels())
    if cls is BoundColumnRef:
        assert walk.references_only_keys(node, ()) is False
        assert walk.references_only_keys(node, (node,)) is True
        return
    assert walk.references_only_keys(node, ()) is True


def test_references_only_keys_rejects_an_unbound_tree():
    with pytest.raises(AssertionError, match="bound"):
        walk.references_only_keys(ColumnRef(table=None, name="path", position=_POS), ())


@pytest.mark.parametrize(
    ("cls", "index"), _CHILD_CASES, ids=[f"{cls.__name__}[{index}]" for cls, index in _CHILD_CASES]
)
def test_references_only_keys_looks_into_every_child_slot(cls, index):
    """A column that is not a key, in any one child slot, makes the
    node reference something else; the key column in the same slot
    does not; and a key written at another position still matches."""
    node, kids = _build(cls, _Sentinels())
    for column, expected in ((_NOT_KEY, False), (_KEY, True)):
        replaced = list(kids)
        replaced[index] = column
        assert walk.references_only_keys(with_children(node, replaced), (_KEY,)) is expected
    moved_key = BoundColumnRef(offset=0, name="path", position=_OTHER_POS)
    replaced = list(kids)
    replaced[index] = moved_key
    assert walk.references_only_keys(with_children(node, replaced), (_KEY,)) is True


def test_references_only_keys_accepts_a_column_inside_a_key_subexpression():
    key = BinaryOp(op=Operator.CONCAT, left=_KEY, right=Literal("x", _POS), position=_POS)
    inside = BinaryOp(op=Operator.CONCAT, left=key, right=Literal("y", _POS), position=_OTHER_POS)
    assert walk.references_only_keys(inside, (key,)) is True
    bare = BinaryOp(op=Operator.GT, left=_KEY, right=Literal("a", _POS), position=_POS)
    assert walk.references_only_keys(bare, (key,)) is False
    assert walk.references_only_keys(bare, (_NOT_KEY, key, _KEY)) is True


def test_references_only_keys_on_a_deep_tree():
    node: Expr = _KEY
    for _ in range(20000):
        node = BinaryOp(op=Operator.ADD, left=node, right=_KEY, position=_POS)
    assert walk.references_only_keys(node, (_KEY,)) is True
    assert walk.references_only_keys(BinaryOp(op=Operator.ADD, left=node, right=_NOT_KEY, position=_POS), (_KEY,)) is False


def test_references_only_keys_raises_on_an_unknown_node_type():
    @dataclass(frozen=True)
    class Unknown(Expr):
        position: Position

    with pytest.raises(AssertionError, match="unhandled expression node type Unknown"):
        walk.references_only_keys(Unknown(_POS), ())


# --- Constant propagation helpers (#142) ------------------------------------------
#
# `FixedColumnRef`, `fix_columns` and `replace_conjuncts` are looked up on
# the module at run time.


def _fixed(offset: int = 1, value: object = 5, position: Position = _POS):
    return walk.FixedColumnRef(offset=offset, name="line_no", value=value, position=position)


def test_a_fixed_column_ref_is_a_leaf_whose_value_is_part_of_its_shape():
    node = _fixed()
    assert children(node) == ()
    assert with_children(node, ()) is node
    assert expr_shape_equal(node, _fixed(position=_OTHER_POS)) is True
    assert expr_shape_equal(node, _fixed(value=6)) is False
    assert expr_shape_equal(node, _fixed(value=5.0)) is False
    assert expr_shape_equal(node, _fixed(offset=2)) is False
    assert expr_shape_equal(node, _NOT_KEY) is False


@pytest.mark.parametrize(
    ("cls", "index"), _CHILD_CASES, ids=[f"{cls.__name__}[{index}]" for cls, index in _CHILD_CASES]
)
def test_fix_columns_replaces_a_listed_column_in_every_child_slot(cls, index):
    """A column whose offset is listed becomes a `FixedColumnRef` with
    that value, keeping its name and position; every other child is
    the same object; a column not listed is left as it is."""
    node, kids = _build(cls, _Sentinels())
    replaced = list(kids)
    replaced[index] = _NOT_KEY
    rebuilt = walk.fix_columns(with_children(node, replaced), {1: 5})
    got = children(rebuilt)
    assert got[index] == walk.FixedColumnRef(offset=1, name="line_no", value=5, position=_NOT_KEY.position)
    assert all(got[i] is replaced[i] for i in range(len(replaced)) if i != index)
    replaced[index] = _KEY
    untouched = with_children(node, replaced)
    assert walk.fix_columns(untouched, {1: 5}) is untouched


def test_fix_columns_with_nothing_listed_is_the_same_object():
    expr = BinaryOp(op=Operator.GT, left=_NOT_KEY, right=Literal(1, _POS), position=_POS)
    assert walk.fix_columns(expr, {}) is expr
    assert walk.fix_columns(_NOT_KEY, {1: None}) == _fixed(value=None)


def test_fix_columns_rejects_an_unbound_tree():
    with pytest.raises(AssertionError, match="bound"):
        walk.fix_columns(ColumnRef(table=None, name="path", position=_POS), {0: "a"})


def test_fix_columns_raises_on_an_unknown_node_type():
    @dataclass(frozen=True)
    class Unknown(Expr):
        position: Position

    with pytest.raises(AssertionError, match="unhandled expression node type Unknown"):
        walk.fix_columns(Unknown(_POS), {0: "a"})


def test_fix_columns_on_a_deep_tree():
    node: Expr = _NOT_KEY
    for _ in range(20000):
        node = BinaryOp(op=Operator.ADD, left=node, right=_KEY, position=_POS)
    rebuilt = walk.fix_columns(node, {1: 7})
    deepest = rebuilt
    while isinstance(deepest, BinaryOp):
        assert deepest.right is _KEY
        deepest = deepest.left
    assert deepest == _fixed(value=7)


def test_replace_conjuncts_keeps_the_and_shape():
    a, b, c, d = (Literal(index, _POS) for index in range(4))
    w, x, y, z = (Literal(index + 10, _POS) for index in range(4))
    left = _and(a, b)
    right = _and(c, d)
    whole = _and(left, right)
    rebuilt = walk.replace_conjuncts(whole, [w, b, c, z])
    assert isinstance(rebuilt, And) and isinstance(rebuilt.left, And) and isinstance(rebuilt.right, And)
    assert rebuilt.left.left is w and rebuilt.left.right is b
    assert rebuilt.right.left is c and rebuilt.right.right is z
    assert rebuilt.position == whole.position
    assert walk.replace_conjuncts(whole, [a, b, c, d]) is whole
    partly = walk.replace_conjuncts(whole, [a, b, y, d])
    assert partly.left is left and partly.right is not right
    assert walk.replace_conjuncts(a, [x]) is x


def test_replace_conjuncts_needs_one_term_per_conjunct():
    a, b = Literal(1, _POS), Literal(2, _POS)
    with pytest.raises(AssertionError):
        walk.replace_conjuncts(_and(a, b), [a])
    with pytest.raises(AssertionError):
        walk.replace_conjuncts(_and(a, b), [a, b, a])


def test_replace_conjuncts_on_a_deep_chain():
    terms = [Literal(index, _POS) for index in range(20000)]
    new_terms = [Literal(-index, _POS) for index in range(20000)]
    rebuilt = walk.replace_conjuncts(walk.join_conjuncts(terms), new_terms)
    got = walk.split_conjuncts(rebuilt)
    assert len(got) == len(new_terms) and all(x is y for x, y in zip(got, new_terms))


# --- SQLite's name-resolution order (issue #144) -------------------------------
#
# The binder walks a tree in the order SQLite resolves names, which is
# `children` except for `LIKE`: SQLite's tree holds `x LIKE y ESCAPE z`
# as the call `like(y, x, z)`, so the pattern comes first.


def _resolution_order_of(node: Expr, kids: tuple[Expr, ...]) -> tuple[Expr, ...]:
    if isinstance(node, Like):
        return (kids[1], kids[0], *kids[2:])
    return kids


@pytest.mark.parametrize("cls", _TYPES, ids=lambda cls: cls.__name__)
@pytest.mark.parametrize("many_length", [0, 1, 3])
@pytest.mark.parametrize("optional_present", [True, False])
def test_resolution_children_are_the_children_with_like_pattern_first(cls, many_length, optional_present):
    node, expected = _build(cls, _Sentinels(), many_length=many_length, optional_present=optional_present)
    assert _same_objects(walk.resolution_children(node), _resolution_order_of(node, expected))


def test_resolution_children_of_like_are_pattern_left_escape():
    left, pattern, escape = Literal(1, _POS), Literal(2, _POS), Literal(3, _POS)
    node = Like(left=left, pattern=pattern, negated=False, position=_POS, escape=escape)
    assert _same_objects(walk.resolution_children(node), (pattern, left, escape))
    node = Like(left=left, pattern=pattern, negated=True, position=_POS)
    assert _same_objects(walk.resolution_children(node), (pattern, left))


@pytest.mark.parametrize("cls", _TYPES, ids=lambda cls: cls.__name__)
@pytest.mark.parametrize("many_length", [0, 1, 3])
@pytest.mark.parametrize("optional_present", [True, False])
def test_rebuilding_in_resolution_order_puts_each_child_in_its_own_slot(cls, many_length, optional_present):
    node, _old = _build(cls, _Sentinels(start=1000), many_length=many_length, optional_present=optional_present)
    direct, new = _build(cls, _Sentinels(start=5000), many_length=many_length, optional_present=optional_present)
    rebuilt = walk.with_resolution_children(node, list(_resolution_order_of(direct, new)))
    assert type(rebuilt) is cls
    assert _same_objects(children(rebuilt), new)
    assert rebuilt == direct


def test_resolution_order_raises_on_an_unknown_node_type():
    @dataclass(frozen=True)
    class Unknown(Expr):
        position: Position

    node = Unknown(_POS)
    with pytest.raises(AssertionError, match="unhandled expression node type Unknown"):
        walk.resolution_children(node)
    with pytest.raises(AssertionError, match="unhandled expression node type Unknown"):
        walk.with_resolution_children(node, [])
