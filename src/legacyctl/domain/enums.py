"""Language-agnostic enumerations for the legacyctl domain.

Nothing in this module may reference a concrete source language (VB6, COBOL,
...). Language specific concepts belong to the adapters.
"""

from __future__ import annotations

from enum import StrEnum


class ParseStatus(StrEnum):
    """Outcome of a deterministic parse of a source construct.

    ``UNKNOWN`` is a first-class, valid outcome: absence of evidence is not
    evidence of behaviour (ADR: semantic safety). Never replace it with a
    guess.
    """

    OK = "OK"
    PARTIAL = "PARTIAL"
    UNKNOWN = "UNKNOWN"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class ComponentKind(StrEnum):
    """Structural unit of a legacy system, independent of any language."""

    MODULE = "MODULE"
    FORM = "FORM"
    CLASS = "CLASS"
    PACKAGE = "PACKAGE"
    WORKSPACE = "WORKSPACE"
    JOB = "JOB"
    UNKNOWN = "UNKNOWN"


class ProcedureKind(StrEnum):
    """Callable unit kind."""

    PROCEDURE = "PROCEDURE"
    FUNCTION = "FUNCTION"
    METHOD = "METHOD"
    HANDLER = "HANDLER"
    TRIGGER = "TRIGGER"
    UNKNOWN = "UNKNOWN"


class DatabaseObjectKind(StrEnum):
    TABLE = "TABLE"
    VIEW = "VIEW"
    PROCEDURE = "PROCEDURE"
    FUNCTION = "FUNCTION"
    TRIGGER = "TRIGGER"
    SEQUENCE = "SEQUENCE"
    VIEW_SYNONYM = "SYNONYM"
    UNKNOWN = "UNKNOWN"


class SqlOperation(StrEnum):
    SELECT = "SELECT"
    INSERT = "INSERT"
    UPDATE = "UPDATE"
    DELETE = "DELETE"
    MERGE = "MERGE"
    CREATE = "CREATE"
    ALTER = "ALTER"
    DROP = "DROP"
    CALL = "CALL"
    TRUNCATE = "TRUNCATE"
    UNKNOWN = "UNKNOWN"


class DataAccessMode(StrEnum):
    """How a SQL statement touches a database object."""

    READ = "READ"
    WRITE = "WRITE"
    UNKNOWN = "UNKNOWN"


class NodeKind(StrEnum):
    """Node types of the unified graph."""

    FILE = "FILE"
    FORM = "FORM"
    MODULE = "MODULE"
    CLASS = "CLASS"
    PROCEDURE = "PROCEDURE"
    VARIABLE = "VARIABLE"
    DATABASE_TABLE = "DATABASE_TABLE"
    DATABASE_VIEW = "DATABASE_VIEW"
    DATABASE_PROCEDURE = "DATABASE_PROCEDURE"
    SQL_STATEMENT = "SQL_STATEMENT"
    ENTRY_POINT = "ENTRY_POINT"
    HUB = "HUB"
    FLOW = "FLOW"
    RULE = "RULE"


class EdgeKind(StrEnum):
    """Edge types of the unified graph."""

    CONTAINS = "CONTAINS"
    CALLS = "CALLS"
    READS = "READS"
    WRITES = "WRITES"
    QUERIES = "QUERIES"
    DEPENDS_ON = "DEPENDS_ON"
    TRIGGERS = "TRIGGERS"
    ENTRY_TO = "ENTRY_TO"
    OBSERVES = "OBSERVES"
    DEFINES = "DEFINES"
    REFERENCES = "REFERENCES"
    BELONGS_TO = "BELONGS_TO"
    INVOKES = "INVOKES"
    # Added by the graph builder: a procedure's IN/OUT parameters are part of
    # the contract, so a slice must carry them.
    HAS_PARAMETER = "HAS_PARAMETER"


class CallKind(StrEnum):
    """Call resolution quality."""

    STATIC = "STATIC"
    DYNAMIC = "DYNAMIC"
    UNRESOLVED = "UNRESOLVED"
    EXTERNAL = "EXTERNAL"


class EntryPointKind(StrEnum):
    FORM = "FORM"
    MENU = "MENU"
    PROCEDURE = "PROCEDURE"
    ENDPOINT = "ENDPOINT"
    COMMAND = "COMMAND"
    BATCH = "BATCH"
    EVENT_HANDLER = "EVENT_HANDLER"
    UNKNOWN = "UNKNOWN"


class RuleStatus(StrEnum):
    """Lifecycle of a business rule inside the catalog."""

    CANDIDATE = "candidate"
    REVIEW = "review"
    VALIDATED = "validated"
    REJECTED = "rejected"


class RuleConfidence(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class EvidenceKind(StrEnum):
    """Separates what was *seen* from what was *interpreted*."""

    OBSERVATION = "observation"
    INFERENCE = "inference"
    UNKNOWN = "unknown"


class GrupoState(StrEnum):
    """Transitional state of a consistency group (see ADR 010).

    The state machine is *knowledge*, not only rules: it is persisted in the
    domain model and formalised in the API contract.
    """

    CRIADO = "CRIADO"
    VALIDANDO = "VALIDANDO"
    EXECUTANDO = "EXECUTANDO"
    FINALIZADO_SUCESSO = "FINALIZADO_SUCESSO"
    FINALIZADO_PARCIAL = "FINALIZADO_PARCIAL"
    INCONCLUSIVO = "INCONCLUSIVO"


class ConsistenciaState(StrEnum):
    """State of a single consistency execution inside a group."""

    EXECUTANDO = "EXECUTANDO"
    FALHA_AUTORIZACAO = "FALHA_AUTORIZACAO"
    TIMEOUT = "TIMEOUT"
    FALHA_EXECUCAO = "FALHA_EXECUCAO"
    SUCESSO = "SUCESSO"


class DivergenceStatus(StrEnum):
    MATCH = "MATCH"
    REQUIRES_REVIEW = "REQUIRES_REVIEW"
    MISSING_IN_NEW = "MISSING_IN_NEW"
    UNKNOWN = "UNKNOWN"


class VerifyStatus(StrEnum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    UNKNOWN = "UNKNOWN"
