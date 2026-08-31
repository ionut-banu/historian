"""Tests for historian.sql.lexer.

Expected values for anything with a plausible SQLite answer (string/
identifier escaping, numeric literal forms, comment syntax) were checked
against the `sqlite3` command-line tool (3.51.0) rather than reasoned
about from memory, per the project rule that SQLite is the definition of
correct. The query used is quoted above each group, same convention as
`tests/test_values.py`.
"""

import pytest

from historian.sql.lexer import (
    KEYWORD_TYPES,
    LexError,
    Position,
    Token,
    TokenType,
    is_keyword,
    tokenize,
)

# The v1 keyword set, per _docs/spec.md §1 and issue #3. Hand-written and
# independent of TokenType's own membership, so a keyword silently
# dropped from the enum turns this test red rather than passing by
# comparing the enum against itself.
V1_KEYWORDS = {
    "SELECT", "DISTINCT", "AS", "FROM", "INNER", "JOIN", "ON", "USING",
    "WHERE", "GROUP", "BY", "HAVING", "ORDER", "ASC", "DESC", "LIMIT",
    "OFFSET", "AND", "OR", "NOT", "LIKE", "IN", "BETWEEN", "IS", "NULL",
    "CASE", "WHEN", "THEN", "ELSE", "END",
}


def _types(tokens):
    return [t.type for t in tokens]


def _texts(tokens):
    return [t.text for t in tokens]


# --- tokenize() basics -----------------------------------------------


def test_empty_source_is_a_single_eof_token():
    tokens = tokenize("")
    assert _types(tokens) == [TokenType.EOF]


def test_stream_always_ends_with_eof():
    tokens = tokenize("SELECT")
    assert tokens[-1].type is TokenType.EOF


def test_every_token_has_type_text_and_position():
    tokens = tokenize("SELECT")
    tok = tokens[0]
    assert tok.type is TokenType.SELECT
    assert tok.text == "SELECT"
    assert isinstance(tok.position, Position)


# --- Keywords ----------------------------------------------------------


def test_keyword_type_set_matches_v1_grammar_exactly():
    assert KEYWORD_TYPES == {TokenType[name] for name in V1_KEYWORDS}


@pytest.mark.parametrize("keyword", sorted(V1_KEYWORDS))
def test_each_keyword_lexes_to_its_own_token_type(keyword):
    for spelling in (keyword, keyword.lower(), keyword.title()):
        tokens = tokenize(spelling)
        assert tokens[0].type is TokenType[keyword]
        assert tokens[0].text == spelling


def test_is_keyword_true_for_exactly_the_keyword_types():
    for member in TokenType:
        expected = member in KEYWORD_TYPES
        assert is_keyword(member) is expected


def test_is_keyword_false_for_eof_and_identifier():
    assert is_keyword(TokenType.EOF) is False
    assert is_keyword(TokenType.IDENTIFIER) is False


# --- Identifiers ---------------------------------------------------------


def test_bare_identifier_preserves_original_case():
    tokens = tokenize("MyColumn")
    assert tokens[0].type is TokenType.IDENTIFIER
    assert tokens[0].text == "MyColumn"


def test_identifier_that_is_not_a_keyword_lexes_as_identifier():
    tokens = tokenize("count")
    assert tokens[0].type is TokenType.IDENTIFIER
    assert tokens[0].text == "count"


def test_leading_underscore_identifier():
    tokens = tokenize("_foo")
    assert tokens[0].type is TokenType.IDENTIFIER
    assert tokens[0].text == "_foo"


@pytest.mark.parametrize(
    "source,keyword_type",
    [("SELECTED", TokenType.SELECT), ("ANDy", TokenType.AND)],
)
def test_keyword_spelling_as_prefix_of_longer_identifier_is_one_token(
    source, keyword_type
):
    """A keyword's spelling occurring as a strict prefix of a longer
    identifier must not split into the keyword token followed by
    whatever remains - the identifier scan has to consume the whole
    run of identifier characters before checking the keyword table."""
    tokens = tokenize(source)
    assert _types(tokens) == [TokenType.IDENTIFIER, TokenType.EOF]
    assert tokens[0].type is not keyword_type
    assert tokens[0].text == source


