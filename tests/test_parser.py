"""Tests for historian.sql.parser and historian.sql.ast.

Issue #8 (Part A) and issue #17 (the int64 literal boundary), grooming
and all sqlite3-verified precedence/overflow evidence in the issue
threads for both. Every expected precedence or overflow behaviour here
was checked against the `sqlite3` command-line tool (3.51.0) during
grooming rather than reasoned about from memory - the query used is
quoted above each group, matching `tests/test_lexer.py` and `tests/
test_values.py`'s convention.
"""

import dataclasses

import pytest

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
    OrderDirection,
    Or,
    SelectStatement,
    Star,
    Stmt,
    UnaryOp,
    UnaryOperator,
)
from historian.sql.lexer import Position, tokenize
from historian.sql.parser import ParseError, parse

INT64_MAX = 9223372036854775807


def _parse(sql: str) -> SelectStatement:
    return parse(tokenize(sql))


def _where(sql: str) -> Expr:
    """Parse *sql* and return its WHERE expression, asserting it is
    present."""
    stmt = _parse(sql)
    assert stmt.where is not None
    return stmt.where


def _select_expr(sql: str) -> Expr:
    """Parse *sql* and return its single select-list item's
    expression."""
    stmt = _parse(sql)
    assert len(stmt.select_list) == 1
    return stmt.select_list[0].expr


# --- The target query and basic SELECT/FROM/WHERE shape ------------------


def test_target_query_shape():
    """`_docs/spec.md`'s M2 target query: two bare columns, a FROM
    table name, and an `=` predicate between a ColumnRef and a string
    Literal."""
    stmt = _parse("SELECT path, author_name FROM blame WHERE path = 'src/a.py'")
    assert isinstance(stmt, SelectStatement)
    assert isinstance(stmt, Stmt)
    assert len(stmt.select_list) == 2
    first, second = stmt.select_list
    assert first.expr == ColumnRef(
        table=None, name="path", position=first.expr.position
    )
    assert second.expr == ColumnRef(
        table=None, name="author_name", position=second.expr.position
    )
    assert stmt.from_table == "blame"
    assert isinstance(stmt.where, BinaryOp)
    assert stmt.where.op is Operator.EQ
    assert stmt.where.left == ColumnRef(
        table=None, name="path", position=stmt.where.left.position
    )
    assert stmt.where.right == Literal(
        value="src/a.py", position=stmt.where.right.position
    )


def test_select_star():
    stmt = _parse("SELECT * FROM blame")
    assert len(stmt.select_list) == 1
    item = stmt.select_list[0]
    assert isinstance(item.expr, Star)
    assert not isinstance(item.expr, ColumnRef)
    assert item.expr.table is None


def test_qualified_star():
    stmt = _parse("SELECT blame.* FROM blame")
    item = stmt.select_list[0]
    assert isinstance(item.expr, Star)
    assert item.expr.table == "blame"


def test_alias_and_qualified_column_are_captured_separately():
    stmt = _parse("SELECT path AS p, blame.author_name FROM blame")
    first, second = stmt.select_list
    assert first.expr == ColumnRef(
        table=None, name="path", position=first.expr.position
    )
    assert first.alias == "p"
    assert second.alias is None
    assert isinstance(second.expr, ColumnRef)
    assert second.expr.table == "blame"
    assert second.expr.name == "author_name"


def test_where_is_optional():
    stmt = _parse("SELECT path FROM blame")
    assert stmt.where is None


def test_expression_alias_requires_as_keyword():
    """The v1 grammar (`_docs/spec.md` §1) writes `<expr> [AS alias]`
    - `AS` is mandatory when an alias is present, not the optional-AS
    SQLite itself also allows."""
    stmt = _parse("SELECT path FROM blame")
    assert stmt.select_list[0].alias is None


# --- FunctionCall (bare IDENTIFIER LPAREN ... RPAREN) ---------------------


def test_function_call_with_star_arg():
    expr = _select_expr("SELECT count(*) FROM blame")
    assert isinstance(expr, FunctionCall)
    assert expr.name == "count"
    assert len(expr.args) == 1
    assert isinstance(expr.args[0], Star)
    assert expr.args[0].table is None


def test_function_call_with_column_arg():
    expr = _select_expr("SELECT sum(line_no) FROM blame")
    assert isinstance(expr, FunctionCall)
    assert expr.name == "sum"
    assert expr.args == (
        ColumnRef(table=None, name="line_no", position=expr.args[0].position),
    )


def test_function_call_with_multiple_args():
    expr = _select_expr("SELECT foo(a, b) FROM blame")
    assert isinstance(expr, FunctionCall)
    assert expr.name == "foo"
    assert len(expr.args) == 2


def test_function_call_with_no_args():
    expr = _select_expr("SELECT foo() FROM blame")
    assert isinstance(expr, FunctionCall)
    assert expr.args == ()


def test_function_call_with_expression_arg():
    expr = _select_expr("SELECT foo(a + 1) FROM blame")
    assert isinstance(expr, FunctionCall)
    assert isinstance(expr.args[0], BinaryOp)


# --- Star positions (issue #31) --------------------------------------------
#
# `*`/`table.*` is legal in exactly two grammar positions: a whole,
# alias-less select-list item, and a function call's sole, unqualified
# argument. Every shape below was confirmed against `sqlite3` 3.51.0
# during grooming - see the issue thread for the full mapping and the
# reasoning for why the parser must not special-case `count`.


def test_bare_star_is_legal_anywhere_in_the_select_list():
    """`sqlite3`: `SELECT *, path FROM blame` and `SELECT path, * FROM
    blame` both parse - a bare `*` is not restricted to the first
    position."""
    stmt = _parse("SELECT *, path FROM blame")
    assert isinstance(stmt.select_list[0].expr, Star)
    assert stmt.select_list[1].expr == ColumnRef(
        table=None, name="path", position=stmt.select_list[1].expr.position
    )

    stmt = _parse("SELECT path, * FROM blame")
    assert stmt.select_list[0].expr == ColumnRef(
        table=None, name="path", position=stmt.select_list[0].expr.position
    )
    assert isinstance(stmt.select_list[1].expr, Star)


