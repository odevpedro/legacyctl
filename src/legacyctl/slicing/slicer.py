"""Phase 2 -- flow slicing.

This is the structural heart of the framework's thesis: the LLM in phase 3 must
*not* see the whole system. It sees one bounded slice -- the subgraph reachable
from a single entry point, plus the source fragments of exactly those
procedures, plus their SQL, plus the state transitions they perform.

Rules implemented here, all deliberate:

* **Bounded, not exhaustive.** The search stops at ``--depth``. Reaching a node
  exactly at the boundary is a *result*, not a failure.
* **Follow evidence, not guesses.** Only edges that exist in the graph are
  traversed. A call the parser could not resolve produces no edge, so it cannot
  silently widen a slice; the parser recorded the gap instead.
* **Structural nodes are excluded from the traversal** (a variable, a file) --
  otherwise ``CONTAINS`` edges would drag in the whole module.
* **Hubs are reported, not traversed.** A transversal hub is listed in
  ``hub_references`` so the extraction stage can decide what to do with it,
  instead of the slice silently absorbing the hub's entire neighbourhood.
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from ..domain.enums import NodeKind
from ..domain.ids import flow_id as make_flow_id
from ..domain.metrics import estimate_tokens
from ..domain.models import (
    BusinessFlow,
    EntryPoint,
    GraphEdge,
    Hub,
    LegacySystem,
    Procedure,
    SourceFragment,
    SystemGraph,
)
from ..graph.persistence import write_graph_json

#: Edge kinds that carry business reachability. ``CONTAINS``/``HAS_PARAMETER``
#: describe *shape*, not *flow*, so following them would walk the whole file.
_TRAVERSED_EDGES = frozenset(
    {
        "CALLS",
        "READS",
        "WRITES",
        "QUERIES",
        "INVOKES",
        "DEPENDS_ON",
        "ENTRY_TO",
        "TRIGGERS",
        "OBSERVES",
    }
)
#: Node kinds that are part of the flow and therefore belong in the slice.
_INCLUDED_KINDS = frozenset(
    {
        NodeKind.PROCEDURE,
        NodeKind.DATABASE_TABLE,
        NodeKind.DATABASE_VIEW,
        NodeKind.DATABASE_PROCEDURE,
        NodeKind.SQL_STATEMENT,
        NodeKind.ENTRY_POINT,
        NodeKind.MODULE,
        NodeKind.FORM,
        NodeKind.CLASS,
        NodeKind.FLOW,
    }
)


class EntryNotFound(LookupError):
    """Raised when ``--entry`` matches no entry point and no known node."""


@dataclass
class SliceResult:
    """A bounded subgraph plus everything the LLM needs to reason about it."""

    flow: BusinessFlow
    graph: SystemGraph
    fragments: list[SourceFragment] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def context_token_estimate(self) -> int:
        return self.flow.context_token_estimate


class FlowSlicer:
    """Builds :class:`SliceResult` objects from a graph and the parsed system."""

    def __init__(self, system: LegacySystem, graph: SystemGraph) -> None:
        self._system = system
        self._graph = graph

    # -- entry resolution -------------------------------------------------

    def resolve_entry(self, entry: str) -> tuple[str, EntryPoint | None]:
        """Resolve ``--entry`` to a graph node ID.

        Accepts an entry-point ID, a form/menu/command/batch label, a procedure
        name, or a graph node ID. Resolution is explicit: an ambiguous match
        raises instead of picking one.
        """
        needle = entry.strip().lower()
        exact_entry = [e for e in self._system.entry_points if e.id.lower() == needle]
        if exact_entry:
            return exact_entry[0].id, exact_entry[0]

        by_label = [e for e in self._system.entry_points if e.name.lower() == needle]
        if len(by_label) == 1:
            return by_label[0].id, by_label[0]
        if len(by_label) > 1:
            raise EntryNotFound(
                f"entry {entry!r} matches {len(by_label)} entry points: "
                + ", ".join(sorted(e.id for e in by_label))
                + "; use the full ID"
            )

        # ``Component.Procedure`` / ``Component.Method`` qualification, the shape
        # used by the specification's own examples.
        if "." in needle:
            component_name, _, member = needle.rpartition(".")
            qualified = [
                candidate
                for candidate in self._system.procedures
                if candidate.name.lower() == member
                and (self._component_name(candidate.component_id) or "").lower() == component_name
            ]
            if len(qualified) == 1:
                return self._procedure_node(qualified[0]), None
            if len(qualified) > 1:
                raise EntryNotFound(
                    f"entry {entry!r} is ambiguous: "
                    + ", ".join(sorted(candidate.id for candidate in qualified))
                )

        candidates = [p for p in self._system.procedures if p.name.lower() == needle]
        if len(candidates) == 1:
            return self._procedure_node(candidates[0]), None
        if len(candidates) > 1:
            raise EntryNotFound(
                f"entry {entry!r} matches {len(candidates)} procedures: "
                + ", ".join(sorted(candidate.id for candidate in candidates))
                + "; use the full ID"
            )

        node = self._graph.node(needle)
        if node is not None:
            return node.id, None
        raise EntryNotFound(f"entry {entry!r} not found among entry points, procedures or nodes")

    # -- slicing ----------------------------------------------------------

    def slice(self, entry: str, depth: int) -> SliceResult:
        if depth < 1:
            raise ValueError("depth must be >= 1")
        start, entry_point = self.resolve_entry(entry)
        if self._graph.node(start) is None:
            raise EntryNotFound(f"entry {entry!r} resolved to unknown node {start!r}")

        warnings: list[str] = []
        reached: dict[str, int] = {start: 0}
        frontier: deque[tuple[str, int]] = deque([(start, 0)])
        boundary: set[str] = set()

        while frontier:
            current, distance = frontier.popleft()
            if distance >= depth:
                boundary.add(current)
                continue
            for edge in self._sorted_edges(current):
                if edge.kind.value not in _TRAVERSED_EDGES:
                    continue
                # Forward only. The graph is directed with the *semantic*
                # direction (entry -> handler -> callee -> table), so walking
                # edges backwards would pull in every sibling handler of a form.
                neighbour = edge.target
                node = self._graph.node(neighbour)
                if node is None or node.kind not in _INCLUDED_KINDS:
                    continue
                if neighbour in reached:
                    continue
                reached[neighbour] = distance + 1
                frontier.append((neighbour, distance + 1))

        truncated = bool(boundary)
        if truncated:
            warnings.append(
                f"depth {depth} reached: {len(boundary)} node(s) still had unexplored "
                "outgoing edges; the slice is a prefix, not the whole flow"
            )

        node_ids = sorted(reached)
        kept_edges = [e for e in self._graph.edges if e.source in reached and e.target in reached]
        hubs = self._hubs_in(set(reached))
        if hubs:
            warnings.append("hub(s) touched by this slice: " + ", ".join(h.name for h in hubs))

        fragments = self._fragments(set(node_ids))
        flow = BusinessFlow(
            id=make_flow_id(self._flow_name(entry_point, entry)),
            name=self._flow_name(entry_point, entry),
            entry_point_id=entry_point.id if entry_point else start,
            entry_label=entry_point.name if entry_point else self._node_label(start),
            depth=depth,
            node_ids=node_ids,
            edge_ids=sorted(e.id for e in kept_edges),
            procedure_ids=sorted(self._procedure_ids(set(node_ids))),
            sql_ids=sorted(n for n in node_ids if self._node_kind(n) is NodeKind.SQL_STATEMENT),
            database_object_ids=sorted(
                n
                for n in node_ids
                if self._node_kind(n)
                in (NodeKind.DATABASE_TABLE, NodeKind.DATABASE_VIEW, NodeKind.DATABASE_PROCEDURE)
            ),
            hub_references=[h.id for h in hubs],
            excluded_hub_ids=[h.id for h in self._graph.hubs if h.id not in {x.id for x in hubs}],
            truncated=truncated,
            source_fragment_ids=[f.id for f in fragments],
            context_token_estimate=self._token_estimate(
                flow_text=self._slice_text(fragments, kept_edges)
            ),
        )

        subgraph = SystemGraph(
            system_id=self._graph.system_id,
            nodes=[n for n in self._graph.nodes if n.id in reached],
            edges=kept_edges,
            clusters=[
                c for c in self._graph.clusters if c.node_ids and set(c.node_ids) <= set(reached)
            ],
            hubs=hubs,
            flows=[flow],
            algorithm=self._graph.algorithm,
            warnings=list(self._graph.warnings),
        )
        return SliceResult(flow=flow, graph=subgraph, fragments=fragments, warnings=warnings)

    # -- persistence ------------------------------------------------------

    def write(self, result: SliceResult, directory: Path, name: str | None = None) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        stem = name or result.flow.id.lower()
        payload = {
            "flow": result.flow.model_dump(mode="json"),
            "graph": result.graph.model_dump(mode="json"),
            "source_fragments": [f.model_dump(mode="json") for f in result.fragments],
            "warnings": result.warnings,
            "context_token_estimate": result.context_token_estimate,
        }
        path = directory / f"{stem}.json"
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        write_graph_json(result.graph, directory / f"{stem}.graph.json")
        return path

    # -- helpers ----------------------------------------------------------

    def _sorted_edges(self, node_id: str) -> list[GraphEdge]:
        edges = [e for e in self._graph.edges if e.source == node_id]
        return sorted(edges, key=lambda e: (e.kind.value, e.id))

    def _procedure_node(self, procedure: Procedure) -> str:
        """A procedure node carries the procedure's own id.

        The graph builder reuses the canonical domain id, so no id has to be
        re-minted here -- a second minting scheme is how a graph silently stops
        joining back to the parsed system.
        """
        return procedure.id

    def _component_name(self, component_id: str) -> str | None:
        component = self._system.component(component_id)
        return component.name if component else None

    def _node_label(self, node_id: str) -> str:
        node = self._graph.node(node_id)
        return node.label if node else node_id

    def _node_kind(self, node_id: str) -> NodeKind | None:
        node = self._graph.node(node_id)
        return node.kind if node else None

    def _procedure_ids(self, node_ids: set[str]) -> list[str]:
        found: list[str] = []
        for node_id in node_ids:
            node = self._graph.node(node_id)
            if node is None or node.kind is not NodeKind.PROCEDURE:
                continue
            procedure_id = node.attributes.get("procedure_id")
            if procedure_id:
                found.append(procedure_id)
        return found

    def _hubs_in(self, node_ids: set[str]) -> list[Hub]:
        """Hubs whose own node landed inside the slice (exact node matching)."""
        return [h for h in self._graph.hubs if h.node_id and h.node_id in node_ids]

    def _fragments(self, node_ids: set[str]) -> list[SourceFragment]:
        fragments: list[SourceFragment] = []
        for procedure in self._system.procedures:
            node_id = self._procedure_node(procedure)
            if node_id not in node_ids:
                continue
            if not procedure.body_text:
                continue
            fragments.append(
                SourceFragment(
                    id=f"FRAG-{procedure.component_id.removeprefix('CMP-')}-{procedure.name.upper()}",
                    file=procedure.file,
                    start_line=procedure.start_line,
                    end_line=procedure.end_line or procedure.start_line,
                    text=procedure.body_text,
                    procedure_id=procedure.id,
                    sanitized=False,
                )
            )
        return sorted(fragments, key=lambda f: f.id)

    def _slice_text(self, fragments: list[SourceFragment], edges: list[GraphEdge]) -> str:
        parts = [f.text for f in fragments]
        parts += [f"{e.source} -{e.kind.value}-> {e.target}" for e in edges]
        return "\n".join(parts)

    @staticmethod
    def _token_estimate(flow_text: str) -> int:
        return estimate_tokens(flow_text)

    def _flow_name(self, entry_point: EntryPoint | None, fallback: str) -> str:
        if entry_point is not None:
            return entry_point.name
        return self._node_label(fallback)


__all__ = ["EntryNotFound", "FlowSlicer", "SliceResult"]
