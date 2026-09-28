"""Canonical VB6 ``AST JSON`` schema -- the interop contract with ProLeap.

The Python engine never parses VB6 semantics itself. It consumes a JSON document
described here. The document is produced either by

* ``proleap_cli analyze <file> --output <ast.json>`` (reference parser, ADR 008), or
* the built-in structural fallback (:mod:`legacyctl.parsers.vb6.extractor`).

Both producers emit *exactly* this shape, so the downstream conversion in
:mod:`legacyctl.parsers.vb6.parser` is identical for both. When ProLeap emits its
native JSON instead, ``translate_native`` maps it into this schema and records
the translation as a diagnostic.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ...domain.enums import (
    CallKind,
    ComponentKind,
    EntryPointKind,
    ParseStatus,
    ProcedureKind,
)

AST_SCHEMA_VERSION = "1.0"


class AstModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class AstLocation(AstModel):
    file: str
    line: int = Field(ge=0)
    column: int = Field(default=0, ge=0)
    end_line: int | None = Field(default=None, ge=0)


class AstAttribute(AstModel):
    name: str
    value: str


class AstParameter(AstModel):
    name: str
    type_name: str | None = None
    direction: str = "IN"  # IN | OUT | INOUT | RETURN
    by_ref: bool = True
    optional: bool = False
    type_confidence: ParseStatus = ParseStatus.UNKNOWN
    line: int = Field(default=0, ge=0)


class AstVariable(AstModel):
    name: str
    type_name: str | None = None
    scope: str = "LOCAL"  # LOCAL | MODULE | GLOBAL | PARAMETER
    line: int = Field(default=0, ge=0)
    type_confidence: ParseStatus = ParseStatus.UNKNOWN


class AstSqlString(AstModel):
    """A string literal that is a candidate SQL statement.

    ``expression`` keeps the concatenation form (``"..." & var & "..."``) so the
    dynamic-SQL heuristic is auditable instead of guessed.
    """

    text: str
    raw: str
    is_dynamic: bool = False
    line: int = Field(default=0, ge=0)
    enclosing_procedure: str | None = None
    state_tokens: list[str] = Field(default_factory=list)
    mentions_state_column: bool = False
    literal_text: str | None = None


class AstCall(AstModel):
    callee: str
    kind: CallKind = CallKind.STATIC
    line: int = Field(default=0, ge=0)
    expression: str | None = None
    receiver: str | None = None
    argument_count: int = 0
    arguments: list[str] = Field(default_factory=list)


class AstStateWrite(AstModel):
    """A proven write of a state literal to a state target.

    Emitted by the fallback with an exact ``line`` so provenance survives into
    the domain model; the ProLeap bridge may leave the list empty.
    """

    target: str
    value: str
    line: int = Field(default=0, ge=0)
    shape: str = "assignment"  # assignment | setter | inline_call


class AstProcedure(AstModel):
    name: str
    kind: ProcedureKind = ProcedureKind.PROCEDURE
    parameters: list[AstParameter] = Field(default_factory=list)
    variables: list[AstVariable] = Field(default_factory=list)
    calls: list[AstCall] = Field(default_factory=list)
    sql_strings: list[AstSqlString] = Field(default_factory=list)
    start_line: int = Field(default=0, ge=0)
    end_line: int | None = Field(default=None, ge=0)
    visibility: str = "PUBLIC"
    return_type: str | None = None
    is_event_handler: bool = False
    event_name: str | None = None
    body: str | None = None
    state_writes: list[AstStateWrite] = Field(default_factory=list)
    parse_status: ParseStatus = ParseStatus.OK
    attention_points: list[str] = Field(default_factory=list)


class AstComponent(AstModel):
    name: str
    kind: ComponentKind = ComponentKind.MODULE
    procedures: list[AstProcedure] = Field(default_factory=list)
    variables: list[AstVariable] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    attributes: list[AstAttribute] = Field(default_factory=list)
    parse_status: ParseStatus = ParseStatus.OK
    attention_points: list[str] = Field(default_factory=list)
    controls: list[str] = Field(default_factory=list)
    project_name: str | None = None


class AstEntryPoint(AstModel):
    name: str
    kind: EntryPointKind = EntryPointKind.PROCEDURE
    component: str
    procedure: str | None = None
    target: str | None = None
    line: int = Field(default=0, ge=0)


class AstDocument(AstModel):
    """Root of the interop document."""

    schema_version: str = AST_SCHEMA_VERSION
    file: str
    language: str = "vb6"
    parser: str = "legacyctl.vb6.fallback"
    parser_version: str = "0.1.0"
    project_name: str | None = None
    component: AstComponent | None = None
    components: list[AstComponent] = Field(default_factory=list)
    entry_points: list[AstEntryPoint] = Field(default_factory=list)
    sql_strings: list[AstSqlString] = Field(default_factory=list)
    option_explicit: bool | None = None
    diagnostics: list[str] = Field(default_factory=list)
    parse_status: ParseStatus = ParseStatus.OK
    referenced_forms: list[str] = Field(default_factory=list)
    sha256: str | None = None

    def all_components(self) -> list[AstComponent]:
        if self.component is not None:
            return [self.component, *self.components]
        return list(self.components)

    def to_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json"), indent=2, sort_keys=False, ensure_ascii=False
        )

    @staticmethod
    def from_path(path: Path) -> AstDocument:
        return AstDocument.model_validate_json(path.read_text(encoding="utf-8"))

    @staticmethod
    def from_dict(payload: dict[str, Any]) -> AstDocument:
        return AstDocument.model_validate(payload)

    def write(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json(), encoding="utf-8")
        return path


def translate_native(payload: dict[str, Any], *, file: str) -> AstDocument:
    """Best-effort mapping of a foreign (ProLeap native) JSON shape.

    Unknown shapes are *not* faked: the document is produced with
    ``parse_status=UNKNOWN`` and a diagnostic, so the caller degrades instead of
    inventing structure.
    """
    if "schema_version" in payload and "component" in payload:
        return AstDocument.from_dict(payload)

    diagnostics = ["translated from foreign AST shape"]
    component_payload = payload.get("component") or payload.get("form") or payload
    name = component_payload.get("name") or file
    procedures: list[AstProcedure] = []
    for raw_proc in component_payload.get("procedures", []) or []:
        procedures.append(
            AstProcedure(
                name=raw_proc.get("name", "<unknown>"),
                start_line=int(raw_proc.get("line", 0) or 0),
                parse_status=ParseStatus.PARTIAL,
            )
        )
    document = AstDocument(
        file=file,
        parser="proleap.native-translation",
        component=AstComponent(name=name, kind=ComponentKind.UNKNOWN, procedures=procedures),
        diagnostics=diagnostics,
        parse_status=ParseStatus.PARTIAL if procedures else ParseStatus.UNKNOWN,
    )
    return document