def test_qualified_star_is_legal_anywhere_in_the_select_list():
    """`sqlite3`: `SELECT blame.*, path FROM blame` and
    `SELECT path, blame.* FROM blame` both parse, the same rule as the
    bare-star case above."""
    stmt = _parse("SELECT blame.*, path FROM blame")
    assert isinstance(stmt.select_list[0].expr, Star)
    assert stmt.select_list[0].expr.table == "blame"

    stmt = _parse("SELECT path, blame.* FROM blame")
    assert isinstance(stmt.select_list[1].expr, Star)
    assert stmt.select_list[1].expr.table == "blame"


def test_bare_star_cannot_take_an_alias():
    """`sqlite3 3.51.0`: `SELECT * AS x FROM blame` ->
    `near "AS": syntax error`, at the `AS` token."""
    sql = "SELECT * AS x FROM blame"
    with pytest.raises(ParseError) as excinfo:
        _parse(sql)
    as_token = tokenize(sql)[2]
    assert as_token.type.name == "AS"
    assert excinfo.value.position == as_token.position


def test_qualified_star_cannot_take_an_alias():
    """`sqlite3 3.51.0`: `SELECT blame.* AS x FROM blame` ->
    `near "AS": syntax error`, same rule as the bare-star case."""
    sql = "SELECT blame.* AS x FROM blame"
    with pytest.raises(ParseError) as excinfo:
        _parse(sql)
    as_token = tokenize(sql)[4]
    assert as_token.type.name == "AS"
    assert excinfo.value.position == as_token.position


def test_qualified_star_illegal_as_a_function_argument():
    """`sqlite3 3.51.0`: `SELECT count(blame.*) FROM blame` ->
    `near "*": syntax error`, at the `*` token. Not special-cased to
    `count` - `sum(blame.*)` fails the same way, checked next."""
    sql = "SELECT count(blame.*) FROM blame"
    with pytest.raises(ParseError) as excinfo:
        _parse(sql)
    star_token = tokenize(sql)[5]
    assert star_token.type.name == "STAR"
    assert excinfo.value.position == star_token.position


def test_qualified_star_illegal_as_argument_to_any_function_name():
    """`sqlite3 3.51.0`: `SELECT sum(blame.*) FROM blame` ->
    `near "*": syntax error` - `table.*` is illegal as an argument to
    any function, not only `count`."""
    sql = "SELECT sum(blame.*) FROM blame"
    with pytest.raises(ParseError) as excinfo:
        _parse(sql)
    star_token = tokenize(sql)[5]
    assert star_token.type.name == "STAR"
    assert excinfo.value.position == star_token.position


def test_bare_star_illegal_as_a_non_first_function_argument():
    """`sqlite3 3.51.0`: `SELECT foo(1, *) FROM blame` ->
    `near "*": syntax error`, at the `*` token."""
    sql = "SELECT foo(1, *) FROM blame"
    with pytest.raises(ParseError) as excinfo:
        _parse(sql)
    star_token = tokenize(sql)[5]
    assert star_token.type.name == "STAR"
    assert excinfo.value.position == star_token.position


def test_bare_star_must_be_the_sole_function_argument():
    """`sqlite3 3.51.0`: `SELECT foo(*, 1) FROM blame` ->
    `near ",": syntax error` - a bare `*` may not be followed by more
    arguments, at the `,` token."""
    sql = "SELECT foo(*, 1) FROM blame"
    with pytest.raises(ParseError) as excinfo:
        _parse(sql)
    comma_token = tokenize(sql)[4]
    assert comma_token.type.name == "COMMA"
    assert excinfo.value.position == comma_token.position


def test_count_star_must_be_the_sole_function_argument():
    """`sqlite3 3.51.0`: `SELECT count(*, path) FROM blame` ->
    `near ",": syntax error`, same rule as `foo(*, 1)` above, on the
    aggregate most likely to be tried first."""
    sql = "SELECT count(*, path) FROM blame"
    with pytest.raises(ParseError) as excinfo:
        _parse(sql)
    comma_token = tokenize(sql)[4]
    assert comma_token.type.name == "COMMA"
    assert excinfo.value.position == comma_token.position


def test_bare_star_illegal_trailing_a_function_argument():
    """`sqlite3 3.51.0`: `SELECT count(blame.path, *) FROM blame` ->
    `near "*": syntax error` - same rule as `foo(1, *)`, with the star
    trailing instead of leading."""
    sql = "SELECT count(blame.path, *) FROM blame"
    with pytest.raises(ParseError) as excinfo:
        _parse(sql)
    star_token = tokenize(sql)[7]
    assert star_token.type.name == "STAR"
    assert excinfo.value.position == star_token.position


@pytest.mark.parametrize("name", ["sum", "min", "max", "avg", "foo"])
def test_bare_star_sole_argument_is_legal_for_any_function_name(name):
    """`sqlite3 3.51.0` accepts `sum(*)`, `min(*)`, `max(*)`, `avg(*)`,
    and even `foo(*)` for a name that is not a real function, as
    syntactically valid calls - it rejects the first four afterward
    with "wrong number of arguments to function X()" and the last with
    "no such function: foo", neither of which is a syntax error. The
    parser must not special-case `count`: arity and function-name
    validation are out of scope for this issue."""
    expr = _select_expr(f"SELECT {name}(*) FROM blame")
    assert isinstance(expr, FunctionCall)
    assert expr.name == name
    assert len(expr.args) == 1
    assert isinstance(expr.args[0], Star)
    assert expr.args[0].table is None


def test_count_star_unchanged_by_the_restructure():
    """`FunctionCall(name="count", args=(Star(table=None),))` is what
    #9's binder depends on for `count(*)` - pinned again here so the
    `_parse_function_call` restructure cannot silently change it."""
    expr = _select_expr("SELECT count(*) FROM blame")
    assert isinstance(expr, FunctionCall)
    assert expr.name == "count"
    assert expr.args == (
        Star(table=None, position=expr.args[0].position),
    )


