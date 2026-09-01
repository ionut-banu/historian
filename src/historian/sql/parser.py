"""Tokens -> AST.

The third stage of the pipeline in `_docs/spec.md` §3. A hand-written
recursive-descent / precedence-climbing parser over the token list
`sql/lexer.py` produces - no parser generator, no table-driven engine,
per `AGENTS.md`'s "keep the operator layer explicit and boring" (this
module is not the operator layer, but the rule not to lean on Python's
dynamism applies just as much to the piece meant to translate to a
Rust `match` later).

Scope: issue #8, Part A only
-----------------------------

`DISTINCT`, `GROUP BY`, `HAVING`, `ORDER BY`, `LIMIT`, `OFFSET`, any
`JOIN`, and `CASE` are not implemented - see `sql/ast.py`'s module
docstring. Ordinary SQL that uses them fails with a generic
`ParseError` ("expected end of query" or similar), which is correct
for now: naming the six §1 non-goals specifically (subqueries, CTEs,
window functions, `UNION`/`INTERSECT`/`EXCEPT`, outer/cross joins, and
would-be UDFs) by their own dedicated error is issue #24, not this
module. This parser only ever raises `ParseError`.

The precedence table
---------------------

Verified against `sqlite3` 3.51.0 during issue #8's grooming (see the
issue thread and `_docs/decisions.md`), lowest to highest binding:

    OR
    AND
    NOT
    comparison tier 2: =  <>  !=  IS [NOT]  LIKE  IN  BETWEEN
    comparison tier 1: <  <=  >  >=
    +  -  (binary)
    *  /
    ||
    +  -  (unary)

The two comparison tiers are the detail most likely to be got wrong -
a single flat "comparison" level parses every ordinary query the same
way and only disagrees with SQLite on a chained comparison like
`3 = 0 < 3`. Each precedence level below is one method, calling the
next-tighter level for its operands, in exactly this order.

Literal construction and the int64 boundary
---------------------------------------------

An `INTEGER` token's digit text becomes a Python `int` if it fits
SQLite's int64 range, and a Python `float` otherwise - see
`_int_literal_value` and `_docs/decisions.md`, 2026-09-01. This is
construction, not comparison, and is unrelated to the 2026-08-27
"numeric comparison is exact" decision - see that entry and `sql/
ast.py`'s `Literal` docstring.

Nesting depth: two limits, not one
------------------------------------

Deeply nested input must never raise a bare `RecursionError` - spec §5
("never a traceback") and issue #8's round-1 QA finding, recorded in
full in `_docs/decisions.md`, 2026-09-01. That entry has the measured
numbers behind the two constants below; this is the shape of the fix.

A recursive-descent parser pays Python stack frames for genuine
recursion, and `sys.setrecursionlimit` is not a lever available here -
raising it only moves the crash, and catching `RecursionError` after
the fact would make whether a query parses depend on how much stack
the caller already used before calling `parse()`, which breaks the
determinism `AGENTS.md` requires. So depth has to be counted
explicitly and checked before it becomes a Python-level problem, the
same way SQLite counts `SQLITE_MAX_EXPR_DEPTH` rather than relying on
its own C call stack.

But not all recursion in this grammar is equal, which is why there are
two limits:

- A run of `(`, `NOT`, or unary `+`/`-` is pure repetition - `(((x)))`
  is exactly `x`, `NOT NOT NOT x` is `x` wrapped three times - so
  `_parse_primary`, `_parse_not`, and `_parse_unary` each parse their
  run with a loop, not by recursing once per token. A loop costs one
  iteration per token, not one Python stack frame, so these three
  forms are bounded by the generous `_MAX_NESTING_DEPTH` (1000,
  matching SQLite's own documented default).
- Genuine recursion - parsing a *new* sub-expression from within
  another one, which only happens for a parenthesised group's
  contents, an `IN (...)` list value, or a function-call argument -
  still goes through `_parse_expr` calling itself, and every such call
  really does cost Python stack frames. That path is bounded by the
  much smaller `_MAX_RECURSION_DEPTH` (50), sized from measured frame
  costs with margin, not from SQLite's declared limit.

Collapsing this to one limit does not work in either direction: a
value low enough to be safe for genuine recursion is too low to accept
ordinary deeply-parenthesised input SQLite itself accepts, and a value
high enough to match SQLite's declared limit would let genuine
recursion exhaust Python's real call stack before the counter ever
fires.
"""

