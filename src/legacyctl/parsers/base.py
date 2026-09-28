"""Ports: interfaces implemented by language adapters.

``LegacyParser`` is the only contract the framework knows about source code.
Adding COBOL, Delphi or PL/SQL support means implementing this protocol; it does
not mean touching the domain.

Contract with an external parser (see ADR 008)::

    proleap_cli analyze <file> --output <ast.json>
    VB6Parser.from_ast(ast_json) -> SourceFile & Procedure[] & Call[]
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ..domain.enums import ComponentKind, EntryPointKind
from ..domain.models import (
    Call,
    Component,
    DatabaseObject,
    LegacySystem,
    Procedure,
    SourceFile,
    SourceFragment,
    SourceLocation,
    SqlStatement,
    StateTransition,
)


@runtime_checkable
class LegacyParser(Protocol):
    """Deterministic structural parser for one source language.

    Implementations MUST NOT use an LLM and MUST NOT invent dependencies: when
    a construct cannot be understood they report ``ParseStatus.UNKNOWN``.
    """

    language: str
    name: str
    version: str
    extensions: tuple[str, ...]
    component_kinds: tuple[ComponentKind, ...]
    node_types: tuple[str, ...]
    edge_types: tuple[str, ...]

    def supports(self, path: Path) -> bool:
        """True when this parser owns the given file."""
        ...

    def parse_file(self, path: Path, *, source_root: Path | None = None) -> ParsedUnit:
        """Parse a single file into language-agnostic structures."""
        ...

    def parse(self, source_root: Path, *, language: str | None = None) -> LegacySystem:
        """Parse an entire source tree into a :class:`LegacySystem`."""
        ...


@runtime_checkable
class EntryPointDetector(Protocol):
    """Discovers user-reachable entry points (forms, menus, batches...)."""

    def detect(
        self, files: Sequence[SourceFile], components: Sequence[Component]
    ) -> list[DetectedEntryPoint]: ...


class ParsedUnit:
    """Flat result of parsing one file, ready to be folded into a system."""

    __slots__ = (
        "calls",
        "components",
        "database_objects",
        "entry_points",
        "file",
        "fragments",
        "procedures",
        "sql_statements",
        "state_transitions",
    )

    def __init__(
        self,
        *,
        file: SourceFile,
        components: list[Component] | None = None,
        procedures: list[Procedure] | None = None,
        calls: list[Call] | None = None,
        sql_statements: list[SqlStatement] | None = None,
        database_objects: list[DatabaseObject] | None = None,
        entry_points: list[DetectedEntryPoint] | None = None,
        fragments: list[SourceFragment] | None = None,
        state_transitions: list[StateTransition] | None = None,
    ) -> None:
        self.file = file
        self.components = components or []
        self.procedures = procedures or []
        self.calls = calls or []
        self.sql_statements = sql_statements or []
        self.database_objects = database_objects or []
        self.entry_points = entry_points or []
        self.fragments = fragments or []
        self.state_transitions = state_transitions or []

    def merge_into(self, system: LegacySystem) -> None:
        system.files.append(self.file)
        system.components.extend(self.components)
        system.procedures.extend(self.procedures)
        system.calls.extend(self.calls)
        system.sql_statements.extend(self.sql_statements)
        system.database_objects.extend(self.database_objects)
        system.source_fragments.extend(self.fragments)
        system.state_transitions.extend(self.state_transitions)
        system.entry_points.extend(_to_entry_point(ep, self.file.path) for ep in self.entry_points)


class DetectedEntryPoint:
    """A candidate entry point discovered structurally."""

    __slots__ = ("component_id", "kind", "name", "procedure_id", "target")

    def __init__(
        self,
        name: str,
        kind: EntryPointKind,
        component_id: str,
        procedure_id: str | None = None,
        target: str | None = None,
    ) -> None:
        self.name = name
        self.kind = kind
        self.component_id = component_id
        self.procedure_id = procedure_id
        self.target = target

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"DetectedEntryPoint({self.name!r}, {self.kind})"


def _to_entry_point(detected: DetectedEntryPoint, file_path: str) -> Any:
    from ..domain.ids import entry_point_id
    from ..domain.models import EntryPoint

    return EntryPoint(
        id=entry_point_id(detected.name),
        name=detected.name,
        kind=detected.kind,
        component_id=detected.component_id,
        procedure_id=detected.procedure_id,
        target=detected.target,
        source=SourceLocation(file=file_path, line=0),
    )


def iter_supported_files(root: Path, extensions: Iterable[str]) -> list[Path]:
    """Return every file under ``root`` with one of ``extensions``, sorted."""
    wanted = {e.lower() for e in extensions}
    return sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in wanted)


__all__ = [
    "DetectedEntryPoint",
    "EntryPointDetector",
    "LegacyParser",
    "ParsedUnit",
    "iter_supported_files",
]
