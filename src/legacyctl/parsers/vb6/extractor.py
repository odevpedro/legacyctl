"""Built-in structural fallback for VB6.

Scope, explicitly documented (see ``adapters/vb6/SKILL.md`` and ADR 008):

* This module is **not** the primary parser. The reference parser is ProLeap,
  consumed through the ``AST JSON`` contract in :mod:`legacyctl.parsers.vb6.ast`.
* It is a *lexical + block structural* analyser built on
  :mod:`legacyctl.parsers.vb6.lexer`. It never guesses: anything it cannot
  account for is reported as ``ParseStatus.UNKNOWN`` / ``PARTIAL`` together with
  a diagnostic.
* It covers the documented gaps of ProLeap: ``.vbp`` project files, ``Attribute``
  headers, embedded SQL string literals, ``{call proc(?, ?)}`` OUT params,
  attention points (``On Error Resume Next``, ``Go To``) and literal state
  assignments.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from ...domain.enums import (
    CallKind,
    ComponentKind,
    EntryPointKind,
    ParseStatus,
    ProcedureKind,
)
from .ast import (
    AST_SCHEMA_VERSION,
    AstAttribute,
    AstCall,
    AstComponent,
    AstDocument,
    AstEntryPoint,
    AstParameter,
    AstProcedure,
    AstSqlString,
    AstStateWrite,
    AstVariable,
)
from .lexer import Token, TokenKind, Vb6LexError, read_source, tokenize
from .statements import Statement, split_statements

PARSER_NAME = "legacyctl.vb6.structural-fallback"
PARSER_VERSION = "0.1.0"

_VB_EXTENSIONS = {".bas", ".frm", ".cls", ".vbp", ".ctl"}
_FORM_CONTROL = re.compile(r"^Begin\s+VB\.", re.IGNORECASE)
_ATTR_NAME = re.compile(r"^Attribute\s+(\w+)\s*=\s*\"([^\"]*)\"", re.IGNORECASE)
_ATTR_BOOL = re.compile(r"^Attribute\s+(\w+)\s*$", re.IGNORECASE)
_STATE_LITERAL = re.compile(r"^[A-Z][A-Z0-9_]{2,}$")
_STATE_LITERAL_EXCLUDED = frozenset(
    {
        "SELECT",
        "INSERT",
        "UPDATE",
        "DELETE",
        "FROM",
        "WHERE",
        "VALUES",
        "CREATE",
        "ALTER",
        "DROP",
        "TABLE",
        "ORDER",
        "GROUP",
        "JOIN",
        "INNER",
        "OUTER",
        "LEFT",
        "RIGHT",
        "FULL",
        "CROSS",
        "RETURNING",
        "CURRENT_TIMESTAMP",
        "CURRENT_DATE",
        "AND",
        "OR",
        "NOT",
        "NULL",
        "TRUE",
        "FALSE",
        "REJECTED",
        "APPROVED",
        # ``UNKNOWN`` is this framework's sentinel for "not proven": it must
        # never be reported as a discovered business state.
        "UNKNOWN",
    }
)
_STATE_NAME_HINT = (
    "estado",
    "state",
    "rastro",
    "situacao",
    "status",
    "resultado",
    "fase",
)
_GRUPO_HINT = ("grupo",)
_CONSISTENCIA_HINT = ("consistencia", "consistencia")
_SQL_START = re.compile(
    r"^\s*(select|insert\s+into|update|delete\s+from|merge|create\s+(table|view|or\s+replace)|"
    r"alter\s+table|drop\s+table|truncate|execute|exec|call|\{call)\b",
    re.IGNORECASE,
)
_STATE_COLUMN = re.compile(
    r"\b(ESTADO|STATE|SITUACAO|SITUAÇÃO|STATUS|ESTADO_RASTRO)\b", re.IGNORECASE
)
_STATE_ASSIGN = re.compile(
    r"^\s*(?P<target>[A-Za-z_][\w.$]*)\s*=\s*\"(?P<value>[A-Z][A-Z0-9_]*)\"\s*$"
)
_STATE_SETTER = re.compile(
    r"^\s*(?:call\s+)?(?P<fn>[A-Za-z_][\w.$]*)\s+(?P<arg1>[A-Za-z_][\w.$]*)\s*,\s*"
    r"\"(?P<value>[A-Z][A-Z0-9_]*)\"\s*$"
)
_STATE_CALL = re.compile(
    r"^\s*(?:call\s+)?[A-Za-z_][\w.$]*\s*\(\s*(?P<arg1>[A-Za-z_][\w.$]*)?\s*,\s*"
    r"(?:(?:adVar|Types\.)?\w+\s*,\s*)?\"(?P<value>[A-Z][A-Z0-9_]*)\""
)
_CALL_ANCESTOR = re.compile(r"^\{call\b", re.IGNORECASE)
_CALL_LITERAL = re.compile(
    r"^\s*\{?\s*call\s+(?P<name>[A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)*|\.[A-Za-z_][\w$]*)"
    r"\s*\((?P<args>[^)]*)\)\s*\}?\s*$",
    re.IGNORECASE,
)
_CALL_HEAD = re.compile(r"^\s*\{?\s*call\b", re.IGNORECASE)
_SQL_KEYWORD_TAIL = re.compile(
    r"\b(from|into|set|values|where|join|group\s+by|order\s+by)\b", re.IGNORECASE
)
_OUT_PARAM = re.compile(r"^P_[A-Z0-9_]+$")
_NOT_CALL_LEADERS = {
    "if",
    "elseif",
    "else",
    "for",
    "each",
    "next",
    "while",
    "wend",
    "do",
    "loop",
    "select",
    "case",
    "set",
    "let",
    "const",
    "dim",
    "redim",
    "erase",
    "print",
    "debug",
    "end",
    "exit",
    "on",
    "goto",
    "resume",
    "error",
    "declare",
    "implements",
    "type",
    "enum",
    "with",
    "open",
    "close",
    "call",
    "option",
    "attribute",
}
_MODIFIERS = {"public", "private", "friend", "static", "global"}
#: VB6 intrinsic functions and the ADO/msgbox surface. They are real calls, but
#: they are *external*: they must never become edges of the internal call graph.
_VB_BUILTINS = {
    "abs",
    "asc",
    "atn",
    "cbool",
    "cbyte",
    "ccur",
    "cdate",
    "cdbl",
    "chr",
    "cint",
    "clng",
    "csng",
    "cstr",
    "cvar",
    "cvdate",
    "cverr",
    "cvlng",
    "date",
    "dateadd",
    "datediff",
    "datepart",
    "dateserial",
    "datevalue",
    "day",
    "ddb",
    "dir",
    "doevents",
    "environ",
    "error",
    "exp",
    "filelen",
    "fix",
    "format",
    "freecounter",
    "fv",
    "hex",
    "hour",
    "iif",
    "inputbox",
    "instr",
    "int",
    "irr",
    "isarray",
    "isdate",
    "isempty",
    "iserror",
    "ismissing",
    "isnull",
    "isnumeric",
    "isobject",
    "join",
    "lbound",
    "lcase",
    "left",
    "len",
    "log",
    "ltrim",
    "mid",
    "minute",
    "mirr",
    "month",
    "msgbox",
    "monthname",
    "now",
    "nper",
    "npv",
    "oct",
    "partition",
    "pmt",
    "pv",
    "qbcolor",
    "qcet",
    "qqcolor",
    "rate",
    "replace",
    "rgb",
    "right",
    "rnd",
    "round",
    "rtrim",
    "second",
    "sgn",
    "shell",
    "sin",
    "sln",
    "space",
    "spc",
    "split",
    "sqr",
    "str",
    "strcomp",
    "strconv",
    "string",
    "strreverse",
    "switch",
    "syd",
    "tab",
    "tan",
    "time",
    "timer",
    "timeserial",
    "timevalue",
    "trim",
    "typename",
    "ubound",
    "ucase",
    "val",
    "vartype",
    "weekday",
    "year",
}
_TYPE_TOKENS = {
    "integer",
    "long",
    "single",
    "double",
    "currency",
    "decimal",
    "date",
    "string",
    "object",
    "boolean",
    "byte",
    "variant",
    "any",
}


def _is_type_token(token: Token) -> bool:
    return token.value.lower() in _TYPE_TOKENS


class Vb6StructuralExtractor:
    """Produces the canonical ``AST JSON`` from a VB6 source file."""

    parser_name = PARSER_NAME
    parser_version = PARSER_VERSION

    def __init__(self, *, relative_to: Path | None = None) -> None:
        self._relative_to = relative_to

    def extract(self, path: Path) -> AstDocument:
        source = read_source(path)
        rel = self._rel(path)
        digest = hashlib.sha256(source.encode("utf-8", errors="replace")).hexdigest()
        try:
            tokens = tokenize(source)
        except Vb6LexError as exc:
            return AstDocument(
                schema_version=AST_SCHEMA_VERSION,
                file=rel,
                parser=self.parser_name,
                parser_version=self.parser_version,
                diagnostics=[str(exc)],
                parse_status=ParseStatus.FAILED,
                sha256=digest,
            )
        statements = split_statements(tokens, source)
        return self._from_statements(path, rel, source, statements, digest)

    # -- statement level ---------------------------------------------------

    def _from_statements(
        self,
        path: Path,
        rel: str,
        source: str,
        statements: list[Statement],
        digest: str,
    ) -> AstDocument:
        diagnostics: list[str] = []
        attributes: list[AstAttribute] = [
            attribute
            for statement in statements
            if self._is_attribute(statement)
            and (attribute := self._attribute(statement)) is not None
        ]
        option_explicit = self._option_explicit(statements)
        suffix = path.suffix.lower()

        if suffix == ".vbp":
            return self._from_project_file(rel, statements, digest, diagnostics)

        name = self._component_name(attributes) or path.stem
        kind = self._component_kind(suffix, statements, attributes, diagnostics)
        attention = self._module_attention(statements, option_explicit)

        procedures: list[AstProcedure] = []
        module_variables: list[AstVariable] = []

        index = 0
        while index < len(statements):
            statement = statements[index]
            header = self._procedure_header(statement)
            if header is None:
                var = self._variable_declaration(statement, "MODULE")
                if var is not None:
                    module_variables.extend(var)
                index += 1
                continue
            end_index, _end_line = self._find_procedure_end(statements, index)
            body = statements[index + 1 : end_index]
            procedures.append(
                self._build_procedure(header, body, statements[index:end_index], source)
            )
            index = end_index + 1

        referenced = self._referenced_components(statements)
        component = AstComponent(
            name=name,
            kind=kind,
            procedures=procedures,
            variables=module_variables,
            depends_on=referenced,
            attributes=attributes,
            parse_status=ParseStatus.OK
            if procedures or kind is not ComponentKind.UNKNOWN
            else ParseStatus.PARTIAL,
            attention_points=attention,
            controls=self._controls(statements),
        )
        document = AstDocument(
            schema_version=AST_SCHEMA_VERSION,
            file=rel,
            parser=self.parser_name,
            parser_version=self.parser_version,
            component=component,
            entry_points=self._entry_points(name, kind, procedures),
            option_explicit=option_explicit,
            diagnostics=diagnostics,
            parse_status=ParseStatus.OK,
            referenced_forms=referenced,
            sha256=digest,
        )
        document.sql_strings = [
            s for proc in procedures for s in proc.sql_strings if s.enclosing_procedure
        ]
        return document

    def _from_project_file(
        self, rel: str, statements: list[Statement], digest: str, diagnostics: list[str]
    ) -> AstDocument:
        """``.vbp`` files list components, they do not contain code."""
        components: list[AstComponent] = []
        entry_points: list[AstEntryPoint] = []
        project_name: str | None = None
        for statement in statements:
            line = statement.text.strip()
            if not line:
                continue
            if "=" not in line:
                if line.startswith("["):
                    # INI section header (``[Host Extender Info]``), not a
                    # directive: part of the project layout, not a parse gap.
                    continue
                diagnostics.append(f"unparsed project directive: {line[:60]!r}")
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"')
            if key.lower() == "name":
                project_name = value
                continue
            kind = _PROJECT_KEYS.get(key.lower())
            if kind is None:
                continue
            components.append(
                AstComponent(
                    name=value,
                    kind=kind,
                    attributes=[AstAttribute(name=key, value=value)],
                    parse_status=ParseStatus.OK,
                )
            )
            entry_points.append(
                AstEntryPoint(
                    name=value, kind=EntryPointKind.FORM, component=value, line=statement.line
                )
            )
        if project_name is None:
            diagnostics.append("project file without Name= directive")
        return AstDocument(
            schema_version=AST_SCHEMA_VERSION,
            file=rel,
            parser=self.parser_name,
            parser_version=self.parser_version,
            project_name=project_name,
            components=components,
            entry_points=entry_points,
            diagnostics=diagnostics,
            parse_status=ParseStatus.OK if components else ParseStatus.UNKNOWN,
            sha256=digest,
        )

    def _build_procedure(
        self,
        header: tuple[
            str, ProcedureKind, list[AstParameter], str, int, bool, str | None, str | None
        ],
        body: list[Statement],
        all_statements: list[Statement],
        source: str,
    ) -> AstProcedure:
        name, kind, params, visibility, start_line, is_handler, event_name, return_type = header
        variables: list[AstVariable] = []
        calls: list[AstCall] = []
        sql_strings: list[AstSqlString] = []
        state_writes: list[AstStateWrite] = []
        attention: list[str] = []

        for statement in body:
            attention.extend(self._statement_attention(statement))
            variables.extend(self._variable_declaration(statement, "LOCAL") or [])
            calls.extend(self._calls_in(statement))
            sql_strings.extend(self._sql_strings(statement, name))
            state_writes.extend(self._state_writes_in(statement))
            variables.extend(self._implicit_assignments(statement, diagnostics=attention))

        end_line = all_statements[-1].end_line if all_statements else start_line
        body_text = "\n".join(s.text for s in all_statements).strip()
        return AstProcedure(
            name=name,
            kind=kind,
            parameters=params,
            variables=variables,
            calls=calls,
            sql_strings=sql_strings,
            start_line=start_line,
            end_line=end_line,
            visibility=visibility,
            return_type=return_type,
            is_event_handler=is_handler,
            event_name=event_name,
            body=body_text,
            state_writes=state_writes,
            parse_status=ParseStatus.OK,
            attention_points=sorted(set(attention)),
        )

    # -- headers / parameters ---------------------------------------------

    def _procedure_header(
        self, statement: Statement
    ) -> (
        tuple[str, ProcedureKind, list[AstParameter], str, int, bool, str | None, str | None] | None
    ):
        tokens = [
            t for t in statement.tokens if t.kind not in (TokenKind.COMMENT, TokenKind.ATTRIBUTE)
        ]
        if not tokens:
            return None
        index = 0
        visibility = "PUBLIC"
        while index < len(tokens) and tokens[index].value.lower() in _MODIFIERS:
            if tokens[index].value.lower() == "private":
                visibility = "PRIVATE"
            index += 1
        if index >= len(tokens) or tokens[index].kind is not TokenKind.KEYWORD:
            return None
        head = tokens[index].value.lower()
        if head not in ("sub", "function", "property", "declare"):
            return None
        index += 1
        if head == "property" and index < len(tokens) and tokens[index].kind is TokenKind.KEYWORD:
            index += 1  # Get / Let / Set
        if index >= len(tokens) or tokens[index].kind not in (
            TokenKind.IDENTIFIER,
            TokenKind.KEYWORD,
        ):
            return None
        name = tokens[index].value
        line = tokens[index].line
        is_handler = bool(re.search(r"_\w+$", name)) and head == "sub"
        event_name = name.split("_", 1)[1] if is_handler and "_" in name else None
        kind = ProcedureKind.FUNCTION if head == "function" else ProcedureKind.PROCEDURE
        params, return_type = self._parameters(statement, index + 1, kind)
        return name, kind, params, visibility, line, is_handler, event_name, return_type

    def _parameters(
        self, statement: Statement, start: int, kind: ProcedureKind
    ) -> tuple[list[AstParameter], str | None]:
        tokens = statement.tokens
        open_index = next((i for i in range(start, len(tokens)) if tokens[i].is_punct("(")), None)
        if open_index is None:
            return (
                [AstParameter(name="", direction="RETURN")]
                if kind is ProcedureKind.FUNCTION
                else []
            ), None
        depth = 0
        close_index = None
        for i in range(open_index, len(tokens)):
            if tokens[i].is_punct("("):
                depth += 1
            elif tokens[i].is_punct(")"):
                depth -= 1
                if depth == 0:
                    close_index = i
                    break
        if close_index is None:
            return [], None
        params: list[AstParameter] = []
        for chunk in _split_top_level(tokens[open_index + 1 : close_index]):
            param = self._parameter(chunk)
            if param is not None:
                params.append(param)
        return_type = None
        tail = tokens[close_index + 1 :]
        if kind is ProcedureKind.FUNCTION and len(tail) >= 2 and tail[0].is_word("as"):
            return_type = tail[1].value
        return params, return_type

    def _parameter(self, tokens: list[Token]) -> AstParameter | None:
        words = [
            t
            for t in tokens
            if t.kind in (TokenKind.IDENTIFIER, TokenKind.KEYWORD, TokenKind.STRING)
        ]
        if not words:
            return None
        name = next((t.value for t in words if t.kind is TokenKind.IDENTIFIER), None)
        if name is None:
            return None
        type_name: str | None = None
        confidence = ParseStatus.UNKNOWN
        lowered = [t.value.lower() for t in words]
        if "as" in lowered:
            position = lowered.index("as")
            if position + 1 < len(words):
                type_name = words[position + 1].value
                confidence = ParseStatus.OK
        direction = "IN"
        if "optional" in lowered:
            direction = "IN"
        if _OUT_PARAM.match(name.upper()) and type_name is not None:
            direction = "OUT"
        return AstParameter(
            name=name,
            type_name=type_name,
            direction=direction,
            by_ref="byval" not in lowered,
            optional="optional" in lowered,
            type_confidence=confidence,
            line=words[0].line,
        )

    def _find_procedure_end(self, statements: list[Statement], start: int) -> tuple[int, int]:
        depth = 0
        for index in range(start + 1, len(statements)):
            statement = statements[index]
            words = [
                t.value.lower()
                for t in statement.significant()
                if t.kind is not TokenKind.PUNCTUATION
            ]
            if not words:
                continue
            if words[0] == "end" and len(words) > 1 and words[1] in ("sub", "function", "property"):
                return index, statement.end_line
            if words[0] in ("sub", "function", "property") and _looks_like_header(statement):
                depth += 1
        return len(statements) - 1, statements[-1].end_line if statements else 0

    # -- declarations ------------------------------------------------------

    def _variable_declaration(self, statement: Statement, scope: str) -> list[AstVariable] | None:
        tokens = [
            t for t in statement.tokens if t.kind not in (TokenKind.COMMENT, TokenKind.ATTRIBUTE)
        ]
        if not tokens:
            return None
        head = tokens[0].value.lower() if tokens[0].kind is TokenKind.KEYWORD else None
        if head not in ("dim", "public", "private", "global", "const"):
            return None
        if head == "public" and len(tokens) > 1 and tokens[1].is_word("sub"):
            return None
        if head == "private" and len(tokens) > 1 and tokens[1].is_word("type"):
            return None
        out: list[AstVariable] = []
        for chunk in _split_top_level(tokens[1:], separator=","):
            names: list[str] = []
            type_name: str | None = None
            confidence = ParseStatus.UNKNOWN
            as_seen = False
            for token in chunk:
                if token.is_word("as"):
                    as_seen = True
                    continue
                if as_seen:
                    type_name = token.value
                    confidence = ParseStatus.OK
                    break
                if token.kind is TokenKind.IDENTIFIER and not token.is_word("new"):
                    names.append(token.value)
            for name in names:
                out.append(
                    AstVariable(
                        name=name,
                        type_name=type_name,
                        scope=scope,
                        line=tokens[0].line,
                        type_confidence=confidence,
                    )
                )
        return out or None

    def _implicit_assignments(
        self, statement: Statement, *, diagnostics: list[str]
    ) -> list[AstVariable]:
        """``x = ...`` without a declaration: an implicit variant variable.

        Without ``Option Explicit`` the type cannot be proven, so the variable is
        recorded with ``type_confidence=UNKNOWN`` and a diagnostic is added
        instead of guessing a type.
        """
        tokens = [
            t for t in statement.tokens if t.kind not in (TokenKind.COMMENT, TokenKind.ATTRIBUTE)
        ]
        if len(tokens) < 3 or tokens[0].kind is not TokenKind.IDENTIFIER:
            return []
        if not tokens[1].is_punct("="):
            return []
        if not any(t.kind is TokenKind.STRING for t in tokens):
            return []
        name = tokens[0].value
        diagnostics.append(f"implicit variant variable {name!r} (no declaration seen)")
        return [AstVariable(name=name, scope="IMPLICIT", line=tokens[0].line)]

    # -- calls -------------------------------------------------------------

    def _state_writes_in(self, statement: Statement) -> list[AstStateWrite]:
        """Proven state writes in ``statement``, with the real source line."""
        line = statement.text.strip()
        if not line or line.startswith("'"):
            return []
        match = match_state_statement(line)
        if match is None:
            return []
        target, value, shape = match
        return [AstStateWrite(target=target, value=value, line=statement.line, shape=shape)]

    def _calls_in(self, statement: Statement) -> list[AstCall]:
        """Every call the statement makes, found by scanning *all* tokens.

        A VB6 call appears in three shapes, and all three are recognised here:

        * invocation     ``CheckCreditLimit(0, requestedCredit)`` -- also inside
          an expression (``ValidateCustomer = CheckCreditLimit(...)``) or inside
          a control statement (``If Not Validation.IsValidCustomerBasics(..)``);
        * member call    ``obj.Method(a)`` / ``Mod.Proc(a)``;
        * statement call ``Mod.Proc arg1, arg2`` (no parentheses in VB6).

        Scanning every token instead of only the statement head is what makes
        calls inside ``If``/``Else``/``Do`` visible; missing them would produce a
        call graph that under-reports the legacy system, which is exactly the
        kind of silent gap this framework must not have.
        """
        tokens = [
            t for t in statement.tokens if t.kind not in (TokenKind.COMMENT, TokenKind.ATTRIBUTE)
        ]
        if not tokens:
            return []
        calls: list[AstCall] = []
        seen: set[tuple[str, str | None]] = set()

        def remember(call: AstCall) -> None:
            key = (call.callee.lower(), (call.receiver or "").lower())
            if key in seen:
                return
            seen.add(key)
            calls.append(call)

        head = tokens[0]
        explicit_call = head.is_word("call") and len(tokens) > 1
        for position, token in enumerate(tokens):
            if token.kind not in (TokenKind.IDENTIFIER, TokenKind.KEYWORD):
                continue
            lowered = token.value.lower()
            if explicit_call and position == 0:
                continue
            if lowered in _NOT_CALL_LEADERS and not (
                token.kind is TokenKind.IDENTIFIER
                and position + 1 < len(tokens)
                and tokens[position + 1].is_punct("(")
            ):
                continue
            following = tokens[position + 1] if position + 1 < len(tokens) else None
            preceding = tokens[position - 1] if position else None

            # ---- member access: ``Recv.Member`` ---------------------------
            if preceding is not None and preceding.is_punct(".") and position >= 2:
                receiver = tokens[position - 2]
                receiver_index = position - 2
                if receiver.kind not in (TokenKind.IDENTIFIER, TokenKind.KEYWORD):
                    continue
                if following is not None and following.is_punct("("):
                    remember(
                        self._call(
                            token, statement, receiver=receiver.value, open_index=position + 1
                        )
                    )
                elif receiver_index == 0 or (receiver_index == 1 and tokens[0].is_word("call")):
                    # ``Mod.Proc arg1, arg2`` / ``Call Mod.Proc arg1`` /
                    # ``Mod.Proc`` (no arguments at all). The decision uses the
                    # *receiver* position: the member sits two tokens later.
                    remember(
                        self._call(
                            token,
                            statement,
                            receiver=receiver.value,
                            open_index=None,
                            arg_start=position + 1,
                        )
                    )
                continue

            # ---- invocation: ``Name(`` -----------------------------------
            if following is not None and following.is_punct("("):
                if _is_type_token(token) and token.kind is TokenKind.KEYWORD:
                    # ``String(`` / ``Long(`` in a type position is a cast.
                    continue
                if position and tokens[position - 1].is_punct("."):
                    continue
                remember(self._call(token, statement, receiver=None, open_index=position + 1))
                continue

            # ---- statement call: ``Name arg`` ----------------------------
            if (
                position == 0 or (explicit_call and position == 1)
            ) and token.kind is TokenKind.IDENTIFIER:
                if following is None or following.is_punct("=") or following.is_word("as"):
                    continue
                if following.kind not in (TokenKind.IDENTIFIER, TokenKind.STRING, TokenKind.NUMBER):
                    continue
                if token.value.lower() in _NOT_CALL_LEADERS:
                    continue
                remember(
                    self._call(
                        token,
                        statement,
                        receiver=None,
                        open_index=None,
                        arg_start=position + 1,
                    )
                )
        return calls

    def _call(
        self,
        token: Token,
        statement: Statement,
        receiver: str | None,
        open_index: int | None,
        arg_start: int | None = None,
    ) -> AstCall:
        text = statement.text
        is_new = "new" in text.lower().split()[:3]
        is_builtin = token.value.lower() in _VB_BUILTINS
        kind = CallKind.DYNAMIC if receiver else CallKind.STATIC
        if is_new or is_builtin:
            # Builtins and object construction are real calls but they are not
            # part of the legacy system's own call graph.
            kind = CallKind.EXTERNAL
        if open_index is not None:
            # The token index is known: the parenthesis is unambiguous. Falling
            # back to text slicing here would read the *last* parenthesis of a
            # nested expression and invent arguments such as
            # ``Validation .IsBlank(name)``.
            arguments = _arguments_at(statement.tokens, open_index)
        elif arg_start is not None and arg_start < len(statement.tokens):
            # VB6 statement calls have no parentheses: ``SetCustomerState 0, "X"``
            # and ``Call Mod.Proc arg1, arg2`` alike. The tokens after the callee
            # (and after the receiver) are the argument list; splitting the
            # *text* would keep the ``Call`` keyword and the qualifier.
            arguments = [
                _render(chunk)
                for chunk in _split_top_level(statement.tokens[arg_start:])
                if _render(chunk)
            ]
        else:
            arguments = []
        return AstCall(
            callee=token.value,
            kind=kind,
            line=token.line,
            expression=text[:200],
            receiver=receiver,
            argument_count=len(arguments),
            arguments=arguments,
        )

    # -- SQL ---------------------------------------------------------------

    def _sql_strings(self, statement: Statement, procedure: str) -> list[AstSqlString]:
        strings = statement.strings()
        if not strings:
            return []
        template, has_amp = _assemble_sql_template(statement)
        joined = _concatenated_text(statement)
        if _CALL_LITERAL.match(joined) or _CALL_HEAD.match(joined):
            # A ``{call ...}`` command is not data-interpolated SQL: keep the
            # literal text verbatim so the procedure name can be proven.
            template, has_amp = joined, has_amp
        looks_sql = bool(_SQL_START.match(joined) or _SQL_START.match(template))
        has_sql_tail = bool(_SQL_KEYWORD_TAIL.search(joined))
        if not (looks_sql or (has_sql_tail and len(joined) > 12)):
            return []
        state_tokens = [s.value for s in strings if is_state_literal(s.value.strip().upper())]
        mentions_state_column = bool(_STATE_COLUMN.search(statement.text))
        return [
            AstSqlString(
                text=template,
                raw=statement.text,
                is_dynamic=has_amp or len(strings) > 1,
                line=strings[0].line,
                enclosing_procedure=procedure,
                state_tokens=state_tokens,
                mentions_state_column=mentions_state_column,
                literal_text=joined,
            )
        ]

    # -- attention points / metadata ---------------------------------------

    def _statement_attention(self, statement: Statement) -> list[str]:
        words = [t.value.lower() for t in statement.significant()]
        text = statement.text.lower()
        found: list[str] = []
        if "on error resume next" in text:
            found.append("On Error Resume Next: silent error handling may hide behaviour")
        if words[:2] == ["on", "error"] and "goto" in words:
            found.append("On Error GoTo: error interception branch")
        if "err." in text:
            found.append("Err.* inspected: error-derived behaviour")
        if words and words[0] == "goto":
            found.append("GO TO: unstructured control flow")
        if "doevents" in words:
            found.append("DoEvents: re-entrancy point")
        if "createobject" in text or "getobject" in text:
            found.append("late binding via CreateObject/GetObject")
        return found

    def _module_attention(
        self, statements: list[Statement], option_explicit: bool | None
    ) -> list[str]:
        found: list[str] = []
        if option_explicit is False:
            found.append("Option Explicit absent: implicit variant variables, types unprovable")
        for statement in statements:
            found.extend(self._statement_attention(statement))
        return sorted(set(found))

    def _option_explicit(self, statements: list[Statement]) -> bool | None:
        for statement in statements:
            words = [t.value.lower() for t in statement.significant()]
            if words[:1] == ["option"] and "explicit" in words[:2]:
                return True
        return (
            False
            if any([t.value.lower() for t in s.significant()][:1] == ["option"] for s in statements)
            else None
        )

    def _component_name(self, attributes: list[AstAttribute]) -> str | None:
        for attribute in attributes:
            if attribute.name.lower() == "vb_name" and attribute.value:
                return attribute.value
        return None

    def _component_kind(
        self,
        suffix: str,
        statements: list[Statement],
        attributes: list[AstAttribute],
        diagnostics: list[str],
    ) -> ComponentKind:
        attribute_names = {a.name.lower() for a in attributes}
        is_form_control = any(_FORM_CONTROL.match(s.text) for s in statements)
        if suffix == ".frm" or is_form_control:
            return ComponentKind.FORM
        if suffix == ".ctl":
            return ComponentKind.UNKNOWN
        if suffix == ".cls":
            if attribute_names & {"vb_creatable", "vb_predeclaredid", "vb_globalnamespace"}:
                return ComponentKind.CLASS
            return ComponentKind.CLASS
        if suffix == ".bas":
            if any(a.name.lower() == "vb_predeclaredid" for a in attributes):
                return ComponentKind.MODULE
            if attribute_names & {"vb_creatable", "vb_globalnamespace"}:
                return ComponentKind.CLASS
            return ComponentKind.MODULE
        diagnostics.append(f"unknown component kind for suffix {suffix!r}")
        return ComponentKind.UNKNOWN

    def _controls(self, statements: list[Statement]) -> list[str]:
        controls: list[str] = []
        for statement in statements:
            if not _FORM_CONTROL.match(statement.text):
                continue
            parts = statement.text.split()
            if len(parts) >= 4:
                controls.append(parts[3])
        return controls

    def _referenced_components(self, statements: list[Statement]) -> list[str]:
        referenced: list[str] = []
        for statement in statements:
            words = [t.value for t in statement.significant()]
            for position, word in enumerate(words):
                if word.lower() == "load" and position + 1 < len(words):
                    referenced.append(words[position + 1])
            if (
                statement.text.strip().lower().startswith("dim ")
                and " as " in statement.text.lower()
            ):
                parts = statement.text.split()
                if "as" in [p.lower() for p in parts]:
                    index = [p.lower() for p in parts].index("as")
                    if index + 1 < len(parts):
                        referenced.append(parts[index + 1])
        return sorted(set(referenced))

    def _entry_points(
        self, name: str, kind: ComponentKind, procedures: list[AstProcedure]
    ) -> list[AstEntryPoint]:
        entries: list[AstEntryPoint] = []
        if kind is ComponentKind.FORM:
            entries.append(AstEntryPoint(name=name, kind=EntryPointKind.FORM, component=name))
        elif kind is ComponentKind.UNKNOWN:
            entries.append(AstEntryPoint(name=name, kind=EntryPointKind.UNKNOWN, component=name))
        for procedure in procedures:
            if procedure.is_event_handler:
                # Click/Load/Unload: the UI invokes them, not another procedure.
                entries.append(
                    AstEntryPoint(
                        name=f"{name}.{procedure.name}",
                        kind=EntryPointKind.EVENT_HANDLER,
                        component=name,
                        procedure=procedure.name,
                        target=procedure.event_name,
                        line=procedure.start_line,
                    )
                )
            elif procedure.visibility.upper() == "PUBLIC":
                # A Public Sub/Function is callable from outside the component,
                # therefore an entry point. Marked COMMAND rather than FORM: it
                # is an operation, not a screen.
                entries.append(
                    AstEntryPoint(
                        name=f"{name}.{procedure.name}",
                        kind=EntryPointKind.COMMAND,
                        component=name,
                        procedure=procedure.name,
                        target=procedure.name,
                        line=procedure.start_line,
                    )
                )
        return entries

    def _is_attribute(self, statement: Statement) -> bool:
        return bool(statement.tokens) and statement.tokens[0].kind is TokenKind.ATTRIBUTE

    def _attribute(self, statement: Statement) -> AstAttribute | None:
        match = _ATTR_NAME.match(statement.text)
        if match:
            return AstAttribute(name=match.group(1), value=match.group(2))
        bare = _ATTR_BOOL.match(statement.text)
        if bare:
            return AstAttribute(name=bare.group(1), value="True")
        return None

    def _rel(self, path: Path) -> str:
        if self._relative_to is None:
            return path.name
        try:
            return str(path.relative_to(self._relative_to))
        except ValueError:
            return path.name


_PROJECT_KEYS: dict[str, ComponentKind] = {
    "module": ComponentKind.MODULE,
    "form": ComponentKind.FORM,
    "class": ComponentKind.CLASS,
    "basmodule": ComponentKind.MODULE,
    "control": ComponentKind.UNKNOWN,
    "propertypage": ComponentKind.UNKNOWN,
    "designer": ComponentKind.UNKNOWN,
}


def _concatenated_text(statement: Statement) -> str:
    """The raw concatenation result: only the string literals, in order."""
    return "".join(token.value for token in statement.tokens if token.kind is TokenKind.STRING)


def _assemble_sql_template(statement: Statement) -> tuple[str, bool]:
    """Turn a concatenated SQL expression into a parseable template.

    VB6 builds dynamic SQL with ``&`` and helper calls such as ``CStr(id)``.
    Each run of non-literal tokens becomes a single ``?`` placeholder and a
    directly-invoked helper (``CStr(x)``, no space before the parenthesis) is
    collapsed into one placeholder, so the statement can be parsed with SQLGlot
    without inventing values. The result is explicitly a *template*, flagged as
    ``is_dynamic`` -- not a statement that was observed verbatim.
    """
    tokens = statement.tokens
    first_string = next(
        (i for i, token in enumerate(tokens) if token.kind is TokenKind.STRING), None
    )
    if first_string is None:
        return "", False

    parts: list[str] = []
    pending = False
    has_amp = False
    previous_word: Token | None = None
    index = first_string
    total = len(tokens)

    while index < total:
        token = tokens[index]
        if token.kind is TokenKind.STRING:
            if pending:
                parts.append("?")
                pending = False
            parts.append(token.value)
            previous_word = None
            index += 1
            continue
        if token.value == "&":
            has_amp = True
            index += 1
            continue
        if token.kind is TokenKind.NUMBER:
            if pending:
                parts.append("?")
                pending = False
            parts.append(token.value)
            previous_word = None
            index += 1
            continue
        if token.value == "(":
            adjacent = (
                previous_word is not None
                and previous_word.offset + len(previous_word.value) == token.offset
                and previous_word.value.rstrip("$").upper() not in _SQL_STRUCTURAL_WORDS
            )
            if adjacent:
                pending = False
                parts.append("?")
                depth = 0
                while index < total:
                    if tokens[index].value == "(":
                        depth += 1
                    elif tokens[index].value == ")":
                        depth -= 1
                        if depth == 0:
                            index += 1
                            break
                    index += 1
            else:
                if pending:
                    parts.append("?")
                    pending = False
                parts.append("(")
                index += 1
            previous_word = None
            continue
        if token.value == ")":
            parts.append(")")
            previous_word = None
            index += 1
            continue
        if token.value in _SQL_OPERATOR_TOKENS:
            if pending:
                parts.append("?")
                pending = False
            parts.append(token.value)
            previous_word = None
            index += 1
            continue
        pending = True
        previous_word = token
        index += 1

    if pending:
        parts.append("?")
    template = re.sub(r"\s+", " ", " ".join(parts)).strip()
    return template, has_amp


_SQL_OPERATOR_TOKENS = frozenset(
    {",", "=", "<", ">", "+", "-", "*", "/", "<=", ">=", "<>", "||", "::", "(", ")"}
)
_SQL_STRUCTURAL_WORDS = frozenset(
    [
        "VALUES",
        "IN",
        "FROM",
        "WHERE",
        "AND",
        "OR",
        "ON",
        "SET",
        "INTO",
        "SELECT",
        "TABLE",
        "EXISTS",
        "NOT",
        "IF",
        "WHEN",
        "THEN",
        "ALL",
        "ANY",
        "SOME",
        "DISTINCT",
        "ORDER",
        "GROUP",
        "BY",
        "HAVING",
        "UNION",
        "JOIN",
        "LEFT",
        "RIGHT",
        "INNER",
        "OUTER",
        "FULL",
        "CROSS",
        "USING",
        "AS",
        "CASE",
        "ELSE",
        "END",
        "UPDATE",
        "DELETE",
        "INSERT",
        "MERGE",
        "CREATE",
        "ALTER",
        "DROP",
        "TRUNCATE",
        "WITH",
        "RETURNING",
        "LIMIT",
        "OFFSET",
        "FETCH",
        "FOR",
        "OVER",
        "PARTITION",
        "BETWEEN",
        "LIKE",
        "IS",
        "NULL",
    ]
)


def _is_assignment(tokens: list[Token]) -> bool:
    """True when the statement's first significant token is an assignment target."""
    for token in tokens:
        if token.kind in (TokenKind.COMMENT, TokenKind.ATTRIBUTE):
            continue
        return len(tokens) > 1 and tokens[1].is_punct("=")
    return False