# --- Non-ASCII identifiers (issue #16) ------------------------------------
#
# SQLite's tokenizer rule, confirmed against sqlite3 3.51.0: an ASCII digit
# starts a number, and every other non-quote, non-operator character -
# including every character above ASCII - starts an identifier. It never
# asks whether a codepoint is alphabetic. Python's str.isalpha()/isdigit()
# do not partition non-ASCII characters this way (superscript '²' and
# Arabic-Indic digits are "digits" to Python; '™' is neither a letter nor
# a digit to Python), so the lexer must not consult them for anything
# above ASCII. See _docs/decisions.md, 2026-09-01.


def test_non_ascii_letters_lex_as_identifier():
    # sqlite3: select café; select 中;  -> both "no such column", i.e.
    # both lex as identifiers, not lexer errors.
    for source in ("café", "中"):
        tokens = tokenize(source)
        assert _types(tokens) == [TokenType.IDENTIFIER, TokenType.EOF]
        assert tokens[0].text == source


def test_superscript_digit_lexes_as_identifier_not_integer():
    # sqlite3: select ²;  -> "no such column: ²", not a parse/numeric
    # error, so SQLite lexed it as an identifier. Python's '²'.isdigit()
    # is True and '²'.isalpha() is False, so this is the case that
    # catches a fix that only widens the digit path to ASCII while
    # leaving the identifier path on Python's character classifiers.
    tokens = tokenize("²")
    assert _types(tokens) == [TokenType.IDENTIFIER, TokenType.EOF]
    assert tokens[0].text == "²"


def test_arabic_indic_digits_lex_as_single_identifier_not_integer():
    # sqlite3: select ١٢٣;  -> "no such column: ١٢٣". Each of these three
    # codepoints is individually str.isdigit() == True in Python, so a
    # lexer that treats "isdigit" as "can start/continue a number" would
    # produce a bogus INTEGER (or crash decoding it as one) instead.
    tokens = tokenize("١٢٣")
    assert _types(tokens) == [TokenType.IDENTIFIER, TokenType.EOF]
    assert tokens[0].text == "١٢٣"


def test_trademark_sign_lexes_as_identifier():
    # sqlite3: select ™;  -> "no such column: ™". '™'.isalpha() and
    # '™'.isalnum() are both False in Python, so this is the character
    # that catches an incomplete fix: one that widens _read_identifier's
    # digit handling but still gates on Python's isalpha/isalnum still
    # raises LexError here. The only rule that gets this right is "any
    # non-ASCII character can be part of an identifier," full stop.
    tokens = tokenize("™")
    assert _types(tokens) == [TokenType.IDENTIFIER, TokenType.EOF]
    assert tokens[0].text == "™"


@pytest.mark.parametrize(
    "char",
    [
        "Ω",  # Greek capital letter (Lu) - already worked, must keep working
        "é",  # Latin small letter with diacritic (Ll)
        "中",  # CJK ideograph (Lo)
        "²",  # superscript two (No) - digit-shaped, not alphabetic
        "١",  # Arabic-Indic digit one (Nd) - isdigit() is True in Python
        "™",  # trademark sign (So) - neither alpha nor digit in Python
        "§",  # section sign (Po)
        "‽",  # interrobang (Po)
        "́",  # combining acute accent (Mn), alone
        "😀",  # emoji (So), outside the Basic Multilingual Plane
    ],
)
def test_non_ascii_characters_across_categories_always_lex_as_identifier(char):
    """General check behind the named examples above: whatever Unicode
    category a non-ASCII character falls in, it starts (and here, is the
    whole of) an identifier - never a LexError, never INTEGER or REAL."""
    tokens = tokenize(char)
    assert _types(tokens) == [TokenType.IDENTIFIER, TokenType.EOF]
    assert tokens[0].text == char


def test_double_quoted_identifier_strips_quotes():
    tokens = tokenize('"path"')
    assert tokens[0].type is TokenType.IDENTIFIER
    assert tokens[0].text == "path"


def test_double_quoted_identifier_decodes_doubled_quote():
    # sqlite3: create table t("a""b" int); -- accepted, column named a"b
    tokens = tokenize('"a""b"')
    assert tokens[0].type is TokenType.IDENTIFIER
    assert tokens[0].text == 'a"b'


