"""Graph construction, clustering and slicing over the shipped fixtures.

Assertions are about *semantic* facts (does the flow reach the table it writes?)
rather than exact counts, so a legitimate parser improvement does not break the
suite -- but a lost edge or a lost call does.
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from legacyctl.clustering import ClusterAnalyzer
from legacyctl.domain.enums import EdgeKind, NodeKind
from legacyctl.domain.models import LegacySystem, SystemGraph
from legacyctl.graph import LegacyGraphBuilder
from legacyctl.slicing import FlowSlicer, SliceResult


def _node_by_label(graph: SystemGraph, label: str) -> list[str]:  # type: ignore[type-arg]
    return [n.id for n in graph.nodes if n.label.lower() == label.lower()]


@pytest.fixture(scope="module")
def system(vb6_root: Path) -> LegacySystem:
    from legacyctl.parsers.vb6 import VB6Parser

    return VB6Parser().parse(vb6_root)


@pytest.fixture(scope="module")
def graph(system: LegacySystem) -> SystemGraph:
    return ClusterAnalyzer().analyze(LegacyGraphBuilder().build(system))


def test_every_procedure_has_a_node(system: LegacySystem, graph: SystemGraph) -> None:
    known = {n.id for n in graph.nodes}
    missing = [p.id for p in system.procedures if p.id not in known]
    assert missing == []


def test_every_call_edge_points_at_a_real_node(graph: SystemGraph) -> None:
    known = {n.id for n in graph.nodes}
    dangling = [e.id for e in graph.edges if e.source not in known or e.target not in known]
    assert dangling == []


def test_edge_ids_are_unique(graph: SystemGraph) -> None:
    ids = [e.id for e in graph.edges]
    assert len(ids) == len(set(ids))


def test_node_ids_are_unique(graph: SystemGraph) -> None:
    ids = [n.id for n in graph.nodes]
    assert len(ids) == len(set(ids))


def test_entry_points_reach_their_procedure(system: LegacySystem, graph: SystemGraph) -> None:
    by_target = {e.target for e in graph.edges if e.kind is EdgeKind.ENTRY_TO}
    validate = next(p for p in system.procedures if p.name.lower() == "validatecustomer")
    assert validate.id in by_target


def test_database_objects_are_nodes_with_read_and_write_edges(graph: SystemGraph) -> None:
    tables = [n for n in graph.nodes if n.kind is NodeKind.DATABASE_TABLE]
    assert tables, "the fixture writes tables, so they must be graph nodes"
    assert any(e.kind is EdgeKind.WRITES for e in graph.edges)
    assert any(e.kind is EdgeKind.READS for e in graph.edges)


def test_sql_statements_are_nodes(graph: SystemGraph) -> None:
    assert any(n.kind is NodeKind.SQL_STATEMENT for n in graph.nodes)


def test_clustering_covers_every_structural_node(graph: SystemGraph) -> None:
    clustered = {n for c in graph.clusters for n in c.node_ids}
    structural = {
        n.id for n in graph.nodes if n.kind in (NodeKind.PROCEDURE, NodeKind.SQL_STATEMENT)
    }
    assert structural <= clustered


def test_algorithm_is_reported(graph: SystemGraph) -> None:
    assert graph.algorithm in ("louvain", "label-propagation", "connected-components")
    assert all(c.algorithm == graph.algorithm for c in graph.clusters)


def test_clustering_is_reproducible(system: LegacySystem) -> None:
    """Identical sources must yield an identical partition, every time.

    Louvain is randomised and ``community_multilevel`` takes no per-call seed,
    so an unseeded run silently re-partitions the system (observed on the VB6
    fixture: 5 clusters one run, 6 the next). That would change every
    content-derived ``cluster_id`` and make two runs of an unchanged system
    produce different reports.
    """
    builder = LegacyGraphBuilder()
    partitions = []
    for _ in range(5):
        analyzed = ClusterAnalyzer().analyze(builder.build(system))
        partitions.append(
            (analyzed.algorithm, tuple(sorted((c.id, c.size) for c in analyzed.clusters)))
        )
    assert len(set(partitions)) == 1, f"clustering is not reproducible: {partitions}"


def test_cluster_ids_survive_a_rebuild(system: LegacySystem) -> None:
    """Two separate builds of the same system agree on cluster identity."""
    first = ClusterAnalyzer().analyze(LegacyGraphBuilder().build(system))
    second = ClusterAnalyzer().analyze(LegacyGraphBuilder().build(system))
    assert [c.id for c in first.clusters] == [c.id for c in second.clusters]
    assert [c.node_ids for c in first.clusters] == [c.node_ids for c in second.clusters]


def test_louvain_seeds_igraphs_global_rng(
    system: LegacySystem, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The randomisation is pinned, not left to chance.

    The repeats in the test above are a smoke check: unseeded Louvain usually
    agrees with itself, so counting partitions cannot reliably catch a
    regression. This asserts the mechanism instead, which is what actually
    makes the runs comparable.
    """
    igraph = pytest.importorskip("igraph")
    from legacyctl.clustering import louvain as louvain_mod

    seen: list[random.Random] = []
    monkeypatch.setattr(igraph, "set_random_number_generator", lambda gen: seen.append(gen))
    graph = ClusterAnalyzer().analyze(LegacyGraphBuilder().build(system))
    assert graph.algorithm == "louvain", f"expected Louvain, got {graph.algorithm}"
    # The regression guard: deleting the seeding call leaves ``seen`` empty.
    assert seen, "igraph RNG was never seeded"
    assert all(isinstance(g, random.Random) for g in seen)
    # And the seed is a fixed one, so the draw sequence Louvain sees is stable.
    assert louvain_mod.LOUVAIN_SEED == 1234
    a, b = random.Random(louvain_mod.LOUVAIN_SEED), random.Random(louvain_mod.LOUVAIN_SEED)
    assert [a.random() for _ in range(5)] == [b.random() for _ in range(5)]


