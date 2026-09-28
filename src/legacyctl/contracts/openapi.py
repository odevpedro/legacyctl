"""OpenAPI 3.1 generation from *validated* business knowledge.

The direction of dependency is the whole point and is enforced here:

    catalog (validated rules)  ->  contract  ->  generated code

Nothing reads the legacy source to invent an operation. A rule that has not
been through human review is not in the contract, and every operation says
which rules produced it via ``x-business-rules``, so a later divergence can be
traced back to a rule and from there to a line of VB6.

The observed state machine and the legacy procedures' OUT parameters are part
of the contract, not an afterthought: they are the half of the traceability
that is not a boolean rule.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml

from ..domain.enums import EntryPointKind, RuleStatus
from ..domain.models import BusinessRule, DatabaseProcedure, LegacySystem
from ..observability import get_log
from .models import ApiContract, ContractOperation

#: Deterministic order for the state enum: observed states sorted, so two runs
#: over the same system produce byte-identical contracts.
CONTRACT_VERSION = "1.0.0"


class ContractError(RuntimeError):
    """The contract cannot be produced; the reason is always explicit."""


def _state_names(system: LegacySystem) -> list[str]:
    names: set[str] = set()
    for transition in system.state_transitions:
        for value in (transition.to_state, transition.from_state):
            if value and value != "UNKNOWN":
                names.add(value)
    return sorted(names)


def _state_schema(names: list[str]) -> dict[str, Any]:
    """The observed state machine, as a closed enum.

    A closed enum is a claim. It is derived only from values the legacy system
    actually writes, and the set is reported so a reviewer can see the claim.
    """
    if not names:
        return {"type": "string", "description": "No state transition was observed."}
    return {
        "type": "string",
        "description": "States observed in the legacy system.",
        "enum": names,
        "x-observed": True,
    }


def _rule_fields(rule: BusinessRule) -> list[str]:
    if rule.condition.fields:
        return sorted({f for f in rule.condition.fields if f})
    expression = rule.condition.expression or ""
    if not expression:
        return []
    head = expression.split()[0].strip("(")
    return [head] if head.isidentifier() else []


def _operation_id(rule: BusinessRule, taken: set[str]) -> str:
    words = [w for w in "".join(c if c.isalnum() else " " for c in rule.name).split() if w]
    base = "".join(w[0].upper() + w[1:] for w in words) or "rule"
    if base[0].isdigit():
        base = f"rule{base}"
    candidate = base[0].lower() + base[1:]
    suffix = 2
    while candidate in taken:
        candidate = f"{base[0].lower() + base[1:]}{suffix}"
        suffix += 1
    taken.add(candidate)
    return candidate


def _responses_for(rule: BusinessRule) -> dict[str, Any]:
    if rule.behavior.type == "reject":
        return {
            "200": {
                "description": "The decision the legacy system took.",
                "content": {
                    "application/json": {"schema": {"$ref": "#/components/schemas/RegraDecision"}}
                },
            },
            "422": {"description": rule.name},
        }
    return {
        "200": {
            "description": "The decision the legacy system took.",
            "content": {
                "application/json": {"schema": {"$ref": "#/components/schemas/RegraDecision"}}
            },
        }
    }


def _parameters_for(rule: BusinessRule) -> list[dict[str, Any]]:
    return [
        {
            "name": field,
            "in": "query",
            "required": False,
            "schema": {"type": "string"},
            "description": "Observed in the legacy condition; type not asserted by the tool.",
        }
        for field in _rule_fields(rule)
    ]


def _operation_description(rule: BusinessRule) -> str:
    """A description that survives into the generated Java.

    OpenAPI Generator copies ``description`` into the Javadoc of the generated
    interface, so this is the only place a Java developer implementing the rule
    can see which rule they are implementing and which legacy code produced it.
    The rule id travels in ``x-business-rules`` for machines; naming it here
    too is what puts it in front of a human.
    """
    parts = [rule.description or rule.name, f"Rule: {rule.id}."]
    procedures = sorted({s.procedure for s in rule.sources if s.procedure})
    if procedures:
        parts.append(f"From legacy: {', '.join(procedures)}.")
    files = sorted({s.file for s in rule.sources if s.file})
    if files:
        lines = ", ".join(
            f"{s.file}:{s.line}" for s in rule.sources if s.file and s.line is not None
        )
        parts.append(f"Legacy source: {lines or ', '.join(files)}.")
    if rule.behavior.type != "unknown":
        parts.append(f"Expected behaviour: {rule.behavior.type}.")
    else:
        parts.append(
            "Expected behaviour: not determined from the legacy code; the team must "
            "confirm it before implementing."
        )
    return " ".join(parts)


def _entry_group(system: LegacySystem, rule: BusinessRule) -> str:
    if not rule.entry_point_id:
        return "rules"
    entry = next((e for e in system.entry_points if e.id == rule.entry_point_id), None)
    if entry is None:
        return "rules"
    return EntryPointKind(entry.kind.value).name.lower()


def _out_parameter_schemas(system: LegacySystem) -> list[dict[str, Any]]:
    """OUT parameters belong to the *database* procedure, not to the client.

    The legacy behaviour lives in the SGBD: ``ExecutarConsistencia`` in VB6 only
    registers the OUT parameters and reads them back. So the contract exposes
    them from the :class:`DatabaseProcedure`, which is where the parser
    actually observed them.
    """
    schemas: list[dict[str, Any]] = []
    for obj in system.database_objects:
        if not isinstance(obj, DatabaseProcedure) or not obj.out_params:
            continue
        properties = {
            parameter.name: {
                "type": "string",
                "description": (
                    f"OUT parameter registered by the legacy client "
                    f"({parameter.type_name or 'untyped'})."
                ),
            }
            for parameter in obj.out_params
        }
        names = sorted(p.name for p in obj.out_params)
        schemas.append(
            {
                "type": "object",
                "title": f"{obj.name}Out",
                "properties": properties,
                "required": names,
                "x-legacy-database-procedure": obj.qualified_name,
                "x-parse-status": obj.parse_status.value,
            }
        )
    return sorted(schemas, key=lambda s: s["title"])


def build_contract(system: LegacySystem, rules: Iterable[BusinessRule]) -> ApiContract:
    """Generate the contract from validated rules only.

    Candidates and rejected rules are counted and reported, never emitted.
    """
    catalogue = list(rules)
    validated = sorted(
        (r for r in catalogue if r.status is RuleStatus.VALIDATED), key=lambda r: r.id
    )
    skipped = {
        "candidate": sum(1 for r in catalogue if r.status is RuleStatus.CANDIDATE),
        "review": sum(1 for r in catalogue if r.status is RuleStatus.REVIEW),
        "rejected": sum(1 for r in catalogue if r.status is RuleStatus.REJECTED),
    }
    if not validated:
        raise ContractError(
            "no validated rules: the contract is derived from reviewed knowledge only. "
            f"Review candidates first (skipped: {skipped})."
        )

    log = get_log()
    taken: set[str] = set()
    operations: list[ContractOperation] = []
    for rule in validated:
        operation_id = _operation_id(rule, taken)
        group = _entry_group(system, rule)
        operations.append(
            ContractOperation(
                operation_id=operation_id,
                summary=rule.name,
                description=_operation_description(rule),
                group=group,
                method="post",
                path=f"/{group}/{operation_id}",
                business_rules=[rule.id],
                fields=_rule_fields(rule),
                condition=rule.condition.model_dump(mode="json"),
                behavior=rule.behavior.model_dump(mode="json"),
                evidence=[e.text for e in rule.evidence],
                source_files=sorted({s.file for s in rule.sources if s.file}),
            )
        )

    states = _state_names(system)
    with log.stage("contract-generate", system_id=system.id) as record:
        record.details["validated_rules"] = len(validated)
        record.details["skipped"] = skipped
        record.details["states"] = len(states)

    return ApiContract(
        title=f"{system.name} API",
        version=CONTRACT_VERSION,
        system_id=system.id,
        operations=operations,
        states=states,
        out_parameter_schemas=_out_parameter_schemas(system),
        rules=validated,
        skipped_rules=skipped,
    )


def to_openapi(system: LegacySystem, contract: ApiContract) -> dict[str, Any]:
    schemas: dict[str, Any] = {
        "RegraDecision": {
            "type": "object",
            "title": "RegraDecision",
            "description": "The decision the legacy system took for one business rule.",
            "properties": {
                "rule": {"type": "string"},
                "outcome": {
                    "type": "string",
                    "enum": ["accept", "reject", "transition", "set_value", "notify", "unknown"],
                },
                "reason": {"type": "string"},
                "target": {"type": "string"},
            },
            "required": ["rule", "outcome"],
        },
        "EstadoObservado": _state_schema(contract.states),
    }
    for schema in contract.out_parameter_schemas:
        schemas[schema.get("title", "Out")] = schema

    paths: dict[str, Any] = {}
    for operation in contract.operations:
        entry: dict[str, Any] = {
            "operationId": operation.operation_id,
            "summary": operation.summary,
            "x-business-rules": operation.business_rules,
            "x-legacy-evidence": operation.evidence,
            "x-legacy-condition": operation.condition,
            "x-legacy-behavior": operation.behavior,
            "responses": _responses_for_operation(operation),
        }
        if operation.description and operation.description != operation.summary:
            entry["description"] = operation.description
        if operation.source_files:
            entry["x-legacy-source"] = operation.source_files
        if operation.fields:
            entry["parameters"] = _parameters_for_operation(operation)
        paths.setdefault(operation.path, {})[operation.method] = entry

    return {
        "openapi": "3.1.0",
        "info": {
            "title": contract.title,
            "version": contract.version,
            "x-system-id": contract.system_id,
            "x-derived-from": "validated business rules only",
            "x-rules-not-included": contract.skipped_rules,
        },
        "paths": dict(sorted(paths.items())),
        "components": {"schemas": dict(sorted(schemas.items()))},
    }


def _responses_for_operation(operation: ContractOperation) -> dict[str, Any]:
    if operation.behavior.get("type") == "reject":
        return {
            "200": {
                "description": "The decision the legacy system took.",
                "content": {
                    "application/json": {"schema": {"$ref": "#/components/schemas/RegraDecision"}}
                },
            },
            "422": {"description": operation.summary},
        }
    return {
        "200": {
            "description": "The decision the legacy system took.",
            "content": {
                "application/json": {"schema": {"$ref": "#/components/schemas/RegraDecision"}}
            },
        }
    }


def _parameters_for_operation(operation: ContractOperation) -> list[dict[str, Any]]:
    return [
        {
            "name": field,
            "in": "query",
            "required": False,
            "schema": {"type": "string"},
            "description": "Observed in the legacy condition; the type is not asserted.",
        }
        for field in operation.fields
    ]


def write_contract(contract: ApiContract, system: LegacySystem, path: Path) -> Path:
    document = to_openapi(system, contract)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(document, sort_keys=False, allow_unicode=True, width=100),
        encoding="utf-8",
    )
    return path
