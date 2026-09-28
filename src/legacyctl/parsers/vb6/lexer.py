"""VB6 lexer.

This is the *tokeniser* half of the built-in structural fallback. It is a real
lexical analyser (not a regex sweep over the raw file): it understands VB6
comments, string literals with doubled-quote escapes, line continuations,
legacy line numbers and the `Attribute` header lines emitted by exported
``.bas`` / ``.frm`` / ``.cls`` files.

The token stream is what the structural extractor reasons about, and it is also
what feeds the SQL extractor, so string contents, offsets and line numbers are
always exact.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class TokenKind(StrEnum):
    IDENTIFIER = "IDENTIFIER"
    KEYWORD = "KEYWORD"
    STRING = "STRING"
    NUMBER = "NUMBER"
    OPERATOR = "OPERATOR"
    PUNCTUATION = "PUNCTUATION"
    COMMENT = "COMMENT"
    NEWLINE = "NEWLINE"
    ATTRIBUTE = "ATTRIBUTE"
    EOF = "EOF"


VB6_KEYWORDS: frozenset[str] = frozenset(
    keyword.lower()
    for keyword in [
        "Alias",
        "And",
        "Any",
        "As",
        "Base",
        "Binary",
        "Boolean",
        "ByRef",
        "Byte",
        "ByVal",
        "Call",
        "Case",
        "CByte",
        "CCur",
        "CDbl",
        "CDate",
        "CDec",
        "CInt",
        "CLng",
        "CObj",
        "CSng",
        "CStr",
        "CVar",
        "Currency",
        "Date",
        "Decimal",
        "Declare",
        "DefBool",
        "DefByte",
        "DefCur",
        "DefDate",
        "DefDec",
        "DefDbl",
        "DefInt",
        "DefLng",
        "DefObj",
        "DefSng",
        "DefStr",
        "DefVar",
        "Dim",
        "Do",
        "Double",
        "Each",
        "Else",
        "ElseIf",
        "Empty",
        "End",
        "Enum",
        "Eqv",
        "Erase",
        "Error",
        "Event",
        "Exit",
        "Explicit",
        "False",
        "For",
        "Friend",
        "Function",
        "Get",
        "Global",
        "GoTo",
        "Goto",
        "If",
        "Imp",
        "Implements",
        "In",
        "Input",
        "Integer",
        "Is",
        "Let",
        "Like",
        "Long",
        "Loop",
        "Me",
        "Mid",
        "Mod",
        "Module",
        "New",
        "Next",
        "Not",
        "Nothing",
        "Null",
        "Object",
        "On",
        "Open",
        "Option",
        "Optional",
        "Or",
        "Output",
        "ParamArray",
        "Preserve",
        "Print",
        "Private",
        "Property",
        "Public",
        "Put",
        "RaiseEvent",
        "Random",
        "Read",
        "ReDim",
        "Resume",
        "Return",
        "RSet",
        "Seek",
        "Select",
        "Set",
        "Single",
        "Static",
        "Step",
        "Stop",
        "String",
        "Sub",
        "Then",
        "To",
        "True",
        "Type",
        "TypeOf",
        "Unload",
        "Until",
        "Variant",
        "Wend",
        "While",
        "With",
        "Write",
        "Xor",
        "Attribute",
    ]
)

MULTI_CHAR_OPERATORS: tuple[str, ...] = ("<=", ">=", "<>", "<<", ">>", ":=")
SINGLE_CHAR_OPERATORS: frozenset[str] = frozenset("+-*/\\^=<>&")


@dataclass(frozen=True, slots=True)
class Token:
    kind: TokenKind
    value: str
    line: int
    column: int
    offset: int

    @property
    def is_identifier(self) -> bool:
        return self.kind is TokenKind.IDENTIFIER

    @property
    def is_keyword(self) -> bool:
        return self.kind is TokenKind.KEYWORD

    def is_punct(self, value: str | None = None) -> bool:
        if self.kind is not TokenKind.PUNCTUATION:
            return False
        return value is None or self.value == value

    def is_word(self, value: str) -> bool:
        return self.kind in (TokenKind.IDENTIFIER, TokenKind.KEYWORD) and (
            self.value.lower() == value.lower()
        )

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"Token({self.kind.value}, {self.value!r}, line={self.line})"


class Vb6LexError(Exception):
    """Raised when the source cannot be tokenised at all (unterminated string)."""


def tokenize(source: str) -> list[Token]:
    """Return the token stream of a VB6 source file.

    Comments and ``Attribute`` lines are preserved as tokens: the extractor needs
    the former for attention points and the latter for component names.
    """
    tokens: list[Token] = []
    i = 0
    line = 1
    col = 1
    n = len(source)
    at_statement_start = True
    continued = False

    def push(
        kind: TokenKind, value: str, start_offset: int, start_line: int, start_col: int
    ) -> None:
        tokens.append(Token(kind, value, start_line, start_col, start_offset))

    while i < n:
        ch = source[i]

        if ch == "\r":
            i += 1
            continue
        if ch == "\n":
            if not continued:
                push(TokenKind.NEWLINE, "\n", i, line, col)
            continued = False
            i += 1
            line += 1
            col = 1
            at_statement_start = True
            continue
        if ch in " \t":
            # A trailing space plus "_" is a VB6 line continuation.
            if ch == " " and _is_continuation(source, i):
                i += 1
                continued = True
                j = i
                while j < n and source[j] in " \t":
                    j += 1
                if j < n and source[j] == "_":
                    j += 1
                while j < n and source[j] in " \t":
                    j += 1
                col += j - i
                i = j
                continue
            i += 1
            col += 1
            continue

        if ch == "'":
            start, start_line, start_col = i, line, col
            end = source.find("\n", i)
            if end == -1:
                end = n
            text = source[i:end]
            push(TokenKind.COMMENT, text, start, start_line, start_col)
            col += len(text)
            i = end
            continue

        if at_statement_start and ch.isdigit():
            # Legacy line number: "120 PRINT X" / "120 GOTO Foo".
            j = i
            while j < n and source[j].isdigit():
                j += 1
            if j < n and source[j] in " \t":
                digits = source[i:j]
                push(TokenKind.NUMBER, digits, i, line, col)
                col += j - i
                i = j
                continue

        if at_statement_start and (ch.isalpha() or ch == "_"):
            word, end = _read_word(source, i)
            if word.lower() == "rem" or word.lower() == "attribute":
                start_line, start_col = line, col
                end_of_line = source.find("\n", i)
                if end_of_line == -1:
                    end_of_line = n
                text = source[i:end_of_line]
                push(TokenKind.ATTRIBUTE, text, i, start_line, start_col)
                i = end_of_line
                continue

        if ch == '"':
            start, start_line, start_col = i, line, col
            j = i + 1
            buf: list[str] = []
            terminated = False
            while j < n:
                if source[j] == '"':
                    if source[j + 1 : j + 2] == '"':
                        buf.append('"')
                        j += 2
                        continue
                    j += 1
                    terminated = True
                    break
                if source[j] == "\n":
                    break
                buf.append(source[j])
                j += 1
            if not terminated:
                raise Vb6LexError(f"unterminated string literal at line {start_line}")
            raw = source[start:j]
            push(TokenKind.STRING, "".join(buf), start, start_line, start_col)
            col += len(raw)
            i = j
            at_statement_start = False
            continue

        if ch.isdigit() or (ch == "." and source[i + 1 : i + 2].isdigit()):
            j = i
            seen_dot = False
            while j < n and (source[j].isdigit() or (source[j] == "." and not seen_dot)):
                if source[j] == ".":
                    seen_dot = True
                j += 1
            push(TokenKind.NUMBER, source[i:j], i, line, col)
            col += j - i
            i = j
            at_statement_start = False
            continue

        if ch.isalpha() or ch == "_":
            word, end = _read_word(source, i)
            kind = TokenKind.KEYWORD if word.lower() in VB6_KEYWORDS else TokenKind.IDENTIFIER
            push(kind, word, i, line, col)
            col += end - i
            i = end
            at_statement_start = False
            continue

        if ch == "&" and source[i : i + 2] in ("&H", "&h", "&O", "&o"):
            j = i + 2
            while j < n and (source[j] in "0123456789abcdefABCDEF"):
                j += 1
            push(TokenKind.NUMBER, source[i:j], i, line, col)
            col += j - i
            i = j
            at_statement_start = False
            continue

        two = source[i : i + 2]
        if two in MULTI_CHAR_OPERATORS:
            push(TokenKind.OPERATOR, two, i, line, col)
            i += 2
            col += 2
            at_statement_start = False
            continue

        if ch in SINGLE_CHAR_OPERATORS:
            push(TokenKind.OPERATOR, ch, i, line, col)
            i += 1
            col += 1
            at_statement_start = False
            continue

        if ch in "(),:;.{}":
            push(TokenKind.PUNCTUATION, ch, i, line, col)
            i += 1
            col += 1
            at_statement_start = ch == ":"
            continue

        push(TokenKind.OPERATOR, ch, i, line, col)
        i += 1
        col += 1
        at_statement_start = False

    tokens.append(Token(TokenKind.EOF, "", line, col, n))
    return tokens


def text_of(source: str, start: int, end: int) -> str:
    return source[start:end].strip()


def _is_continuation(source: str, index: int) -> bool:
    """True when the space at ``index`` is a VB6 ``_`` line continuation."""
    j = index + 1
    while j < len(source) and source[j] in " \t":
        j += 1
    if j < len(source) and source[j] == "_":
        j += 1
    while j < len(source) and source[j] in " \t":
        j += 1
    return j < len(source) and source[j] in "\r\n"


def _read_word(source: str, start: int) -> tuple[str, int]:
    i = start
    n = len(source)
    while i < n and (source[i].isalnum() or source[i] == "_"):
        i += 1
    return source[start:i], i


def read_source(path: Path) -> str:
    """Read a VB6 source file, tolerating the legacy Windows encodings."""
    raw = path.read_bytes()
    for encoding in ("utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace")