def test_count_star_alias_attaches_to_the_call_not_the_star():
    """`sqlite3`: `SELECT count(*) AS n FROM blame` parses - unlike a
    bare select-list `*`, a function call's result can be aliased. The
    alias belongs to the `SelectItem` wrapping the `FunctionCall`, not
    to the `Star` argument inside it."""
    stmt = _parse("SELECT count(*) AS n FROM blame")
    item = stmt.select_list[0]
    assert item.alias == "n"
    assert isinstance(item.expr, FunctionCall)
    assert isinstance(item.expr.args[0], Star)


def test_zero_argument_function_call_unaffected():
    """`sqlite3`: `SELECT count() FROM blame` parses - a different,
    already-working shape, unrelated to this fix and re-pinned so the
    `_parse_function_call` restructure cannot break it."""
    expr = _select_expr("SELECT count() FROM blame")
    assert isinstance(expr, FunctionCall)
    assert expr.name == "count"
    assert expr.args == ()


def test_multiplication_still_parses_unaffected_by_star_position_rules():
    """The lexer emits one `STAR` token type for both the multiplication
    operator and the star forms above; the parser must keep
    disambiguating by position alone, with no new token type."""
    expr = _select_expr("SELECT 2 * 3 FROM blame")
    assert isinstance(expr, BinaryOp)
    assert expr.op is Operator.MUL

    where = _where("SELECT path FROM blame WHERE line_no > 1 * 2")
    assert isinstance(where, BinaryOp)
    assert where.op is Operator.GT
    assert isinstance(where.right, BinaryOp)
    assert where.right.op is Operator.MUL


def test_where_star_still_raises_parse_error():
    """Already correct today, not a defect - `sqlite3 3.51.0` also
    rejects `SELECT path FROM blame WHERE *` (`near "*": syntax
    error`). Re-pinned as a regression test so a future change cannot
    silently reintroduce it: `STAR` is not a valid `_parse_primary`
    token and `WHERE`'s expression parsing has no star special-case."""
    with pytest.raises(ParseError):
        _parse("SELECT path FROM blame WHERE *")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * + 1 FROM blame",
        "SELECT (*) FROM blame",
        "SELECT -* FROM blame",
        "SELECT path FROM blame WHERE line_no IN (*, 1)",
        "SELECT path FROM blame WHERE line_no IN (1, *)",
    ],
)
def test_star_already_illegal_shapes_stay_illegal(sql):
    """Already correctly rejected today - re-pinned as regression tests
    since they share the same underlying rule this issue fixes: a bare
    `*` may only start an expression at the two sanctioned call sites,
    and `_parse_primary` has no other case that accepts a `STAR`
    token."""
    with pytest.raises(ParseError):
        _parse(sql)


# --- Precedence -----------------------------------------------------------
#
# `select 3 = 0 < 3;` -> 0, confirmed against sqlite3 during grooming:
# only possible if `<` binds tighter than `=`, giving `3 = (0 < 3)`.


def test_and_binds_tighter_than_or():
    expr = _where("SELECT path FROM blame WHERE a=1 AND b=2 OR c=3")
    assert isinstance(expr, Or)
    assert isinstance(expr.left, And)
    assert expr.right == BinaryOp(
        op=Operator.EQ,
        left=ColumnRef(table=None, name="c", position=expr.right.left.position),
        right=Literal(value=3, position=expr.right.right.position),
        position=expr.right.position,
    )


def test_not_binds_tighter_than_and_looser_than_comparison():
    expr = _where("SELECT path FROM blame WHERE NOT a=1 AND b=2")
    assert isinstance(expr, And)
    assert isinstance(expr.left, Not)
    assert isinstance(expr.left.operand, BinaryOp)
    assert expr.left.operand.op is Operator.EQ


def test_between_and_does_not_swallow_trailing_predicate():
    """`select 5 between 1 and 10 and 0;` -> 0, confirmed against
    sqlite3 during grooming: BETWEEN's own AND must not consume the
    trailing `AND y=1`."""
    expr = _where("SELECT path FROM blame WHERE x BETWEEN 1 AND 10 AND y=1")
    assert isinstance(expr, And)
    assert isinstance(expr.left, Between)
    assert expr.left.low == Literal(value=1, position=expr.left.low.position)
    assert expr.left.high == Literal(value=10, position=expr.left.high.position)
    assert expr.right == BinaryOp(
        op=Operator.EQ,
        left=ColumnRef(table=None, name="y", position=expr.right.left.position),
        right=Literal(value=1, position=expr.right.right.position),
        position=expr.right.position,
    )


def test_concat_binds_tighter_than_plus():
    """`select 'a' || 1 + 1;` -> 1, confirmed against sqlite3 during
    grooming: matches `('a' || 1) + 1`, not `'a' || (1 + 1)` (= 'a2')."""
    expr = _select_expr("SELECT 'a' || 1 + 1 FROM blame")
    assert isinstance(expr, BinaryOp)
    assert expr.op is Operator.ADD
    assert isinstance(expr.left, BinaryOp)
    assert expr.left.op is Operator.CONCAT
    assert expr.left.left == Literal(value="a", position=expr.left.left.position)
    assert expr.left.right == Literal(value=1, position=expr.left.right.position)
    assert expr.right == Literal(value=1, position=expr.right.position)


def test_multiplication_binds_tighter_than_addition():
    expr = _select_expr("SELECT 2 + 3 * 4 FROM blame")
    assert isinstance(expr, BinaryOp)
    assert expr.op is Operator.ADD
    assert expr.left == Literal(value=2, position=expr.left.position)
    assert isinstance(expr.right, BinaryOp)
    assert expr.right.op is Operator.MUL


def test_percent_parses_at_multiplicative_precedence():
    """`%` (issue #75) sits in `_MULTIPLICATIVE_OPERATORS` alongside
    `*`/`/`, inside the existing `_parse_multiplicative` tier."""
    expr = _select_expr("SELECT line_no % 2 FROM blame")
    assert isinstance(expr, BinaryOp)
    assert expr.op is Operator.MOD
    assert expr.left == ColumnRef(table=None, name="line_no", position=expr.left.position)
    assert expr.right == Literal(value=2, position=expr.right.position)


