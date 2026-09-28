"""Tests for the reports.

A report is read by someone deciding what to trust, so these tests are about
what it *claims*. An understated report hides work that was done; an
overstated one invents coverage.
"""

from __future__ import annotations

from pathlib import Path

from legacyctl.domain.enums import DivergenceStatus, RuleStatus, VerifyStatus
from legacyctl.domain.models import (
    BusinessRule,
    Cluster,
    DivergenceRecord,
    DivergenceReport,
    GoldenMasterCase,
    GraphNode,
    Hub,
    LegacySystem,
    SystemGraph,
    VerifyResult,
)
from legacyctl.reporting import (
    render_divergence_report,
    render_legacy_report,
    summarise,
)


def make_system() -> LegacySystem:
    return LegacySystem(id="SYS-T", name="Test", source_root="/tmp/t")


def make_graph() -> SystemGraph:
    return SystemGraph(
        system_id="SYS-T",
        nodes=[
            GraphNode(id="N1", kind="PROCEDURE", label="Do"),
            GraphNode(id="N2", kind="PROCEDURE", label="Call"),
        ],
        edges=[],
        clusters=[
            Cluster(
                id="C1",
                algorithm="louvain",
                index=0,
                node_ids=["N1", "N2"],
                size=2,
                share_of_system=1.0,
            )
        ],
        hubs=[Hub(id="H1", name="Do", node_id="N1", degree=7, flows_through=["F1"])],
    )


def make_rule(status: RuleStatus = RuleStatus.VALIDATED) -> BusinessRule:
    return BusinessRule.model_validate(
        {
            "id": "RULE-T-001",
            "name": "Reject when credit is over the limit",
            "status": status.value,
            "condition": {"type": "data", "expression": "credit > limit", "fields": ["credit"]},
            "behavior": {"type": "reject", "target": None},
            "sources": [{"file": "Customer.bas", "line": 87}],
        }
    )


def make_case() -> GoldenMasterCase:
    return GoldenMasterCase.model_validate(
        {
            "case_id": "GM-1",
            "flow_id": "FLOW-T",
            "description": "d",
            "in_params": {"credit": "10"},
            "out_params": {"RULE-T-001": "reject"},
            "evidence": "observação manual",
        }
    )


def make_result(status: VerifyStatus = VerifyStatus.PASSED) -> VerifyResult:
    return VerifyResult(
        case_id="GM-1",
        flow_id="FLOW-T",
        status=status,
        matches=["RULE-T-001"] if status is VerifyStatus.PASSED else [],
        divergences=[]
        if status is VerifyStatus.PASSED
        else ["RULE-T-001: expected reject, got accept"],
        legacy_output={"RULE-T-001": "reject"},
        new_output={"RULE-T-001": "accept"},
    )


def make_divergence() -> DivergenceReport:
    return DivergenceReport(
        system_id="SYS-T",
        records=[
            DivergenceRecord(
                id="DIV-1",
                flow_id="FLOW-T",
                rule_id="RULE-T-001",
                status=DivergenceStatus.REQUIRES_REVIEW,
                summary="RULE-T-001: expected reject, got accept",
                legacy="reject",
                new_system="accept",
                source="Customer.bas:87",
                detected_by="rule-simulator",
            )
        ],
    )


class TestDivergenceReport:
    def test_every_divergence_names_rule_flow_and_source(self) -> None:
        text = render_divergence_report(make_divergence())
        assert "RULE-T-001" in text
        assert "FLOW-T" in text
        assert "Customer.bas:87" in text
        assert DivergenceStatus.REQUIRES_REVIEW.value in text

    def test_the_sections_the_spec_asks_for_are_present(self) -> None:
        text = render_divergence_report(make_divergence())
        for heading in (
            "## Rule",
            "## Flow",
            "## Legacy",
            "## New System",
            "## Source",
            "## Status",
        ):
            assert heading in text

    def test_both_sides_are_shown(self) -> None:
        text = render_divergence_report(make_divergence())
        assert "reject" in text
        assert "accept" in text

    def test_no_divergence_is_not_claimed_as_full_agreement(self) -> None:
        # A clean run must not read as "the systems agree everywhere".
        text = render_divergence_report(DivergenceReport(system_id="SYS-T"))
        assert "not a claim that the systems agree everywhere" in text

    def test_a_blocking_count_is_shown(self) -> None:
        assert "1 blocking" in render_divergence_report(make_divergence())