from __future__ import annotations

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
    SelectItem,
    SelectStatement,
    Star,
    UnaryOp,
    UnaryOperator,
)
from historian.sql.lexer import Position, Token, TokenType

__all__ = ["ParseError", "parse"]

#: SQLite's int64 range. A decimal `INTEGER` literal whose digit text
#: exceeds this becomes a `float` instead of an `int` - see the module
#: docstring and `_docs/decisions.md`, 2026-09-01. The lexer never
#: emits a signed `INTEGER` token (a leading `-` is always its own
#: `MINUS` token), so there is no negative bound to check here.
_INT64_MAX = 9223372036854775807

#: How many `(`, `NOT`, or unary `+`/`-` tokens may run together before
#: `_parse_primary`/`_parse_not`/`_parse_unary` (below) give up and
#: raise `ParseError` instead of continuing. Mirrors SQLite's own
#: `SQLITE_MAX_EXPR_DEPTH`, whose documented default is 1000
#: (confirmed via `PRAGMA compile_options` against the `sqlite3`
#: build used to verify this project's SQL behaviour) - see the
#: module docstring and `_docs/decisions.md`, 2026-09-01, for why this
#: is safe to enforce with a plain counter rather than Python's call
#: stack: each of those three forms is parsed with a loop, not
#: recursion, so a run this long costs one loop iteration per token,
#: not one Python stack frame per token.
_MAX_NESTING_DEPTH = 1000

#: How many times `_parse_expr` may recursively re-enter itself while
#: parsing one query - once per parenthesised group's contents, once
#: per value inside an `IN (...)` list, and once per function-call
#: argument. Unlike `_MAX_NESTING_DEPTH` above, this recursion is real
#: Python call-stack recursion, so it cannot be set anywhere near
#: 1000: see the module docstring and `_docs/decisions.md`,
#: 2026-09-01 for the measured frame costs this is sized against.
_MAX_RECURSION_DEPTH = 50

# Tier-1 comparison tokens (`<  <=  >  >=`) and the operator each maps to.
_RELATIONAL_OPERATORS: dict[TokenType, Operator] = {
    TokenType.LT: Operator.LT,
    TokenType.LE: Operator.LE,
    TokenType.GT: Operator.GT,
    TokenType.GE: Operator.GE,
}

# Binary `+`/`-` and `*`/`/` tokens and the operator each maps to.
_ADDITIVE_OPERATORS: dict[TokenType, Operator] = {
    TokenType.PLUS: Operator.ADD,
    TokenType.MINUS: Operator.SUB,
}
_MULTIPLICATIVE_OPERATORS: dict[TokenType, Operator] = {
    TokenType.STAR: Operator.MUL,
    TokenType.SLASH: Operator.DIV,
}


class ParseError(Exception):
    """The token stream is not a valid v1 SELECT statement.

    Carries the message and the `Position` of the offending token, the
    same shape as `LexError` (`sql/lexer.py`), so a caller can report
    it per `_docs/spec.md` §5 without a traceback ever reaching the
    user.
    """

    def __init__(self, message: str, position: Position) -> None:
        super().__init__(message)
        self.position = position


def parse(tokens: list[Token]) -> SelectStatement:
    """Parse a complete `SELECT` statement from *tokens* (as produced
    by `sql.lexer.tokenize`), including its trailing `EOF`.

    Raises `ParseError` if *tokens* is not a single, complete `SELECT`
    statement - including trailing garbage after one, and including an
    optional trailing `;`, which is consumed if present.
    """
    parser = _Parser(tokens)
    statement = parser.parse_select_statement()
    parser.expect_end()
    return statement


def _describe(token: Token) -> str:
    """A human-readable description of *token* for an error message,
    e.g. `identifier 'LEFT'`, `string 'x'`, `end of query`. Matches
    the shape used in `_docs/spec.md` §5's error examples."""
    if token.type is TokenType.EOF:
        return "end of query"
    if token.type is TokenType.IDENTIFIER:
        return f"identifier {token.text!r}"
    if token.type is TokenType.STRING:
        return f"string {token.text!r}"
    if token.type in (TokenType.INTEGER, TokenType.REAL):
        return f"number {token.text!r}"
    return repr(token.text)