def test_percent_binds_tighter_than_addition():
    """`select 2 + 7 % 3;` -> 3, confirmed against sqlite3: only
    possible as `2 + (7 % 3)`."""
    expr = _select_expr("SELECT 2 + 7 % 3 FROM blame")
    assert isinstance(expr, BinaryOp)
    assert expr.op is Operator.ADD
    assert expr.left == Literal(value=2, position=expr.left.position)
    assert isinstance(expr.right, BinaryOp)
    assert expr.right.op is Operator.MOD


def test_percent_is_left_associative_with_star():
    """`select 7 % 3 * 2;` -> 2, confirmed against sqlite3: only
    possible as `(7 % 3) * 2`, left-associative within the
    multiplicative tier."""
    expr = _select_expr("SELECT 7 % 3 * 2 FROM blame")
    assert isinstance(expr, BinaryOp)
    assert expr.op is Operator.MUL
    assert isinstance(expr.left, BinaryOp)
    assert expr.left.op is Operator.MOD
    assert expr.right == Literal(value=2, position=expr.right.position)


def test_concat_binds_tighter_than_multiplication():
    """`select 'a' || 1 * 2;` -> 0, confirmed against sqlite3: matches
    `('a' || 1) * 2` (= 0, numeric affinity of 'a1' is 0), not
    `'a' || (1 * 2)` (= 'a2'). `||` binding tighter than `*` means it
    grabs the shared operand `1` first, so `*` ends up as the
    outermost node with the concat as its left operand."""
    expr = _select_expr("SELECT 'a' || 1 * 2 FROM blame")
    assert isinstance(expr, BinaryOp)
    assert expr.op is Operator.MUL
    assert isinstance(expr.left, BinaryOp)
    assert expr.left.op is Operator.CONCAT


def test_unary_minus_binds_tighter_than_concat():
    expr = _select_expr("SELECT -1 || 'x' FROM blame")
    assert isinstance(expr, BinaryOp)
    assert expr.op is Operator.CONCAT
    assert isinstance(expr.left, UnaryOp)
    assert expr.left.op is UnaryOperator.NEG


def test_relational_binds_tighter_than_equality():
    """`select 3 = 0 < 3;` -> 0, confirmed against sqlite3 during
    grooming."""
    expr = _select_expr("SELECT 3 = 0 < 3 FROM blame")
    assert isinstance(expr, BinaryOp)
    assert expr.op is Operator.EQ
    assert expr.left == Literal(value=3, position=expr.left.position)
    assert isinstance(expr.right, BinaryOp)
    assert expr.right.op is Operator.LT


def test_arithmetic_binds_tighter_than_relational():
    expr = _select_expr("SELECT 2 + 3 < 10 FROM blame")
    assert isinstance(expr, BinaryOp)
    assert expr.op is Operator.LT
    assert isinstance(expr.left, BinaryOp)
    assert expr.left.op is Operator.ADD


def test_chained_equality_is_left_associative():
    """`select 1 = 1 = 1;` -> 1, confirmed against sqlite3 during
    grooming: comparison-tier operators chain rather than requiring
    parentheses."""
    expr = _select_expr("SELECT 1 = 1 = 1 FROM blame")
    assert isinstance(expr, BinaryOp)
    assert expr.op is Operator.EQ
    assert isinstance(expr.left, BinaryOp)
    assert expr.left.op is Operator.EQ
    assert expr.right == Literal(value=1, position=expr.right.position)


def test_between_operands_may_contain_arithmetic():
    """`select 3 between 1+1 and 10;` -> 1, confirmed against sqlite3
    during grooming: BETWEEN's bounds parse at tier 1, which includes
    everything tighter, arithmetic included."""
    expr = _where("SELECT path FROM blame WHERE x BETWEEN 1 + 1 AND 10")
    assert isinstance(expr, Between)
    assert isinstance(expr.low, BinaryOp)
    assert expr.low.op is Operator.ADD


# --- NOT-compound forms parse without error --------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT a NOT LIKE 'x%' FROM blame",
        "SELECT a NOT IN (1, 2) FROM blame",
        "SELECT a NOT BETWEEN 1 AND 2 FROM blame",
        "SELECT a IS NOT NULL FROM blame",
    ],
)
def test_not_compound_forms_parse_without_error(sql):
    _parse(sql)  # must not raise


def test_not_like():
    expr = _select_expr("SELECT a NOT LIKE 'x%' FROM blame")
    assert isinstance(expr, Like)
    assert expr.negated is True


def test_not_in():
    expr = _select_expr("SELECT a NOT IN (1, 2) FROM blame")
    assert isinstance(expr, In)
    assert expr.negated is True
    assert len(expr.values) == 2


def test_not_between():
    expr = _select_expr("SELECT a NOT BETWEEN 1 AND 2 FROM blame")
    assert isinstance(expr, Between)
    assert expr.negated is True


def test_is_not_null():
    """`x IS NOT NULL` is `Is(x, Literal(None), negated=True)` - see
    sql/ast.py's module docstring for why IS NULL/IS NOT NULL are not
    their own node types."""
    expr = _select_expr("SELECT a IS NOT NULL FROM blame")
    assert isinstance(expr, Is)
    assert expr.negated is True
    assert expr.right == Literal(value=None, position=expr.right.position)


def test_is_null():
    expr = _select_expr("SELECT a IS NULL FROM blame")
    assert isinstance(expr, Is)
    assert expr.negated is False
    assert expr.right == Literal(value=None, position=expr.right.position)


def test_plain_is():
    expr = _select_expr("SELECT a IS b FROM blame")
    assert isinstance(expr, Is)
    assert expr.negated is False
    assert expr.right == ColumnRef(table=None, name="b", position=expr.right.position)


def test_in_empty_list():
    """`select 1 in ();` -> 0 (always false, not an error), confirmed
    against sqlite3 during grooming."""
    expr = _select_expr("SELECT a IN () FROM blame")
    assert isinstance(expr, In)
    assert expr.values == ()


# --- Literal construction and the int64 boundary (issue #17) -------------
#
# `sqlite3 :memory: "select 9223372036854775807, typeof(...);"` ->
# `9223372036854775807|integer`. One past that ->
# `9.22337203685478e+18|real`. Confirmed during grooming.


def test_int64_max_stays_an_int():
    expr = _select_expr("SELECT 9223372036854775807 FROM blame")
    assert isinstance(expr, Literal)
    assert expr.value == 9223372036854775807
    assert isinstance(expr.value, int)


