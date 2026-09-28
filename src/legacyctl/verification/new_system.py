"""Ports that answer "what does the new system do for this case?".

The generated code is a contract surface, not a deployed service: the MVP does
not stand up a Spring Boot application. So the default implementation is a
deterministic evaluator of the *validated rules*, clearly labelled as a
simulation, and a real HTTP port is available for when a service does exist.

The distinction matters and is never blurred: a simulation says so, so a
divergence report can tell a reader whether they are looking at a real
behaviour comparison or a rule-level simulation.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from ..domain.models import BusinessRule, GoldenMasterCase

#: Operands we are willing to coerce when comparing. Anything else stays a
#: string and is reported as such rather than silently parsed.
_NUMERIC = re.compile(r"^-?\d+(\.\d+)?$")

#: A bare name: a field reference, never a literal.
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")

#: A single quoted literal that encloses the whole operand. ``"a" Or b = "c"``
#: must not match: it merely starts and ends with a quote.
_QUOTED = re.compile(r"""^"([^"]*)"$|^'([^']*)'$""")


@runtime_checkable
class NewSystemPort(Protocol):
    """Anything able to answer a Golden Master case against the new system."""

    name: str
    #: True when the answer came from a real running system.
    is_real: bool

    def evaluate(self, case: GoldenMasterCase, rules: list[BusinessRule]) -> dict[str, str]: ...


def coerce(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


@dataclass(slots=True)
class RuleSimulator:
    """Evaluates validated rule conditions against a case's inputs.

    Deliberately dumb: it recognises ``field <op> operand`` and nothing else.
    When a rule's condition cannot be evaluated it returns ``unknown`` for the
    outcome rather than guessing, so an unevaluatable rule surfaces as a
    divergence to be explained instead of a silent pass.

    Outcomes are the ones the rules actually declare. A rule that only guards
    -- ``requestedCredit > creditLimit`` with no recorded behaviour -- reports
    ``fired`` or ``not_fired``; it never claims a customer was accepted or
    rejected, because nothing in the legacy evidence says which.
    """

    name: str = "rule-simulator"
    is_real: bool = False

    _COMPARISON = re.compile(
        r"^\s*(?P<field>[A-Za-z_][A-Za-z0-9_.]*)\s*(?P<op>>=|<=|<>|=|>|<)\s*(?P<value>.+?)\s*$"
    )

    def evaluate(self, case: GoldenMasterCase, rules: list[BusinessRule]) -> dict[str, str]:
        result: dict[str, str] = {}
        for rule in rules:
            if case.flow_id and rule.flow_id and rule.flow_id != case.flow_id:
                continue
            result[rule.id] = self._decide(rule, case.inputs)
        return result

    def _decide(self, rule: BusinessRule, inputs: dict[str, str]) -> str:
        match = self._COMPARISON.match(rule.condition.expression or "")
        if not match:
            # A VB6 call such as ``Validation.IsBlank(parametros)`` is a guard
            # this simulator does not understand. Reporting the rule's declared
            # behavior anyway would pass every case regardless of the input.
            return "unknown"
        left = inputs.get(match.group("field"))
        right = _resolve(match.group("value"), inputs)
        if left is None or right is None:
            return "unknown"
        verdict = _compare(left, match.group("op"), right)
        if verdict is None:
            return "unknown"
        if verdict:
            return self._declared_outcome(rule) or "fired"
        # The condition did not hold, so the rule never spoke about this input.
        return "not_fired"

    @staticmethod
    def _declared_outcome(rule: BusinessRule) -> str | None:
        """The outcome the rule itself declares, or None when it declares none.

        A guard like ``requestedCredit > creditLimit`` says *when* something
        happens, not *what* happens; the extractor records that as an unknown
        behavior. Returning "accept" or "reject" there would be the simulator
        inventing a business decision nobody recorded.
        """
        if rule.behavior.type in ("accept", "reject", "transition"):
            return rule.behavior.type
        return None


def _resolve(operand: str, inputs: dict[str, str]) -> str | None:
    """Resolve the right-hand side of a comparison to a single concrete value.

    Returns None whenever the operand is not one value: a bare identifier is a
    *field reference* (``requestedCredit > creditLimit`` reads the limit from
    the inputs), while a quoted or numeric operand is a literal. A compound
    operand -- ``cpf = "0" Or cpf = "1"``, ``IsValid(a, b)`` -- is None, because
    evaluating half of it and reporting the answer would be a guess dressed up
    as a check.
    """
    text = operand.strip()
    quoted = _QUOTED.match(text)
    if quoted:
        return quoted.group(1) if quoted.group(1) is not None else quoted.group(2)
    if _NUMERIC.match(text):
        return text
    if _IDENTIFIER.fullmatch(text):
        return inputs.get(text)
    return None


def _compare(left: str, op: str, right: str) -> bool | None:
    """Compare two operands, coercing to numbers when both sides look numeric."""
    right = right.strip().strip("\"'")
    if _NUMERIC.match(left.strip()) and _NUMERIC.match(right):
        a: Any = float(left)
        b: Any = float(right)
    else:
        a, b = left.strip(), right
    try:
        match op:
            case ">=":
                return bool(a >= b)
            case "<=":
                return bool(a <= b)
            case "<>":
                return bool(a != b)
            case "=":
                return bool(a == b)
            case ">":
                return bool(a > b)
            case "<":
                return bool(a < b)
    except TypeError:
        return None
    return None


@dataclass(slots=True)
class HttpNewSystem:
    """Calls a deployed new system over HTTP.

    Selected explicitly; the base URL is configuration, never a guess. A
    failure raises with the reason so the verifier can record it as a
    divergence, rather than being swallowed into a pass.
    """

    name: str = "http"
    is_real: bool = True
    timeout: float = 10.0
    base_url: str = ""
    _client: Any = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not self.base_url:
            self.base_url = os.environ.get("LEGACYCTL_NEW_SYSTEM_URL", "")

    def evaluate(self, case: GoldenMasterCase, rules: list[BusinessRule]) -> dict[str, str]:
        import httpx

        if not self.base_url:
            raise RuntimeError(
                "no new system URL: set LEGACYCTL_NEW_SYSTEM_URL or use the simulator"
            )
        url = f"{self.base_url.rstrip('/')}/verify"
        response = httpx.post(
            url,
            json={"case_id": case.id, "flow_id": case.flow_id, "inputs": case.inputs},
            timeout=self.timeout,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"new system returned HTTP {response.status_code}")
        payload = response.json() or {}
        return {str(key): coerce(value) for key, value in payload.items()}


def build_new_system(provider: str | None = None) -> NewSystemPort:
    choice = (provider or os.environ.get("LEGACYCTL_NEW_SYSTEM") or "simulator").lower()
    if choice in ("simulator", "simulation", "rules"):
        return RuleSimulator()
    if choice in ("http", "deployed"):
        return HttpNewSystem()
    raise ValueError(f"unknown new-system provider {choice!r}")
