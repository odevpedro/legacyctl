"""Graph interchange formats.

Four formats, four audiences:

* ``GraphML`` -- Gephi/CyGraph visual exploration (section "Visualizacao").
* ``CSV`` -- diffable, reviewable, and readable by the reporting stage.
* ``SQLite`` -- the queryable store the CLI and later phases read from.
* ``SystemGraph`` JSON -- the canonical, versionable artefact.

Everything is written deterministically (sorted rows, stable float rounding) so
that two runs over the same sources produce byte-identical files.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from ..domain.models import SystemGraph

_GRAPHML_NS = "http://graphml.graphdrawing.org/xmlns"
_XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
_EDGES = ("calls", "reads", "writes", "queries", "contains", "depends_on", "entry_to")


def _round(value: float) -> float:
    return round(value, 6)


def _node_row(node: Any) -> dict[str, Any]:
    return {
        "id": node.id,
        "kind": node.kind.value,
        "label": node.label,
        "file": node.file or "",
        "line": node.line if node.line is not None else "",
        "cluster_id": node.cluster_id or "",
        "degree": node.degree,
        "degree_centrality": _round(node.degree_centrality),
        "betweenness": _round(node.betweenness),
    }


def _edge_row(edge: Any) -> dict[str, Any]:
    return {
        "id": edge.id,
        "kind": edge.kind.value,
        "source": edge.source,
        "target": edge.target,
        "label": edge.label,
        "via": edge.via or "",
        "confidence": edge.confidence.value,
    }


def write_graph_csv(graph: SystemGraph, nodes_path: Path, edges_path: Path) -> None:
    nodes_path.parent.mkdir(parents=True, exist_ok=True)
    node_fields = [
        "id",
        "kind",
        "label",
        "file",
        "line",
        "cluster_id",
        "degree",
        "degree_centrality",
        "betweenness",
    ]
    with nodes_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=node_fields)
        writer.writeheader()
        for node in graph.nodes:
            writer.writerow(_node_row(node))
    edge_fields = ["id", "kind", "source", "target", "label", "via", "confidence"]
    with edges_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=edge_fields)
        writer.writeheader()
        for edge in graph.edges:
            writer.writerow(_edge_row(edge))


def write_graph_json(graph: SystemGraph, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = graph.model_dump(mode="json")
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_graphml(graph: SystemGraph, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    root = ET.Element(
        "graphml",
        {"xmlns:_GRAPHML": _GRAPHML_NS, "xmlns:xsi": _XSI_NS},
    )
    root.set("id", "legacy-system-graph")
    for kind in ("node", "edge"):
        key = ET.SubElement(root, "key")
        key.set("id", f"{kind}_id")
        key.set("for", kind)
        key.set("attr.name", "id")
        key.set("attr.type", "string")
    for field in ("kind", "label", "file", "cluster_id"):
        key = ET.SubElement(root, "key")
        key.set("id", f"node_{field}")
        key.set("for", "node")
        key.set("attr.name", field)
        key.set("attr.type", "string")
    for field in ("kind", "label", "confidence"):
        key = ET.SubElement(root, "key")
        key.set("id", f"edge_{field}")
        key.set("for", "edge")
        key.set("attr.name", field)
        key.set("attr.type", "string")

    graph_element = ET.SubElement(root, "graph")
    graph_element.set("id", graph.system_id)
    graph_element.set("edgedefault", "directed")
    for node in graph.nodes:
        element = ET.SubElement(graph_element, "node", {"id": node.id})
        for field in ("kind", "label", "file", "cluster_id"):
            ET.SubElement(element, "data", {"key": f"node_{field}"}).text = (
                getattr(node, field) or ""
            )
    for edge in graph.edges:
        element = ET.SubElement(
            graph_element,
            "edge",
            {"id": edge.id, "source": edge.source, "target": edge.target},
        )
        for field in ("kind", "label", "confidence"):
            ET.SubElement(element, "data", {"key": f"edge_{field}"}).text = (
                getattr(edge, field) or ""
            )
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


def write_sqlite(graph: SystemGraph, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        cursor = connection.cursor()
        cursor.executescript(
            """
            DROP TABLE IF EXISTS nodes;
            DROP TABLE IF EXISTS edges;
            DROP TABLE IF EXISTS clusters;
            DROP TABLE IF EXISTS hubs;
            DROP TABLE IF EXISTS flows;
            DROP TABLE IF EXISTS meta;
            CREATE TABLE nodes (
                id TEXT PRIMARY KEY, kind TEXT, label TEXT, file TEXT, line INTEGER,
                cluster_id TEXT, degree INTEGER, degree_centrality REAL, betweenness REAL
            );
            CREATE TABLE edges (
                id TEXT PRIMARY KEY, kind TEXT, source TEXT, target TEXT,
                label TEXT, via TEXT, confidence TEXT
            );
            CREATE TABLE clusters (
                id TEXT PRIMARY KEY, algorithm TEXT, idx INTEGER, size INTEGER,
                share REAL, cohesion REAL, label TEXT, is_giant INTEGER, warning TEXT
            );
            CREATE TABLE hubs (
                id TEXT PRIMARY KEY, name TEXT, degree INTEGER,
                degree_centrality REAL, betweenness REAL, reason TEXT
            );
            CREATE TABLE flows (
                id TEXT PRIMARY KEY, name TEXT, entry_point_id TEXT, depth INTEGER,
                truncated INTEGER, procedure_ids TEXT, sql_ids TEXT
            );
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
            """
        )
        cursor.executemany(
            "INSERT OR REPLACE INTO nodes VALUES (:id,:kind,:label,:file,:line,:cluster_id,"
            ":degree,:degree_centrality,:betweenness)",
            [_node_row(n) for n in graph.nodes],
        )
        cursor.executemany(
            "INSERT OR REPLACE INTO edges "
            "VALUES (:id,:kind,:source,:target,:label,:via,:confidence)",
            [_edge_row(e) for e in graph.edges],
        )
        cursor.executemany(
            "INSERT OR REPLACE INTO clusters VALUES (:id,:algorithm,:idx,:size,:share,"
            ":cohesion,:label,:is_giant,:warning)",
            [
                {
                    "id": c.id,
                    "algorithm": c.algorithm,
                    "idx": c.index,
                    "size": c.size,
                    "share": _round(c.share_of_system),
                    "cohesion": _round(c.cohesion),
                    "label": c.label,
                    "is_giant": int(c.is_giant),
                    "warning": c.warning or "",
                }
                for c in graph.clusters
            ],
        )
        cursor.executemany(
            "INSERT OR REPLACE INTO hubs VALUES (:id,:name,:degree,:degree_centrality,"
            ":betweenness,:reason)",
            [
                {
                    "id": h.id,
                    "name": h.name,
                    "degree": h.degree,
                    "degree_centrality": _round(h.degree_centrality),
                    "betweenness": _round(h.betweenness),
                    "reason": h.reason,
                }
                for h in graph.hubs
            ],
        )
        cursor.executemany(
            "INSERT OR REPLACE INTO flows VALUES (:id,:name,:entry_point_id,:depth,"
            ":truncated,:procedure_ids,:sql_ids)",
            [
                {
                    "id": f.id,
                    "name": f.name,
                    "entry_point_id": f.entry_point_id,
                    "depth": f.depth,
                    "truncated": int(f.truncated),
                    "procedure_ids": ",".join(f.procedure_ids),
                    "sql_ids": ",".join(f.sql_ids),
                }
                for f in graph.flows
            ],
        )
        cursor.executemany(
            "INSERT OR REPLACE INTO meta VALUES (?,?)",
            sorted({"system_id": graph.system_id, "algorithm": graph.algorithm}.items()),
        )
        connection.commit()
    finally:
        connection.close()


def read_sqlite_nodes(path: Path) -> list[dict[str, Any]]:
    """Read nodes back from SQLite (used by the report and the tests)."""
    connection = sqlite3.connect(path)
    try:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute("SELECT * FROM nodes ORDER BY id")]
    finally:
        connection.close()


__all__ = [
    "read_sqlite_nodes",
    "write_graph_csv",
    "write_graph_json",
    "write_graphml",
    "write_sqlite",
]
