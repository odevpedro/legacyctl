"""The port the rule-extraction phase talks to.

The pipeline must be runnable end to end on a machine with no API key and no
network, and it must be *reproducible* when it claims to be. So the port is
deliberately tiny and the default implementation is a deterministic reader
rather than a model: it only ever reports what the slice text literally
contains. A hosted model is an optional, explicit choice.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

PROMPT_TEMPLATE = "rule_extraction.md"

_FENCE = re.compile(r"```(?:json)?\s*(?P<body>.*?)```", re.DOTALL)
_ARRAY = re.compile(r"\[.*\]", re.DOTALL)


@runtime_checkable
class RuleExtractorPort(Protocol):
    """Anything able to turn a rendered slice into raw rule candidates."""

    name: str

    def extract(self, prompt: str, context: dict[str, Any]) -> list[dict[str, Any]]: ...


@dataclass(slots=True)
class ExtractionResult:
    rules: list[dict[str, Any]] = field(default_factory=list)
    provider: str = "unknown"
    raw: str = ""
    error: str | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.error is None


def load_prompt_template() -> str:
    from pathlib import Path

    path = Path(__file__).parent / "prompts" / PROMPT_TEMPLATE
    return path.read_text(encoding="utf-8")


def render_prompt(context: dict[str, Any]) -> str:
    """Substitute the slice into the prompt.

    The template uses ``{slice_context}`` and nothing else, so a stray brace in
    the VB6 source can never be mistaken for a format field.
    """
    payload = json.dumps(context, indent=2, ensure_ascii=False, sort_keys=True)
    return load_prompt_template().replace("{slice_context}", payload)


def parse_rule_payload(raw: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Tolerant JSON recovery.

    Models wrap JSON in prose and fences. Refusing the whole response because
    of a fence would lose real findings; accepting prose silently would put
    text into the catalog. So: extract the array, then validate each element.
    """
    warnings: list[str] = []
    text = raw.strip()
    if not text:
        return [], ["provider returned an empty response"]
    fenced = _FENCE.search(text)
    if fenced:
        text = fenced.group("body").strip()
    if not text.startswith("["):
        array = _ARRAY.search(text)
        if array:
            warnings.append("response was not a bare JSON array; recovered the array literal")
            text = array.group(0)
        else:
            return [], ["response contained no JSON array"]
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as error:
        return [], [f"response is not valid JSON: {error}"]
    if not isinstance(parsed, list):
        return [], ["response JSON was not an array"]
    rules: list[dict[str, Any]] = []
    for index, item in enumerate(parsed):
        if not isinstance(item, dict):
            warnings.append(f"element {index} is not an object; dropped")
            continue
        if not str(item.get("name", "")).strip():
            warnings.append(f"element {index} has no name; dropped")
            continue
        rules.append(item)
    return rules, warnings