class TestLegacyReport:
    def test_a_stage_that_never_ran_says_so(self) -> None:
        text = render_legacy_report(
            system=make_system(),
            graph=make_graph(),
            rules=[],
            results=[],
            divergences=None,
            cases=[],
        )
        assert "not generated" in text
        assert "not run" in text
        assert "verification did not run" in text

    def test_an_empty_verification_is_not_reported_as_a_clean_run(self) -> None:
        text = render_legacy_report(
            system=make_system(),
            graph=make_graph(),
            rules=[make_rule()],
            results=[],
            divergences=None,
            cases=[make_case()],
        )
        assert "verification did not run" in text

    def test_a_clean_verification_reports_the_undecidable_cases(self) -> None:
        # Regression: this used to say "no divergence report was produced" even
        # though verification had run and simply found nothing.
        text = render_legacy_report(
            system=make_system(),
            graph=make_graph(),
            rules=[make_rule()],
            results=[make_result(VerifyStatus.PASSED), make_result(VerifyStatus.SKIPPED)],
            divergences=DivergenceReport(system_id="SYS-T"),
            cases=[make_case(), make_case()],
        )
        assert "found no divergence" in text
        assert "could not be decided" in text
        assert "narrower result than it looks" in text

    def test_a_real_divergence_is_listed_with_its_rule(self) -> None:
        text = render_legacy_report(
            system=make_system(),
            graph=make_graph(),
            rules=[make_rule()],
            results=[make_result(VerifyStatus.FAILED)],
            divergences=make_divergence(),
            cases=[make_case()],
        )
        assert "1 divergence(s), 1 blocking" in text
        assert "RULE-T-001" in text
        assert "Customer.bas:87" in text

    def test_traceability_ties_each_rule_to_its_legacy_line(self) -> None:
        text = render_legacy_report(
            system=make_system(),
            graph=make_graph(),
            rules=[make_rule()],
            results=[],
            divergences=None,
            cases=[],
        )
        assert "## Traceability" in text
        assert "Customer.bas:87" in text

    def test_a_candidate_rule_is_marked_as_not_in_the_contract(self) -> None:
        text = render_legacy_report(
            system=make_system(),
            graph=make_graph(),
            rules=[make_rule(RuleStatus.CANDIDATE)],
            results=[],
            divergences=None,
            cases=[],
        )
        assert "not in contract" in text

    def test_the_validation_section_explains_the_gate(self) -> None:
        text = render_legacy_report(
            system=make_system(),
            graph=make_graph(),
            rules=[make_rule(RuleStatus.CANDIDATE)],
            results=[],
            divergences=None,
            cases=[],
        )
        assert "only through human review" in text

    def test_hub_flow_counts_render_as_numbers(self) -> None:
        # Regression: flows_through is a list and rendered as "[]".
        text = render_legacy_report(
            system=make_system(),
            graph=make_graph(),
            rules=[],
            results=[],
            divergences=None,
            cases=[],
        )
        assert "| `H1` | Do | 7 | 1 |" in text

    def test_the_report_has_every_section_the_spec_lists(self) -> None:
        text = render_legacy_report(
            system=make_system(),
            graph=make_graph(),
            rules=[make_rule()],
            results=[],
            divergences=None,
            cases=[],
        )
        for section in (
            "## System Overview",
            "## Structural Metrics",
            "## Graph Metrics",
            "## Hubs",
            "## Extracted Rules",
            "## Validation Status",
            "## API Contracts",
            "## Verification Results",
            "## Divergences",
            "## Traceability",
        ):
            assert section in text, section


class TestSummarise:
    def test_no_records_reads_as_no_divergences(self) -> None:
        assert summarise([]) == "no divergences"

    def test_records_are_counted_by_status(self) -> None:
        assert "1 REQUIRES_REVIEW" in summarise(make_divergence().records)

    def test_a_path_can_be_written(self, tmp_path: Path) -> None:
        from legacyctl.reporting import write_divergence_report

        path = write_divergence_report(make_divergence(), tmp_path / "r.md")
        assert path.is_file()
        assert "Divergence Report" in path.read_text(encoding="utf-8")