def _split_top_level(tokens: list[Token], separator: str = ",") -> list[list[Token]]:
    chunks: list[list[Token]] = [[]]
    depth = 0
    for token in tokens:
        if token.is_punct("(") or token.is_punct("["):
            depth += 1
        elif token.is_punct(")") or token.is_punct("]"):
            depth -= 1
        if token.value == separator and depth == 0:
            chunks.append([])
            continue
        chunks[-1].append(token)
    return [chunk for chunk in chunks if chunk]


def _split_commas(text: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for char in text:
        if char in "([":
            depth += 1
        elif char in ")]":
            depth -= 1
        if char == "," and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        current.append(char)
    if "".join(current).strip():
        parts.append("".join(current))
    return parts


def _arguments_at(tokens: list[Token], open_index: int) -> list[str]:
    if open_index >= len(tokens) or not tokens[open_index].is_punct("("):
        return []
    depth = 0
    close_index = None
    for index in range(open_index, len(tokens)):
        if tokens[index].is_punct("("):
            depth += 1
        elif tokens[index].is_punct(")"):
            depth -= 1
            if depth == 0:
                close_index = index
                break
    if close_index is None:
        return []
    return [
        _render(chunk)
        for chunk in _split_top_level(tokens[open_index + 1 : close_index])
        if _render(chunk)
    ]


def _render(tokens: list[Token]) -> str:
    parts: list[str] = []
    for token in tokens:
        if not parts:
            parts.append(token.value)
            continue
        previous = parts[-1]
        if (
            token.value in (",", ".")
            or token.is_punct("(")
            or token.is_punct(")")
            or previous.endswith(("(", "."))
        ):
            parts.append(token.value)
        else:
            parts.append(" " + token.value)
    return "".join(parts)


def _looks_like_header(statement: Statement) -> bool:
    words = [t.value.lower() for t in statement.significant()]
    return bool(words) and words[0] in ("sub", "function", "property")


def is_state_literal(value: str) -> bool:
    """True for an ALL-CAPS token that can name a state.

    Deliberately shape-based (uppercase, digits, underscores) and paired with a
    SQL keyword blocklist: the *meaning* comes from the statement shape (state
    setter / state target), never from a hard-coded list of state names, so new
    states in a legacy system are discovered instead of whitelisted.
    """
    candidate = value.strip().upper()
    if not _STATE_LITERAL.match(candidate):
        return False
    return candidate not in _STATE_LITERAL_EXCLUDED


def match_state_statement(line: str) -> tuple[str, str, str] | None:
    """Return ``(state_target, literal, shape)`` for a state-setting statement.

    Only *shape* decides: a literal in ALL-CAPS assigned to a state-looking
    target, or passed to a state-looking setter. No business state is
    hard-coded, so new states in a legacy system are discovered.
    """
    assign = _STATE_ASSIGN.match(line)
    if assign and is_state_literal(assign.group("value")):
        target = assign.group("target")
        if looks_like_state_variable(target.rsplit(".", 1)[-1]):
            return target, assign.group("value"), "assignment"
    setter = _STATE_SETTER.match(line)
    if setter and is_state_literal(setter.group("value")):
        function = setter.group("fn").rsplit(".", 1)[-1]
        if looks_like_state_variable(function) or looks_like_state_variable(setter.group("arg1")):
            return function, setter.group("value"), "setter"
    inline = _STATE_CALL.match(line)
    if inline and is_state_literal(inline.group("value")):
        arg = (inline.group("arg1") or "").rsplit(".", 1)[-1]
        if arg and looks_like_state_variable(arg):
            return arg, inline.group("value"), "inline_call"
    return None


def looks_like_state_variable(name: str) -> bool:
    lowered = name.lower()
    return any(hint in lowered for hint in _STATE_NAME_HINT)


def infer_state_domain(name: str) -> str:
    lowered = name.lower()
    if any(hint in lowered for hint in _GRUPO_HINT):
        return "GRUPO"
    if any(hint in lowered for hint in _CONSISTENCIA_HINT):
        return "CONSISTENCIA"
    return "UNKNOWN"


def supported_extension(path: Path) -> bool:
    return path.suffix.lower() in _VB_EXTENSIONS
