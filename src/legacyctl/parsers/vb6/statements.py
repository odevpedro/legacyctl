"""Statement-level view of a VB6 token stream.

VB6 uses the newline as a statement separator, so a *statement* is the run of
tokens between two newlines (``_`` continuations already merged by the lexer).
Working at statement granularity gives the structural extractor exact source
text, which is what makes SQL extraction, attention points and state-transition
evidence auditable rather than guessed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .lexer import Token, TokenKind


@dataclass(slots=True)
class Statement:
    tokens: list[Token] = field(default_factory=list)
    line: int = 0
    end_line: int = 0
    text: str = ""

    @property
    def first(self) -> Token | None:
        return self.tokens[0] if self.tokens else None

    def significant(self) -> list[Token]:
        return [t for t in self.tokens if t.kind not in (TokenKind.COMMENT, TokenKind.ATTRIBUTE)]

    def is_empty(self) -> bool:
        return not self.significant()

    def identifiers(self) -> list[Token]:
        return [t for t in self.tokens if t.kind is TokenKind.IDENTIFIER]

    def strings(self) -> list[Token]:
        return [t for t in self.tokens if t.kind is TokenKind.STRING]

    def keyword(self, value: str, index: int = 0) -> Token | None:
        matches = [t for t in self.tokens if t.is_word(value)]
        return matches[index] if index < len(matches) else None

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"Statement(line={self.line}, {self.text!r})"


def split_statements(tokens: list[Token], source: str) -> list[Statement]:
    """Group tokens into statements, recovering the exact source text."""
    statements: list[Statement] = []
    current: Statement | None = None

    for token in tokens:
        if token.kind is TokenKind.EOF:
            break
        if token.kind is TokenKind.NEWLINE:
            if current is not None:
                statements.append(current)
                current = None
            continue
        if current is None:
            current = Statement(line=token.line, end_line=token.line)
        current.tokens.append(token)
        current.end_line = token.line

    if current is not None:
        statements.append(current)

    for statement in statements:
        statement.text = _slice(source, statement.tokens)
    return statements


def _slice(source: str, tokens: list[Token]) -> str:
    if not tokens:
        return ""
    start = tokens[0].offset
    last = tokens[-1]
    if last.kind is TokenKind.STRING:
        end = _string_end(source, last.offset)
    else:
        end = last.offset + len(last.value)
    return source[start:end].strip()


def _string_end(source: str, offset: int) -> int:
    """Offset just past the closing quote of the string literal at ``offset``."""
    index = offset + 1
    length = len(source)
    while index < length:
        if source[index] == '"':
            if source[index + 1 : index + 2] == '"':
                index += 2
                continue
            return index + 1
        index += 1
    return length
