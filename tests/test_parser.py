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