def test_one_past_int64_max_becomes_a_float():
    expr = _select_expr("SELECT 9223372036854775808 FROM blame")
    assert isinstance(expr, Literal)
    assert isinstance(expr.value, float)
    assert expr.value == 9223372036854775808.0


def test_arbitrarily_large_integer_becomes_a_float_not_an_error():
    """Python's own `int()` never overflows, so this only happens
    because literal construction explicitly checks the int64 bound."""
    expr = _select_expr("SELECT 99999999999999999999999999999999 FROM blame")
    assert isinstance(expr, Literal)
    assert isinstance(expr.value, float)
    assert not isinstance(expr.value, int)


def test_negative_int64_min_parses_without_error():
    """Whether this folds to a negative int Literal or stays a UnaryOp
    wrapping a float Literal is unspecified (issue #8's grooming) - it
    is a UnaryOp wrapping a float here, and int64-min is deliberately
    not special-cased (see _docs/decisions.md, 2026-09-01, and
    UnaryOp's docstring in sql/ast.py). Only "parses, and is
    numerically -9223372036854775808" is asserted."""
    expr = _select_expr("SELECT -9223372036854775808 FROM blame")
    assert isinstance(expr, UnaryOp)
    assert expr.op is UnaryOperator.NEG
    assert isinstance(expr.operand, Literal)
    assert -expr.operand.value == -9223372036854775808


def test_real_literal_is_a_float():
    expr = _select_expr("SELECT 1.5 FROM blame")
    assert expr == Literal(value=1.5, position=expr.position)


def test_string_literal_is_a_str():
    expr = _select_expr("SELECT 'x' FROM blame")
    assert expr == Literal(value="x", position=expr.position)


def test_null_literal_is_none():
    expr = _select_expr("SELECT NULL FROM blame")
    assert expr == Literal(value=None, position=expr.position)
    assert isinstance(expr, Literal)


# --- Parse errors -----------------------------------------------------


def test_missing_from_raises_parse_error_at_offending_token():
    with pytest.raises(ParseError) as excinfo:
        _parse("SELECT path WHERE path = 'x'")
    assert isinstance(excinfo.value.position, Position)
    # WHERE, not SELECT or path, is where FROM was expected.
    assert excinfo.value.position.column == tokenize(
        "SELECT path WHERE path = 'x'"
    )[2].position.column


def test_empty_select_list_raises_parse_error():
    with pytest.raises(ParseError):
        _parse("SELECT FROM blame")


def test_dangling_where_raises_parse_error():
    with pytest.raises(ParseError):
        _parse("SELECT path FROM blame WHERE")


def test_parse_error_message_names_what_was_expected():
    with pytest.raises(ParseError) as excinfo:
        _parse("SELECT path WHERE path = 'x'")
    assert "FROM" in str(excinfo.value)


def test_bare_alias_error_names_as():
    """#25 (`_docs/decisions.md`, 2026-09-18): AS stays mandatory, but
    the message for the bare form must say so - `expected FROM, found
    identifier 'p'` never mentioned AS, so a user hit a generic error
    with no hint that `AS p` was what was missing."""
    with pytest.raises(ParseError) as excinfo:
        _parse("SELECT path p FROM blame")
    assert "AS" in str(excinfo.value)


def test_trailing_garbage_after_statement_is_a_parse_error():
    with pytest.raises(ParseError):
        _parse("SELECT path FROM blame EXTRA")


def test_trailing_semicolon_is_consumed():
    stmt = _parse("SELECT path FROM blame;")
    assert stmt.from_table == "blame"


def test_no_traceback_reaches_caller_as_syntax_or_value_error():
    """The parser raises exactly ParseError for malformed input, never
    a bare SyntaxError/ValueError leaking from Python's own int()/
    float() or list indexing."""
    for sql in [
        "SELECT path WHERE path = 'x'",
        "SELECT FROM blame",
        "SELECT path FROM blame WHERE",
        "SELECT path FROM",
        "SELECT (1 FROM blame",
        "SELECT 1 +",
    ]:
        with pytest.raises(ParseError):
            _parse(sql)


# --- Nesting depth: no RecursionError may ever escape (issue #8, round 1) -
#
# QA found `parse()` crashing with a raw, unhandled `RecursionError`
# on deeply nested parentheses: 89 levels of `(...)` parsed, 90 raised
# a bare traceback. `sqlite3 3.51.0` accepts 90 levels of bare parens
# and returns `1`. Two requirements follow, per AGENTS.md ("where
# historian and SQLite disagree, SQLite is right") and spec §5 ("never
# a traceback"): historian must parse everything SQLite parses here,
# and must raise `ParseError` - never let `RecursionError` escape -
# wherever it does decline to go further.
#
# Measured directly against this build of `sqlite3 3.51.0` (see
# `_docs/decisions.md` for the full numbers and how they were taken):
# its *documented* limit is `SQLITE_MAX_EXPR_DEPTH` = 1000 (confirmed
# via `PRAGMA compile_options`), but its own parser's internal stack
# overflows well before that in practice - at 93 levels of bare
# parens, and at 31 levels of nested function calls or nested
# `IN`-lists. `sql/parser.py`'s two limits below (`_MAX_NESTING_DEPTH`
# and `_MAX_RECURSION_DEPTH`) are chosen to clear all three of those
# numbers with room to spare, while staying safely inside Python's own
# recursion budget regardless of how much stack the caller of parse()
# has already used - see the module docstring for why there are two
# limits and not one.

_MAX_NESTING_DEPTH = 1000  # sql/parser.py's _MAX_NESTING_DEPTH
_MAX_RECURSION_DEPTH = 50  # sql/parser.py's _MAX_RECURSION_DEPTH


def _nested_parens(depth: int) -> str:
    return f"SELECT {'(' * depth}1{')' * depth} FROM blame"


def _not_chain(depth: int) -> str:
    return f"SELECT {'NOT ' * depth}1 FROM blame"


def _unary_chain(depth: int) -> str:
    # Space-separated: `--` lexes as a SQL line comment, which would
    # swallow the rest of the query instead of producing `depth`
    # separate MINUS tokens.
    return f"SELECT {'- ' * depth}1 FROM blame"


