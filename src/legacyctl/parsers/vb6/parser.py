"""``VB6Parser``: turns the canonical ``AST JSON`` into domain structures.

The parser is intentionally thin and deterministic. It performs no parsing of
its own: the AST comes from ProLeap (reference) or from the built-in structural
fallback, and this class only *translates*:

    AstDocument -> SourceFile + Component[] + Procedure[] + Call[]
                        + SqlStatement[] + DatabaseProcedure[] + EntryPoint[]

Everything it cannot map with certainty is carried over as ``ParseStatus.UNKNOWN``
or as a diagnostic. It never guesses a table, a callee or a state.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

from ...config.settings import Settings, get_settings
from ...domain.enums import (
    CallKind,
    ComponentKind,
    DatabaseObjectKind,
    EntryPointKind,
    EvidenceKind,
    ParseStatus,
)
from ...domain.ids import database_procedure_id, procedure_id, slugify, system_id
from ...domain.models import (
    Call,
    Component,
    DatabaseObject,
    DatabaseProcedure,
    LegacySystem,
    Procedure,
    SourceFile,
    SourceFragment,
    SourceLocation,
    SqlStatement,
    StateTransition,
    Variable,
)
from ..base import DetectedEntryPoint, LegacyParser, ParsedUnit, iter_supported_files
from ..sql_analyzer import SqlAnalyzer
from .ast import AstCall, AstComponent, AstDocument, AstParameter, AstProcedure
from .extractor import (
    Vb6StructuralExtractor,
    infer_state_domain,
    is_state_literal,
    supported_extension,
)

# State-shape matching now lives in the extractor: it is a property of the
# VB6 surface syntax, and the fallback records the line it matched on.
from .lexer import read_source
from .proleap import ProleapBridge, ProleapError, source_digest

_REGISTER_OUT = re.compile(r"^registerout(param(eter)?)?$", re.IGNORECASE)
_SET_IN = re.compile(r"^set(in|param(eter)?)$", re.IGNORECASE)


class VB6Parser(LegacyParser):
    """Language adapter for Visual Basic 6 (``.bas``/``.frm``/``.cls``/``.vbp``)."""

    language = "vb6"
    name = "vb6"
    version = "0.1.0"
    extensions = (".bas", ".frm", ".cls", ".vbp", ".ctl")
    component_kinds = (ComponentKind.MODULE, ComponentKind.FORM, ComponentKind.CLASS)
    node_types = (
        "FILE",
        "FORM",
        "MODULE",
        "CLASS",
        "PROCEDURE",
        "FUNCTION",
        "VARIABLE",
        "DATABASE_TABLE",
        "DATABASE_VIEW",
        "DATABASE_PROCEDURE",
        "SQL_STATEMENT",
        "ENTRY_POINT",
    )
    edge_types = (
        "CONTAINS",
        "CALLS",
        "READS",
        "WRITES",
        "QUERIES",
        "DEPENDS_ON",
        "ENTRY_TO",
        "DEFINES",
        "INVOKES",
    )

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        use_cache: bool = True,
        allow_fallback: bool = True,
    ) -> None:
        self._settings = settings or get_settings()
        self._use_cache = use_cache
        self._allow_fallback = allow_fallback
        self._sql = SqlAnalyzer()
        self._bridge = ProleapBridge(
            self._settings.proleap_jar,
            cache_dir=self._settings.ast_dir,
            java_executable=self._settings.proleap_java,
            timeout=self._settings.proleap_timeout,
        )
        self.diagnostics: list[str] = []

    # -- LegacyParser ------------------------------------------------------

    def supports(self, path: Path) -> bool:
        return supported_extension(path)

    def parse_file(self, path: Path, *, source_root: Path | None = None) -> ParsedUnit:
        root = source_root or path.parent
        document = self.ast_for(path, source_root=root)
        return self.from_ast(document, source_path=path)

    def parse(self, source_root: Path, *, language: str | None = None) -> LegacySystem:
        if language is not None and language.lower() not in ("vb6", "visual basic 6", "vb"):
            raise ValueError(f"VB6Parser cannot handle language {language!r}")
        files = [
            p for p in iter_supported_files(source_root, self.extensions) if supported_extension(p)
        ]
        system = LegacySystem(
            id=system_id(source_root.name),
            name=source_root.name,
            source_root=str(source_root),
            language=self.language,
            adapter=self.name,
        )
        project_names: list[str] = []
        for path in files:
            try:
                unit = self.parse_file(path, source_root=source_root)
            except Exception as exc:
                self.diagnostics.append(f"{path.name}: {type(exc).__name__}: {exc}")
                continue
            unit.merge_into(system)
            if unit.file.parser.startswith("proleap"):
                project_names.append(path.name)
        system = _resolve_calls(system)
        system = _attach_database_procedures(system)
        system = _attach_state_transitions(system)
        system = _mark_project_ownership(system)
        self.diagnostics.extend(system_parser_diagnostics(system))
        return system

    # -- AST acquisition ---------------------------------------------------

    def ast_for(self, path: Path, *, source_root: Path | None = None) -> AstDocument:
        """ProLeap first, then the cache, then the documented built-in fallback."""
        digest = source_digest(path)
        relative = _relative(path, source_root)
        diagnostics: list[str] = []

        if self._use_cache:
            cached = self._bridge.read_cache(path, digest)
            if cached is not None:
                # A cache hit is not a diagnostic. The channel is reserved for
                # gaps in the knowledge, and noise there trains a reviewer to
                # ignore it.
                return cached

        if self._bridge.available():
            output = self._settings.ast_dir / f"{path.name}.proleap.json"
            try:
                document = self._bridge.analyze(path, output)
            except ProleapError as exc:
                diagnostics.append(f"proleap failed for {relative}: {exc}")
            else:
                document.file = relative
                self._bridge.write_cache(path, document, digest)
                return document

        if not self._allow_fallback:
            raise ProleapError(f"no reference parser available and fallback disabled: {relative}")

        extractor = Vb6StructuralExtractor(relative_to=source_root)
        document = extractor.extract(path)
        if diagnostics:
            document.diagnostics = [*document.diagnostics, *diagnostics]
            document.parse_status = ParseStatus.PARTIAL
        self._bridge.write_cache(path, document, digest)
        return document

    # -- AST -> domain -----------------------------------------------------

    def from_ast(self, document: AstDocument, *, source_path: Path | None = None) -> ParsedUnit:
        source = ""
        if source_path is not None and source_path.is_file():
            source = read_source(source_path)
        line_count = source.count("\n") + 1 if source else 0
        rel = document.file
        file = SourceFile(
            id=SourceFile.make_id(rel),
            path=rel,
            language=self.language,
            sha256=document.sha256 or "",
            line_count=line_count,
            parse_status=document.parse_status,
            parser=document.parser,
            parser_version=document.parser_version,
            diagnostics=list(document.diagnostics),
        )

        components: list[Component] = []
        procedures: list[Procedure] = []
        calls: list[Call] = []
        sql_statements: list[SqlStatement] = []
        entry_points: list[DetectedEntryPoint] = []
        fragments: list[SourceFragment] = []
        transitions: list[StateTransition] = []
        objects: list[DatabaseObject] = []

        for ast_component in document.all_components():
            (
                component,
                comp_procedures,
                comp_calls,
                comp_sql,
                comp_objects,
                comp_fragments,
                comp_transitions,
            ) = self._convert_component(ast_component, rel, source)
            components.append(component)
            procedures.extend(comp_procedures)
            calls.extend(comp_calls)
            sql_statements.extend(comp_sql)
            objects.extend(comp_objects)
            fragments.extend(comp_fragments)
            transitions.extend(comp_transitions)
            for entry in document.entry_points:
                if entry.component == ast_component.name:
                    entry_points.append(
                        DetectedEntryPoint(
                            name=entry.name,
                            kind=entry.kind,
                            component_id=component.id,
                            procedure_id=(
                                procedure_id(ast_component.name, entry.procedure)
                                if entry.procedure
                                else None
                            ),
                            target=entry.target,
                        )
                    )

        return ParsedUnit(
            file=file,
            components=components,
            procedures=procedures,
            calls=calls,
            sql_statements=sql_statements,
            database_objects=objects,
            entry_points=entry_points,
            fragments=fragments,
            state_transitions=transitions,
        )

    def _convert_component(
        self, ast: AstComponent, rel: str, source: str
    ) -> tuple[
        Component,
        list[Procedure],
        list[Call],
        list[SqlStatement],
        list[DatabaseObject],
        list[SourceFragment],
        list[StateTransition],
    ]:
        is_project_declaration = rel.lower().endswith(".vbp")
        # A ``.vbp`` *declaration* must never share an id with the real module
        # that owns the code (``executar_consistencia.bas``): slugifying both
        # yields the same string, and the declaration would shadow the module.
        component_id = Component.make_id(
            f"project:{ast.name}" if is_project_declaration else ast.name
        )
        procedures: list[Procedure] = []
        calls: list[Call] = []
        sql_statements: list[SqlStatement] = []
        objects: list[DatabaseObject] = []
        fragments: list[SourceFragment] = []
        transitions: list[StateTransition] = []
        procedure_ids: list[str] = []

        for ast_procedure in ast.procedures:
            pid = procedure_id(ast.name, ast_procedure.name)
            procedure_ids.append(pid)
            parameters = [_variable_from_parameter(p) for p in ast_procedure.parameters]
            procedures.append(
                Procedure(
                    id=pid,
                    name=ast_procedure.name,
                    component_id=component_id,
                    kind=ast_procedure.kind,
                    file=rel,
                    start_line=ast_procedure.start_line,
                    end_line=ast_procedure.end_line,
                    parameters=parameters,
                    return_type=ast_procedure.return_type,
                    is_public=ast_procedure.visibility.upper() == "PUBLIC",
                    parse_status=ast_procedure.parse_status,
                    body_text=ast_procedure.body,
                    source=SourceLocation(
                        file=rel, line=ast_procedure.start_line, procedure=ast_procedure.name
                    ),
                    attributes={
                        "event": ast_procedure.event_name or "",
                        "attention": "; ".join(ast_procedure.attention_points),
                    },
                )
            )
            calls.extend(self._convert_calls(ast_procedure, pid, rel))
            stmts, objs = self._convert_sql(ast_procedure, rel, ast.name)
            sql_statements.extend(stmts)
            objects.extend(objs)
            if ast_procedure.body:
                fragments.append(
                    SourceFragment(
                        id=f"FRAG-{slugify(rel)}-{slugify(ast_procedure.name)}",
                        file=rel,
                        start_line=ast_procedure.start_line,
                        end_line=ast_procedure.end_line or ast_procedure.start_line,
                        text=ast_procedure.body,
                        procedure_id=pid,
                    )
                )
            transitions.extend(self._state_transitions(ast_procedure, rel, ast.name))

        component = Component(
            id=component_id,
            name=ast.name,
            kind=ast.kind,
            file=rel,
            parse_status=ast.parse_status,
            procedure_ids=procedure_ids,
            variable_ids=[f"VAR-{v.name.upper()}" for v in ast.variables],
            dependencies=ast.depends_on,
            attention_points=ast.attention_points,
            attributes={
                "controls": ", ".join(ast.controls),
                "project": ast.project_name or "",
                **{a.name: a.value for a in ast.attributes},
            },
        )
        return component, procedures, calls, sql_statements, objects, fragments, transitions

    def _convert_calls(self, ast: AstProcedure, pid: str, rel: str) -> list[Call]:
        calls: list[Call] = []
        for index, ast_call in enumerate(ast.calls):
            qualifier = f"{ast_call.receiver}." if ast_call.receiver else ""
            calls.append(
                Call(
                    id=(
                        f"CALL-{slugify(pid)}-{slugify(qualifier + ast_call.callee)}"
                        f"-L{ast_call.line}-{index:02d}"
                    ),
                    caller_id=pid,
                    callee_name=qualifier + ast_call.callee,
                    kind=ast_call.kind,
                    line=ast_call.line,
                    call_expression=ast_call.expression,
                    arguments=list(ast_call.arguments),
                )
            )
        return calls

    def _convert_sql(
        self, ast: AstProcedure, rel: str, component_name: str
    ) -> tuple[list[SqlStatement], list[DatabaseObject]]:
        statements: list[SqlStatement] = []
        objects: list[DatabaseObject] = []
        out_params, in_params = _out_and_in_params(ast.calls)
        for index, ast_sql in enumerate(ast.sql_strings):
            analysis = self._sql.analyze(ast_sql.text)
            location = SourceLocation(
                file=rel,
                line=ast_sql.line,
                procedure=ast_procedure_id(component_name, ast.name),
                statement=f"stmt{index + 1}",
            )
            statement = SqlStatement(
                id=SqlStatement.make_id(location, ast_sql.raw),
                operation=analysis.operation,
                dialect=analysis.dialect,
                raw=ast_sql.raw,
                normalized=analysis.normalized,
                tables=analysis.tables,
                views=analysis.views,
                columns=analysis.columns,
                parse_status=analysis.parse_status,
                is_dynamic=ast_sql.is_dynamic,
                procedure_name=analysis.procedure_name,
                db_schema=analysis.db_schema,
                out_params=(
                    [variable.name for variable in out_params] if analysis.procedure_name else []
                ),
                source=location,
                access_mode=analysis.access_mode,
            )
            statements.append(statement)
            if analysis.procedure_name:
                objects.append(
                    DatabaseProcedure(
                        id=database_procedure_id(analysis.procedure_name),
                        name=analysis.procedure_name.rpartition(".")[2],
                        db_schema=analysis.db_schema,
                        in_params=in_params,
                        out_params=out_params,
                        parse_status=analysis.parse_status,
                        sql_references=analysis.tables + analysis.views,
                        first_seen=location,
                    )
                )
            else:
                for table in analysis.tables:
                    objects.append(
                        DatabaseObject(
                            id=f"TABLE-{slugify(table)}",
                            name=table,
                            kind=DatabaseObjectKind.TABLE,
                            parse_status=analysis.parse_status,
                            first_seen=location,
                        )
                    )
                for view in analysis.views:
                    objects.append(
                        DatabaseObject(
                            id=f"VIEW-{slugify(view)}",
                            name=view,
                            kind=DatabaseObjectKind.VIEW,
                            parse_status=analysis.parse_status,
                            first_seen=location,
                        )
                    )
        return statements, objects

    def _state_transitions(
        self, ast: AstProcedure, rel: str, component_name: str
    ) -> list[StateTransition]:
        """Literal state evidence found in the procedure body.

        Three documented shapes are promoted, all of them literal observations:

        1. ``estadoGrupo = "VALIDANDO"`` -- direct assignment to a state target.
        2. ``AtualizarEstadoGrupo idGrupo, "VALIDANDO"`` -- a state setter called
           with a state literal.
        3. ``UPDATE RASTRO_GRUPO SET ESTADO = 'CRIADO'`` -- a state literal
           written to a state column by SQL.

        Plus one *inference*: consecutive literals observed in the same procedure
        are emitted as an ordered transition with ``evidence_kind=INFERENCE``,
        because code order is not proof of control flow.
        """
        if not ast.state_writes and not ast.sql_strings:
            return []
        trigger = f"{component_name}.{ast.name}"
        pid = procedure_id(component_name, ast.name)
        observed: list[StateTransition] = []
        ordered: list[tuple[str, str, int]] = []

        for write in ast.state_writes:
            domain = infer_state_domain(f"{write.target} {ast.name}")
            observed.append(
                StateTransition(
                    state_domain=domain,
                    from_state="UNKNOWN",
                    to_state=write.value,
                    trigger=trigger,
                    source=SourceLocation(file=rel, line=write.line, procedure=pid),
                    evidence_kind=EvidenceKind.OBSERVATION,
                )
            )
            ordered.append((domain, write.value, write.line))

        for index in range(len(ordered) - 1):
            previous_domain, previous, previous_line = ordered[index]
            domain, current, _ = ordered[index + 1]
            if previous == current:
                continue
            if previous_domain != domain and "UNKNOWN" not in (previous_domain, domain):
                continue
            observed.append(
                StateTransition(
                    state_domain=domain if domain != "UNKNOWN" else previous_domain,
                    from_state=previous,
                    to_state=current,
                    trigger=trigger,
                    # Point at the write that establishes ``from_state``.
                    source=SourceLocation(file=rel, line=previous_line, procedure=pid),
                    evidence_kind=EvidenceKind.INFERENCE,
                )
            )

        for sql in ast.sql_strings:
            if not sql.mentions_state_column:
                continue
            for token in sql.state_tokens:
                value = token.strip().upper()
                if not is_state_literal(value):
                    continue
                observed.append(
                    StateTransition(
                        state_domain=infer_state_domain(f"{sql.raw} {ast.name}"),
                        from_state="UNKNOWN",
                        to_state=value,
                        trigger=trigger,
                        source=SourceLocation(file=rel, line=sql.line, procedure=pid),
                        evidence_kind=EvidenceKind.OBSERVATION,
                    )
                )
        return observed


def _variable_from_parameter(parameter: AstParameter) -> Variable:
    return Variable(
        name=parameter.name,
        type_name=parameter.type_name,
        scope="PARAMETER",
        is_parameter=True,
        is_out=parameter.direction in ("OUT", "INOUT"),
        is_return=parameter.direction == "RETURN",
        is_global=False,
        type_confidence=parameter.type_confidence,
    )


def ast_procedure_id(component_name: str, procedure_name: str) -> str:
    return procedure_id(component_name, procedure_name)


def _out_and_in_params(calls: Iterable[AstCall]) -> tuple[list[Variable], list[Variable]]:
    out_params: list[Variable] = []
    in_params: list[Variable] = []
    for call in calls:
        leaf = call.callee.rsplit(".", 1)[-1]
        positional = [a.strip().strip('"') for a in call.arguments]
        if _REGISTER_OUT.match(leaf):
            for argument in positional:
                if re.fullmatch(r"P_[A-Z0-9_]+", argument.upper()):
                    out_params.append(
                        Variable(name=argument, is_out=True, is_parameter=True, scope="PARAMETER")
                    )
        elif _SET_IN.match(leaf):
            for argument in positional[1:]:
                if argument:
                    in_params.append(Variable(name=argument, is_parameter=True, scope="PARAMETER"))
    return _dedupe_variables(out_params), _dedupe_variables(in_params)


def _dedupe_variables(variables: list[Variable]) -> list[Variable]:
    seen: set[str] = set()
    out: list[Variable] = []
    for variable in variables:
        key = variable.name.upper()
        if key in seen:
            continue
        seen.add(key)
        out.append(variable)
    return out


def _resolve_calls(system: LegacySystem) -> LegacySystem:
    """Bind ``CALLS`` edges to procedure IDs, or mark them unresolved."""
    by_name: dict[str, list[Procedure]] = {}
    for procedure in system.procedures:
        by_name.setdefault(procedure.name.lower(), []).append(procedure)
    components_by_id = {c.id: c for c in system.components}

    for call in system.calls:
        leaf = call.callee_name.rsplit(".", 1)[-1].lower()
        if call.kind is CallKind.EXTERNAL:
            call.callee_id = None
            continue
        candidates = by_name.get(leaf, [])
        if call.callee_name.lower() in by_name:
            candidates = by_name[call.callee_name.lower()]
        caller = next((p for p in system.procedures if p.id == call.caller_id), None)
        if caller is not None and "." in call.callee_name:
            component = components_by_id.get(caller.component_id)
            if component is not None:
                qualified = f"{component.name}.{leaf}"
                if qualified.lower() in by_name:
                    candidates = by_name[qualified.lower()]
        if len(candidates) == 1:
            call.callee_id = candidates[0].id
        else:
            call.callee_id = None
            if len(candidates) > 1:
                call.kind = CallKind.UNRESOLVED
    return system


def _attach_database_procedures(system: LegacySystem) -> LegacySystem:
    """Merge per-file observations into one knowledge object per database object.

    The same table observed from three procedures is one node in the graph, not
    three; a stored procedure keeps every ``sql_reference`` and every
    ``called_by`` edge so the ``CALLS`` boundary into the database is visible.
    """
    procedures: dict[str, DatabaseProcedure] = {}
    tables: dict[str, DatabaseObject] = {}

    for obj in system.database_objects:
        if obj.kind is DatabaseObjectKind.PROCEDURE:
            continue
        existing = tables.get(obj.id)
        if existing is None:
            tables[obj.id] = obj
            continue
        if existing.parse_status is not ParseStatus.OK and obj.parse_status is ParseStatus.OK:
            existing.parse_status = obj.parse_status
        if existing.first_seen is None:
            existing.first_seen = obj.first_seen

    for statement in system.sql_statements:
        if not statement.procedure_name:
            continue
        key = statement.procedure_name.upper()
        existing = procedures.get(key)
        if existing is None:
            schema, _, name = statement.procedure_name.rpartition(".")
            existing = DatabaseProcedure(
                id=database_procedure_id(statement.procedure_name),
                name=name,
                db_schema=schema or None,
                parse_status=statement.parse_status,
                first_seen=statement.source,
            )
            procedures[key] = existing
        if existing.parse_status is not ParseStatus.UNKNOWN and (
            statement.parse_status is ParseStatus.PARTIAL
        ):
            existing.parse_status = ParseStatus.PARTIAL
        for table in [*statement.tables, *statement.views]:
            if table not in existing.sql_references:
                existing.sql_references.append(table)
            tables.setdefault(
                f"TABLE-{slugify(table)}",
                DatabaseObject(
                    id=f"TABLE-{slugify(table)}",
                    name=table,
                    kind=DatabaseObjectKind.TABLE,
                    parse_status=statement.parse_status,
                    first_seen=statement.source,
                ),
            )
        for parameter in statement.out_params:
            if not any(p.name.upper() == parameter.upper() for p in existing.out_params):
                existing.out_params.append(
                    Variable(name=parameter, is_out=True, is_parameter=True, scope="PARAMETER")
                )
        caller = statement.source.procedure
        if caller and caller not in existing.called_by:
            existing.called_by.append(caller)

    system.database_objects = [*tables.values(), *procedures.values()]
    return system


def _attach_state_transitions(system: LegacySystem) -> LegacySystem:
    deduped: dict[tuple[str, str, str, str], StateTransition] = {}
    for transition in system.state_transitions:
        key = (
            transition.state_domain,
            transition.from_state,
            transition.to_state,
            transition.trigger or "",
        )
        deduped.setdefault(key, transition)
    system.state_transitions = sorted(
        deduped.values(), key=lambda t: (t.state_domain, t.to_state, t.trigger or "")
    )
    return system


def _mark_project_ownership(system: LegacySystem) -> LegacySystem:
    """Fold ``.vbp`` project declarations into the components that own them.

    A ``.vbp`` file only *declares* components (``Module=Validation``); the real
    code lives in ``Validation.bas``. The stub is dropped and the real component
    is tagged with the project that declares it, so the graph does not contain
    phantom duplicates.
    """
    real: dict[str, Component] = {}
    stubs: list[Component] = []
    for component in system.components:
        if component.file.lower().endswith(".vbp"):
            stubs.append(component)
            continue
        real.setdefault(component.name.lower(), component)

    kept: list[Component] = []
    for stub in stubs:
        owner = real.get(stub.name.lower())
        if owner is None:
            owner = real.get(stub.name.lower().removesuffix(".frm"))
        if owner is not None:
            owner.attributes["declared_in"] = stub.file
            owner.attributes["project_kind"] = stub.kind.value
            continue
        stub.attributes["orphan_project_declaration"] = "true"
        kept.append(stub)
    # Drop the matched stubs by identity: never by position, because the file
    # order is alphabetical and ``Project.vbp`` sorts *between* real modules.
    dropped = {id(s) for s in stubs} - {id(s) for s in kept}
    system.components = [c for c in system.components if id(c) not in dropped] + kept

    valid_components = {c.id for c in system.components}
    system.entry_points = [
        entry
        for entry in system.entry_points
        if entry.component_id in valid_components
        or entry.source is None
        or not entry.source.file.lower().endswith(".vbp")
    ]
    return system.reindex()


def _relative(path: Path, source_root: Path | None) -> str:
    if source_root is None:
        return path.name
    try:
        return str(path.relative_to(source_root))
    except ValueError:
        return path.name


def system_parser_diagnostics(system: LegacySystem) -> list[str]:
    diagnostics: list[str] = []
    for file in system.files:
        if file.parse_status in (ParseStatus.UNKNOWN, ParseStatus.FAILED):
            diagnostics.append(
                f"{file.path}: parse_status={file.parse_status.value} "
                f"via {file.parser} (dependencies not inferred)"
            )
        for message in file.diagnostics:
            diagnostics.append(f"{file.path}: {message}")
    return diagnostics


__all__ = ["EntryPointKind", "LegacyParser", "VB6Parser"]
