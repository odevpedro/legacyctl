"""Deterministic identifier minting.

Every traceable element of the pipeline carries a stable, human readable ID so
that the chain

    catalog -> contract -> generated code -> tests -> divergence reports

can always be walked backwards to the original source location.

Identifiers are derived from content, never from randomness or wall-clock time,
so re-running the pipeline over the same inputs yields the same IDs.
"""

from __future__ import annotations

import re
import unicodedata

from .enums import NodeKind

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")
_MAX_SLUG = 60


def slugify(value: str) -> str:
    """Return a stable upper-case slug suitable for an identifier segment."""
    normalized = unicodedata.normalize("NFKD", value)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii").lower()
    slug = _SLUG_STRIP.sub("_", ascii_only).strip("_").upper()
    if len(slug) > _MAX_SLUG:
        slug = slug[:_MAX_SLUG].rstrip("_")
    return slug or "UNNAMED"


def system_id(name: str) -> str:
    return f"SYS-{slugify(name)}"


def file_id(relative_path: str) -> str:
    return f"FILE-{slugify(relative_path)}"


def component_id(component_name: str) -> str:
    return f"CMP-{slugify(component_name)}"


def procedure_id(module: str, procedure: str) -> str:
    return f"PROC-{slugify(module)}-{slugify(procedure)}"


def table_id(table: str) -> str:
    return f"TABLE-{slugify(table)}"


def view_id(view: str) -> str:
    return f"VIEW-{slugify(view)}"


def database_procedure_id(name: str) -> str:
    return f"DBPROC-{slugify(name)}"


def sql_statement_id(source: str, line: int, digest: str) -> str:
    return f"SQL-{slugify(source)}-L{line}-{slugify(digest)}"


def entry_point_id(name: str) -> str:
    return f"ENTRY-{slugify(name)}"


def flow_id(name: str) -> str:
    return f"FLOW-{slugify(name)}"


def hub_id(name: str) -> str:
    return f"HUB-{slugify(name)}"


def rule_id(domain_key: str, ordinal: int) -> str:
    """Hub rules use the ``HUB-*`` namespace, flow rules the ``RULE-*`` one."""
    if domain_key.upper().startswith("HUB"):
        return f"{domain_key.upper()}-{ordinal:03d}"
    return f"RULE-{slugify(domain_key)}-{ordinal:03d}"


def edge_id(kind: NodeKind | str, source: str, target: str, via: str | None = None) -> str:
    """Edge IDs are content-derived, so re-runs produce identical graphs."""
    name = kind.value if isinstance(kind, NodeKind) else str(kind)
    tail = f"-{slugify(via)}" if via else ""
    return f"EDGE-{slugify(name)}-{slugify(source)}-{slugify(target)}{tail}"


def cluster_id(algorithm: str, index: int) -> str:
    return f"CLUSTER-{slugify(algorithm)}-{index:04d}"


def node_id(kind: NodeKind, key: str) -> str:
    """Graph node IDs are namespaced by node type to stay globally unique."""
    prefix = {
        NodeKind.FILE: "FILE",
        NodeKind.FORM: "FORM",
        NodeKind.MODULE: "MODULE",
        NodeKind.CLASS: "CLASS",
        NodeKind.PROCEDURE: "PROC",
        NodeKind.VARIABLE: "VAR",
        NodeKind.DATABASE_TABLE: "TABLE",
        NodeKind.DATABASE_VIEW: "VIEW",
        NodeKind.DATABASE_PROCEDURE: "DBPROC",
        NodeKind.SQL_STATEMENT: "SQL",
        NodeKind.ENTRY_POINT: "ENTRY",
        NodeKind.HUB: "HUB",
        NodeKind.FLOW: "FLOW",
        NodeKind.RULE: "RULE",
    }[kind]
    return f"{prefix}-{slugify(key)}"


def execution_id(system: str, stage: str, sequence: int) -> str:
    return f"EXEC-{slugify(system)}-{slugify(stage)}-{sequence:04d}"