def _nested_in(depth: int) -> str:
    return f"SELECT {'a IN (' * depth}1{')' * depth} FROM blame"


def _nested_calls(depth: int) -> str:
    return f"SELECT {'f(' * depth}1{')' * depth} FROM blame"


def test_qa_reported_depth_parses_like_sqlite():
    """The exact case QA reported: `sqlite3` accepts 90 levels of bare
    parens and returns `1`. historian must too - a depth limit is only
    correct if it sits above what SQLite actually accepts."""
    expr = _select_expr(_nested_parens(90))
    assert expr == Literal(value=1, position=expr.position)


def test_nested_parens_up_to_max_depth_parse():
    expr = _select_expr(_nested_parens(_MAX_NESTING_DEPTH))
    assert expr == Literal(value=1, position=expr.position)


def test_nested_parens_beyond_max_depth_raise_parse_error():
    with pytest.raises(ParseError) as excinfo:
        _parse(_nested_parens(_MAX_NESTING_DEPTH + 1))
    assert isinstance(excinfo.value.position, Position)


def test_absurdly_deep_nested_parens_raise_parse_error_not_recursion_error():
    """No RecursionError may escape at any depth, including far beyond
    anything a real query - or a fuzzer - would plausibly generate."""
    with pytest.raises(ParseError):
        _parse(_nested_parens(20_000))


def test_not_chain_up_to_max_depth_parses():
    expr = _select_expr(_not_chain(_MAX_NESTING_DEPTH))
    assert isinstance(expr, Not)


def test_not_chain_beyond_max_depth_raises_parse_error():
    with pytest.raises(ParseError) as excinfo:
        _parse(_not_chain(_MAX_NESTING_DEPTH + 1))
    assert isinstance(excinfo.value.position, Position)


def test_absurdly_long_not_chain_raises_parse_error_not_recursion_error():
    with pytest.raises(ParseError):
        _parse(_not_chain(20_000))


def test_unary_chain_up_to_max_depth_parses():
    expr = _select_expr(_unary_chain(_MAX_NESTING_DEPTH))
    assert isinstance(expr, UnaryOp)


def test_unary_chain_beyond_max_depth_raises_parse_error():
    with pytest.raises(ParseError) as excinfo:
        _parse(_unary_chain(_MAX_NESTING_DEPTH + 1))
    assert isinstance(excinfo.value.position, Position)


def test_absurdly_long_unary_chain_raises_parse_error_not_recursion_error():
    with pytest.raises(ParseError):
        _parse(_unary_chain(20_000))


def test_nested_in_list_up_to_recursion_depth_parses():
    # Each of the `depth` nested `IN (...)` layers costs one
    # `_parse_expr` re-entry *on top of* the one already spent parsing
    # the select-list item itself, so `depth` layers reach
    # `depth + 1` on `self._depth` - `_MAX_RECURSION_DEPTH - 1` layers
    # is the deepest that stays at exactly `_MAX_RECURSION_DEPTH`.
    expr = _select_expr(_nested_in(_MAX_RECURSION_DEPTH - 1))
    assert isinstance(expr, In)


def test_nested_in_list_beyond_recursion_depth_raises_parse_error():
    with pytest.raises(ParseError) as excinfo:
        _parse(_nested_in(_MAX_RECURSION_DEPTH))
    assert isinstance(excinfo.value.position, Position)


def test_absurdly_deep_nested_in_list_raises_parse_error_not_recursion_error():
    with pytest.raises(ParseError):
        _parse(_nested_in(20_000))


def test_nested_function_calls_up_to_recursion_depth_parse():
    # Same off-by-one as the IN-list case above: `depth` nested calls
    # cost `depth + 1` on `self._depth`.
    expr = _select_expr(_nested_calls(_MAX_RECURSION_DEPTH - 1))
    assert isinstance(expr, FunctionCall)


def test_nested_function_calls_beyond_recursion_depth_raises_parse_error():
    with pytest.raises(ParseError) as excinfo:
        _parse(_nested_calls(_MAX_RECURSION_DEPTH))
    assert isinstance(excinfo.value.position, Position)


def test_absurdly_deep_nested_function_calls_raise_parse_error_not_recursion_error():
    with pytest.raises(ParseError):
        _parse(_nested_calls(20_000))


def test_deep_nesting_parse_error_message_names_the_problem():
    with pytest.raises(ParseError) as excinfo:
        _parse(_nested_parens(_MAX_NESTING_DEPTH + 1))
    assert "nest" in str(excinfo.value).lower()


def test_long_and_chain_still_parses_unaffected_by_depth_limit():
    """AND/OR/comparison chaining is loop-based, not recursive, so it
    was never at risk of RecursionError - unaffected by this fix. QA
    already checked a 500-clause chain by hand; this pins it as a
    regression test."""
    sql = "SELECT " + " AND ".join(["a"] * 5000) + " FROM blame"
    expr = _select_expr(sql)
    assert isinstance(expr, And)


# --- Module and node-shape constraints -------------------------------


def test_select_statement_is_a_stmt_not_an_expr():
    stmt = _parse("SELECT path FROM blame")
    assert isinstance(stmt, Stmt)
    assert not isinstance(stmt, Expr)


def test_ast_nodes_are_frozen():
    lit = Literal(value=1, position=Position(line=1, column=1, offset=0))
    with pytest.raises(dataclasses.FrozenInstanceError):
        lit.value = 2  # type: ignore[misc]


def test_every_expression_node_carries_a_position():
    stmt = _parse("SELECT path, 1 + 2, a AND b FROM blame WHERE x = 1")
    assert isinstance(stmt.position, Position)
    for item in stmt.select_list:
        assert isinstance(item.expr.position, Position)
    assert isinstance(stmt.where.position, Position)


def test_parser_module_has_no_git_or_subprocess_import():
    """AGENTS.md: 'The parser, the planner and the executor are plain
    Python with no git and no subprocess imports.'"""
    import ast as python_ast
    import inspect

    import historian.sql.parser as parser_module

    source = inspect.getsource(parser_module)
    tree = python_ast.parse(source)
    imported_names = set()
    for node in python_ast.walk(tree):
        if isinstance(node, python_ast.Import):
            imported_names.update(alias.name for alias in node.names)
        elif isinstance(node, python_ast.ImportFrom) and node.module:
            imported_names.add(node.module)
    assert "git" not in imported_names
    assert "subprocess" not in imported_names


