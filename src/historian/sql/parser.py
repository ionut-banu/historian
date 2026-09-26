"""Tokens -> AST.

The third stage of the pipeline in `_docs/spec.md` §3. A hand-written
recursive-descent / precedence-climbing parser over the token list
`sql/lexer.py` produces - no parser generator, no table-driven engine,
per `AGENTS.md`'s "keep the operator layer explicit and boring" (this
module is not the operator layer, but the rule not to lean on Python's
dynamism applies just as much to the piece meant to translate to a
Rust `match` later).

Scope: issue #8, Part A; `GROUP BY`/`HAVING` added by #69;
`ORDER BY` added by #61; `LIMIT`/`OFFSET` added by #77;
`DISTINCT` added by #78
-------------------------------------------------------------

Any `JOIN` and `CASE` are not implemented - see `sql/ast.py`'s module
docstring. Ordinary SQL that uses them fails with a generic
`ParseError` ("expected end of query" or similar), which is correct
for now: naming the six §1 non-goals specifically (subqueries, CTEs,
window functions, `UNION`/`INTERSECT`/`EXCEPT`, outer/cross joins, and
would-be UDFs) by their own dedicated error is issue #24, not this
module. This parser only ever raises `ParseError`.

`SELECT [DISTINCT] <select_list> ...` (issue #78): an optional
`DISTINCT` keyword is read immediately after `SELECT`, before the
select list - `parse_select_statement` just checks for the token with
`self._match(TokenType.DISTINCT)` right after consuming `SELECT`
itself, nowhere near the select list's own parsing or anything else
in the grammar. `sql/ast.py`'s `SelectStatement.distinct` carries the
result straight through, `False` when the keyword is absent.

`GROUP BY <expr>, ...` and `HAVING <predicate>` (issue #69) parse
after `WHERE` and before end-of-statement - `GROUP BY`'s list is
ordinary comma-separated expressions (an ordinal like `GROUP BY 2`
is just an `INTEGER` literal here; resolving it to a select-list
position is the binder's job), and `HAVING` is one expression at the
same precedence as `WHERE`'s.

`ORDER BY <expr | ordinal> [ASC | DESC], ...` (issue #61) parses after
`HAVING` and before end-of-statement - a comma-separated list of
`OrderByItem`s, each one ordinary-expression-or-ordinal (an ordinal
like `ORDER BY 2` is, again, just an `INTEGER` literal here - and
`ORDER BY -1` is `UnaryOp(NEG, Literal(1, ...))`, since the lexer
never emits a signed `INTEGER` token; telling a negative ordinal from
a negative-valued expression is the binder's job, not this module's)
followed by an optional `ASC`/`DESC`, defaulting to `ASC` when
neither is written.

`LIMIT <expr> [OFFSET <expr>]` (issue #77) parses after `ORDER BY` and
before end-of-statement - `<expr>` is parsed generically via
`_parse_expr()`, exactly like `ORDER BY`'s own item, deferring the
literal-integer-vs-anything-else decision to the binder (`sql/
binder.py`'s `_ordinal_value`, reused unchanged - see that module's
own docstring). The one thing this module *does* decide is the comma
form (`LIMIT m, n`): §1's grammar has no comma in it, it is a
deliberate v2 non-goal (issue #77's own grooming), and a bound `LIMIT`
expression immediately followed by `,` is recognised and rejected here
with a message naming the comma form and pointing at `OFFSET` instead
- not the generic "expected end of query" a stray comma would
otherwise produce via `expect_end()`. `OFFSET` with no preceding
`LIMIT` is not a grammar this module recognises at all (§1 has no bare
`OFFSET`) - it is simply unconsumed trailing input, caught the same
generic way any other unsupported clause is.

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
    OrderByItem,
    OrderDirection,
    Or,
    SelectItem,
    SelectStatement,
    Star,
    UnaryOp,
    UnaryOperator,
)
from historian.sql.lexer import Position, Token, TokenType
from historian.values import INT64_MAX

__all__ = ["ParseError", "UnsupportedGrammarError", "parse"]

# `INT64_MAX` (`historian.values`, issue #53): a decimal `INTEGER`
# literal whose digit text exceeds this becomes a `float` instead of
# an `int` - see the module docstring and `_docs/decisions.md`,
# 2026-09-01. The lexer never emits a signed `INTEGER` token (a
# leading `-` is always its own `MINUS` token), so there is no
# negative bound to check here - unlike `exec/expression.py`, which
# needs `INT64_MIN` too.

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
    TokenType.PERCENT: Operator.MOD,
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


def _unsupported_grammar_message(feature: str) -> str:
    """The shared three-line message §5 shows for a permanently
    out-of-scope construct, e.g.:

        window functions are not supported
          historian implements a subset of SQL. See the non-goals in
          _docs/spec.md §1.

    *feature* is a short phrase - "CTEs", "subqueries", "window
    functions", "compound queries (UNION)"/"compound queries
    (INTERSECT)"/"compound queries (EXCEPT)", or "outer and cross
    joins" - matching §1's own wording where §1 names the construct
    directly (`_docs/spec.md` §1). Always grammatically plural, so the
    fixed "{feature} are not supported" template agrees for every
    caller: a bare "UNION are not supported" reads wrong, since UNION
    is a singular keyword, not a plural feature name - wrapped in
    "compound queries (...)" instead of passed bare, matching SQLite's
    own term for `UNION`/`INTERSECT`/`EXCEPT` ("compound select
    statement") while still naming the specific keyword seen."""
    return (
        f"{feature} are not supported\n"
        "  historian implements a subset of SQL. See the non-goals in\n"
        "  _docs/spec.md §1."
    )


class UnsupportedGrammarError(ParseError):
    """One of §1's six reachable non-goals - subqueries, CTEs, window
    functions, `UNION`/`INTERSECT`/`EXCEPT`, or outer/cross joins -
    rather than v1 grammar that simply has not been built yet (which
    stays a plain `ParseError`, e.g. `INNER JOIN` and plain `JOIN`,
    v1 grammar §6 phase 3 hasn't built, or `NATURAL JOIN`, which §1's
    literal "outer and cross joins" wording does not name).

    Deliberately a subclass of `ParseError`, not a new sibling
    exception (issue #24's grooming, correcting issue #8's original
    proposal): `cli.py`'s `except (LexError, ParseError, BindError,
    EvalError)` clause, a closed set fixed by issue #49, catches this
    via `isinstance` with zero changes to `cli.py` - exit code `1`,
    same as any other bad query, not `4`'s "a bug in historian"
    backstop. See `_docs/spec.md` §3 ("Unsupported grammar") and §5
    for the message shape this carries."""

    def __init__(self, feature: str, position: Position) -> None:
        super().__init__(_unsupported_grammar_message(feature), position)


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
    if value > INT64_MAX:
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

    def _unsupported(self, feature: str) -> UnsupportedGrammarError:
        """`UnsupportedGrammarError` naming *feature*, positioned at
        the current (not-yet-consumed) token - issue #24's six §1
        non-goal detection sites all raise via this, mirroring
        `_error`'s shape for the ordinary case."""
        return UnsupportedGrammarError(feature, self._peek().position)

    def expect_end(self) -> None:
        """After a complete statement: consume an optional trailing
        `;`, then require `EOF`. Anything else - a second statement, an
        unconsumed clause this grammar does not implement - is
        rejected here rather than silently ignored.

        `UNION`/`INTERSECT`/`EXCEPT` (issue #24) are checked first,
        by token text since none of the three are lexer keywords: the
        query is otherwise complete, so this is the only place they
        can be recognised specifically rather than falling into the
        generic "expected end of query" message below. Named as
        "compound queries (<KEYWORD>)", not the bare keyword - `UNION`
        etc. are singular, and the shared message template
        ("{feature} are not supported") needs a grammatically plural
        subject; a bare "UNION are not supported" reads wrong."""
        if self._check(TokenType.IDENTIFIER):
            word = self._peek().text.upper()
            if word in ("UNION", "INTERSECT", "EXCEPT"):
                raise self._unsupported(f"compound queries ({word})")
        self._match(TokenType.SEMICOLON)
        if not self._check(TokenType.EOF):
            raise self._error(
                f"expected end of query, found {_describe(self._peek())}"
            )

    # -- statement ----------------------------------------------------

    def parse_select_statement(self) -> SelectStatement:
        # CTEs (issue #24): `WITH` is not a lexer keyword, so this is
        # a text check, fired only when `WITH` is the very first token
        # of the whole query - `parse_select_statement` is only ever
        # called once, at the top level, never recursively for a
        # subquery (those are rejected on sight in `_parse_primary`
        # and the `FROM`-position check below, not by parsing a nested
        # statement), so "first token of `parse_select_statement`"
        # already means "first token of the query".
        if self._check(TokenType.IDENTIFIER) and self._peek().text.upper() == "WITH":
            raise self._unsupported("CTEs")
        start = self._expect(TokenType.SELECT, "SELECT").position
        distinct = self._match(TokenType.DISTINCT)
        select_list = self._parse_select_list()
        if self._check(TokenType.IDENTIFIER):
            # An identifier here, instead of `,` or `FROM`, is almost
            # always a bare select-list alias - `SELECT path p FROM
            # blame` - since `AS` is mandatory (spec §1; see
            # `_docs/decisions.md`, 2026-09-18, for why it stays that
            # way). The plain `_expect(FROM, ...)` below would report
            # this as "expected FROM, found identifier 'p'", which
            # never mentions AS and leaves a reader of that message no
            # closer to knowing what to add.
            raise self._error(
                "expected ',' or FROM, found "
                f"{_describe(self._peek())} - a select-list alias "
                "requires AS before it"
            )
        self._expect(TokenType.FROM, "FROM")
        # Subquery in FROM (issue #24): `FROM` expects a bare
        # `IDENTIFIER` and never reaches `_parse_primary`'s own
        # subquery check below, so this needs its own - fired only
        # when `(` is *immediately* followed by the `SELECT` keyword,
        # so `FROM (garbage)` (malformed, not a subquery) still falls
        # through to the ordinary "expected a table name" message.
        if self._check(TokenType.LPAREN) and self._peek(1).type is TokenType.SELECT:
            raise self._unsupported("subqueries")
        from_table = self._expect(TokenType.IDENTIFIER, "a table name").text
        # Outer and cross joins (issue #24, keyed on the token
        # *sequence* per the orchestrator's amendment, not the bare
        # word): historian has no table-alias grammar yet, so any
        # identifier immediately after the bare table name is already
        # a guaranteed error today. But `INNER JOIN` is v1 grammar not
        # yet built, and joins usually bring aliases (`FROM blame b
        # JOIN ...`) - if this fired on a bare `LEFT`/`RIGHT`/`FULL`/
        # `CROSS` identifier here, adding table aliases later would
        # silently turn `FROM blame left` (an alias named `left`) into
        # a false "not supported" error with nothing to signal the
        # regression. So: `LEFT`/`RIGHT`/`FULL` must be followed by
        # `JOIN` or by `OUTER JOIN`, and `CROSS` must be followed by
        # `JOIN` - `JOIN` is a reserved keyword, so the lookahead is
        # unambiguous. A bare `LEFT` with no following `JOIN` falls
        # through unchanged to today's ordinary error.
        if self._check(TokenType.IDENTIFIER):
            word = self._peek().text.upper()
            if word in ("LEFT", "RIGHT", "FULL", "CROSS"):
                after = self._peek(1)
                if after.type is TokenType.JOIN:
                    raise self._unsupported("outer and cross joins")
                if (
                    word != "CROSS"
                    and after.type is TokenType.IDENTIFIER
                    and after.text.upper() == "OUTER"
                    and self._peek(2).type is TokenType.JOIN
                ):
                    raise self._unsupported("outer and cross joins")
        where: Expr | None = None
        if self._match(TokenType.WHERE):
            where = self._parse_expr()
        group_by: tuple[Expr, ...] = ()
        if self._match(TokenType.GROUP):
            self._expect(TokenType.BY, "BY")
            group_by = self._parse_expr_list()
        having: Expr | None = None
        if self._match(TokenType.HAVING):
            having = self._parse_expr()
        order_by: tuple[OrderByItem, ...] = ()
        if self._match(TokenType.ORDER):
            self._expect(TokenType.BY, "BY")
            order_by = self._parse_order_by_list()
        limit: Expr | None = None
        offset: Expr | None = None
        if self._match(TokenType.LIMIT):
            limit = self._parse_expr()
            if self._check(TokenType.COMMA):
                raise self._error(
                    "LIMIT m, n is not supported - write LIMIT n OFFSET m instead"
                )
            if self._match(TokenType.OFFSET):
                offset = self._parse_expr()
        return SelectStatement(
            select_list=select_list,
            from_table=from_table,
            where=where,
            group_by=group_by,
            having=having,
            order_by=order_by,
            limit=limit,
            offset=offset,
            position=start,
            distinct=distinct,
        )

    def _parse_expr_list(self) -> tuple[Expr, ...]:
        exprs = [self._parse_expr()]
        while self._match(TokenType.COMMA):
            exprs.append(self._parse_expr())
        return tuple(exprs)

    def _parse_order_by_list(self) -> tuple[OrderByItem, ...]:
        items = [self._parse_order_by_item()]
        while self._match(TokenType.COMMA):
            items.append(self._parse_order_by_item())
        return tuple(items)

    def _parse_order_by_item(self) -> OrderByItem:
        """One `ORDER BY` key: an expression or ordinal, then an
        optional `ASC`/`DESC` - `ASC` when neither is written, matching
        `sql/ast.py`'s `OrderByItem` docstring."""
        start = self._peek().position
        expr = self._parse_expr()
        direction = OrderDirection.ASC
        if self._match(TokenType.ASC):
            direction = OrderDirection.ASC
        elif self._match(TokenType.DESC):
            direction = OrderDirection.DESC
        return OrderByItem(expr=expr, direction=direction, position=start)

    def _parse_select_list(self) -> tuple[SelectItem, ...]:
        items = [self._parse_select_item()]
        while self._match(TokenType.COMMA):
            items.append(self._parse_select_item())
        return tuple(items)

    def _parse_select_item(self) -> SelectItem:
        start = self._peek().position
        expr = self._parse_star_or_expr()
        alias: str | None = None
        if self._check(TokenType.AS):
            # `*`/`table.*` cannot take an alias - confirmed against
            # sqlite3 3.51.0 ("near \"AS\": syntax error") during
            # issue #31's grooming. A `FunctionCall` wrapping a Star
            # (`count(*) AS n`) is unaffected: the alias attaches to
            # the call, not to the Star inside it, so this check is
            # only reached when *this* select item's own expression is
            # a bare or qualified star.
            if isinstance(expr, Star):
                raise self._error("AS is not allowed after '*'")
            self._advance()
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
                escape = self._parse_optional_escape()
                left = Like(
                    left=left,
                    pattern=pattern,
                    negated=False,
                    position=left.position,
                    escape=escape,
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
            escape = self._parse_optional_escape()
            return Like(
                left=left,
                pattern=pattern,
                negated=True,
                position=left.position,
                escape=escape,
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

    def _parse_optional_escape(self) -> Expr | None:
        """`[ESCAPE <expr>]`, trailing a `LIKE`/`NOT LIKE` pattern
        (issue #51). The escape operand parses at tier 1
        (`_parse_relational`), exactly like `pattern` itself - an
        arbitrary expression, not restricted to a `STRING` literal.
        Confirmed against `sqlite3` 3.51.0: `select '10%' like '10' ||
        '!%' escape ('!');` and `select '10%' like '10!%' escape
        substr('!x',1,1);` both run and return `1`. Returns `None` when
        no `ESCAPE` clause is present."""
        if not self._match(TokenType.ESCAPE):
            return None
        return self._parse_relational()

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
        if token.type is TokenType.SELECT:
            # Subqueries (issue #24): every one of a parenthesised
            # expression (`x = (SELECT ...)`), an `IN (...)` list
            # value, and a function-call argument already funnels
            # through here via `_parse_expr`, so this single check
            # covers all three at once - and, as a consequence nobody
            # had to build separately, `EXISTS (SELECT ...)` too:
            # `EXISTS` is not a lexer keyword, so it parses as an
            # ordinary function call whose sole argument hits this
            # same check. "Correlated anything" is subsumed here too -
            # v1 has no subquery grammar for anything to correlate
            # from. The fourth site, `FROM (SELECT ...)`, does not
            # reach `_parse_primary` at all - see the dedicated check
            # in `parse_select_statement`.
            raise self._unsupported("subqueries")
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
        """A bare identifier, resolved to one of two shapes by what
        follows it: `name(` is a `FunctionCall`, `name.` is a
        table-qualified `ColumnRef`, and anything else is a bare
        `ColumnRef`.

        `name.*` is deliberately not a third shape here: a `Star` may
        only be built at the two sanctioned call sites -
        `_parse_star_or_expr` (a whole select-list item) and
        `_parse_function_call` (a function's sole bare argument) - and
        both recognise `identifier.*`/bare `*` themselves, before ever
        reaching this method. Every other expression context (an
        arithmetic or comparison operand, a parenthesised group, an
        `IN (...)` list value, a non-sole function argument) parses an
        identifier through here, so `table.*` there now falls through
        to the ordinary `_expect(IDENTIFIER, ...)` below, which raises
        `ParseError` pointing at the `*` token - exactly where sqlite3
        points (issue #31)."""
        first = self._advance()
        if self._check(TokenType.LPAREN):
            return self._parse_function_call(first)
        if self._check(TokenType.DOT):
            self._advance()
            name = self._expect(TokenType.IDENTIFIER, "a column name after '.'").text
            return ColumnRef(table=first.text, name=name, position=first.position)
        return ColumnRef(table=None, name=first.text, position=first.position)

    def _parse_function_call(self, name_token: Token) -> Expr:
        """A call is exactly one of three shapes, matching sqlite3's
        own grammar (confirmed during issue #31's grooming) - never a
        mix of them: no arguments (`count()`), the single bare token
        `*` (`count(*)`, legal for any function name - arity and
        name validation happen later, not in the parser), or an
        ordinary comma-separated expression list. A bare `*` is
        recognised once, before that list is entered, so it can never
        appear anywhere else in it: `foo(1, *)` and `foo(*, 1)` both
        fail naturally - the first in `_parse_primary`, which has no
        `STAR` case, and the second when `,` is found where `)` was
        expected.

        `DISTINCT` (issue #84) is read only on the third shape, right
        after `(` and before the first argument - the no-argument and
        bare-`*` shapes above are both checked first and return before
        `DISTINCT` is ever consulted, so `count(DISTINCT *)` never
        reaches this branch: `*` is not a legal token in
        `_parse_expr()`, so it fails there the same way `foo(1, *)`
        already does. Legal for any function name, matching how a bare
        `*` already is - arity and name validation happen later, not
        in the parser."""
        self._advance()  # LPAREN
        call: Expr
        if self._check(TokenType.RPAREN):
            self._advance()
            call = FunctionCall(
                name=name_token.text, args=(), position=name_token.position
            )
        elif self._check(TokenType.STAR):
            star_token = self._advance()
            self._expect(TokenType.RPAREN, "')'")
            call = FunctionCall(
                name=name_token.text,
                args=(Star(table=None, position=star_token.position),),
                position=name_token.position,
            )
        else:
            distinct = self._match(TokenType.DISTINCT)
            args = [self._parse_expr()]
            while self._match(TokenType.COMMA):
                args.append(self._parse_expr())
            self._expect(TokenType.RPAREN, "')'")
            call = FunctionCall(
                name=name_token.text,
                args=tuple(args),
                position=name_token.position,
                distinct=distinct,
            )
        # Window functions (issue #24): checked right here, at the
        # source of every call, rather than at the select-item
        # fallthrough that used to surface this as a confusing
        # "a select-list alias requires AS" error three levels up.
        # `OVER` is not a lexer keyword, so this is a text check.
        # Checking here (not only when the call is a whole top-level
        # select item) also catches a call buried inside a larger
        # expression, e.g. `1 + count(*) OVER (...)`.
        if self._check(TokenType.IDENTIFIER) and self._peek().text.upper() == "OVER":
            raise self._unsupported("window functions")
        return call
