"""Typed, validated view of the generated contract.

The OpenAPI document is the artifact; these models are what the rest of the
tooling reads, so a malformed operation is caught here rather than by a
generator three stages later.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator


class ContractOperation(BaseModel):
    """One operation, plus the knowledge that produced it."""

    operation_id: str
    summary: str
    description: str = ""
    group: str = "rules"
    method: str = "post"
    path: str
    business_rules: list[str] = Field(default_factory=list)
    fields: list[str] = Field(default_factory=list)
    condition: dict[str, Any] = Field(default_factory=dict)
    behavior: dict[str, Any] = Field(default_factory=dict)
    evidence: list[str] = Field(default_factory=list)
    source_files: list[str] = Field(default_factory=list)

    @field_validator("method")
    @classmethod
    def _known_method(cls, value: str) -> str:
        allowed = {"get", "post", "put", "patch", "delete"}
        if value.lower() not in allowed:
            raise ValueError(f"unsupported HTTP method {value!r}")
        return value.lower()

    @field_validator("business_rules")
    @classmethod
    def _must_cite_a_rule(cls, value: list[str]) -> list[str]:
        """An operation with no rule behind it has no business justification."""
        if not value:
            raise ValueError("every operation must reference at least one business rule")
        return value


class ApiContract(BaseModel):
    title: str
    version: str
    system_id: str
    operations: list[ContractOperation]
    states: list[str] = Field(default_factory=list)
    out_parameter_schemas: list[dict[str, Any]] = Field(default_factory=list)
    rules: list[Any] = Field(default_factory=list)
    skipped_rules: dict[str, int] = Field(default_factory=dict)

    @property
    def rule_ids(self) -> list[str]:
        return sorted({r for op in self.operations for r in op.business_rules})
