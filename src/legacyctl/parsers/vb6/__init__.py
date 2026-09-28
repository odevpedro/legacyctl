"""VB6 adapter: lexer, structural fallback, ProLeap bridge and domain translation."""

from __future__ import annotations

from .ast import AST_SCHEMA_VERSION, AstDocument, translate_native
from .extractor import Vb6StructuralExtractor
from .lexer import Token, TokenKind, Vb6LexError, tokenize
from .parser import VB6Parser
from .proleap import ProleapBridge, ProleapError
from .statements import Statement, split_statements

__all__ = [
    "AST_SCHEMA_VERSION",
    "AstDocument",
    "ProleapBridge",
    "ProleapError",
    "Statement",
    "Token",
    "TokenKind",
    "VB6Parser",
    "Vb6LexError",
    "Vb6StructuralExtractor",
    "split_statements",
    "tokenize",
    "translate_native",
]