def _int_literal_value(text: str) -> int | float:
    """Convert an `INTEGER` token's digit text to the `Value` a
    `Literal` should hold: a Python `int` if it fits SQLite's int64
    range, a Python `float` otherwise.

    Python's own `int()` never overflows, so this only happens because
    the value is explicitly checked against the int64 bound and
    converted - see the module docstring and `_docs/decisions.md`,
    2026-09-01. int64-min is deliberately not special-cased: see
    `UnaryOp`'s docstring in `sql/ast.py`.
    """
    value = int(text)
    if value > _INT64_MAX:
        return float(text)
    return value


class _Parser:
    """Single-pass recursive-descent parser over `tokens`. Holds a
    cursor index rather than consuming the list, so lookahead
    (`_peek(1)`, `_peek(2)`) is just indexing."""

    def __init__(self, tokens: list[Token]) -> None:
        self._tokens = tokens
        self._index = 0
        # How many nested `_parse_expr` calls are currently on the
        # Python call stack - see `_MAX_RECURSION_DEPTH`.
        self._depth = 0

    # -- cursor -----------------------------------------------------

    def _peek(self, ahead: int = 0) -> Token:
        index = self._index + ahead
        if index >= len(self._tokens):
            # Every token stream ends with EOF (sql/lexer.py), so this
            # only happens when *ahead* looks past it - return that
            # trailing EOF rather than raising, so a `_peek(2)` used
            # for lookahead near the end of a short, malformed query
            # never itself crashes the parser.
            return self._tokens[-1]
        return self._tokens[index]

    def _advance(self) -> Token:
        token = self._peek()
        if token.type is not TokenType.EOF:
            self._index += 1
        return token

    def _check(self, token_type: TokenType) -> bool:
        return self._peek().type is token_type

    def _match(self, token_type: TokenType) -> bool:
        if self._check(token_type):
            self._advance()
            return True
        return False

    def _expect(self, token_type: TokenType, expected: str) -> Token:
        """Consume and return the current token if it is *token_type*,
        else raise `ParseError` naming what was *expected*."""
        token = self._peek()
        if token.type is not token_type:
            raise self._error(f"expected {expected}, found {_describe(token)}")
        return self._advance()

    def _error(self, message: str) -> ParseError:
        return ParseError(message, self._peek().position)

    def expect_end(self) -> None:
        """After a complete statement: consume an optional trailing
        `;`, then require `EOF`. Anything else - a second statement, an
        unconsumed clause this grammar does not implement - is
        rejected here rather than silently ignored."""
        self._match(TokenType.SEMICOLON)
        if not self._check(TokenType.EOF):
            raise self._error(
                f"expected end of query, found {_describe(self._peek())}"
            )

    # -- statement ----------------------------------------------------

    def parse_select_statement(self) -> SelectStatement:
        start = self._expect(TokenType.SELECT, "SELECT").position
        select_list = self._parse_select_list()
        self._expect(TokenType.FROM, "FROM")
        from_table = self._expect(TokenType.IDENTIFIER, "a table name").text
        where: Expr | None = None
        if self._match(TokenType.WHERE):
            where = self._parse_expr()
        return SelectStatement(
            select_list=select_list,
            from_table=from_table,
            where=where,
            position=start,
        )

    def _parse_select_list(self) -> tuple[SelectItem, ...]:
        items = [self._parse_select_item()]
        while self._match(TokenType.COMMA):
            items.append(self._parse_select_item())
        return tuple(items)

    def _parse_select_item(self) -> SelectItem:
        start = self._peek().position
        expr = self._parse_star_or_expr()
        alias: str | None = None
        if self._match(TokenType.AS):
            alias = self._expect(TokenType.IDENTIFIER, "an alias name").text
        return SelectItem(expr=expr, alias=alias, position=start)

    def _parse_star_or_expr(self) -> Expr:
        """A select-list item's expression, with `*`/`table.*` handled
        first: `*` cannot start any other expression (it is a binary
        operator token everywhere else), so seeing it here is
        unambiguous."""
        if self._check(TokenType.STAR):
            token = self._advance()
            return Star(table=None, position=token.position)
        if (
            self._check(TokenType.IDENTIFIER)
            and self._peek(1).type is TokenType.DOT
            and self._peek(2).type is TokenType.STAR
        ):
            table_token = self._advance()  # IDENTIFIER
            self._advance()  # DOT
            self._advance()  # STAR
            return Star(table=table_token.text, position=table_token.position)
        return self._parse_expr()

    # -- expressions, loosest to tightest -----------------------------

    def _parse_expr(self) -> Expr:
        """Parse one expression. The single choke point every genuine
        recursive re-entry passes through - a parenthesised group's
        contents, an `IN (...)` list value, a function-call argument -
        so it is where `_MAX_RECURSION_DEPTH` is enforced, before
        Python's own call stack ever gets close to its limit. See the
        module docstring."""
        start = self._peek()
        self._depth += 1
        if self._depth > _MAX_RECURSION_DEPTH:
            self._depth -= 1
            raise ParseError(
                "expression nested too deeply "
                f"(max {_MAX_RECURSION_DEPTH} levels of parentheses, "
                "IN, or function-call nesting)",
                start.position,
            )
        try:
            return self._parse_or()
        finally:
            self._depth -= 1

    def _parse_or(self) -> Expr:
        left = self._parse_and()
        while self._check(TokenType.OR):
            self._advance()
            right = self._parse_and()
            left = Or(left=left, right=right, position=left.position)
        return left

    def _parse_and(self) -> Expr:
        left = self._parse_not()
        while self._check(TokenType.AND):
            self._advance()
            right = self._parse_not()
            left = And(left=left, right=right, position=left.position)
        return left

    def _parse_not(self) -> Expr:
        """`NOT NOT NOT x` nests. An explicit loop rather than the
        obvious `self._parse_not()` self-recursion for the operand:
        that would cost one Python stack frame per `NOT`, and this is
        one of the two forms `_MAX_NESTING_DEPTH` (not
        `_MAX_RECURSION_DEPTH`) governs precisely because it doesn't
        have to - see the module docstring."""
        positions: list[Position] = []
        while self._check(TokenType.NOT):
            token = self._advance()
            positions.append(token.position)
            if len(positions) > _MAX_NESTING_DEPTH:
                raise ParseError(
                    "expression nested too deeply "
                    f"(max {_MAX_NESTING_DEPTH} levels of NOT)",
                    token.position,
                )
        node = self._parse_comparison()
        for position in reversed(positions):
            node = Not(operand=node, position=position)
        return node

    def _parse_comparison(self) -> Expr:
        """Comparison tier 2: `=  <>  !=  IS [NOT]  LIKE  IN  BETWEEN`,
        each optionally preceded by `NOT` for `LIKE`/`IN`/`BETWEEN`
        (that `NOT` is this operator's own modifier, not the general
        prefix `Not` node - it can only appear here, after a left
        operand already exists). A loop, not a single check: SQLite
        chains same-tier comparisons left-associatively
        (`1 = 1 = 1`, `1 IN (1,2) = 1` are both valid and confirmed
        against `sqlite3` during grooming), so after building one
        tier-2 node the loop checks again for another.
        """
        left = self._parse_relational()
        while True:
            token = self._peek()
            if token.type in (TokenType.EQ, TokenType.NE):
                self._advance()
                op = Operator.EQ if token.type is TokenType.EQ else Operator.NE
                right = self._parse_relational()
                left = BinaryOp(op=op, left=left, right=right, position=left.position)
            elif token.type is TokenType.IS:
                left = self._parse_is(left)
            elif token.type is TokenType.LIKE:
                self._advance()
                pattern = self._parse_relational()
                left = Like(
                    left=left, pattern=pattern, negated=False, position=left.position
                )
            elif token.type is TokenType.IN:
                self._advance()
                values = self._parse_in_list()
                left = In(
                    left=left, values=values, negated=False, position=left.position
                )
            elif token.type is TokenType.BETWEEN:
                self._advance()
                low, high = self._parse_between_bounds()
                left = Between(
                    operand=left,
                    low=low,
                    high=high,
                    negated=False,
                    position=left.position,
                )
            elif token.type is TokenType.NOT:
                left = self._parse_negated_comparison(left)
            else:
                return left

    def _parse_is(self, left: Expr) -> Expr:
        self._advance()  # IS
        negated = self._match(TokenType.NOT)
        if self._check(TokenType.NULL):
            null_token = self._advance()
            right: Expr = Literal(value=None, position=null_token.position)
        else:
            right = self._parse_relational()
        return Is(left=left, right=right, negated=negated, position=left.position)

    def _parse_negated_comparison(self, left: Expr) -> Expr:
        """`left NOT LIKE ...` / `left NOT IN (...)` /
        `left NOT BETWEEN ... AND ...`, having already seen the `NOT`
        token pending at the front of the stream."""
        self._advance()  # NOT
        if self._check(TokenType.LIKE):
            self._advance()
            pattern = self._parse_relational()
            return Like(
                left=left, pattern=pattern, negated=True, position=left.position
            )
        if self._check(TokenType.IN):
            self._advance()
            values = self._parse_in_list()
            return In(left=left, values=values, negated=True, position=left.position)
        if self._check(TokenType.BETWEEN):
            self._advance()
            low, high = self._parse_between_bounds()
            return Between(
                operand=left, low=low, high=high, negated=True, position=left.position
            )
        raise self._error(
            "expected LIKE, IN or BETWEEN after NOT, found "
            f"{_describe(self._peek())}"
        )

    def _parse_in_list(self) -> tuple[Expr, ...]:
        self._expect(TokenType.LPAREN, "'(' after IN")
        if self._check(TokenType.RPAREN):
            self._advance()
            return ()  # `IN ()` is valid SQL - always false, confirmed
            # against sqlite3 during grooming.
        values = [self._parse_expr()]
        while self._match(TokenType.COMMA):
            values.append(self._parse_expr())
        self._expect(TokenType.RPAREN, "')'")
        return tuple(values)

    def _parse_between_bounds(self) -> tuple[Expr, Expr]:
        """`BETWEEN low AND high`. Both bounds are parsed at tier 1
        (`_parse_relational`), one level tighter than comparison tier 2
        itself - consuming the `AND` directly here, rather than
        recursing back into `_parse_and`, is what stops it from
        swallowing a trailing `AND <predicate>` that follows the whole
        `BETWEEN` (issue #8's grooming)."""
        low = self._parse_relational()
        self._expect(TokenType.AND, "AND")
        high = self._parse_relational()
        return low, high

    def _parse_relational(self) -> Expr:
        """Comparison tier 1: `<  <=  >  >=`."""
        left = self._parse_additive()
        while True:
            op = _RELATIONAL_OPERATORS.get(self._peek().type)
            if op is None:
                return left
            self._advance()
            right = self._parse_additive()
            left = BinaryOp(op=op, left=left, right=right, position=left.position)

    def _parse_additive(self) -> Expr:
        """Binary `+`/`-`."""
        left = self._parse_multiplicative()
        while True:
            op = _ADDITIVE_OPERATORS.get(self._peek().type)
            if op is None:
                return left
            self._advance()
            right = self._parse_multiplicative()
            left = BinaryOp(op=op, left=left, right=right, position=left.position)

    def _parse_multiplicative(self) -> Expr:
        """`*`/`/`."""
        left = self._parse_concat()
        while True:
            op = _MULTIPLICATIVE_OPERATORS.get(self._peek().type)
            if op is None:
                return left
            self._advance()
            right = self._parse_concat()
            left = BinaryOp(op=op, left=left, right=right, position=left.position)

    def _parse_concat(self) -> Expr:
        """`||`, binding tighter than `*`/`/` - confirmed against
        `sqlite3` during grooming (`'a' || 1 + 1` is `1`, matching
        `('a' || 1) + 1`, not `'a' || (1 + 1)` which is `'a2'`)."""
        left = self._parse_unary()
        while self._check(TokenType.CONCAT):
            self._advance()
            right = self._parse_unary()
            left = BinaryOp(
                op=Operator.CONCAT, left=left, right=right, position=left.position
            )
        return left

    def _parse_unary(self) -> Expr:
        """Prefix `+`/`-`, the tightest-binding operators in the
        table. `--x`/`+-x` nest, via an explicit loop rather than
        `self._parse_unary()` self-recursion on the operand - the same
        reasoning as `_parse_not` above: this is governed by
        `_MAX_NESTING_DEPTH`, not `_MAX_RECURSION_DEPTH`, because a
        loop costs no Python stack per `+`/`-`."""
        ops: list[tuple[UnaryOperator, Position]] = []
        while True:
            token = self._peek()
            if token.type is TokenType.PLUS:
                op = UnaryOperator.POS
            elif token.type is TokenType.MINUS:
                op = UnaryOperator.NEG
            else:
                break
            self._advance()
            ops.append((op, token.position))
            if len(ops) > _MAX_NESTING_DEPTH:
                raise ParseError(
                    "expression nested too deeply "
                    f"(max {_MAX_NESTING_DEPTH} levels of unary +/-)",
                    token.position,
                )
        node = self._parse_primary()
        for op, position in reversed(ops):
            node = UnaryOp(op=op, operand=node, position=position)
        return node

    # -- primary expressions ------------------------------------------

    def _parse_primary(self) -> Expr:
        token = self._peek()
        if token.type is TokenType.INTEGER:
            self._advance()
            value = _int_literal_value(token.text)
            return Literal(value=value, position=token.position)
        if token.type is TokenType.REAL:
            self._advance()
            return Literal(value=float(token.text), position=token.position)
        if token.type is TokenType.STRING:
            self._advance()
            return Literal(value=token.text, position=token.position)
        if token.type is TokenType.NULL:
            self._advance()
            return Literal(value=None, position=token.position)
        if token.type is TokenType.LPAREN:
            # A run of `(` is stripped with a loop, not by recursing
            # once per paren: `(expr)` produces no AST node of its own
            # (`inner` is returned unchanged below), so however many
            # parens wrap one expression, only one `_parse_expr` call
            # is needed for its contents. Bounded by
            # `_MAX_NESTING_DEPTH`, not `_MAX_RECURSION_DEPTH` - the
            # module docstring explains why this one form gets the
            # much larger limit.
            depth = 0
            while self._check(TokenType.LPAREN):
                paren = self._advance()
                depth += 1
                if depth > _MAX_NESTING_DEPTH:
                    raise ParseError(
                        "expression nested too deeply "
                        f"(max {_MAX_NESTING_DEPTH} levels of parentheses)",
                        paren.position,
                    )
            inner = self._parse_expr()
            for _ in range(depth):
                self._expect(TokenType.RPAREN, "')'")
            return inner
        if token.type is TokenType.IDENTIFIER:
            return self._parse_identifier_primary()
        raise self._error(f"expected expression, found {_describe(token)}")

    def _parse_identifier_primary(self) -> Expr:
        """A bare identifier, resolved to one of three shapes by what
        follows it: `name(` is a `FunctionCall`, `name.` is a
        table-qualified `ColumnRef` (or `Star`, for `name.*`), and
        anything else is a bare `ColumnRef`."""
        first = self._advance()
        if self._check(TokenType.LPAREN):
            return self._parse_function_call(first)
        if self._check(TokenType.DOT):
            self._advance()
            if self._check(TokenType.STAR):
                self._advance()
                return Star(table=first.text, position=first.position)
            name = self._expect(TokenType.IDENTIFIER, "a column name after '.'").text
            return ColumnRef(table=first.text, name=name, position=first.position)
        return ColumnRef(table=None, name=first.text, position=first.position)

    def _parse_function_call(self, name_token: Token) -> Expr:
        self._advance()  # LPAREN
        args: list[Expr] = []
        if not self._check(TokenType.RPAREN):
            args.append(self._parse_function_arg())
            while self._match(TokenType.COMMA):
                args.append(self._parse_function_arg())
        self._expect(TokenType.RPAREN, "')'")
        return FunctionCall(
            name=name_token.text, args=tuple(args), position=name_token.position
        )

    def _parse_function_arg(self) -> Expr:
        """A function-call argument, with a bare `*` (`count(*)`)
        handled first the same way `_parse_star_or_expr` handles a
        select-list `*` - it cannot start any other expression, so it
        is unambiguous here too."""
        if self._check(TokenType.STAR):
            token = self._advance()
            return Star(table=None, position=token.position)
        return self._parse_expr()
