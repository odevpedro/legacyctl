"""Turn one slice into a rule catalog.

This is the *only* place where a model is consulted, and it sees only the
slice. The composed context is sanitized before it leaves the process, the
response is validated against the domain model, and every admitted rule
carries the fragment it came from. A rejected candidate is reported, not
swallowed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..adapters import load_adapter
from ..domain.models import (
    BusinessFlow,
    BusinessRule,
    RuleCatalog,
    RuleSource,
    SourceFragment,
)
from ..llm.provider import (
    RuleExtractorPort,
    build_extractor,
    render_prompt,
)
from ..observability import get_log, timed
from ..security.sanitizer import get_sanitizer
from .catalog import RuleCatalogStore, RuleRejected, build_rule


@dataclass(slots=True)
class RuleExtractionReport:
    flow: BusinessFlow
    rules: list[BusinessRule] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)
    provider: str = "unknown"
    prompt_version: str = "unknown"
    input_token_estimate: int = 0
    output_token_estimate: int = 0
    duration_ms: float = 0.0
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.rejected


def _observed_in(fragment: SourceFragment, observation: str) -> bool:
    """True when the observation points at a line of this fragment."""
    if not fragment.text or not observation:
        return False
    _, _, tail = observation.partition(": ")
    needle = (tail or observation).strip()
    return any(needle and needle in line for line in fragment.text.splitlines())


def _candidate_key(candidate: dict[str, Any]) -> tuple[str, str]:
    """Stable ordering key: name first, then the first observation."""
    observations = candidate.get("observation") or []
    first = str(observations[0]) if observations else ""
    return (str(candidate.get("name", "")).strip().lower(), first)


def estimate_tokens(text: str) -> int:
    """~4 characters per token, the usual English/code rule of thumb.

    It is an *estimate* and is labelled as one everywhere it is reported: the
    point is to compare a slice against the whole system, not to bill anyone.
    """
    return max(1, len(text) // 4)


def _adapter_guidance(language: str) -> dict[str, Any]:
    """Attach the language's own ``SKILL.md`` to the context.

    The specification puts the skill text next to the slice: the parser already
    knows what the code *says*, and the skill says what must not be assumed
    about it. Absence is reported rather than filled in.
    """
    adapter = load_adapter(language)
    if adapter is None:
        return {
            "language": language,
            "guidance_loaded": False,
            "notes": (
                f"no adapters/{language}/SKILL.md found; extract only from the "
                "fragments and do not assume language-specific behaviour"
            ),
        }
    return {
        "language": adapter.language,
        "guidance_loaded": True,
        "implemented": adapter.implemented,
        "skill": adapter.skill,
        "schema": adapter.schema,
    }


def compose_context(
    flow: BusinessFlow, fragments: list[SourceFragment], language: str = "vb6"
) -> dict[str, Any]:
    """The complete, sanitized payload handed to the extractor.

    Only slice data: identifiers, provenance, redacted source. No other
    component, no other flow, no file outside the slice.
    """
    sanitizer = get_sanitizer()
    return {
        "flow": {
            "id": flow.id,
            "name": flow.name,
            "entry_point_id": flow.entry_point_id,
            "entry_label": flow.entry_label,
            "depth": flow.depth,
            "truncated": flow.truncated,
        },
        "adapter": _adapter_guidance(language),
        "procedures": list(flow.procedure_ids),
        "sql_statements": list(flow.sql_ids),
        "database_objects": list(flow.database_object_ids),
        "hubs": list(flow.hub_references),
        "source_fragments": [
            {
                "id": fragment.id,
                "procedure": fragment.procedure_id,
                "file": fragment.file,
                "start_line": fragment.start_line,
                "end_line": fragment.end_line,
                "text": sanitizer.sanitize(fragment.text),
            }
            for fragment in fragments
        ],
    }


class RuleExtractor:
    """Runs one slice through the port and into the catalog."""

    def __init__(
        self,
        store: RuleCatalogStore,
        provider: RuleExtractorPort | None = None,
    ) -> None:
        self._store = store
        self._provider = provider or build_extractor()
        self._log = get_log()

    def extract(
        self,
        flow: BusinessFlow,
        fragments: list[SourceFragment],
        persist: bool = True,
    ) -> RuleExtractionReport:
        context = compose_context(flow, fragments)
        prompt = render_prompt(context)
        report = RuleExtractionReport(flow=flow, provider=self._provider.name)

        with timed() as duration:
            raw_candidates = self._provider.extract(prompt, context)
        report.duration_ms = duration
        report.input_token_estimate = estimate_tokens(prompt)
        report.output_token_estimate = estimate_tokens(
            " ".join(str(c.get("name", "")) for c in raw_candidates)
        )

        domain_key = self._domain_key(flow)
        rules: list[BusinessRule] = []
        rejected: list[tuple[str, str]] = []
        # Candidates are ordered by their own content before ids are minted, so
        # the same slice always yields the same rule ids. A catalog whose ids
        # moved between runs could not be reviewed or diffed.
        for ordinal, candidate in enumerate(sorted(raw_candidates, key=_candidate_key), start=1):
            label = str(candidate.get("name", "<unnamed>"))
            try:
                rule = build_rule(
                    candidate,
                    flow_id=flow.id,
                    entry_point_id=flow.entry_point_id,
                    domain_key=domain_key,
                    ordinal=ordinal,
                    source=self._source_for(candidate, fragments),
                )
            except RuleRejected as error:
                rejected.append((label, str(error)))
                continue
            rules.append(rule)

        report.rules = rules
        report.rejected = rejected
        report.warnings = [f"{name}: {reason}" for name, reason in rejected]

        with self._log.stage(
            "extract-rules",
            system_id=self._store.system_id,
            flow_id=flow.id,
            provider=self._provider.name,
            prompt_version=report.prompt_version,
            input_token_estimate=report.input_token_estimate,
            output_token_estimate=report.output_token_estimate,
        ) as record:
            record.duration_ms = report.duration_ms
            record.details["rules"] = len(rules)
            record.details["rejected"] = len(rejected)

        if persist and rules:
            self._store.add(rules)
        return report

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _source_for(candidate: dict[str, Any], fragments: list[SourceFragment]) -> RuleSource:
        """Point the evidence at the fragment it was actually read from.

        Defaulting every rule to the first fragment of the slice would make the
        provenance wrong in a way a reviewer could not see.
        """
        observations = [str(o) for o in candidate.get("observation") or []]
        chosen: SourceFragment | None = None
        for fragment in fragments:
            for observation in observations:
                if _observed_in(fragment, observation):
                    chosen = fragment
                    break
            if chosen is not None:
                break
        if chosen is None:
            chosen = fragments[0] if fragments else None
        return RuleSource(
            file=chosen.file if chosen else "",
            procedure=chosen.procedure_id if chosen else None,
            line=chosen.start_line if chosen else 0,
            fragment_id=chosen.id if chosen else None,
            sql_statement_id=None,
        )

    @staticmethod
    def _domain_key(flow: BusinessFlow) -> str:
        """Hub rules are namespaced by hub, flow rules by the flow id."""
        if flow.hub_references and not flow.procedure_ids:
            return f"HUB-{flow.hub_references[0]}"
        return flow.id.replace("FLOW-", "")


def load_catalog(store: RuleCatalogStore) -> RuleCatalog:
    return store.load()


def default_store(output_dir: Path, system_id: str) -> RuleCatalogStore:
    return RuleCatalogStore(output_dir / "catalog" / f"{system_id.lower()}-rules.yaml", system_id)
