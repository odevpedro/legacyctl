"""Verification: Golden Master (expected) against the new system (actual).

The comparison is deliberately value-based and *explains* itself. When a
case's expected value is a rule id, the result for that rule is compared and
the divergence record names the rule, the flow and the source line, because
the feedback loop is ``divergence -> rule -> catalog -> human review``. Nothing
here ever changes a rule: a failed comparison is information for a person.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from ..domain.enums import DivergenceStatus, RuleConfidence, RuleStatus, VerifyStatus
from ..domain.models import (
    BusinessRule,
    DivergenceRecord,
    DivergenceReport,
    GoldenMasterCase,
    VerifyResult,
)
from ..observability import get_log
from .golden_master import advisory_cases, blocking_cases
from .new_system import NewSystemPort, coerce


@dataclass(slots=True)
class VerificationRun:
    results: list[VerifyResult] = field(default_factory=list)
    report: DivergenceReport | None = None
    provider: str = "unknown"
    provider_is_real: bool = False

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.status is VerifyStatus.PASSED)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if r.status is VerifyStatus.FAILED)

    @property
    def skipped(self) -> int:
        return sum(1 for r in self.results if r.status is VerifyStatus.SKIPPED)

    @property
    def ok(self) -> bool:
        """True when nothing diverged.

        A skipped case does not make the run ok -- it is a question nobody has
        answered yet, and it stays visible in the summary so a reader can see
        the run is narrower than it looks.
        """
        return self.failed == 0 and self.report is not None and self.report.blocking == 0


def _expected_outputs(case: GoldenMasterCase) -> dict[str, str]:
    """The values the legacy system is known to produce, keyed by rule id.

    A case may express its expectation as ``{"RULE-X-001": "reject"}`` (per rule)
    or as free-form output (``{"approved": "false", "reason": "..."}``). When
    it names a rule we compare that rule; otherwise we compare the whole
    payload and cannot attribute the difference to a specific rule -- which the
    divergence record states rather than hiding.
    """
    if case.legacy_output:
        return dict(case.legacy_output)
    return dict(case.expected)


def _looks_like_rule_keyed(values: dict[str, str]) -> bool:
    return bool(values) and all(key.startswith("RULE-") or key.startswith("HUB-") for key in values)


class Verifier:
    def __init__(self, new_system: NewSystemPort) -> None:
        self._new_system = new_system
        self._log = get_log()

    def run(
        self,
        system_id: str,
        cases: list[GoldenMasterCase],
        rules: list[BusinessRule],
    ) -> VerificationRun:
        validated = [r for r in rules if r.status is RuleStatus.VALIDATED]
        by_id = {r.id: r for r in validated}
        run = VerificationRun(
            provider=self._new_system.name, provider_is_real=self._new_system.is_real
        )
        records: list[DivergenceRecord] = []
        advisory = {c.id for c in advisory_cases(cases)}

        for case in cases:
            result = self._compare(case, by_id)
            if case.id in advisory and result.status is VerifyStatus.FAILED:
                # A low-confidence case is reported but must not decide the run.
                result.detail = (
                    f"advisory only: evidence is {case.evidence!r} and the case is "
                    f"marked {case.confidence.value} confidence"
                )
            run.results.append(result)
            records.extend(self._records_for(case, result, by_id))

        run.report = DivergenceReport(
            system_id=system_id,
            generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
            records=records,
            by_status=_count_by_status(records),
            notes=[
                f"new system: {run.provider}"
                + ("" if run.provider_is_real else " (simulation of the validated rules)"),
                f"cases: {len(cases)} ({len(blocking_cases(cases))} blocking)",
                f"skipped (not decidable from the evidence): {run.skipped}",
            ],
        )
        with self._log.stage("verify", system_id=system_id) as record:
            record.details["cases"] = len(cases)
            record.details["passed"] = run.passed
            record.details["failed"] = run.failed
            record.details["skipped"] = run.skipped
            record.details["provider"] = run.provider
        return run

    # -- internals --------------------------------------------------------

    def _compare(self, case: GoldenMasterCase, rules: dict[str, BusinessRule]) -> VerifyResult:
        expected = _expected_outputs(case)
        rule_keyed = _looks_like_rule_keyed(expected)
        try:
            actual = self._new_system.evaluate(case, list(rules.values()))
        except Exception as error:
            # Reported as a skipped case with the reason, never swallowed into a pass.
            return VerifyResult(
                case_id=case.id,
                flow_id=case.flow_id,
                status=VerifyStatus.SKIPPED,
                legacy_output=expected,
                new_output={},
                detail=f"new system could not be queried: {error}",
            )

        if not rule_keyed:
            return self._compare_payload(case, expected, actual)

        matches: list[str] = []
        divergences: list[str] = []
        undecidable: list[str] = []
        for rule_id, want in sorted(expected.items()):
            got = actual.get(rule_id)
            if got is None:
                divergences.append(f"{rule_id}: absent from the new system (expected {want})")
            elif coerce(want) == "unknown":
                undecidable.append(f"{rule_id}: expectation is itself unknown")
            elif coerce(got) == "unknown":
                undecidable.append(
                    f"{rule_id}: the new system cannot evaluate this rule "
                    f"(expected {want}); it needs a real run or a better rule"
                )
            elif coerce(got) == coerce(want):
                matches.append(rule_id)
            else:
                divergences.append(f"{rule_id}: expected {want}, got {got}")

        if not matches and not divergences and undecidable:
            # Neither side can answer. Reporting this as a pass would be a
            # green light built on nothing, and as a failure would blame the
            # new system for a gap in the evidence.
            return VerifyResult(
                case_id=case.id,
                flow_id=case.flow_id,
                status=VerifyStatus.SKIPPED,
                divergences=undecidable,
                legacy_output=expected,
                new_output={k: coerce(v) for k, v in actual.items()},
                detail="not decidable from the recorded evidence; needs a real "
                "legacy run or a more precise rule",
            )
        return VerifyResult(
            case_id=case.id,
            flow_id=case.flow_id,
            status=VerifyStatus.FAILED if divergences else VerifyStatus.PASSED,
            matches=matches,
            divergences=divergences,
            legacy_output=expected,
            new_output={k: coerce(v) for k, v in actual.items()},
            detail="; ".join(undecidable) if undecidable else None,
        )

    def _compare_payload(
        self,
        case: GoldenMasterCase,
        expected: dict[str, str],
        actual: dict[str, str],
    ) -> VerifyResult:
        """Compare free-form output. A difference here cannot name a rule."""
        matches: list[str] = []
        divergences: list[str] = []
        for key, want in sorted(expected.items()):
            if key not in actual:
                divergences.append(f"{key}: missing in the new system (expected {want})")
            elif coerce(actual[key]) == coerce(want):
                matches.append(key)
            else:
                divergences.append(f"{key}: expected {want}, got {actual[key]}")
        for key in sorted(set(actual) - set(expected)):
            divergences.append(f"{key}: unexpected in the new system ({actual[key]})")
        return VerifyResult(
            case_id=case.id,
            flow_id=case.flow_id,
            status=VerifyStatus.PASSED if not divergences else VerifyStatus.FAILED,
            matches=matches,
            divergences=divergences,
            legacy_output=expected,
            new_output={k: coerce(v) for k, v in actual.items()},
            detail="compared as an unattributed payload; name rules in the case to "
            "attribute a difference",
        )

    def _records_for(
        self,
        case: GoldenMasterCase,
        result: VerifyResult,
        rules: dict[str, BusinessRule],
    ) -> list[DivergenceRecord]:
        if result.status is not VerifyStatus.FAILED:
            return []
        records: list[DivergenceRecord] = []
        for index, divergence in enumerate(result.divergences, start=1):
            rule_id = divergence.split(":", 1)[0] if ":" in divergence else None
            rule = rules.get(rule_id) if rule_id else None
            records.append(
                DivergenceRecord(
                    id=f"DIV-{case.id}-{index:02d}",
                    flow_id=case.flow_id,
                    rule_id=rule_id,
                    status=(
                        DivergenceStatus.MISSING_IN_NEW
                        if "absent" in divergence or "missing" in divergence
                        else DivergenceStatus.REQUIRES_REVIEW
                    ),
                    summary=divergence,
                    legacy=_legacy_side(divergence, result.legacy_output, rule_id),
                    new_system=_new_side(divergence, result.new_output, rule_id),
                    source=_source_of(rule, case),
                    detected_by=self._new_system.name,
                    blocking=case.confidence is not RuleConfidence.LOW,
                )
            )
        return records


def _legacy_side(divergence: str, expected: dict[str, str], rule_id: str | None) -> str | None:
    if rule_id and rule_id in expected:
        return coerce(expected[rule_id])
    return divergence.partition(": ")[2] or None


def _new_side(divergence: str, actual: dict[str, str], rule_id: str | None) -> str | None:
    if rule_id and rule_id in actual:
        return coerce(actual[rule_id])
    return divergence.rpartition(", got ")[2] or None


def _source_of(rule: BusinessRule | None, case: GoldenMasterCase) -> str | None:
    if rule and rule.sources:
        source = rule.sources[0]
        return f"{source.file}:{source.line}"
    return case.captured_from or case.evidence


def _count_by_status(records: list[DivergenceRecord]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        counts[record.status.value] = counts.get(record.status.value, 0) + 1
    return dict(sorted(counts.items()))
