"""The rule catalog's invariants.

These are the guarantees a reviewer relies on when they are asked to approve a
rule: nothing enters without evidence, nothing becomes validated without a
name, ids are stable across runs, and the LLM phase is reproducible offline.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from legacyctl.domain.enums import RuleConfidence, RuleStatus
from legacyctl.domain.models import BusinessFlow, RuleSource
from legacyctl.llm.provider import (
    DeterministicRuleExtractor,
    OpenAICompatibleExtractor,
    build_extractor,
    parse_rule_payload,
    render_prompt,
)
from legacyctl.rules import RuleCatalogStore, RuleExtractor, RuleRejected, build_rule

GOOD: dict[str, Any] = {
    "name": "Reject customer when CPF is blank",
    "description": "ValidateCustomer refuses blank documents.",
    "condition": {"type": "input", "expression": "IsBlank(cpf)", "fields": ["cpf"]},
    "behavior": {"type": "reject", "reason": "CPF is mandatory", "target": None},
    "observation": ["If IsBlank(cpf) Then"],
    "inference": [],
}


def test_candidate_without_observation_is_refused() -> None:
    """A decision nobody can point at is a hallucination, not a rule."""
    payload = {**GOOD, "observation": []}
    with pytest.raises(RuleRejected):
        build_rule(payload, flow_id=None, entry_point_id=None, domain_key="D", ordinal=1)


def test_candidate_without_name_is_refused() -> None:
    with pytest.raises(RuleRejected):
        build_rule(
            {**GOOD, "name": "  "}, flow_id=None, entry_point_id=None, domain_key="D", ordinal=1
        )


def test_confidence_is_derived_not_asserted() -> None:
    """The extractor cannot promote itself to high confidence."""
    many = build_rule(
        {**GOOD, "observation": ["a", "b"]},
        flow_id=None,
        entry_point_id=None,
        domain_key="D",
        ordinal=1,
    )
    assert many.confidence is RuleConfidence.HIGH

    inferred = build_rule(
        {**GOOD, "inference": ["guessed"]},
        flow_id=None,
        entry_point_id=None,
        domain_key="D",
        ordinal=1,
    )
    assert inferred.confidence is not RuleConfidence.HIGH


def test_evidence_is_split_into_observation_and_inference() -> None:
    rule = build_rule(
        {**GOOD, "inference": ["the reason is not stated"]},
        flow_id=None,
        entry_point_id=None,
        domain_key="D",
        ordinal=1,
        source=RuleSource(file="a.bas", line=3),
    )
    kinds = {e.evidence_kind.value for e in rule.evidence}
    assert kinds == {"observation", "inference"}
    assert all(e.source is not None for e in rule.evidence)


def test_new_rule_is_a_candidate_not_validated() -> None:
    rule = build_rule(GOOD, flow_id=None, entry_point_id=None, domain_key="D", ordinal=1)
    assert rule.status is RuleStatus.CANDIDATE


# -- catalog ---------------------------------------------------------------


@pytest.fixture()
def store(tmp_path: Path) -> RuleCatalogStore:
    catalog = RuleCatalogStore(tmp_path / "rules.yaml", "SYS-TEST")
    catalog.add(
        [
            build_rule(
                {**GOOD, "name": f"Rule {index}"},
                flow_id="FLOW-A",
                entry_point_id="ENTRY-A",
                domain_key="A",
                ordinal=index,
            )
            for index in (1, 2, 3)
        ]
    )
    return catalog


def test_catalog_round_trips(store: RuleCatalogStore) -> None:
    reloaded = RuleCatalogStore(store.path, "SYS-TEST").load()
    assert [r.id for r in reloaded.rules] == [r.id for r in store.load().rules]
    assert reloaded.system_id == "SYS-TEST"


def test_catalog_refuses_a_foreign_system(tmp_path: Path, store: RuleCatalogStore) -> None:
    with pytest.raises(RuleRejected):
        RuleCatalogStore(store.path, "SYS-OTHER").load()


def test_review_requires_a_named_reviewer(store: RuleCatalogStore) -> None:
    with pytest.raises(RuleRejected):
        store.review("RULE-A-001", "approve", reviewer="", note="looks right")


def test_approve_is_the_only_path_to_validated(store: RuleCatalogStore) -> None:
    rule = store.review("RULE-A-002", "approve", "hoper", "confirmed")
    assert rule.status is RuleStatus.VALIDATED
    assert rule.reviewed_by == "hoper"
    assert [r.id for r in store.validated()] == ["RULE-A-002"]


def test_reject_records_the_decision(store: RuleCatalogStore) -> None:
    rule = store.review("RULE-A-003", "reject", "hoper", "not a business rule")
    assert rule.status is RuleStatus.REJECTED
    assert rule.review_note == "not a business rule"
    assert store.validated() == []


def test_unknown_decision_is_refused(store: RuleCatalogStore) -> None:
    with pytest.raises(RuleRejected):
        store.review("RULE-A-001", "maybe", "hoper")


def test_unknown_rule_is_refused(store: RuleCatalogStore) -> None:
    with pytest.raises(RuleRejected):
        store.review("RULE-NOPE-999", "approve", "hoper")


def test_ambiguous_rule_name_is_refused(store: RuleCatalogStore) -> None:
    store.add(
        [
            build_rule(
                {**GOOD, "name": "Rule 1"},
                flow_id="FLOW-B",
                entry_point_id=None,
                domain_key="B",
                ordinal=1,
            )
        ]
    )
    with pytest.raises(RuleRejected):
        store.find("rule 1")


# -- provider --------------------------------------------------------------


def test_default_provider_is_offline_and_deterministic() -> None:
    provider = build_extractor()
    assert isinstance(provider, DeterministicRuleExtractor)
    assert build_extractor("deterministic").name == provider.name


def test_unknown_provider_is_refused() -> None:
    with pytest.raises(ValueError):
        build_extractor("gpt-9-ultra")


def test_hosted_provider_requires_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LEGACYCTL_LLM_API_KEY", raising=False)
    with pytest.raises(ValueError):
        OpenAICompatibleExtractor(api_key=None, base_url="http://localhost")


def test_payload_recovery_from_a_fenced_response() -> None:
    raw = 'Here you go:\n```json\n[{"name": "R", "observation": ["x"]}]\n```\nThanks!'
    rules, warnings = parse_rule_payload(raw)
    assert [r["name"] for r in rules] == ["R"]
    assert not warnings


def test_payload_rejects_prose_only_response() -> None:
    rules, warnings = parse_rule_payload("I could not find any rule.")
    assert rules == []
    assert warnings


def test_payload_drops_unnamed_elements_and_says_so() -> None:
    rules, warnings = parse_rule_payload('[{"observation": ["x"]}, "nope"]')
    assert rules == []
    assert len(warnings) == 2


def test_prompt_contains_the_slice_and_no_format_fields() -> None:
    prompt = render_prompt({"source_fragments": [{"text": "If x Then {weird}"}]})
    assert "{weird}" in prompt
    assert "{slice_context}" not in prompt


def test_extraction_is_reproducible(vb6_system, vb6_graph) -> None:  # type: ignore[no-untyped-def]
    """The same slice must yield the same rule ids, every time.

    Without this the catalog cannot be diffed or reviewed between runs.
    """

    def ids() -> list[str]:
        from legacyctl.slicing import FlowSlicer

        result = FlowSlicer(vb6_system, vb6_graph).slice("CustomerForm.ValidateCustomer", 5)
        report = RuleExtractor(
            RuleCatalogStore(Path("/nonexistent/rules.yaml"), vb6_system.id),
            provider=DeterministicRuleExtractor(),
        ).extract(result.flow, result.fragments, persist=False)
        return [r.id for r in report.rules]

    assert ids() == ids()
    assert ids(), "the fixture flow must yield at least one candidate"


def test_extraction_covers_the_slice_only(vb6_system, vb6_graph) -> None:  # type: ignore[no-untyped-def]
    from legacyctl.rules import compose_context
    from legacyctl.slicing import FlowSlicer

    result = FlowSlicer(vb6_system, vb6_graph).slice("CustomerForm.ValidateCustomer", 5)
    context = compose_context(result.flow, result.fragments)
    assert set(context["procedures"]) <= set(result.flow.procedure_ids)
    assert {f["id"] for f in context["source_fragments"]} == {f.id for f in result.fragments}
    serialised = json.dumps(context)
    assert "LegacyS3cr3t" not in serialised


def test_sanitizer_is_applied_to_the_composed_context() -> None:
    from legacyctl.domain.models import SourceFragment
    from legacyctl.rules import compose_context

    flow = _flow()
    fragment = SourceFragment(
        id="FRAG-1",
        file="Database.bas",
        start_line=1,
        end_line=1,
        text='Private Const CONN As String = "Password=hunter2"',
        procedure_id="PROC-DATABASE-OPEN",
    )
    context = compose_context(flow, [fragment])
    assert "hunter2" not in json.dumps(context)


def _flow() -> BusinessFlow:
    return BusinessFlow(
        id="FLOW-TEST",
        name="test",
        entry_point_id="ENTRY-TEST",
        entry_label="Test",
        depth=2,
        node_ids=["PROC-A"],
        edge_ids=[],
        procedure_ids=["PROC-A"],
        sql_ids=[],
        database_object_ids=[],
    )
