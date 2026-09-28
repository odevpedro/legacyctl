"""Language-agnostic intermediate model of a legacy system.

Design rules (see ``docs/domain-model.md``):

* No class in this module mentions a concrete language. ``VB6Procedure`` does
  not exist; there is only :class:`Procedure`.
* Every construct that could not be understood keeps ``parse_status=UNKNOWN``
  instead of being filled with a guess.
* Every element carries a deterministic ID and, when it came from source, a
  :class:`SourceLocation`.
"""

from __future__ import annotations

import hashlib
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .enums import (
    CallKind,
    ComponentKind,
    ConsistenciaState,
    DataAccessMode,
    DatabaseObjectKind,
    DivergenceStatus,
    EdgeKind,
    EntryPointKind,
    EvidenceKind,
    GrupoState,
    NodeKind,
    ParseStatus,
    ProcedureKind,
    RuleConfidence,
    RuleStatus,
    SqlOperation,
    VerifyStatus,
)
from .ids import (
    component_id,
    edge_id,
    entry_point_id,
    file_id,
    flow_id,
    hub_id,
    node_id,
    procedure_id,
    sql_statement_id,
    table_id,
    view_id,
)


class DomainModel(BaseModel):
    """Base for every model: immutable-ish, strict, no extra fields."""

    model_config = ConfigDict(extra="forbid", frozen=False)

    def fingerprint(self) -> str:
        """Stable content digest, used for reproducible AST caching."""
        payload = self.model_dump_json(exclude_none=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


class SourceLocation(DomainModel):
    """Where a fact was observed in the legacy sources."""

    file: str
    line: int = Field(ge=0)
    procedure: str | None = None
    statement: str | None = None

    def ref(self) -> str:
        base = f"{self.file}:{self.line}"
        return f"{base}#{self.procedure}" if self.procedure else base


class Variable(DomainModel):
    name: str
    type_name: str | None = None
    scope: str = "LOCAL"
    is_parameter: bool = False
    is_return: bool = False
    is_out: bool = False
    is_global: bool = False
    type_confidence: ParseStatus = ParseStatus.UNKNOWN
    source: SourceLocation | None = None

    @property
    def id(self) -> str:
        return f"VAR-{self.name.upper()}"


class Procedure(DomainModel):
    """A callable unit. The only procedure concept in the framework."""

    id: str
    name: str
    component_id: str
    kind: ProcedureKind = ProcedureKind.PROCEDURE
    file: str
    start_line: int = Field(ge=0)
    end_line: int | None = Field(default=None, ge=0)
    parameters: list[Variable] = Field(default_factory=list)
    return_type: str | None = None
    is_public: bool = True
    parse_status: ParseStatus = ParseStatus.OK
    body_text: str | None = None
    source: SourceLocation | None = None
    attributes: dict[str, str] = Field(default_factory=dict)

    @staticmethod
    def make_id(component: str, name: str) -> str:
        return procedure_id(component, name)

    @property
    def in_params(self) -> list[Variable]:
        return [p for p in self.parameters if not p.is_out and not p.is_return]

    @property
    def out_params(self) -> list[Variable]:
        return [p for p in self.parameters if p.is_out or p.is_return]


class Call(DomainModel):
    """A call edge from one procedure to another, or to an unresolved target."""

    id: str
    caller_id: str
    callee_name: str
    callee_id: str | None = None
    kind: CallKind = CallKind.STATIC
    line: int = Field(ge=0)
    call_expression: str | None = None
    arguments: list[str] = Field(default_factory=list)

    @property
    def is_resolved(self) -> bool:
        return self.callee_id is not None


class SqlStatement(DomainModel):
    """A SQL string observed in the sources plus its deterministic analysis."""

    id: str
    operation: SqlOperation = SqlOperation.UNKNOWN
    dialect: str | None = None
    raw: str
    normalized: str | None = None
    tables: list[str] = Field(default_factory=list)
    views: list[str] = Field(default_factory=list)
    columns: list[str] = Field(default_factory=list)
    parse_status: ParseStatus = ParseStatus.UNKNOWN
    is_dynamic: bool = False
    procedure_name: str | None = None
    #: Never named ``schema``: on a pydantic v2 model that attribute resolves to
    #: the deprecated ``BaseModel.schema()`` method instead of the value.
    db_schema: str | None = None
    out_params: list[str] = Field(default_factory=list)
    source: SourceLocation
    access_mode: DataAccessMode = DataAccessMode.UNKNOWN

    @property
    def qualified_procedure_name(self) -> str:
        if not self.procedure_name:
            return ""
        return f"{self.db_schema}.{self.procedure_name}" if self.db_schema else self.procedure_name

    @staticmethod
    def make_id(source: SourceLocation, raw: str) -> str:
        digest = hashlib.sha256(raw.encode()).hexdigest()[:8]
        return sql_statement_id(source.file, source.line, digest)

    @property
    def missing_evidence(self) -> bool:
        return self.parse_status in (ParseStatus.UNKNOWN, ParseStatus.FAILED)


class DatabaseObject(DomainModel):
    id: str
    name: str
    kind: DatabaseObjectKind
    db_schema: str | None = None
    in_params: list[str] = Field(default_factory=list)
    out_params: list[str] = Field(default_factory=list)
    parse_status: ParseStatus = ParseStatus.UNKNOWN
    called_by: list[str] = Field(default_factory=list)
    first_seen: SourceLocation | None = None

    @property
    def qualified_name(self) -> str:
        return f"{self.db_schema}.{self.name}" if self.db_schema else self.name


class DatabaseProcedure(DatabaseObject):
    """A stored procedure called via ``{call schema.proc(?, ?)}``.

    OUT params (``P_RESULTADO_NEGOCIO``, ``P_MENSAGEM``, ``P_PAYLOAD_JSON``)
    are part of the model: the legacy behaviour lives in the database, not only
    in the client code.
    """

    kind: DatabaseObjectKind = DatabaseObjectKind.PROCEDURE
    in_params: list[Variable] = Field(default_factory=list)  # type: ignore[assignment]
    out_params: list[Variable] = Field(default_factory=list)  # type: ignore[assignment]
    sql_references: list[str] = Field(default_factory=list)
    state_machine: list[StateTransition] = Field(default_factory=list)

    @property
    def signature(self) -> str:
        args = ", ".join(f"{p.name}: {p.type_name or 'UNKNOWN'}" for p in self.in_params)
        outs = ", ".join(f"OUT {p.name}: {p.type_name or 'UNKNOWN'}" for p in self.out_params)
        parts = [p for p in (args, outs) if p]
        return f"{self.qualified_name}({'; '.join(parts)})"


class StateTransition(DomainModel):
    """A knowledge-level state transition, persisted and formalised.

    ``state_domain`` is either ``GRUPO`` or ``CONSISTENCIA``; using strings
    instead of concrete enums keeps the domain open for other domains while the
    known domains remain enumerated.
    """

    state_domain: str
    from_state: str
    to_state: str
    trigger: str | None = None
    source: SourceLocation | None = None
    evidence_kind: EvidenceKind = EvidenceKind.OBSERVATION

    @property
    def is_known_grupo_transition(self) -> bool:
        if self.state_domain.upper() != "GRUPO":
            return False
        allowed = {s.value for s in GrupoState} | {s.value for s in ConsistenciaState}
        return self.from_state in allowed and self.to_state in allowed


class Component(DomainModel):
    """A structural unit: module, form, class, package, job..."""

    id: str
    name: str
    kind: ComponentKind
    file: str
    parse_status: ParseStatus = ParseStatus.OK
    procedure_ids: list[str] = Field(default_factory=list)
    variable_ids: list[str] = Field(default_factory=list)
    entry_point_ids: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    attributes: dict[str, str] = Field(default_factory=dict)
    attention_points: list[str] = Field(default_factory=list)

    @staticmethod
    def make_id(name: str) -> str:
        return component_id(name)


class SourceFile(DomainModel):
    id: str
    path: str
    language: str
    component_ids: list[str] = Field(default_factory=list)
    procedure_ids: list[str] = Field(default_factory=list)
    sha256: str = ""
    line_count: int = 0
    parse_status: ParseStatus = ParseStatus.OK
    parser: str = "unknown"
    parser_version: str | None = None
    diagnostics: list[str] = Field(default_factory=list)

    @staticmethod
    def make_id(relative_path: str) -> str:
        return file_id(relative_path)


class Dependency(DomainModel):
    """An explicit dependency between two graph nodes."""

    source_id: str
    target_id: str
    kind: str
    via: str | None = None
    parse_status: ParseStatus = ParseStatus.OK


class EntryPoint(DomainModel):
    id: str
    name: str
    kind: EntryPointKind
    component_id: str
    procedure_id: str | None = None
    target: str | None = None
    source: SourceLocation | None = None

    @staticmethod
    def make_id(name: str) -> str:
        return entry_point_id(name)


class Hub(DomainModel):
    """A cross-cutting component detected structurally, not by name."""

    id: str
    name: str
    #: Graph node this hub *is*. Exact matching only: a slice must not decide
    #: "is this node a hub?" by comparing display names.
    node_id: str | None = None
    component_id: str | None = None
    degree: int = 0
    degree_centrality: float = 0.0
    betweenness: float = 0.0
    flows_through: list[str] = Field(default_factory=list)
    hub_bypass_count: int = 0
    reason: str = "high degree centrality"

    @staticmethod
    def make_id(name: str) -> str:
        return hub_id(name)


class BusinessFlow(DomainModel):
    """A bounded slice of the graph rooted at an entry point."""

    id: str
    name: str
    entry_point_id: str
    entry_label: str
    depth: int = Field(ge=0)
    node_ids: list[str] = Field(default_factory=list)
    edge_ids: list[str] = Field(default_factory=list)
    procedure_ids: list[str] = Field(default_factory=list)
    sql_ids: list[str] = Field(default_factory=list)
    database_object_ids: list[str] = Field(default_factory=list)
    hub_references: list[str] = Field(default_factory=list)
    excluded_hub_ids: list[str] = Field(default_factory=list)
    truncated: bool = False
    source_fragment_ids: list[str] = Field(default_factory=list)
    context_token_estimate: int = 0

    @staticmethod
    def make_id(name: str) -> str:
        return flow_id(name)


class SourceFragment(DomainModel):
    id: str
    file: str
    start_line: int = Field(ge=0)
    end_line: int = Field(ge=0)
    text: str
    procedure_id: str | None = None
    sanitized: bool = False


class LegacySystem(DomainModel):
    """The root aggregate of a deterministic analysis run."""

    id: str
    name: str
    source_root: str
    language: str = "unknown"
    files: list[SourceFile] = Field(default_factory=list)
    components: list[Component] = Field(default_factory=list)
    procedures: list[Procedure] = Field(default_factory=list)
    calls: list[Call] = Field(default_factory=list)
    database_objects: list[DatabaseObject] = Field(default_factory=list)
    sql_statements: list[SqlStatement] = Field(default_factory=list)
    entry_points: list[EntryPoint] = Field(default_factory=list)
    dependencies: list[Dependency] = Field(default_factory=list)
    source_fragments: list[SourceFragment] = Field(default_factory=list)
    state_transitions: list[StateTransition] = Field(default_factory=list)
    adapter: str = "unknown"

    @model_validator(mode="after")
    def _reindex(self) -> Self:
        self.files = sorted(self.files, key=lambda f: f.path)
        self.components = sorted(self.components, key=lambda c: c.id)
        self.procedures = sorted(self.procedures, key=lambda p: p.id)
        self.calls = sorted(self.calls, key=lambda c: c.id)
        self.sql_statements = sorted(self.sql_statements, key=lambda s: s.id)
        self.database_objects = sorted(self.database_objects, key=lambda d: d.id)
        self.entry_points = sorted(self.entry_points, key=lambda e: e.id)
        return self

    def reindex(self) -> Self:
        """Re-apply the deterministic ordering after mutation.

        The pipeline mutates the system *after* construction (dropping ``.vbp``
        stubs, merging database objects, ...), and every stage downstream relies
        on a stable order so reports and graph exports are reproducible.
        """
        self.files = sorted(self.files, key=lambda f: f.path)
        self.components = sorted(self.components, key=lambda c: c.id)
        self.procedures = sorted(self.procedures, key=lambda p: p.id)
        self.calls = sorted(self.calls, key=lambda c: c.id)
        self.sql_statements = sorted(self.sql_statements, key=lambda s: s.id)
        self.database_objects = sorted(self.database_objects, key=lambda d: d.id)
        self.entry_points = sorted(self.entry_points, key=lambda e: e.id)
        return self

    def procedure(self, procedure_id_: str) -> Procedure | None:
        return next((p for p in self.procedures if p.id == procedure_id_), None)

    def procedure_by_name(self, name: str) -> Procedure | None:
        lowered = name.lower()
        return next((p for p in self.procedures if p.name.lower() == lowered), None)

    def component(self, component_id_: str) -> Component | None:
        return next((c for c in self.components if c.id == component_id_), None)

    def sql_for_procedure(self, procedure_id_: str) -> list[SqlStatement]:
        return [s for s in self.sql_statements if s.source.procedure == procedure_id_]


def make_table_object(name: str, first_seen: SourceLocation | None = None) -> DatabaseObject:
    return DatabaseObject(
        id=table_id(name), name=name, kind=DatabaseObjectKind.TABLE, first_seen=first_seen
    )


def make_view_object(name: str, first_seen: SourceLocation | None = None) -> DatabaseObject:
    return DatabaseObject(
        id=view_id(name), name=name, kind=DatabaseObjectKind.VIEW, first_seen=first_seen
    )


__all__ = [
    "BusinessFlow",
    "BusinessRule",
    "Call",
    "Cluster",
    "Component",
    "DatabaseObject",
    "DatabaseObjectKind",
    "DatabaseProcedure",
    "Dependency",
    "DivergenceRecord",
    "DivergenceReport",
    "DomainModel",
    "EntryPoint",
    "EntryPointKind",
    "GoldenMasterCase",
    "GraphEdge",
    "GraphNode",
    "Hub",
    "LegacySystem",
    "Procedure",
    "ProcedureKind",
    "RuleBehavior",
    "RuleCatalog",
    "RuleCondition",
    "RuleEvidence",
    "RuleSource",
    "SourceFile",
    "SourceFragment",
    "SourceLocation",
    "SqlOperation",
    "SqlStatement",
    "StateTransition",
    "SystemGraph",
    "Variable",
    "VerifyResult",
    "make_table_object",
    "make_view_object",
]


class RuleSource(DomainModel):
    """One provenance anchor of a rule: the exact place to re-read the code."""

    file: str
    procedure: str | None = None
    line: int = Field(default=0, ge=0)
    fragment_id: str | None = None
    sql_statement_id: str | None = None


class RuleEvidence(DomainModel):
    """Verbatim excerpt supporting a rule. Never paraphrased by the model."""

    type: str = "code"  # code | sql | graph | docs
    text: str
    source: RuleSource | None = None
    evidence_kind: EvidenceKind = EvidenceKind.OBSERVATION


class RuleCondition(DomainModel):
    """Machine-checkable condition shape. ``UNKNOWN`` beats a plausible guess."""

    type: str = "unknown"  # comparison | set_membership | existence | state | unknown
    expression: str | None = None
    fields: list[str] = Field(default_factory=list)


class RuleBehavior(DomainModel):
    """What the legacy system does when the condition holds."""

    type: str = "unknown"  # reject | approve | update | notify | route | unknown
    reason: str | None = None
    target: str | None = None


class BusinessRule(DomainModel):
    """A candidate (or human-validated) business rule.

    The LLM in phase 3 only ever produces ``status=CANDIDATE``; promotion to
    ``VALIDATED`` requires a human decision recorded in
    :attr:`reviewed_by` / :attr:`reviewed_at` (see ``legacyctl rule review``).
    """

    id: str
    name: str
    description: str = ""
    flow_id: str | None = None
    entry_point_id: str | None = None
    status: RuleStatus = RuleStatus.CANDIDATE
    confidence: RuleConfidence = RuleConfidence.MEDIUM
    condition: RuleCondition = Field(default_factory=RuleCondition)
    behavior: RuleBehavior = Field(default_factory=RuleBehavior)
    observation: list[str] = Field(default_factory=list)
    inference: list[str] = Field(default_factory=list)
    sources: list[RuleSource] = Field(default_factory=list)
    evidence: list[RuleEvidence] = Field(default_factory=list)
    state_transitions: list[str] = Field(default_factory=list)
    database_objects: list[str] = Field(default_factory=list)
    procedures: list[str] = Field(default_factory=list)
    review_note: str | None = None
    reviewed_by: str | None = None

    def touches(self, node_ids: set[str]) -> bool:
        """True when the rule references any of the given graph node IDs."""
        referenced = {
            *(s.procedure for s in self.sources if s.procedure),
            *(s.sql_statement_id for s in self.sources if s.sql_statement_id),
            *self.procedures,
            *self.database_objects,
            *self.state_transitions,
        }
        return bool(referenced & node_ids)

    def is_promotable(self) -> bool:
        """A rule may only feed the contract once a human validated it."""
        return self.status is RuleStatus.VALIDATED and bool(self.sources)


class RuleCatalog(DomainModel):
    """Versioned, diffable YAML catalog -- the reviewable artefact of phase 4."""

    version: int = Field(default=1, ge=1)
    system_id: str
    rules: list[BusinessRule] = Field(default_factory=list)
    notes: str | None = None

    def rule(self, rule_id_: str) -> BusinessRule | None:
        return next((r for r in self.rules if r.id == rule_id_), None)

    def by_status(self, status: RuleStatus) -> list[BusinessRule]:
        return [r for r in self.rules if r.status is status]

    @property
    def validated(self) -> list[BusinessRule]:
        return self.by_status(RuleStatus.VALIDATED)


class Cluster(DomainModel):
    """A detected community of the legacy system."""

    id: str
    algorithm: str = "louvain"
    index: int = Field(default=0, ge=0)
    node_ids: list[str] = Field(default_factory=list)
    size: int = Field(default=0, ge=0)
    share_of_system: float = 0.0
    cohesion: float = 0.0
    label: str = ""
    is_giant: bool = False
    warning: str | None = None
    procedure_ids: list[str] = Field(default_factory=list)
    database_object_ids: list[str] = Field(default_factory=list)


class SystemGraph(DomainModel):
    """The unified graph: language-agnostic nodes and edges with provenance."""

    system_id: str
    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)
    clusters: list[Cluster] = Field(default_factory=list)
    hubs: list[Hub] = Field(default_factory=list)
    flows: list[BusinessFlow] = Field(default_factory=list)
    algorithm: str = "louvain"
    warnings: list[str] = Field(default_factory=list)

    def node(self, node_id_: str) -> GraphNode | None:
        return next((n for n in self.nodes if n.id == node_id_), None)

    def neighbours(self, node_id_: str) -> set[str]:
        out: set[str] = set()
        for edge in self.edges:
            if edge.source == node_id_:
                out.add(edge.target)
            elif edge.target == node_id_:
                out.add(edge.source)
        return out - {node_id_}

    def degree(self, node_id_: str) -> int:
        return len(self.neighbours(node_id_))