def test_parser_never_imports_schema_row_or_table_catalog():
    """The parser is schema-blind: it never checks that `blame`, or any
    column, actually exists (issue #8's grooming, 'what this issue
    does not own')."""
    import inspect

    import historian.sql.parser as parser_module

    source = inspect.getsource(parser_module)
    assert "Schema" not in source
    assert "Row" not in source


def test_unresolved_column_ref_and_from_table():
    """A ColumnRef is unresolved (table: str | None, name: str) and
    FROM is a bare, unresolved table name - neither is checked against
    a catalog."""
    stmt = _parse("SELECT nonexistent_column FROM nonexistent_table")
    assert stmt.from_table == "nonexistent_table"
    assert stmt.select_list[0].expr == ColumnRef(
        table=None,
        name="nonexistent_column",
        position=stmt.select_list[0].expr.position,
    )


# --- GROUP BY / HAVING (issue #69) ----------------------------------------


def test_no_group_by_or_having_defaults_to_empty_and_none():
    stmt = _parse("SELECT path FROM blame")
    assert stmt.group_by == ()
    assert stmt.having is None


def test_group_by_single_column():
    stmt = _parse("SELECT author_name, count(*) FROM blame GROUP BY author_name")
    assert len(stmt.group_by) == 1
    assert stmt.group_by[0] == ColumnRef(
        table=None, name="author_name", position=stmt.group_by[0].position
    )
    assert stmt.having is None


def test_group_by_multiple_expressions():
    """`GROUP BY` is a plain comma-separated expression list, not just
    bare columns - an expression (`line_no % 2`) is as legal as a
    column reference."""
    stmt = _parse(
        "SELECT author_name, path, count(*) FROM blame "
        "GROUP BY author_name, path"
    )
    assert len(stmt.group_by) == 2


def test_group_by_ordinal_is_a_plain_integer_literal():
    """`GROUP BY 2` parses the ordinal as an ordinary `INTEGER`
    `Literal` - resolving it to a select-list position is the
    binder's job, not the parser's (see `sql/ast.py`'s docstring)."""
    stmt = _parse("SELECT author_name, count(*) FROM blame GROUP BY 1")
    assert stmt.group_by == (Literal(value=1, position=stmt.group_by[0].position),)


def test_group_by_expression():
    """`GROUP BY` on an expression, not just a bare column. `line_no +
    1` stands in for this general "not a bare column" shape; the `%`
    operator (issue #75) gets its own dedicated differential case in
    `tests/differential/test_blame.py`."""
    stmt = _parse("SELECT line_no FROM blame GROUP BY line_no + 1")
    assert isinstance(stmt.group_by[0], BinaryOp)
    assert stmt.group_by[0].op is Operator.ADD


def test_having_parses_as_a_predicate():
    stmt = _parse(
        "SELECT author_name, count(*) FROM blame "
        "GROUP BY author_name HAVING count(*) > 1"
    )
    assert isinstance(stmt.having, BinaryOp)
    assert stmt.having.op is Operator.GT


def test_having_without_group_by_parses():
    """`HAVING` with no `GROUP BY` at all is legal grammar - the
    binder (not this module) is what decides whether the query means
    anything, per issue #69."""
    stmt = _parse("SELECT count(*) FROM blame HAVING count(*) > 1")
    assert stmt.group_by == ()
    assert isinstance(stmt.having, BinaryOp)


def test_where_group_by_having_all_together():
    stmt = _parse(
        "SELECT author_name, count(*) FROM blame WHERE line_no > 0 "
        "GROUP BY author_name HAVING count(*) > 1"
    )
    assert stmt.where is not None
    assert len(stmt.group_by) == 1


# --- ORDER BY (issue #61) ---------------------------------------------


def test_no_order_by_defaults_to_empty():
    stmt = _parse("SELECT path FROM blame")
    assert stmt.order_by == ()


def test_order_by_single_column_defaults_to_ascending():
    stmt = _parse("SELECT path FROM blame ORDER BY path")
    assert len(stmt.order_by) == 1
    item = stmt.order_by[0]
    assert item.expr == ColumnRef(table=None, name="path", position=item.expr.position)
    assert item.direction is OrderDirection.ASC


def test_order_by_explicit_asc():
    stmt = _parse("SELECT path FROM blame ORDER BY path ASC")
    assert stmt.order_by[0].direction is OrderDirection.ASC


def test_order_by_desc():
    stmt = _parse("SELECT path FROM blame ORDER BY path DESC")
    assert stmt.order_by[0].direction is OrderDirection.DESC


def test_order_by_multiple_keys_mixed_direction():
    stmt = _parse("SELECT path, line_no FROM blame ORDER BY path ASC, line_no DESC")
    assert len(stmt.order_by) == 2
    assert stmt.order_by[0].direction is OrderDirection.ASC
    assert stmt.order_by[1].direction is OrderDirection.DESC


def test_order_by_ordinal_is_a_plain_integer_literal():
    """`ORDER BY 2` parses the ordinal as an ordinary `INTEGER`
    `Literal`, exactly like `GROUP BY`'s own ordinal - resolving it to
    a select-list position is the binder's job (issue #61's own
    grooming), not the parser's."""
    stmt = _parse("SELECT path, line_no FROM blame ORDER BY 2")
    item = stmt.order_by[0]
    assert item.expr == Literal(value=2, position=item.expr.position)
    assert item.direction is OrderDirection.ASC


def test_order_by_negative_ordinal_parses_as_unary_minus():
    """`ORDER BY -1` parses via the ordinary unary-minus path - the
    lexer never emits a signed `INTEGER` token - so it is `UnaryOp(NEG,
    Literal(1, ...))` here, not a negative `Literal`. Recognising this
    shape as an ordinal (confirmed against `sqlite3`: `ORDER BY -1` is
    "1st ORDER BY term out of range") is the binder's job."""
    stmt = _parse("SELECT path FROM blame ORDER BY -1")
    assert isinstance(stmt.order_by[0].expr, UnaryOp)
    assert stmt.order_by[0].expr.op is UnaryOperator.NEG


