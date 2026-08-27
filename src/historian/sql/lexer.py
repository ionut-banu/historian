"""SQL text -> tokens.

The first stage of the pipeline in `_docs/spec.md` §3. Every token
carries a `Position` so that a query the parser cannot make sense of -
and later, a query the binder cannot resolve - can be reported pointing
at the offending character, per §5's error format, rather than as a
traceback.

Positions are character-based, not byte-based
----------------------------------------------

`Position.offset` is a Unicode code-point index into the source string
(`len()`/slicing semantics), and `Position.column` counts code points
too. Query text - and the `blame.line` values that end up embedded in
it inside string literals - can contain non-ASCII characters, and a
byte offset would silently misalign the `^` in §5's error display the
first time one appeared before the error column. Python strings already
index by code point, so this is "don't convert to bytes anywhere," not
new machinery.

Token type set
--------------

One `TokenType` member per v1 keyword (`TokenType.SELECT`, not a shared
`KEYWORD` type carrying text) - see `_docs/decisions.md`, 2026-08-27,
"One token type per keyword, not a shared KEYWORD type". A mistyped
keyword literal at a parser call site (`TokenType.SELCT`) then fails
loud, as `AttributeError` at import, instead of silently never matching
a shared type's text comparison.

Aggregate and scalar function names (`count`, `sum`, ...) are
deliberately not keywords - they lex as plain `IDENTIFIER`, and it is
the parser's job to recognise `IDENTIFIER LPAREN` as a call. SQLite
allows a column named `count`; making it a keyword here would take that
away before the parser gets a say.

Which errors are the lexer's
-----------------------------

`LexError` is raised for exactly three things, each confirmed against
`sqlite3` rather than assumed: an unterminated single-quoted string, an
unterminated double-quoted identifier, and a character that starts no
valid v1 token. An identifier that is not a keyword, or is not a real
column, is not a lexer error - that is `sql/binder.py`'s job, and it
needs schema context this module never has.

Not in this module
-------------------

Parsing, precedence, and any tree structure (`sql/parser.py`).
Scientific notation (`1e10`), hex integers (`0x1F`), `==` as an alias
for `=`, and `/* */` block comments are real SQLite syntax but are not
part of the v1 grammar in §1 - deferred to issue #6. Boolean literals
are deferred too; `historian.values.Value` deliberately excludes `bool`
(see its module docstring).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto

__all__ = [
    "KEYWORD_TYPES",
    "LexError",
    "Position",
    "Token",
    "TokenType",
    "is_keyword",
    "tokenize",
]


class TokenType(Enum):
    """Every kind of token the v1 grammar can produce.

    Keywords each get their own member (see the module docstring); the
    rest are one member per literal kind or punctuation mark, plus a
    single `EOF` that always terminates the stream so the parser never
    has to special-case running off the end.
    """

    # Keywords - the v1 grammar's closed 30-keyword set (_docs/spec.md §1).
    SELECT = auto()
    DISTINCT = auto()
    AS = auto()
    FROM = auto()
    INNER = auto()
    JOIN = auto()
    ON = auto()
    USING = auto()
    WHERE = auto()
    GROUP = auto()
    BY = auto()
    HAVING = auto()
    ORDER = auto()
    ASC = auto()
    DESC = auto()
    LIMIT = auto()
    OFFSET = auto()
    AND = auto()
    OR = auto()
    NOT = auto()
    LIKE = auto()
    IN = auto()
    BETWEEN = auto()
    IS = auto()
    NULL = auto()
    CASE = auto()
    WHEN = auto()
    THEN = auto()
    ELSE = auto()
    END = auto()

    # Literals and names.
    IDENTIFIER = auto()
    STRING = auto()
    INTEGER = auto()
    REAL = auto()

    # Operators and punctuation.
    EQ = auto()  # =
    NE = auto()  # <> or !=
    LT = auto()  # <
    LE = auto()  # <=
    GT = auto()  # >
    GE = auto()  # >=
    PLUS = auto()  # +
    MINUS = auto()  # -
    STAR = auto()  # *
    SLASH = auto()  # /
    CONCAT = auto()  # ||
    LPAREN = auto()  # (
    RPAREN = auto()  # )
    COMMA = auto()  # ,
    DOT = auto()  # .
    SEMICOLON = auto()  # ;

    EOF = auto()


#: The keyword `TokenType` members, exactly - a test in `tests/
#: test_lexer.py` asserts this set equals the v1 grammar's 30-keyword
#: list, so a keyword added to the grammar later that the lexer forgets
#: to wire up fails a test here rather than surfacing as a silent parse
#: gap.
KEYWORD_TYPES: frozenset[TokenType] = frozenset(
    {
        TokenType.SELECT,
        TokenType.DISTINCT,
        TokenType.AS,
        TokenType.FROM,
        TokenType.INNER,
        TokenType.JOIN,
        TokenType.ON,
        TokenType.USING,
        TokenType.WHERE,
        TokenType.GROUP,
        TokenType.BY,
        TokenType.HAVING,
        TokenType.ORDER,
        TokenType.ASC,
        TokenType.DESC,
        TokenType.LIMIT,
        TokenType.OFFSET,
        TokenType.AND,
        TokenType.OR,
        TokenType.NOT,
        TokenType.LIKE,
        TokenType.IN,
        TokenType.BETWEEN,
        TokenType.IS,
        TokenType.NULL,
        TokenType.CASE,
        TokenType.WHEN,
        TokenType.THEN,
        TokenType.ELSE,
        TokenType.END,
    }
)

# Keyword text (uppercase) -> TokenType, for case-insensitive lookup of a
# scanned identifier against the keyword set.
_KEYWORDS_BY_TEXT: dict[str, TokenType] = {
    member.name: member for member in KEYWORD_TYPES
}


def is_keyword(token_type: TokenType) -> bool:
    """Is *token_type* one of the 30 v1 keywords?

    The escape hatch for call sites that need "any keyword," not one in
    particular - an error message, or a check for whether a token can
    start a clause - so they don't have to enumerate all thirty members
    or fall back to string comparison against `token.text`.
    """
    return token_type in KEYWORD_TYPES


@dataclass(frozen=True)
class Position:
    """A location in SQL source text, in Unicode code points.

    `line` is 1-based. `column` is 1-based, counted in code points, not
    bytes. `offset` is a 0-based code-point index into the source string
    - `len()`/slicing semantics. A token's position is the position of
    its first character.
    """

    line: int
    column: int
    offset: int


@dataclass(frozen=True)
class Token:
    """One lexical token: its type, its decoded text, and where it
    starts in the source.

    `text` is the token's decoded value - quotes stripped and escapes
    un-escaped for `STRING` and quoted `IDENTIFIER`, the keyword's own
    spelling as written for keywords, the digits as written for numeric
    literals. There is no separate field recording whether an identifier
    was quoted; the parser and binder never need to know.
    """

    type: TokenType
    text: str
    position: Position


class LexError(Exception):
    """The source text contains something no v1 token can start with,
    or a string/quoted-identifier literal that is never closed.

    Carries the message and the `Position` at which the problem starts
    - the opening quote for an unterminated literal, the offending
    character itself for everything else - so a caller can report it
    per `_docs/spec.md` §5 without re-deriving the location.
    """

    def __init__(self, message: str, position: Position) -> None:
        super().__init__(message)
        self.position = position


def tokenize(source: str) -> list[Token]:
    """Turn *source* into a list of tokens, always ending with `EOF`.

    Pure function of its argument: the same source string always
    produces the same token stream (`AGENTS.md`'s determinism rule).
    Raises `LexError` for an unterminated string, an unterminated
    quoted identifier, or a character that starts no valid token.
    """
    return _Lexer(source).run()


_WHITESPACE = " \t\r"


class _Lexer:
    """Single-pass scanner over `source`, tracking line/column/offset by
    hand so every token's `Position` is exact."""

    def __init__(self, source: str) -> None:
        self._source = source
        self._length = len(source)
        self._pos = 0
        self._line = 1
        self._column = 1

    # -- low-level cursor movement ----------------------------------

    def _at_end(self) -> bool:
        return self._pos >= self._length

    def _peek(self, ahead: int = 0) -> str:
        index = self._pos + ahead
        if index >= self._length:
            return ""
        return self._source[index]

    def _position(self) -> Position:
        return Position(line=self._line, column=self._column, offset=self._pos)

    def _advance(self) -> str:
        """Consume and return the current character, updating line and
        column. `\\n` starts a new line; every other character - including
        other whitespace - advances the column by one."""
        char = self._source[self._pos]
        self._pos += 1
        if char == "\n":
            self._line += 1
            self._column = 1
        else:
            self._column += 1
        return char

    # -- driver -------------------------------------------------------

    def run(self) -> list[Token]:
        tokens: list[Token] = []
        while True:
            self._skip_whitespace_and_comments()
            start = self._position()
            if self._at_end():
                tokens.append(Token(TokenType.EOF, "", start))
                return tokens
            tokens.append(self._next_token(start))

    def _skip_whitespace_and_comments(self) -> None:
        while not self._at_end():
            char = self._peek()
            if char in _WHITESPACE or char == "\n":
                self._advance()
                continue
            if char == "-" and self._peek(1) == "-":
                self._advance()
                self._advance()
                while not self._at_end() and self._peek() != "\n":
                    self._advance()
                continue
            break

    def _next_token(self, start: Position) -> Token:
        char = self._peek()

        if char == "'":
            return self._read_string(start)
        if char == '"':
            return self._read_quoted_identifier(start)
        if char.isdigit():
            return self._read_number(start)
        if char == "." and self._peek(1).isdigit():
            return self._read_number(start)
        if char.isalpha() or char == "_":
            return self._read_identifier(start)

        return self._read_operator(start)

    # -- string and quoted-identifier literals -------------------------

    def _read_string(self, start: Position) -> Token:
        self._advance()  # opening '
        chars: list[str] = []
        while True:
            if self._at_end():
                raise LexError(
                    "unterminated string literal starting at "
                    f"line {start.line}, column {start.column}",
                    start,
                )
            char = self._advance()
            if char == "'":
                if self._peek() == "'":
                    self._advance()
                    chars.append("'")
                    continue
                break
            chars.append(char)
        return Token(TokenType.STRING, "".join(chars), start)

    def _read_quoted_identifier(self, start: Position) -> Token:
        self._advance()  # opening "
        chars: list[str] = []
        while True:
            if self._at_end():
                raise LexError(
                    "unterminated quoted identifier starting at "
                    f"line {start.line}, column {start.column}",
                    start,
                )
            char = self._advance()
            if char == '"':
                if self._peek() == '"':
                    self._advance()
                    chars.append('"')
                    continue
                break
            chars.append(char)
        return Token(TokenType.IDENTIFIER, "".join(chars), start)

    # -- numeric literals ------------------------------------------------

    def _read_number(self, start: Position) -> Token:
        chars: list[str] = []
        is_real = False
        while self._peek().isdigit():
            chars.append(self._advance())
        if self._peek() == ".":
            is_real = True
            chars.append(self._advance())
            while self._peek().isdigit():
                chars.append(self._advance())
        token_type = TokenType.REAL if is_real else TokenType.INTEGER
        return Token(token_type, "".join(chars), start)

    # -- identifiers and keywords -----------------------------------

    def _read_identifier(self, start: Position) -> Token:
        chars: list[str] = []
        while self._peek().isalnum() or self._peek() == "_":
            chars.append(self._advance())
        text = "".join(chars)
        keyword_type = _KEYWORDS_BY_TEXT.get(text.upper())
        if keyword_type is not None:
            return Token(keyword_type, text, start)
        return Token(TokenType.IDENTIFIER, text, start)

    # -- operators and punctuation -------------------------------------

    _TWO_CHAR_OPERATORS = {
        "<>": TokenType.NE,
        "!=": TokenType.NE,
        "<=": TokenType.LE,
        ">=": TokenType.GE,
        "||": TokenType.CONCAT,
    }

    _ONE_CHAR_OPERATORS = {
        "=": TokenType.EQ,
        "<": TokenType.LT,
        ">": TokenType.GT,
        "+": TokenType.PLUS,
        "-": TokenType.MINUS,
        "*": TokenType.STAR,
        "/": TokenType.SLASH,
        "(": TokenType.LPAREN,
        ")": TokenType.RPAREN,
        ",": TokenType.COMMA,
        ".": TokenType.DOT,
        ";": TokenType.SEMICOLON,
    }

    def _read_operator(self, start: Position) -> Token:
        two_char = self._peek() + self._peek(1)
        token_type = self._TWO_CHAR_OPERATORS.get(two_char)
        if token_type is not None:
            self._advance()
            self._advance()
            return Token(token_type, two_char, start)

        char = self._peek()
        token_type = self._ONE_CHAR_OPERATORS.get(char)
        if token_type is not None:
            self._advance()
            return Token(token_type, char, start)

        # `!` not followed by `=` (handled above as `NE`) starts no valid
        # v1 token, the same as any other unrecognised character.
        self._advance()
        raise LexError(
            f"unexpected character {char!r} at line {start.line}, "
            f"column {start.column}",
            start,
        )