class GraphNode(DomainModel):
    """One node of the unified graph, namespaced by :class:`NodeKind`."""

    id: str
    kind: NodeKind
    label: str
    file: str | None = None
    line: int | None = Field(default=None, ge=0)
    cluster_id: str | None = None
    degree: int = Field(default=0, ge=0)
    degree_centrality: float = 0.0
    betweenness: float = 0.0
    attributes: dict[str, str] = Field(default_factory=dict)

    @staticmethod
    def make_id(kind: NodeKind, key: str) -> str:
        return node_id(kind, key)


class GraphEdge(DomainModel):
    """One edge of the unified graph, always with a provenance anchor."""

    id: str
    kind: EdgeKind
    source: str
    target: str
    label: str = ""
    confidence: ParseStatus = ParseStatus.OK
    via: str | None = None
    attributes: dict[str, str] = Field(default_factory=dict)

    @staticmethod
    def make_id(kind: EdgeKind, source: str, target: str, via: str | None = None) -> str:
        return edge_id(kind, source, target, via)


class GoldenMasterCase(DomainModel):
    """A curated characterization case: inputs plus the expected legacy output.

    The fields follow the specification's Golden Master shape, including the
    procedure's OUT parameters and the observed state trace, because the
    traceability chain ``rules -> contracts -> tests -> divergences`` covers
    state transitions and not only boolean rules.

    ``evidence`` is mandatory in practice: a case that cannot name where its
    expectation came from (a database trace, a manual observation, an actual
    legacy run) is a guess, and a guess is refused by the loader unless it is
    explicitly marked ``confidence: low``.
    """

    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(alias="case_id")
    flow_id: str | None = None
    entry_point: str = ""
    procedure: str | None = None
    description: str = ""
    inputs: dict[str, str] = Field(default_factory=dict, alias="in_params")
    expected: dict[str, str] = Field(default_factory=dict, alias="out_params")
    legacy_output: dict[str, str] = Field(default_factory=dict)
    state_trace: list[str] = Field(default_factory=list)
    captured_from: str | None = None
    evidence: str | None = None
    confidence: RuleConfidence = RuleConfidence.HIGH
    tags: list[str] = Field(default_factory=list)