def test_order_by_expression_not_a_bare_column():
    stmt = _parse("SELECT line_no FROM blame ORDER BY line_no + 1")
    assert isinstance(stmt.order_by[0].expr, BinaryOp)
    assert stmt.order_by[0].expr.op is Operator.ADD


def test_order_by_after_where_group_by_having():
    stmt = _parse(
        "SELECT author_name, count(*) FROM blame WHERE line_no > 0 "
        "GROUP BY author_name HAVING count(*) > 1 ORDER BY count(*) DESC"
    )
    assert stmt.where is not None
    assert len(stmt.group_by) == 1
    assert stmt.having is not None
    assert len(stmt.order_by) == 1
    assert isinstance(stmt.order_by[0].expr, FunctionCall)


def test_order_by_missing_by_is_a_parse_error():
    with pytest.raises(ParseError):
        _parse("SELECT path FROM blame ORDER path")


def test_order_by_trailing_comma_is_a_parse_error():
    with pytest.raises(ParseError):
        _parse("SELECT path FROM blame ORDER BY path,")


# --- LIMIT / OFFSET (issue #77) ------------------------------------------
#
# `<n>` parses generically via `_parse_expr()`, exactly like `ORDER
# BY`'s own item - the literal-integer-vs-anything-else decision is
# `sql/binder.py`'s job (issue #77's own design), not this module's.
# The one thing this module does decide is the comma form (`LIMIT m,
# n`), out of scope per issue #77 and rejected with a named message
# rather than falling through to a generic "expected end of query".


def test_no_limit_defaults_to_none():
    stmt = _parse("SELECT path FROM blame")
    assert stmt.limit is None
    assert stmt.offset is None


def test_limit_bare_integer_literal():
    stmt = _parse("SELECT path FROM blame LIMIT 3")
    assert stmt.limit == Literal(value=3, position=stmt.limit.position)
    assert stmt.offset is None


def test_limit_with_offset():
    stmt = _parse("SELECT path FROM blame LIMIT 3 OFFSET 2")
    assert stmt.limit == Literal(value=3, position=stmt.limit.position)
    assert stmt.offset == Literal(value=2, position=stmt.offset.position)


def test_limit_after_order_by():
    stmt = _parse("SELECT path FROM blame ORDER BY path LIMIT 1")
    assert len(stmt.order_by) == 1
    assert stmt.limit == Literal(value=1, position=stmt.limit.position)


def test_limit_negative_parses_as_unary_minus():
    """`LIMIT -1` - the lexer never emits a signed `INTEGER` token, so
    this is `UnaryOp(NEG, Literal(1, ...))`, not a negative `Literal` -
    exactly `ORDER BY -1`'s own shape. Recognising this as a legal,
    negative `LIMIT` value (meaning "no limit", per issue #77's design)
    is the binder's job."""
    stmt = _parse("SELECT path FROM blame LIMIT -1")
    assert isinstance(stmt.limit, UnaryOp)
    assert stmt.limit.op is UnaryOperator.NEG


def test_limit_unary_paren_nested_literal_parses_generically():
    """`LIMIT -(-2)` parses as an ordinary expression tree - the
    parser does not special-case this shape any more than it does for
    `ORDER BY`; unwrapping it to an integer is `_ordinal_value`'s job
    (`sql/binder.py`, issue #77, reusing #61's own helper)."""
    stmt = _parse("SELECT path FROM blame LIMIT -(-2)")
    assert isinstance(stmt.limit, UnaryOp)
    assert stmt.limit.op is UnaryOperator.NEG
    assert isinstance(stmt.limit.operand, UnaryOp)


def test_limit_arithmetic_expression_parses_generically():
    """`LIMIT 1+1` parses fine here - confirmed against sqlite3 during
    this issue's own grooming, it is legal SQL there. Rejecting it as
    not a literal integer is `sql/binder.py`'s job (issue #77's
    narrowing decision), not this module's."""
    stmt = _parse("SELECT path FROM blame LIMIT 1+1")
    assert isinstance(stmt.limit, BinaryOp)
    assert stmt.limit.op is Operator.ADD


def test_offset_arithmetic_expression_parses_generically():
    stmt = _parse("SELECT path FROM blame LIMIT 5 OFFSET 1+1")
    assert isinstance(stmt.offset, BinaryOp)
    assert stmt.offset.op is Operator.ADD


def test_offset_without_limit_is_a_parse_error():
    """§1's grammar has no bare `OFFSET` clause - `OFFSET` with no
    preceding `LIMIT` is simply unconsumed trailing input, caught by
    `expect_end()` exactly like any other unsupported clause."""
    with pytest.raises(ParseError):
        _parse("SELECT path FROM blame OFFSET 2")


def test_limit_missing_expression_is_a_parse_error():
    with pytest.raises(ParseError):
        _parse("SELECT path FROM blame LIMIT")


def test_limit_offset_missing_expression_is_a_parse_error():
    with pytest.raises(ParseError):
        _parse("SELECT path FROM blame LIMIT 3 OFFSET")


def test_limit_comma_form_is_rejected_with_a_named_message_not_a_bare_token_error():
    """`LIMIT m, n` (issue #77's own out-of-scope decision) is
    recognised - a bound `LIMIT` expression immediately followed by a
    comma - and rejected with a message naming the comma form and
    pointing at `OFFSET` instead, not a generic "expected end of
    query, found ','"."""
    with pytest.raises(ParseError) as exc_info:
        _parse("SELECT path FROM blame LIMIT 3, 2")
    message = str(exc_info.value)
    assert "LIMIT" in message
    assert "OFFSET" in message


def test_limit_comma_form_error_position_is_the_comma():
    stmt_tokens_query = "SELECT path FROM blame LIMIT 3, 2"
    with pytest.raises(ParseError) as exc_info:
        _parse(stmt_tokens_query)
    # The comma sits right after "LIMIT 3 " - column 33 (1-based).
    assert exc_info.value.position.column == stmt_tokens_query.index(",") + 1
