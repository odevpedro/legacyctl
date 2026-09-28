"""Phase 0 -> unified graph.

The graph is the pivot of the whole framework: everything after parsing is
expressed over *nodes and edges*, and every slice, cluster, rule, contract and
generated class references the same node IDs. Nothing here is language
specific -- the input is the intermediate model, so adding a COBOL adapter
produces the identical graph schema.

Node/edge types are the minimum required by the specification plus a few
explicitly documented additions (``HAS_PARAMETER``, ``HAS_RETURN``,
``DEPENDS_ON_TABLE``), because dropping a relation that the legacy code
expresses would make a slice lie about the system it came from.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ..domain.enums import CallKind, DataAccessMode, EdgeKind, NodeKind, ParseStatus
from ..domain.models import (
    BusinessFlow,
    GraphEdge,
    GraphNode,
    LegacySystem,
    SystemGraph,
)
from .persistence import write_graph_csv, write_graphml, write_sqlite

NODE_ATTRIBUTE_KEYS = (
    "id",
    "kind",
    "label",
    "file",
    "line",
    "cluster_id",
    "degree",
    "degree_centrality",
    "betweenness",
)


class LegacyGraphBuilder:
    """Builds the unified :class:`SystemGraph` from the intermediate model."""

    def build(self, system: LegacySystem) -> SystemGraph:
        nodes: dict[str, GraphNode] = {}
        edges: dict[str, GraphEdge] = {}

        def node(node_: GraphNode) -> GraphNode:
            return nodes.setdefault(node_.id, node_)

        def edge(
            kind: EdgeKind, source: str, target: str, via: str | None = None, **attrs: Any
        ) -> None:
            if source not in nodes or target not in nodes:
                # Never invent a relationship to a node we did not observe.
                return
            if kind is EdgeKind.CALLS:
                # One edge per (caller, callee) pair, as in the specification's
                # own graph sketch. A procedure calling another one four times
                # is *one* relationship with four call sites -- emitting four
                # parallel edges would make every downstream degree, centrality
                # and slice count wrong.
                self._merge_call_site(edges, source, target, via, attrs.pop("label", ""))
                return
            built = GraphEdge(
                id=GraphEdge.make_id(kind, source, target, via),
                kind=kind,
                source=source,
                target=target,
                label=attrs.pop("label", ""),
                via=via,
                **attrs,
            )
            edges.setdefault(built.id, built)

        self._add_files(system, node)
        self._add_components(system, node, edge)
        self._add_procedures(system, node, edge)
        self._add_database(system, node, edge)
        self._add_entry_points(system, node, edge)
        self._add_flows(system, node, edge)

        for item in nodes.values():
            item.degree = sum(1 for e in edges.values() if item.id in (e.source, e.target))
        return SystemGraph(
            system_id=system.id,
            nodes=sorted(nodes.values(), key=lambda n: (n.kind.value, n.id)),
            edges=sorted(edges.values(), key=lambda e: e.id),
        )

    @staticmethod
    def _merge_call_site(
        edges: dict[str, GraphEdge],
        source: str,
        target: str,
        via: str | None,
        label: str,
    ) -> None:
        existing = next(
            (
                e
                for e in edges.values()
                if e.kind is EdgeKind.CALLS and e.source == source and e.target == target
            ),
            None,
        )
        if existing is None or not via:
            edges.setdefault(
                GraphEdge.make_id(EdgeKind.CALLS, source, target, via),
                GraphEdge(
                    id=GraphEdge.make_id(EdgeKind.CALLS, source, target, via),
                    kind=EdgeKind.CALLS,
                    source=source,
                    target=target,
                    label=label,
                    via=via,
                ),
            )
            return
        sites = [s for s in (existing.via or "").split(",") if s]
        if via not in sites:
            sites.append(via)
        existing.via = ",".join(sites)
        existing.attributes = {
            **(existing.attributes or {}),
            "call_sites": str(len(sites)),
        }

    # -- node families ----------------------------------------------------

    def _add_files(self, system: LegacySystem, node: Any) -> None:
        for source in system.files:
            node(
                GraphNode(
                    id=GraphNode.make_id(NodeKind.FILE, source.path),
                    kind=NodeKind.FILE,
                    label=source.path,
                    file=source.path,
                    attributes={
                        "language": source.language,
                        "parser": source.parser,
                        "parse_status": source.parse_status.value,
                        "diagnostics": str(len(source.diagnostics)),
                    },
                )
            )

    def _add_components(self, system: LegacySystem, node: Any, edge: Any) -> None:
        file_nodes = {f.path: GraphNode.make_id(NodeKind.FILE, f.path) for f in system.files}
        for component in system.components:
            kind = {
                "MODULE": NodeKind.MODULE,
                "FORM": NodeKind.FORM,
                "CLASS": NodeKind.CLASS,
            }.get(component.kind.value, NodeKind.MODULE)
            component_node = GraphNode(
                id=component.id,
                kind=kind,
                label=component.name,
                file=component.file,
                attributes={
                    "component_id": component.id,
                    "parse_status": component.parse_status.value,
                    **component.attributes,
                },
            )
            node(component_node)
            file_node = file_nodes.get(component.file)
            if file_node:
                edge(EdgeKind.CONTAINS, file_node, component_node.id, label="declares")
            for dependency in component.dependencies:
                target = self._component_node_id(system, dependency)
                if target and target != component_node.id:
                    edge(EdgeKind.DEPENDS_ON, component_node.id, target, label="references")

    def _add_procedures(self, system: LegacySystem, node: Any, edge: Any) -> None:
        for procedure in system.procedures:
            component_node = self._component_node_id_by_id(system, procedure.component_id)
            procedure_node = GraphNode(
                id=procedure.id,
                kind=NodeKind.PROCEDURE,
                label=procedure.name,
                file=procedure.file,
                line=procedure.start_line,
                attributes={
                    "procedure_id": procedure.id,
                    "kind": procedure.kind.value,
                    "return_type": procedure.return_type or "",
                    "parameters": ",".join(p.name for p in procedure.parameters),
                    "out_parameters": ",".join(p.name for p in procedure.parameters if p.is_out),
                    "parse_status": procedure.parse_status.value,
                },
            )
            node(procedure_node)
            file_node = GraphNode.make_id(NodeKind.FILE, procedure.file)
            edge(EdgeKind.CONTAINS, file_node, procedure_node.id, label="defines")
            if component_node:
                edge(EdgeKind.CONTAINS, component_node, procedure_node.id, label="defines")
            for index, parameter in enumerate(procedure.parameters):
                variable_node = GraphNode(
                    id=GraphNode.make_id(NodeKind.VARIABLE, f"{procedure.id}::{parameter.name}"),
                    kind=NodeKind.VARIABLE,
                    label=parameter.name,
                    file=procedure.file,
                    line=procedure.start_line,
                    attributes={
                        "type_name": parameter.type_name or "",
                        "scope": parameter.scope,
                        "position": str(index),
                        "is_out": str(parameter.is_out).lower(),
                        "is_return": str(parameter.is_return).lower(),
                        "type_confidence": parameter.type_confidence.value,
                    },
                )
                node(variable_node)
                edge(
                    EdgeKind.HAS_PARAMETER,
                    procedure_node.id,
                    variable_node.id,
                    label=parameter.name,
                )

        for call in system.calls:
            if not call.callee_id:
                continue
            caller = self._procedure_node_id(system, call.caller_id)
            callee = self._procedure_node_id(system, call.callee_id)
            if caller and callee:
                edge(
                    EdgeKind.CALLS,
                    caller,
                    callee,
                    via=call.id,
                    label=call.callee_name,
                    confidence=(
                        ParseStatus.OK
                        if call.is_resolved and call.kind is not CallKind.DYNAMIC
                        else ParseStatus.PARTIAL
                        if call.is_resolved
                        else ParseStatus.UNKNOWN
                    ),
                )

    def _add_database(self, system: LegacySystem, node: Any, edge: Any) -> None:
        for obj in system.database_objects:
            kind = {
                "TABLE": NodeKind.DATABASE_TABLE,
                "VIEW": NodeKind.DATABASE_VIEW,
                "PROCEDURE": NodeKind.DATABASE_PROCEDURE,
            }.get(obj.kind.value, NodeKind.DATABASE_TABLE)
            attributes = {
                "database_object_kind": obj.kind.value,
                "db_schema": obj.db_schema or "",
                "parse_status": obj.parse_status.value,
            }
            if obj.called_by:
                attributes["called_by"] = ",".join(obj.called_by)
            out_params = [p.name for p in getattr(obj, "out_params", [])]
            if out_params:
                attributes["out_params"] = ",".join(out_params)
            references = list(getattr(obj, "sql_references", []))
            if references:
                attributes["sql_references"] = ",".join(references)
            node(
                GraphNode(
                    id=obj.id,
                    kind=kind,
                    label=obj.qualified_name,
                    file=obj.first_seen.file if obj.first_seen else None,
                    line=obj.first_seen.line if obj.first_seen else None,
                    attributes=attributes,
                )
            )

        for statement in system.sql_statements:
            sql_node = GraphNode(
                id=statement.id,
                kind=NodeKind.SQL_STATEMENT,
                label=(
                    f"{statement.operation.value} "
                    f"{', '.join(statement.tables) or statement.procedure_name or ''}"
                ).strip(),
                file=statement.source.file,
                line=statement.source.line,
                attributes={
                    "operation": statement.operation.value,
                    "normalized": statement.normalized or "",
                    "is_dynamic": str(statement.is_dynamic).lower(),
                    "parse_status": statement.parse_status.value,
                    "access_mode": statement.access_mode.value,
                    "procedure_id": statement.source.procedure or "",
                },
            )
            node(sql_node)
            procedure_node = self._procedure_node_id(system, statement.source.procedure)
            if procedure_node:
                edge(EdgeKind.DEPENDS_ON, procedure_node, sql_node.id, label="executes")

            for target_name, names in (
                (NodeKind.DATABASE_TABLE, statement.tables),
                (NodeKind.DATABASE_VIEW, statement.views),
            ):
                for name in names:
                    target = self._database_node_id(system, name, target_name)
                    if not target:
                        continue
                    edge(
                        _data_edge_kind(statement.access_mode),
                        sql_node.id,
                        target,
                        label=statement.operation.value,
                        confidence=statement.parse_status,
                    )
            if statement.procedure_name:
                target = self._database_node_id(
                    system, statement.procedure_name, NodeKind.DATABASE_PROCEDURE
                )
                if target:
                    edge(EdgeKind.INVOKES, sql_node.id, target, label="{call}")

        for dependency in system.dependencies:
            source = self._any_node_id(system, dependency.source_id)
            target = self._any_node_id(system, dependency.target_id)
            if source and target:
                edge(EdgeKind.DEPENDS_ON, source, target, label=dependency.kind)

    def _add_entry_points(self, system: LegacySystem, node: Any, edge: Any) -> None:
        for entry in system.entry_points:
            entry_node = GraphNode(
                id=entry.id,
                kind=NodeKind.ENTRY_POINT,
                label=entry.name,
                file=entry.source.file if entry.source else None,
                line=entry.source.line if entry.source else None,
                attributes={
                    "entry_point_kind": entry.kind.value,
                    "component_id": entry.component_id,
                },
            )
            node(entry_node)
            component_node = self._component_node_id_by_id(system, entry.component_id)
            if component_node:
                edge(EdgeKind.ENTRY_TO, entry_node.id, component_node, label=entry.kind.value)
            if entry.procedure_id:
                procedure_node = self._procedure_node_id(system, entry.procedure_id)
                if procedure_node:
                    edge(EdgeKind.ENTRY_TO, entry_node.id, procedure_node, label=entry.kind.value)

    def _add_flows(self, system: LegacySystem, node: Any, edge: Any) -> None:
        flows: Iterable[BusinessFlow] = getattr(system, "business_flows", [])
        for flow in flows:
            flow_node = GraphNode(
                id=flow.id,
                kind=NodeKind.FLOW,
                label=flow.name,
                attributes={
                    "entry_label": flow.entry_label,
                    "depth": str(flow.depth),
                    "truncated": str(flow.truncated).lower(),
                },
            )
            node(flow_node)
            for member in flow.node_ids:
                if member != flow.id:
                    edge(EdgeKind.DEPENDS_ON, flow.id, member, label="contains")

    # -- id helpers -------------------------------------------------------

    def _component_node_id(self, system: LegacySystem, name: str) -> str | None:
        for component in system.components:
            if component.name.lower() == name.lower():
                return self._component_node_id_by_id(system, component.id)
        return None

    def _component_node_id_by_id(self, system: LegacySystem, component_id_: str) -> str | None:
        # Canonical: the graph node *is* the component, so it carries the
        # component's own id. Minting a second id for the same entity silently
        # breaks every join between the parsed system and the graph.
        component = system.component(component_id_)
        return component.id if component else None

    def _procedure_node_id(self, system: LegacySystem, procedure_id_: str | None) -> str | None:
        if not procedure_id_:
            return None
        procedure = system.procedure(procedure_id_)
        if procedure is None:
            return None
        return procedure.id

    def _database_node_id(self, system: LegacySystem, name: str, kind: NodeKind) -> str | None:
        target = next((d for d in system.database_objects if d.name.upper() == name.upper()), None)
        if target is None:
            return None
        return target.id

    def _any_node_id(self, system: LegacySystem, reference: str) -> str | None:
        for node_id in (
            self._procedure_node_id(system, reference),
            self._component_node_id_by_id(system, reference),
            GraphNode.make_id(NodeKind.FILE, reference),
        ):
            if node_id:
                return node_id
        if any(d.id == reference for d in system.database_objects):
            return reference
        if any(s.id == reference for s in system.sql_statements):
            return reference
        return None


def _data_edge_kind(access: DataAccessMode) -> EdgeKind:
    if access is DataAccessMode.WRITE:
        return EdgeKind.WRITES
    if access is DataAccessMode.READ:
        return EdgeKind.READS
    return EdgeKind.QUERIES


def export(graph: SystemGraph, output_dir: Path, *, db_path: Path | None = None) -> dict[str, str]:
    """Write the graph to every interchange format the CLI promises."""
    output_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, str] = {}
    graphml = output_dir / "system.graphml"
    write_graphml(graph, graphml)
    written["graphml"] = str(graphml)
    nodes_csv = output_dir / "graph-nodes.csv"
    edges_csv = output_dir / "graph-edges.csv"
    write_graph_csv(graph, nodes_csv, edges_csv)
    written["nodes_csv"] = str(nodes_csv)
    written["edges_csv"] = str(edges_csv)
    if db_path is not None:
        write_sqlite(graph, db_path)
        written["sqlite"] = str(db_path)
    return written


__all__ = ["LegacyGraphBuilder", "export", "write_graph_csv", "write_graphml", "write_sqlite"]