def test_double_quoted_keyword_spelling_lexes_as_identifier_not_keyword():
    tokens = tokenize('"select"')
    assert tokens[0].type is TokenType.IDENTIFIER
    assert tokens[0].text == "select"


def test_unterminated_double_quoted_identifier_raises_at_opening_quote():
    with pytest.raises(LexError) as exc_info:
        tokenize('SELECT "abc FROM t')
    assert exc_info.value.position.offset == 7


# --- Strings ---------------------------------------------------------


def test_single_quoted_string_strips_quotes():
    tokens = tokenize("'hello'")
    assert tokens[0].type is TokenType.STRING
    assert tokens[0].text == "hello"


def test_single_quoted_string_decodes_doubled_quote():
    # sqlite3: select 'it''s'; -> it's
    tokens = tokenize("'it''s'")
    assert tokens[0].type is TokenType.STRING
    assert tokens[0].text == "it's"


def test_unterminated_string_raises_at_opening_quote():
    # sqlite3: select 'abc  ->  Error: unrecognized token: "'abc"
    with pytest.raises(LexError) as exc_info:
        tokenize("SELECT 'abc")
    assert exc_info.value.position.offset == 7
    assert exc_info.value.position.column == 8


# --- Numeric literals ------------------------------------------------


def test_integer_lexes_as_integer():
    tokens = tokenize("42")
    assert tokens[0].type is TokenType.INTEGER
    assert tokens[0].text == "42"


@pytest.mark.parametrize(
    "source",
    ["5.5", "5.", ".5"],
    # sqlite3: select 5.5, 5., .5, typeof(5.), typeof(.5);
    #   -> 5.5|5.0|0.5|real|real -- all three are valid reals
)
def test_real_forms_lex_as_a_single_real_token(source):
    tokens = tokenize(source)
    assert _types(tokens) == [TokenType.REAL, TokenType.EOF]
    assert tokens[0].text == source


def test_scientific_notation_is_not_a_single_token_in_v1():
    """1e10 is real SQLite syntax but out of scope for this issue (#6).
    It lexes as INTEGER "1" followed by IDENTIFIER "e10"."""
    tokens = tokenize("1e10")
    assert _types(tokens) == [
        TokenType.INTEGER,
        TokenType.IDENTIFIER,
        TokenType.EOF,
    ]
    assert _texts(tokens)[:2] == ["1", "e10"]


def test_hex_integer_is_not_a_single_token_in_v1():
    """0x1F is real SQLite syntax but out of scope for this issue (#6).
    It lexes as INTEGER "0" followed by IDENTIFIER "x1F"."""
    tokens = tokenize("0x1F")
    assert _types(tokens) == [
        TokenType.INTEGER,
        TokenType.IDENTIFIER,
        TokenType.EOF,
    ]
    assert _texts(tokens)[:2] == ["0", "x1F"]


def test_sign_is_not_part_of_a_numeric_token():
    tokens = tokenize("-5")
    assert _types(tokens) == [TokenType.MINUS, TokenType.INTEGER, TokenType.EOF]


# --- Operators and maximal munch --------------------------------------


@pytest.mark.parametrize(
    "source,expected_type",
    [
        ("<>", TokenType.NE),
        ("!=", TokenType.NE),
        ("<=", TokenType.LE),
        (">=", TokenType.GE),
        ("||", TokenType.CONCAT),
    ],
)
def test_two_character_operators_lex_as_one_token(source, expected_type):
    tokens = tokenize(source)
    # A count assertion, not just "the type appears somewhere" - this is
    # exactly the check that would fail if munch order were wrong and
    # the lexer emitted two single-character tokens instead.
    assert _types(tokens) == [expected_type, TokenType.EOF]


@pytest.mark.parametrize(
    "source,expected_type",
    [("<", TokenType.LT), (">", TokenType.GT), ("=", TokenType.EQ)],
)
def test_single_character_operators_not_followed_by_pairing_char(
    source, expected_type
):
    tokens = tokenize(source)
    assert _types(tokens) == [expected_type, TokenType.EOF]


def test_double_equals_lexes_as_two_eq_tokens():
    """SQLite accepts == as an alias for =, but §1's grammar doesn't, so
    == is deliberately left as two adjacent EQ tokens (#6)."""
    tokens = tokenize("==")
    assert _types(tokens) == [TokenType.EQ, TokenType.EQ, TokenType.EOF]


