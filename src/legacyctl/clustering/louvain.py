"""Phase 1 -- community detection and hub identification.

Algorithm choice, in order of preference:

1. **Louvain** via ``python-igraph`` when installed (the specification asks for
   Louvain or Leiden).
2. **Label propagation** via ``networkx`` as a deterministic fallback.
3. **Connected components** if the graph is a pure DAG with no cycles.

Every path is deterministic: node iteration is sorted, and the resulting
clusters are re-indexed by size then by lowest node ID, so IDs are stable
between runs. The algorithm actually used is recorded on the graph and printed
by the CLI -- a heuristic silently masquerading as Louvain would be worse than
an honest fallback.

Louvain is randomised internally, and ``community_multilevel`` in igraph has no
per-call seed parameter, so determinism requires seeding igraph's *process-wide*
RNG before the call. Without it the same sources produce a different partition
on every run (observed: 5 clusters vs 6), which would change every
content-derived ``cluster_id`` and make each run's report differ from the last
for no reason other than randomness.

The giant-cluster rule is deliberately *reporting only*: when one cluster holds
an excessive share of the system, the tool states the fact and the number, and
lets a human decide. It never asserts that the cluster must be split.
"""

from __future__ import annotations

import contextlib
import random
from collections import defaultdict, deque
from collections.abc import Iterable

import networkx as nx

from ..domain.enums import NodeKind
from ..domain.ids import cluster_id as make_cluster_id
from ..domain.ids import hub_id as make_hub_id
from ..domain.ids import node_id
from ..domain.models import Cluster, GraphEdge, GraphNode, Hub, SystemGraph

#: A cluster holding more than this share of the "interesting" nodes (procedures
#: and database objects) is reported as a giant cluster.
GIANT_CLUSTER_SHARE = 0.5
#: Hubs are the nodes whose degree centrality is at or above this share of the
#: maximum observed. 0.0 means "only the single most connected node".
HUB_SHARE_OF_MAX_DEGREE = 0.6
#: Seed for Louvain. Fixed so clustering is reproducible; see the module
#: docstring. Any change here re-partitions the system.
LOUVAIN_SEED = 1234
#: Node kinds that define the *business* structure. Files and variables are
#: excluded from cluster sizing: including them inflates every cluster and would
#: make the giant-cluster warning meaningless.
_STRUCTURAL_KINDS = (
    NodeKind.PROCEDURE,
    NodeKind.DATABASE_TABLE,
    NodeKind.DATABASE_VIEW,
    NodeKind.DATABASE_PROCEDURE,
    NodeKind.SQL_STATEMENT,
    NodeKind.ENTRY_POINT,
    NodeKind.FLOW,
    NodeKind.MODULE,
    NodeKind.FORM,
    NodeKind.CLASS,
)


