"""Tests for the verification loop.

These pin down the behaviour that matters: the verifier must never invent an
answer, and it must never report a guess as a pass.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from legacyctl.domain.enums import DivergenceStatus, RuleConfidence, RuleStatus, VerifyStatus
from legacyctl.domain.models import BusinessRule, GoldenMasterCase
from legacyctl.verification import (
    GoldenMasterError,
    GoldenMasterStore,
    RuleSimulator,
    Verifier,
    advisory_cases,
    blocking_cases,
    build_new_system,
    coerce,
)
from legacyctl.verification.new_system import _resolve

FIXTURES = Path(__file__).resolve().parents[2] / "catalog" / "golden-master"


def make_rule(
    rule_id: str = "RULE-TEST-001",
    expression: str = "credit > limit",
    behavior: str = "unknown",
    status: RuleStatus = RuleStatus.VALIDATED,
    flow_id: str = "FLOW-TEST",
) -> BusinessRule:
    return BusinessRule.model_validate(
        {
            "id": rule_id,
            "flow_id": flow_id,
            "name": "test rule",
            "description": "a rule",
            "status": status.value,
            "condition": {
                "type": "data",
                "expression": expression,
                "fields": [expression.split()[0].split(".")[0]] if expression else [],
            },
            "behavior": {"type": behavior, "target": None},
            "sources": [{"file": "Legacy.bas", "line": 10, "procedure": "Test"}],
        }
    )


def make_case(
    case_id: str = "GM-TEST-001",
    inputs: dict[str, str] | None = None,
    expected: dict[str, str] | None = None,
    confidence: RuleConfidence = RuleConfidence.HIGH,
    evidence: str = "observação manual",
) -> GoldenMasterCase:
    return GoldenMasterCase.model_validate(
        {
            "case_id": case_id,
            "flow_id": "FLOW-TEST",
            "description": "a case",
            "in_params": inputs if inputs is not None else {"credit": "100", "limit": "50"},
            "out_params": expected if expected is not None else {"RULE-TEST-001": "fired"},
            "evidence": evidence,
            "confidence": confidence.value,
        }
    )


class TestResolution:
    """A field reference is a lookup, a literal is a literal."""

    def test_bare_identifier_is_read_from_inputs(self) -> None:
        assert _resolve("limit", {"limit": "50"}) == "50"

    def test_missing_field_is_unresolvable(self) -> None:
        assert _resolve("limit", {}) is None

    def test_quoted_literal_is_taken_literally(self) -> None:
        assert _resolve('"00000000000"', {}) == "00000000000"

    def test_compound_or_is_not_half_evaluated(self) -> None:
        # Regression: this used to be accepted as a literal because it merely
        # began and ended with a quote, so half a condition decided the case.
        assert _resolve('cpf = "0" Or cpf = "1"', {"cpf": "1"}) is None

    def test_vb6_call_is_not_evaluated(self) -> None:
        assert _resolve("IsValid(a, b)", {"a": "1", "b": "2"}) is None

    def test_numbers_compare_numerically(self) -> None:
        # "9" > "10" as text is backwards; as numbers it is right.
        rule = make_rule(expression="credit > 9")
        simulator = RuleSimulator()
        assert simulator._decide(rule, {"credit": "10"}) == "fired"
        assert simulator._decide(rule, {"credit": "5"}) == "not_fired"


class TestSimulator:
    def test_true_condition_fires_a_guard(self) -> None:
        assert RuleSimulator()._decide(make_rule(), {"credit": "100", "limit": "50"}) == "fired"

    def test_false_condition_does_not_fire(self) -> None:
        assert RuleSimulator()._decide(make_rule(), {"credit": "10", "limit": "50"}) == "not_fired"

    def test_declared_behavior_wins_over_the_guard_wording(self) -> None:
        rule = make_rule(behavior="reject")
        assert RuleSimulator()._decide(rule, {"credit": "100", "limit": "50"}) == "reject"

    def test_understood_guard_with_no_declared_behavior_is_not_a_verdict(self) -> None:
        # A guard says *when*, not *what*: inventing accept/reject here would
        # be the simulator deciding the business.
        assert RuleSimulator()._decide(make_rule(), {"credit": "100", "limit": "50"}) == "fired"

    def test_unevaluatable_condition_is_unknown(self) -> None:
        assert RuleSimulator()._decide(make_rule(expression="IsBlank(x)"), {"x": ""}) == "unknown"

    def test_undeclared_behavior_is_not_reported_as_a_pass(self) -> None:
        # Regression: the declared behavior used to be returned without ever
        # evaluating the guard, so every case passed.
        result = RuleSimulator().evaluate(make_case(), [make_rule(expression="IsBlank(x)")])
        assert result == {"RULE-TEST-001": "unknown"}

    def test_only_rules_of_the_same_flow_are_evaluated(self) -> None:
        simulator = RuleSimulator()
        rules = [make_rule(flow_id="FLOW-OTHER")]
        assert simulator.evaluate(make_case(), rules) == {}

    def test_coerce_normalises_values(self) -> None:
        assert coerce(True) == "true"
        assert coerce(None) == ""
        assert coerce(10) == "10"


class TestVerifier:
    def test_matching_case_passes(self) -> None:
        run = Verifier(RuleSimulator()).run("SYS-TEST", [make_case()], [make_rule()])
        assert run.passed == 1
        assert run.ok

    def test_unevaluated_rule_is_skipped_not_failed(self) -> None:
        case = make_case(expected={"RULE-TEST-001": "fired"})
        rule = make_rule(expression='cpf = "0" Or cpf = "1"')
        run = Verifier(RuleSimulator()).run("SYS-TEST", [case], [rule])
        result = run.results[0]
        assert result.status is VerifyStatus.SKIPPED
        assert run.failed == 0
        assert run.skipped == 1
        assert "not decidable" in (result.detail or "")

    def test_real_divergence_fails_with_a_readable_message(self) -> None:
        case = make_case(inputs={"credit": "10", "limit": "50"})
        run = Verifier(RuleSimulator()).run("SYS-TEST", [case], [make_rule()])
        result = run.results[0]
        assert result.status is VerifyStatus.FAILED
        assert "expected fired, got not_fired" in result.divergences[0]

    def test_divergence_record_names_the_rule_flow_and_source(self) -> None:
        case = make_case(inputs={"credit": "10", "limit": "50"})
        run = Verifier(RuleSimulator()).run("SYS-TEST", [case], [make_rule()])
        record = run.report.records[0] if run.report else None
        assert record is not None
        assert record.rule_id == "RULE-TEST-001"
        assert record.flow_id == "FLOW-TEST"
        assert record.source == "Legacy.bas:10"
        assert record.status is DivergenceStatus.REQUIRES_REVIEW
        assert record.detected_by == "rule-simulator"

    def test_a_candidate_rule_cannot_answer_a_case(self) -> None:
        # Only validated rules exist in the new system, so an unreviewed rule
        # shows up as missing instead of being quietly simulated.
        case = make_case()
        run = Verifier(RuleSimulator()).run(
            "SYS-TEST", [case], [make_rule(status=RuleStatus.CANDIDATE)]
        )
        assert run.results[0].status is VerifyStatus.FAILED
        assert "absent" in run.results[0].divergences[0]

    def test_low_confidence_case_is_advisory_only(self) -> None:
        case = make_case(
            case_id="GM-ADVISORY",
            inputs={"credit": "10", "limit": "50"},
            confidence=RuleConfidence.LOW,
        )
        run = Verifier(RuleSimulator()).run("SYS-TEST", [case], [make_rule()])
        assert run.results[0].status is VerifyStatus.FAILED
        assert (run.report.blocking if run.report else -1) == 0
        assert "advisory only" in (run.results[0].detail or "")
        assert case.id in {c.id for c in advisory_cases([case])}
        assert case.id not in {c.id for c in blocking_cases([case])}

    def test_broken_new_system_is_reported_not_swallowed(self) -> None:
        class Broken:
            name = "broken"
            is_real = True

            def evaluate(self, case: GoldenMasterCase, rules: list[BusinessRule]) -> dict[str, str]:
                raise RuntimeError("connection refused")

        run = Verifier(Broken()).run("SYS-TEST", [make_case()], [make_rule()])  # type: ignore[arg-type]
        result = run.results[0]
        assert result.status is VerifyStatus.SKIPPED
        assert "connection refused" in (result.detail or "")

    def test_free_form_output_is_compared_as_a_payload(self) -> None:
        class Fixed:
            name = "fixed"
            is_real = True

            def evaluate(self, case: GoldenMasterCase, rules: list[BusinessRule]) -> dict[str, str]:
                return {"approved": "false", "reason": "credit limit exceeded"}

        case = make_case(expected={"approved": "false", "reason": "limite excedido"})
        run = Verifier(Fixed()).run("SYS-TEST", [case], [make_rule()])  # type: ignore[arg-type]
        assert run.results[0].status is VerifyStatus.FAILED
        assert any("reason" in d for d in run.results[0].divergences)

    def test_the_run_records_that_it_was_a_simulation(self) -> None:
        run = Verifier(RuleSimulator()).run("SYS-TEST", [make_case()], [make_rule()])
        assert run.provider_is_real is False
        assert run.report is not None
        assert any("simulation" in note for note in run.report.notes)


class TestGoldenMasterStore:
    def test_a_case_without_evidence_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "gm.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "system_id": "SYS-TEST",
                    "cases": [{"case_id": "GM-1", "description": "d", "out_params": {"a": "b"}}],
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(GoldenMasterError, match="no evidence source"):
            GoldenMasterStore(path).load()

    def test_an_invented_evidence_source_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "gm.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "cases": [
                        {
                            "case_id": "GM-1",
                            "description": "d",
                            "out_params": {"a": "b"},
                            "evidence": "the model seemed sure",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(GoldenMasterError, match="not one of the accepted sources"):
            GoldenMasterStore(path).load()

    def test_a_case_that_expects_nothing_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "gm.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "cases": [
                        {"case_id": "GM-1", "description": "d", "evidence": "observação manual"}
                    ]
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(GoldenMasterError, match="expects nothing at all"):
            GoldenMasterStore(path).load()

    def test_a_missing_file_is_an_empty_catalog(self, tmp_path: Path) -> None:
        assert GoldenMasterStore(tmp_path / "absent.yaml").load() == []

    def test_round_trip_keeps_the_case(self, tmp_path: Path) -> None:
        store = GoldenMasterStore(tmp_path / "gm.yaml")
        case = make_case()
        store.save([case], "SYS-TEST")
        loaded = store.load()
        assert len(loaded) == 1
        assert loaded[0].id == case.id
        assert loaded[0].expected == case.expected
        assert loaded[0].inputs == case.inputs

    def test_a_saved_case_keeps_its_evidence(self, tmp_path: Path) -> None:
        path = GoldenMasterStore(tmp_path / "gm.yaml").save([make_case()], "SYS-TEST")
        assert "observação manual" in path.read_text(encoding="utf-8")


class TestShippedCatalogs:
    """The curated fixtures must load; they are the MVP's regression baseline."""

    @pytest.mark.parametrize("name", ["vb6", "consistency"])
    def test_catalog_loads(self, name: str) -> None:
        cases = GoldenMasterStore(FIXTURES / f"{name}.yaml").load()
        assert cases
        assert all(case.evidence for case in cases)
        assert all(case.expected for case in cases)

    def test_case_ids_are_unique(self) -> None:
        ids = [c.id for c in GoldenMasterStore(FIXTURES / "vb6.yaml").load()]
        assert len(ids) == len(set(ids))


class TestProviderSelection:
    def test_default_is_the_simulation(self) -> None:
        port = build_new_system()
        assert isinstance(port, RuleSimulator)
        assert port.is_real is False

    def test_http_is_chosen_explicitly(self) -> None:
        port = build_new_system("http")
        assert port.is_real is True

    def test_an_unknown_provider_is_an_error(self) -> None:
        with pytest.raises(ValueError, match="unknown new-system provider"):
            build_new_system("telepathy")