def test_all_punctuation_tokens():
    source = "+ - * / ( ) , . ;"
    tokens = tokenize(source)
    assert _types(tokens) == [
        TokenType.PLUS,
        TokenType.MINUS,
        TokenType.STAR,
        TokenType.SLASH,
        TokenType.LPAREN,
        TokenType.RPAREN,
        TokenType.COMMA,
        TokenType.DOT,
        TokenType.SEMICOLON,
        TokenType.EOF,
    ]


# --- Comments ----------------------------------------------------------


def test_line_comment_runs_to_end_of_line():
    tokens = tokenize("SELECT 1 -- this is a comment\nFROM t")
    assert _types(tokens) == [
        TokenType.SELECT,
        TokenType.INTEGER,
        TokenType.FROM,
        TokenType.IDENTIFIER,
        TokenType.EOF,
    ]


def test_line_comment_runs_to_end_of_input_with_no_trailing_newline():
    tokens = tokenize("SELECT 1 -- trailing comment, no newline")
    assert _types(tokens) == [TokenType.SELECT, TokenType.INTEGER, TokenType.EOF]


def test_source_of_only_whitespace_and_comments_is_a_single_eof():
    tokens = tokenize("  \n-- just a comment\n  \n")
    assert _types(tokens) == [TokenType.EOF]


def test_block_comment_is_not_supported_in_v1():
    """/* */ is out of scope for this issue (#6): / lexes as SLASH and
    * as STAR, not as a comment delimiter."""
    tokens = tokenize("/* not a comment */")
    assert _types(tokens) == [
        TokenType.SLASH,
        TokenType.STAR,
        TokenType.NOT,
        TokenType.IDENTIFIER,
        TokenType.IDENTIFIER,
        TokenType.STAR,
        TokenType.SLASH,
        TokenType.EOF,
    ]


# --- Whitespace (issue #16) ----------------------------------------------


def test_form_feed_is_skipped_like_other_whitespace():
    tokens = tokenize("SELECT\f1")
    assert _types(tokens) == [TokenType.SELECT, TokenType.INTEGER, TokenType.EOF]
    assert tokens[1].text == "1"


def test_form_feed_separates_arbitrary_tokens():
    """Not just after a keyword: a separator between any two tokens."""
    tokens = tokenize("a\fFROM\fb")
    assert _types(tokens) == [
        TokenType.IDENTIFIER,
        TokenType.FROM,
        TokenType.IDENTIFIER,
        TokenType.EOF,
    ]
    assert tokens[0].text == "a"
    assert tokens[2].text == "b"


def test_form_feed_advances_column_not_line():
    # Same convention as test_carriage_return_and_tab_advance_column_not_line:
    # a single non-newline whitespace character advances the column by one.
    source = "a\fb"
    tokens = tokenize(source)
    b_token = tokens[1]
    assert b_token.position.line == 1
    assert b_token.position.column == 3


def test_form_feed_inside_string_literal_is_preserved():
    """Whitespace-skipping must never reach inside a string literal - a
    form feed between the quotes is data, not a separator."""
    tokens = tokenize("'a\fb'")
    assert tokens[0].type is TokenType.STRING
    assert tokens[0].text == "a\fb"
    assert len(tokens[0].text) == 3


# --- Illegal characters -------------------------------------------------


def test_unrecognised_character_raises_lex_error_naming_the_character():
    with pytest.raises(LexError) as exc_info:
        tokenize("SELECT @foo")
    assert "@" in str(exc_info.value)
    assert exc_info.value.position.offset == 7


@pytest.mark.parametrize("char", ["@", "#", "$", "`"])
def test_various_unrecognised_characters_raise(char):
    with pytest.raises(LexError):
        tokenize(char)


def test_bang_not_followed_by_equals_raises():
    with pytest.raises(LexError):
        tokenize("!")


def test_vertical_tab_still_raises_lex_error():
    """Unlike form feed, vertical tab is not whitespace to SQLite either
    (confirmed: sqlite3 rejects it) - it must stay rejected here, not be
    added alongside \\f."""
    with pytest.raises(LexError):
        tokenize("SELECT\v1")


# --- Positions -----------------------------------------------------------