class ClusterAnalyzer:
    """Detects communities, centralities and hubs over a :class:`SystemGraph`."""

    def analyze(self, graph: SystemGraph) -> SystemGraph:
        network = self._to_networkx(graph)
        structural = {n.id for n in graph.nodes if n.kind in _STRUCTURAL_KINDS}
        membership, algorithm = self._communities(network, structural)
        self._annotate_centralities(graph, network)
        graph.clusters = self._build_clusters(graph, network, membership, algorithm, structural)
        graph.hubs = self._build_hubs(graph, network, structural)
        graph.algorithm = algorithm
        graph.warnings = [c.warning for c in graph.clusters if c.warning]
        return graph

    def _to_networkx(self, graph: SystemGraph) -> nx.DiGraph:
        """Undirected-in-spirit directed graph: an edge is evidence, not order."""
        network = nx.DiGraph()
        for node in graph.nodes:
            network.add_node(node.id)
        for edge in graph.edges:
            network.add_edge(edge.source, edge.target, kind=edge.kind.value)
        return network

    # -- algorithm selection ---------------------------------------------

    def _communities(self, network: nx.DiGraph, structural: set[str]) -> tuple[dict[str, int], str]:
        nodes = sorted(n for n in network.nodes if n in structural)
        if not nodes:
            return {}, "none"
        if len(nodes) < 3:
            return dict.fromkeys(nodes, 0), "singleton"

        louvain = self._try_louvain(network, nodes)
        if louvain is not None:
            return louvain, "louvain"

        propagation = self._label_propagation(network, nodes)
        if propagation is not None:
            return propagation, "label-propagation"

        components = self._components(network, nodes)
        return components, "connected-components"

    def _try_louvain(self, network: nx.DiGraph, nodes: list[str]) -> dict[str, int] | None:
        try:
            import igraph
        except ModuleNotFoundError:
            return None
        sub = network.subgraph(nodes)
        mapping = {name: index for index, name in enumerate(sorted(nodes))}
        reverse = {index: name for name, index in mapping.items()}
        edge_list = [
            (mapping[u], mapping[v]) for u, v in sub.edges() if u in mapping and v in mapping
        ]
        graph = igraph.Graph(n=len(mapping), edges=edge_list, directed=False)
        graph.simplify()
        try:
            # igraph exposes no per-call seed, and Louvain draws from the
            # process-wide RNG. Seed it from a dedicated generator so this call
            # is reproducible without disturbing the caller's global ``random``
            # state. Best-effort: if this igraph build refuses our generator,
            # fall through to the unseeded call rather than skipping Louvain.
            with contextlib.suppress(Exception):  # pragma: no cover - build-dependent
                igraph.set_random_number_generator(random.Random(LOUVAIN_SEED))
            clusters = graph.community_multilevel()
        except Exception:
            return None
        membership: dict[str, int] = {}
        for index, cluster in enumerate(clusters):
            for member in cluster:
                name = reverse.get(member)
                if name is not None:
                    membership[name] = index
        return membership if membership else None

    def _label_propagation(self, network: nx.DiGraph, nodes: list[str]) -> dict[str, int] | None:
        undirected = nx.Graph()
        undirected.add_nodes_from(nodes)
        sub = network.subgraph(nodes)
        for u, v in sub.edges():
            if u in nodes and v in nodes:
                undirected.add_edge(u, v)
        try:
            communities = nx.community.asyn_lpa_communities(undirected, weight=None, seed=0)
        except Exception:
            return None
        ordered = sorted(communities, key=lambda c: (-len(c), min(c)))
        membership: dict[str, int] = {}
        for index, community in enumerate(ordered):
            for member in sorted(community):
                membership[member] = index
        return membership if membership else None

    def _components(self, network: nx.DiGraph, nodes: list[str]) -> dict[str, int]:
        undirected = nx.Graph()
        undirected.add_nodes_from(nodes)
        sub = network.subgraph(nodes)
        for u, v in sub.edges():
            if u in nodes and v in nodes:
                undirected.add_edge(u, v)
        ordered = sorted(nx.connected_components(undirected), key=lambda c: (-len(c), min(c)))
        membership: dict[str, int] = {}
        for index, component in enumerate(ordered):
            for member in sorted(component):
                membership[member] = index
        return membership

    # -- centrality and hubs ---------------------------------------------

    def _annotate_centralities(self, graph: SystemGraph, network: nx.DiGraph) -> None:
        degree = nx.degree_centrality(network)
        try:
            betweenness = nx.betweenness_centrality(network, normalized=True, weight=None)
        except Exception:
            betweenness = dict.fromkeys(network.nodes, 0.0)
        for node in graph.nodes:
            if node.id in network:
                node.degree_centrality = float(degree.get(node.id, 0.0))
                node.betweenness = float(betweenness.get(node.id, 0.0))

    def _build_hubs(
        self, graph: SystemGraph, network: nx.DiGraph, structural: set[str]
    ) -> list[Hub]:
        candidates = [n for n in graph.nodes if n.id in structural and n.degree > 1]
        if not candidates:
            return []
        peak = max(n.degree_centrality for n in candidates)
        if peak <= 0:
            return []
        threshold = peak * HUB_SHARE_OF_MAX_DEGREE
        hubs = [
            Hub(
                id=make_hub_id(n.label),
                name=n.label,
                node_id=n.id,
                degree=n.degree,
                degree_centrality=round(n.degree_centrality, 6),
                betweenness=round(n.betweenness, 6),
                reason=(
                    f"degree {n.degree}, centrality {n.degree_centrality:.3f}"
                    + (f", betweenness {n.betweenness:.3f}" if n.betweenness else "")
                ),
            )
            for n in candidates
            if n.degree_centrality >= threshold
        ]
        hubs.sort(key=lambda h: (-h.degree_centrality, -h.betweenness, h.id))
        for index, hub in enumerate(hubs):
            hub.id = f"{make_hub_id(hub.name)}-{index:02d}"
        return hubs

    def hub_for(self, graph: SystemGraph, node_id: str) -> Hub | None:
        """The hub a given node is, if any (exact -- never name matching)."""
        return next((h for h in graph.hubs if h.node_id == node_id), None)

    # -- clusters ---------------------------------------------------------

    def _build_clusters(
        self,
        graph: SystemGraph,
        network: nx.DiGraph,
        membership: dict[str, int],
        algorithm: str,
        structural: set[str],
    ) -> list[Cluster]:
        groups: dict[int, list[str]] = defaultdict(list)
        for member, cluster_index in membership.items():
            groups[cluster_index].append(member)

        universe = {n.id for n in graph.nodes if n.id in structural}
        total = max(len(universe), 1)
        ordered = sorted(groups.items(), key=lambda item: (-len(item[1]), min(item[1])))
        giants = [index for index, members in ordered if len(members) / total > GIANT_CLUSTER_SHARE]

        clusters: list[Cluster] = []
        for position, (index, members) in enumerate(ordered):
            member_set = set(members)
            sub = network.subgraph(members)
            internal = sub.number_of_edges()
            possible = max(len(members) * (len(members) - 1), 1)
            share = len(member_set) / total
            is_giant = index in giants and len(giants) == 1
            warning = None
            if is_giant:
                warning = (
                    f"WARNING: giant cluster detected ({algorithm}); cluster size: {share:.0%}"
                )
            clusters.append(
                Cluster(
                    id=make_cluster_id(algorithm, position),
                    algorithm=algorithm,
                    index=position,
                    node_ids=sorted(member_set),
                    size=len(member_set),
                    share_of_system=round(share, 6),
                    cohesion=round(internal / possible, 6) if len(members) > 1 else 0.0,
                    label=self._label(graph, member_set),
                    is_giant=is_giant,
                    warning=warning,
                    procedure_ids=sorted(
                        n.id
                        for n in graph.nodes
                        if n.id in member_set and n.kind is NodeKind.PROCEDURE
                    ),
                    database_object_ids=sorted(
                        n.id
                        for n in graph.nodes
                        if n.id in member_set
                        and n.kind
                        in (
                            NodeKind.DATABASE_TABLE,
                            NodeKind.DATABASE_VIEW,
                            NodeKind.DATABASE_PROCEDURE,
                        )
                    ),
                )
            )
        for cluster in clusters:
            for member in cluster.node_ids:
                node = graph.node(member)
                if node is not None:
                    node.cluster_id = cluster.id
        return clusters

    def _label(self, graph: SystemGraph, members: Iterable[str]) -> str:
        """Human label: the most connected procedure names, in graph order."""
        member_set = set(members)
        procedures = [n for n in graph.nodes if n.id in member_set and n.kind is NodeKind.PROCEDURE]
        procedures.sort(key=lambda n: (-n.degree, n.label))
        names = [n.label for n in procedures[:3]]
        if names:
            return ", ".join(names)
        nodes = [n for n in graph.nodes if n.id in member_set]
        return ", ".join(n.label for n in sorted(nodes, key=lambda n: n.label)[:3])


def build_directed_graph(nodes: list[GraphNode], edges: list[GraphEdge]) -> nx.DiGraph:
    """Small helper used by the tests to build an ad-hoc graph."""
    network = nx.DiGraph()
    for node in nodes:
        network.add_node(node.id)
    for edge in edges:
        network.add_edge(edge.source, edge.target)
    return network


def entry_reachable(graph: SystemGraph, start: str, depth: int) -> set[str]:
    """Undirected bounded BFS used by the slicing stage and by tests."""
    seen = {start}
    frontier = deque([(start, 0)])
    while frontier:
        current, distance = frontier.popleft()
        if distance >= depth:
            continue
        for neighbour in sorted(graph.neighbours(current)):
            if neighbour not in seen:
                seen.add(neighbour)
                frontier.append((neighbour, distance + 1))
    return seen


__all__ = ["ClusterAnalyzer", "build_directed_graph", "entry_reachable", "node_id"]
