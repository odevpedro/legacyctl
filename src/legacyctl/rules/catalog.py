"""The rule catalog: the only place a rule may exist before human review.

Two rules govern this module, and both are enforced in code rather than by
convention:

* a rule is created as a **candidate** with evidence attached, and nothing
  else can set ``validated`` -- only :meth:`RuleCatalog.review`, which records
  a person and a note;
* a rule that has no ``observation`` is refused. A decision nobody can point
  at in the source is not a discovery, it is a hallucination with a status.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml

from ..domain.enums import EvidenceKind, RuleConfidence, RuleStatus
from ..domain.ids import rule_id
from ..domain.models import (
    BusinessRule,
    RuleBehavior,
    RuleCatalog,
    RuleCondition,
    RuleEvidence,
    RuleSource,
)
from ..observability import get_log

#: Minimum evidence for a rule to be admitted to the catalog at all.
MINIMUM_OBSERVATIONS = 1


class RuleRejected(ValueError):
    """A candidate that cannot be admitted, with the reason it cannot."""


class RuleCatalogStore:
    """Load/save a :class:`RuleCatalog` as YAML, with review transitions."""

    def __init__(self, path: Path, system_id: str) -> None:
        self.path = path
        self.system_id = system_id

    # -- persistence ------------------------------------------------------

    def load(self) -> RuleCatalog:
        if not self.path.is_file():
            return RuleCatalog(system_id=self.system_id)
        data = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        catalog = RuleCatalog.model_validate(data)
        if catalog.system_id != self.system_id:
            raise RuleRejected(
                f"catalog at {self.path} belongs to system {catalog.system_id!r}, "
                f"not {self.system_id!r}"
            )
        return catalog

    def save(self, catalog: RuleCatalog) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = catalog.model_dump(mode="json", exclude_defaults=False)
        self.path.write_text(
            yaml.safe_dump(payload, sort_keys=False, allow_unicode=True, width=100),
            encoding="utf-8",
        )
        return self.path

    # -- queries ----------------------------------------------------------

    def find(self, rule_ref: str) -> BusinessRule | None:
        catalog = self.load()
        needle = rule_ref.strip().lower()
        for rule in catalog.rules:
            if rule.id.lower() == needle:
                return rule
        matches = [r for r in catalog.rules if r.name.lower() == needle]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise RuleRejected(
                f"{rule_ref!r} matches {len(matches)} rules: "
                + ", ".join(sorted(r.id for r in matches))
                + "; use the rule id"
            )
        return None

    def validated(self) -> list[BusinessRule]:
        return [r for r in self.load().rules if r.status is RuleStatus.VALIDATED]

    def by_status(self, status: RuleStatus) -> list[BusinessRule]:
        return [r for r in self.load().rules if r.status is status]

    # -- mutations --------------------------------------------------------

    def add(self, rules: Iterable[BusinessRule]) -> list[BusinessRule]:
        catalog = self.load()
        known = {r.id for r in catalog.rules}
        added: list[BusinessRule] = []
        for rule in rules:
            if rule.id in known:
                continue
            known.add(rule.id)
            catalog.rules.append(rule)
            added.append(rule)
        catalog.rules.sort(key=lambda r: r.id)
        if added:
            self.save(catalog)
            log = get_log()
            for rule in added:
                with log.stage("rule-add", system_id=catalog.system_id, rule_id=rule.id) as rec:
                    rec.details["name"] = rule.name
                    rec.details["status"] = rule.status.value
                    rec.details["confidence"] = rule.confidence.value
        return added

    def review(
        self,
        rule_ref: str,
        decision: str,
        reviewer: str,
        note: str | None = None,
    ) -> BusinessRule:
        """The only transition into ``validated`` or ``rejected``."""
        catalog = self.load()
        rule = next((r for r in catalog.rules if r.id.lower() == rule_ref.lower()), None)
        if rule is None:
            found = self.find(rule_ref)
            if found is None:
                raise RuleRejected(f"no rule {rule_ref!r} in the catalog")
            rule = found
        if decision == "approve":
            rule.status = RuleStatus.VALIDATED
        elif decision == "reject":
            rule.status = RuleStatus.REJECTED
        elif decision == "reset":
            rule.status = RuleStatus.REVIEW
        else:
            raise RuleRejected(f"unknown review decision {decision!r}")
        if rule.status in (RuleStatus.VALIDATED, RuleStatus.REJECTED) and not reviewer:
            raise RuleRejected("a human decision must name the reviewer")
        rule.reviewed_by = reviewer or None
        rule.review_note = note
        if rule.status is RuleStatus.VALIDATED:
            rule.confidence = RuleConfidence.HIGH
        catalog.rules = sorted(catalog.rules, key=lambda r: r.id)
        self.save(catalog)
        log = get_log()
        with log.stage("rule-review", system_id=catalog.system_id, rule_id=rule.id) as rec:
            rec.details["decision"] = decision
            rec.details["reviewer"] = reviewer
        return rule

    def promote_to_review(self, rule_ref: str) -> BusinessRule:
        catalog = self.load()
        rule = next((r for r in catalog.rules if r.id.lower() == rule_ref.lower()), None)
        if rule is None:
            raise RuleRejected(f"no rule {rule_ref!r} in the catalog")
        rule.status = RuleStatus.REVIEW
        self.save(catalog)
        return rule


def build_rule(
    candidate: dict[str, Any],
    *,
    flow_id: str | None,
    entry_point_id: str | None,
    domain_key: str,
    ordinal: int,
    source: RuleSource | None = None,
) -> BusinessRule:
    """Validate one extracted candidate into a catalog rule.

    Raises :class:`RuleRejected` rather than dropping silently, so the CLI can
    tell the operator which candidate failed and why.
    """
    name = str(candidate.get("name", "")).strip()
    if not name:
        raise RuleRejected("candidate has no name")

    observations = [str(o).strip() for o in candidate.get("observation") or [] if str(o).strip()]
    if len(observations) < MINIMUM_OBSERVATIONS:
        raise RuleRejected(f"rule {name!r} has no observation to point at")

    inferences = [str(i).strip() for i in candidate.get("inference") or [] if str(i).strip()]

    evidence: list[RuleEvidence] = [
        RuleEvidence(
            type="observation",
            text=observation,
            source=source,
            evidence_kind=EvidenceKind.OBSERVATION,
        )
        for observation in observations
    ]
    evidence.extend(
        RuleEvidence(
            type="inference",
            text=inference,
            source=source,
            evidence_kind=EvidenceKind.INFERENCE,
        )
        for inference in inferences
    )

    condition_raw = candidate.get("condition") or {}
    behavior_raw = candidate.get("behavior") or {}
    if not isinstance(condition_raw, dict) or not isinstance(behavior_raw, dict):
        raise RuleRejected(f"rule {name!r} has a malformed condition/behavior")

    # Confidence is derived, never asserted by the extractor. A rule backed by
    # inferences only cannot be high confidence, whatever the model claimed.
    confidence = (
        RuleConfidence.HIGH
        if len(observations) >= 2 and not inferences
        else RuleConfidence.MEDIUM
        if observations
        else RuleConfidence.LOW
    )

    return BusinessRule(
        id=rule_id(domain_key, ordinal),
        name=name,
        description=str(candidate.get("description", "")).strip(),
        flow_id=flow_id,
        entry_point_id=entry_point_id,
        status=RuleStatus.CANDIDATE,
        confidence=confidence,
        condition=RuleCondition.model_validate(
            {
                "type": str(condition_raw.get("type") or "unknown"),
                "expression": condition_raw.get("expression"),
                "fields": [str(f) for f in condition_raw.get("fields") or []],
            }
        ),
        behavior=RuleBehavior.model_validate(
            {
                "type": str(behavior_raw.get("type") or "unknown"),
                "reason": behavior_raw.get("reason"),
                "target": behavior_raw.get("target"),
            }
        ),
        observation=observations,
        inference=inferences,
        sources=[source] if source else [],
        evidence=evidence,
        state_transitions=[str(behavior_raw.get("target"))]
        if behavior_raw.get("type") == "transition" and behavior_raw.get("target")
        else [],
    )