def test_position_offset_is_code_point_index_not_byte_index():
    # 'é' is one code point but two UTF-8 bytes; 'x' must sit at offset
    # 2 (after "é'"), not offset 3, which is what a byte-counting lexer
    # would produce.
    source = "'é' x"
    tokens = tokenize(source)
    assert tokens[0].type is TokenType.STRING
    assert tokens[0].position.offset == 0
    ident = tokens[1]
    assert ident.type is TokenType.IDENTIFIER
    assert ident.text == "x"
    assert ident.position.offset == 4
    assert ident.position.column == 5


def test_position_offset_multibyte_cjk():
    # "日本語" is three code points (nine UTF-8 bytes). The token after
    # the closing quote must be at code-point offset 5, not 11.
    source = "'日本語' y"
    tokens = tokenize(source)
    ident = tokens[1]
    assert ident.text == "y"
    assert ident.position.offset == 6
    assert ident.position.column == 7


def test_token_position_is_first_character_of_token():
    tokens = tokenize("  SELECT")
    assert tokens[0].position.offset == 2
    assert tokens[0].position.column == 3
    assert tokens[0].position.line == 1


def test_line_and_column_after_embedded_newline():
    source = "SELECT a\nFROM b"
    tokens = tokenize(source)
    from_token = tokens[2]
    assert from_token.type is TokenType.FROM
    assert from_token.position.line == 2
    assert from_token.position.column == 1
    b_token = tokens[3]
    assert b_token.position.line == 2
    assert b_token.position.column == 6


def test_line_and_column_after_multiple_newlines():
    source = "SELECT\n\n  a"
    tokens = tokenize(source)
    a_token = tokens[1]
    assert a_token.type is TokenType.IDENTIFIER
    assert a_token.position.line == 3
    assert a_token.position.column == 3


def test_newline_inside_string_still_advances_line_tracking():
    """A v1 token cannot itself span a newline (strings and identifiers
    don't in SQLite either - confirmed: a bare embedded newline inside a
    single-quoted string is legal SQL and does not end the string, but
    since our unterminated-string check only fires at end-of-input, a
    string containing a literal newline is representable; what matters
    here is that line/column tracking resumes correctly for tokens after
    it)."""
    source = "'a\nb' c"
    tokens = tokenize(source)
    assert tokens[0].type is TokenType.STRING
    assert tokens[0].text == "a\nb"
    ident = tokens[1]
    assert ident.text == "c"
    assert ident.position.line == 2
    assert ident.position.column == 4


def test_carriage_return_and_tab_advance_column_not_line():
    source = "a\tb"
    tokens = tokenize(source)
    b_token = tokens[1]
    assert b_token.position.line == 1
    assert b_token.position.column == 3


# --- Determinism -----------------------------------------------------


def test_tokenizing_same_source_twice_is_equal():
    source = "SELECT a, b FROM t WHERE a = 1 -- comment\n"
    assert tokenize(source) == tokenize(source)


def test_tokenizing_same_source_twice_is_equal_with_errors_absent():
    source = "SELECT * FROM blame WHERE path LIKE 'src/%' LIMIT 10"
    first = tokenize(source)
    second = tokenize(source)
    assert first == second
    assert first is not second


# --- Realistic query, end to end ---------------------------------------


def test_realistic_query_tokenizes_as_expected():
    source = "SELECT author_name, count(*) FROM blame WHERE path LIKE 'src/auth/%' GROUP BY author_name"
    tokens = tokenize(source)
    assert _types(tokens) == [
        TokenType.SELECT,
        TokenType.IDENTIFIER,
        TokenType.COMMA,
        TokenType.IDENTIFIER,
        TokenType.LPAREN,
        TokenType.STAR,
        TokenType.RPAREN,
        TokenType.FROM,
        TokenType.IDENTIFIER,
        TokenType.WHERE,
        TokenType.IDENTIFIER,
        TokenType.LIKE,
        TokenType.STRING,
        TokenType.GROUP,
        TokenType.BY,
        TokenType.IDENTIFIER,
        TokenType.EOF,
    ]
    string_tokens = [t for t in tokens if t.type is TokenType.STRING]
    assert len(string_tokens) == 1
    assert string_tokens[0].text == "src/auth/%"