class VerifyResult(DomainModel):
    """Outcome of comparing the generated system against the Golden Master."""

    case_id: str
    flow_id: str | None = None
    status: VerifyStatus = VerifyStatus.UNKNOWN
    matches: list[str] = Field(default_factory=list)
    divergences: list[str] = Field(default_factory=list)
    legacy_output: dict[str, str] = Field(default_factory=dict)
    new_output: dict[str, str] = Field(default_factory=dict)
    detail: str | None = None


class DivergenceRecord(DomainModel):
    """One legacy-vs-new difference, tied to the rule that should have held."""

    id: str
    flow_id: str | None = None
    rule_id: str | None = None
    status: DivergenceStatus = DivergenceStatus.REQUIRES_REVIEW
    summary: str
    legacy: str | None = None
    new_system: str | None = None
    source: str | None = None
    detected_by: str = "golden-master"
    #: Whether this difference may fail a run. A divergence drawn from a
    #: low-confidence case is still reported, but a guess must not block.
    blocking: bool = True


class DivergenceReport(DomainModel):
    """Human-readable account of what the new system does differently."""

    system_id: str
    generated_at: str | None = None
    records: list[DivergenceRecord] = Field(default_factory=list)
    by_status: dict[str, int] = Field(default_factory=dict)
    blocking: int = Field(default=0, ge=0)
    notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _summarise(self) -> Self:
        self.by_status = {}
        for record in self.records:
            self.by_status[record.status.value] = self.by_status.get(record.status.value, 0) + 1
        self.blocking = sum(
            1
            for r in self.records
            if r.blocking
            and r.status in (DivergenceStatus.REQUIRES_REVIEW, DivergenceStatus.MISSING_IN_NEW)
        )
        return self