class DeterministicRuleExtractor:
    """Offline default: reads decisions out of the slice, no model involved.

    It is intentionally conservative. It recognises the decision shapes the
    fixtures actually contain -- comparisons, negative validation branches and
    state writes -- and it marks everything it cannot read as an inference.
    It exists so that ``legacyctl extract-rules`` is meaningful, reproducible
    and testable without a key, not so that it can replace a model.
    """

    name = "deterministic"

    _CONDITION = re.compile(r"^\s*If\s+(?P<expr>.+?)\s+Then\s*$", re.IGNORECASE | re.DOTALL)
    _NEGATIVE = re.compile(r"^\s*Not\s+(?P<expr>.+)$", re.IGNORECASE)
    _COMPARISON = re.compile(
        r"(?P<field>[A-Za-z_][A-Za-z0-9_.]*)\s*(?P<op>>=|<=|<>|=|>|<)\s*(?P<value>.+?)\s*$"
    )
    _STATE_WRITE = re.compile(
        r'^\s*(?:SetState|State)\s+[A-Za-z0-9_."\']*\s*,\s*"(?P<value>[^"]+)"',
        re.IGNORECASE,
    )
    _VALIDATOR = re.compile(
        r"(?P<module>[A-Za-z_][A-Za-z0-9_]*)\.(?P<name>Is[A-Z][A-Za-z0-9_]*)\s*\((?P<args>[^)]*)\)",
    )

    def extract(self, prompt: str, context: dict[str, Any]) -> list[dict[str, Any]]:
        fragments = context.get("source_fragments") or []
        state_values: list[str] = []
        rules: list[dict[str, Any]] = []

        for fragment in fragments:
            text = str(fragment.get("text", ""))
            procedure = str(fragment.get("procedure", "")) or str(fragment.get("id", ""))
            self._collect_states(text, state_values)
            for candidate in self._decisions_in(text, procedure):
                rules.append(candidate)

        for value in sorted(set(state_values)):
            rules.append(
                {
                    "name": f"Set state to {value}",
                    "description": f"The flow assigns the state {value}.",
                    "condition": {"type": "state", "expression": None, "fields": []},
                    "behavior": {
                        "type": "transition",
                        "reason": "Assigned directly in the legacy source.",
                        "target": value,
                    },
                    "observation": [f"state written: {value}"],
                    "inference": [],
                }
            )
        logger.info(
            "deterministic extraction produced %d candidate(s) from %d fragment(s)",
            len(rules),
            len(fragments),
        )
        return rules

    def _collect_states(self, text: str, sink: list[str]) -> None:
        for line in text.splitlines():
            match = self._STATE_WRITE.match(line)
            if match:
                sink.append(match.group("value"))

    def _decisions_in(self, text: str, procedure: str) -> list[dict[str, Any]]:
        lines = text.splitlines()
        rules: list[dict[str, Any]] = []
        for index, line in enumerate(lines):
            stripped = line.strip()
            if stripped.lower().startswith("end if"):
                continue
            condition = self._CONDITION.match(stripped)
            if not condition:
                continue
            expression = condition.group("expr")
            outcome = self._outcome_after(lines, index)
            negative = self._NEGATIVE.match(expression)
            validator = self._VALIDATOR.search(expression)
            if validator:
                rules.append(
                    self._validator_rule(validator, negative is not None, outcome, procedure, line)
                )
                continue
            comparison = self._COMPARISON.match(expression)
            if comparison and outcome:
                rules.append(self._comparison_rule(comparison, outcome, procedure, line))
        return rules

    def _outcome_after(self, lines: list[str], index: int) -> str:
        for following in lines[index + 1 : index + 8]:
            candidate = following.strip()
            if not candidate or candidate.lower().startswith("end if"):
                continue
            if candidate.lower().startswith("exit"):
                return "reject"
            if candidate.lower().startswith("debug"):
                continue
            return "branch"
        return "branch"

    def _validator_rule(
        self,
        match: re.Match[str],
        negated: bool,
        outcome: str,
        procedure: str,
        line: str,
    ) -> dict[str, Any]:
        name = match.group("name")
        args = [a.strip() for a in match.group("args").split(",") if a.strip()]
        verb = "Reject" if negated else "Accept"
        return {
            "name": f"{verb} when {name} is {self._verdict(negated)}",
            "description": f"{name} is called from {procedure or 'the flow'}.",
            "condition": {
                "type": "input",
                "expression": f"{match.group('module')}.{name}({', '.join(args)})",
                "fields": args,
            },
            "behavior": {
                "type": "reject" if negated else "accept",
                "reason": None,
                "target": None,
            },
            "observation": [f"{procedure}: {line.strip()}" if procedure else line.strip()],
            "inference": [
                f"'{name}' is treated as a validator; the slice does not state the reason."
            ]
            if outcome in ("branch", "reject")
            else [],
        }

    def _comparison_rule(
        self,
        match: re.Match[str],
        outcome: str,
        procedure: str,
        line: str,
    ) -> dict[str, Any]:
        field = match.group("field")
        operator = match.group("op")
        value = match.group("value").strip()
        return {
            "name": f"Branch when {field} {operator} {value}",
            "description": f"{field} is compared against {value} in {procedure or 'the flow'}.",
            "condition": {
                "type": "data",
                "expression": f"{field} {operator} {value}",
                "fields": [field],
            },
            "behavior": {"type": "unknown", "reason": None, "target": None},
            "observation": [f"{procedure}: {line.strip()}" if procedure else line.strip()],
            "inference": [
                f"the slice shows the comparison but not which side is the rejection "
                f"(outcome detected as '{outcome}')"
            ],
        }

    @staticmethod
    def _verdict(negated: bool) -> str:
        return "false" if negated else "true"


class OpenAICompatibleExtractor:
    """Optional hosted model, selected explicitly by the operator.

    Not used by default and not used by the tests: the pipeline has to be
    reproducible without one. A failure here is reported, never swallowed.
    """

    name = "openai-compatible"

    def __init__(
        self,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
    ) -> None:
        self._model = model or os.environ.get("LEGACYCTL_LLM_MODEL", "gpt-4o-mini")
        self._base_url = base_url or os.environ.get("LEGACYCTL_LLM_BASE_URL")
        self._api_key = api_key or os.environ.get("LEGACYCTL_LLM_API_KEY")
        if not self._api_key:
            raise ValueError(
                "no API key: set LEGACYCTL_LLM_API_KEY or use the deterministic extractor"
            )

    def extract(self, prompt: str, context: dict[str, Any]) -> list[dict[str, Any]]:
        from openai import OpenAI  # imported lazily: optional dependency

        client = OpenAI(api_key=self._api_key, base_url=self._base_url)
        response = client.chat.completions.create(
            model=self._model,
            temperature=0,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.choices[0].message.content or ""
        rules, _warnings = parse_rule_payload(raw)
        return rules


def build_extractor(provider: str | None = None) -> RuleExtractorPort:
    """Select the extractor by name; ``None`` means the offline default."""
    choice = (provider or os.environ.get("LEGACYCTL_LLM_PROVIDER") or "deterministic").lower()
    if choice in ("deterministic", "offline", "stub"):
        return DeterministicRuleExtractor()
    if choice in ("openai", "openai-compatible", "hosted"):
        return OpenAICompatibleExtractor()
    raise ValueError(f"unknown LLM provider {choice!r}")
