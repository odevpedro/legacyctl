"""Deterministic metrics of the analysis.

The numbers produced here are the evidence for the central thesis of the
framework: a flow slice carries a small fraction of the system context, which
is what makes LLM-based semantic extraction tractable and reviewable.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Metrics(BaseModel):
    """Snapshot of structural/graph/knowledge metrics."""

    model_config = ConfigDict(extra="allow")

    number_of_source_files: int = 0
    number_of_components: int = 0
    number_of_procedures: int = 0
    number_of_calls: int = 0
    number_of_sql_statements: int = 0
    number_of_database_objects: int = 0
    number_of_graph_nodes: int = 0
    number_of_graph_edges: int = 0
    number_of_clusters: int = 0
    number_of_hubs: int = 0
    number_of_flows: int = 0
    number_of_rules: int = 0
    number_of_validated_rules: int = 0
    number_of_divergences: int = 0
    number_of_uncertain_statements: int = 0
    states_modeled: int = 0
    context_reduction_ratio: float = 0.0
    hub_bypass_count: int = 0

    def as_table(self) -> list[dict[str, Any]]:
        return [{"metric": k, "value": v} for k, v in self.model_dump().items()]


def estimate_tokens(text: str) -> int:
    """Rough, deterministic token estimate (~4 chars/token).

    Deliberately an estimate and not a tokenizer call: the value must be
    reproducible across environments and must never require network access.
    """
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


class Counter(BaseModel):
    """Small helper for accumulating integer counters during a run."""

    model_config = ConfigDict(extra="allow")
    values: dict[str, int] = Field(default_factory=dict)

    def bump(self, key: str, amount: int = 1) -> None:
        self.values[key] = self.values.get(key, 0) + amount

    def get(self, key: str) -> int:
        return self.values.get(key, 0)