def test_hub_refers_to_a_real_node(graph: SystemGraph) -> None:
    known = {n.id for n in graph.nodes}
    for hub in graph.hubs:
        assert hub.node_id in known, hub.id


def test_giant_cluster_warning_is_emitted(system: LegacySystem) -> None:
    """A synthetic all-to-all system must be flagged, not silently accepted.

    The fixture graph is small enough not to trip the threshold, so the
    threshold is exercised with a deliberately degenerate input.
    """
    from legacyctl.domain.models import GraphEdge, GraphNode

    small = LegacyGraphBuilder().build(system)
    assert not any("giant" in w.lower() for w in ClusterAnalyzer().analyze(small).warnings)

    count = 12
    nodes = [
        GraphNode(id=f"PROC-X-P{i}", kind=NodeKind.PROCEDURE, label=f"P{i}") for i in range(count)
    ]
    edges = [
        GraphEdge(
            id=GraphEdge.make_id(EdgeKind.CALLS, f"PROC-X-P{i}", f"PROC-X-P{j}"),
            kind=EdgeKind.CALLS,
            source=f"PROC-X-P{i}",
            target=f"PROC-X-P{j}",
        )
        for i in range(count)
        for j in range(count)
        if i != j
    ]
    degenerate = SystemGraph(system_id=system.id, nodes=nodes, edges=edges)
    analyzed = ClusterAnalyzer().analyze(degenerate)
    assert any("giant" in w.lower() for w in analyzed.warnings), analyzed.warnings


def test_slice_reaches_the_database_object_it_writes(
    system: LegacySystem, graph: SystemGraph
) -> None:
    result: SliceResult = FlowSlicer(system, graph).slice("CustomerForm.ValidateCustomer", 5)
    assert result.flow.procedure_ids, "a flow slice with no procedures is useless"
    assert result.flow.sql_ids or result.flow.database_object_ids
    assert result.flow.context_token_estimate > 0
    assert result.fragments
    assert all(f.text for f in result.fragments)


def test_slice_is_forward_only_and_bounded(system: LegacySystem, graph: SystemGraph) -> None:
    slicer = FlowSlicer(system, graph)
    shallow = slicer.slice("CustomerForm.ValidateCustomer", 1)
    deep = slicer.slice("CustomerForm.ValidateCustomer", 5)
    assert len(shallow.flow.node_ids) < len(deep.flow.node_ids)
    assert set(shallow.flow.node_ids) <= set(deep.flow.node_ids)


def test_slice_reports_truncation_instead_of_pretending_completeness(
    system: LegacySystem, graph: SystemGraph
) -> None:
    shallow = FlowSlicer(system, graph).slice("CustomerForm.ValidateCustomer", 1)
    assert shallow.flow.truncated
    assert any("prefix" in w for w in shallow.warnings)


def test_fully_closed_slice_is_not_marked_truncated(
    system: LegacySystem, graph: SystemGraph
) -> None:
    """Once the closure is reached, extra depth must not invent a warning."""
    deep = FlowSlicer(system, graph).slice("CustomerForm.ValidateCustomer", 6)
    assert not deep.flow.truncated
    assert not any("prefix" in w for w in deep.warnings)


def test_slice_warns_about_hubs_it_touches(system: LegacySystem, graph: SystemGraph) -> None:
    result = FlowSlicer(system, graph).slice("CustomerForm.cmdSave_Click", 6)
    if result.flow.hub_references:
        assert any("hub" in w.lower() for w in result.warnings)


def test_slice_write_emits_json_and_graph(
    system: LegacySystem, graph: SystemGraph, tmp_path: Path
) -> None:
    result = FlowSlicer(system, graph).slice("CustomerForm.ValidateCustomer", 5)
    written = Path(FlowSlicer(system, graph).write(result, tmp_path))
    assert written.is_file() and written.stat().st_size > 0
    assert (written.parent / f"{written.stem}.graph.json").is_file()


def test_unknown_entry_raises_instead_of_guessing(system: LegacySystem, graph: SystemGraph) -> None:
    from legacyctl.slicing import EntryNotFound

    with pytest.raises(EntryNotFound):
        FlowSlicer(system, graph).slice("NoSuchEntryPoint", 3)


def test_ambiguous_entry_raises_instead_of_picking_one(tmp_path: Path) -> None:
    """A bare name that exists in more than one component must not be guessed.

    Picking the first match would make the resulting contract depend on
    directory iteration order, so resolution has to fail loudly.
    """
    from legacyctl.parsers.vb6 import VB6Parser
    from legacyctl.slicing import EntryNotFound

    for name in ("first", "second"):
        (tmp_path / f"{name}.bas").write_text("Public Sub Shared()\nEnd Sub\n", encoding="utf-8")
    system = VB6Parser().parse(tmp_path)
    analyzed = ClusterAnalyzer().analyze(LegacyGraphBuilder().build(system))
    with pytest.raises(EntryNotFound):
        FlowSlicer(system, analyzed).slice("Shared", 3)
    # The qualified form still resolves.
    assert FlowSlicer(system, analyzed).slice("First.Shared", 3).flow.entry_label
