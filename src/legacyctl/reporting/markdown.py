"""Reports a person can read: the pipeline summary and the divergence report.

Two documents, both Markdown, both written for a reviewer deciding what to
approve next. Neither is a dump: a report that lists everything equally tells
the reader nothing, so what a section says is ordered by what needs a human.

The traceability section is the point of the whole tool. A reader must be able
to go from a generated endpoint to the validated rule to the legacy file and
line, and every row in the tables below carries those keys for that reason.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from ..domain.enums import DivergenceStatus, RuleStatus, VerifyStatus
from ..domain.models import (
    BusinessRule,
    DivergenceRecord,
    DivergenceReport,
    GoldenMasterCase,
    LegacySystem,
    SystemGraph,
    VerifyResult,
)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return "_none_\n"
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return "\n".join(lines) + "\n"


def _yes(value: bool) -> str:
    return "yes" if value else "no"


def render_divergence_report(report: DivergenceReport) -> str:
    """One block per divergence, always naming rule, flow and source.

    A divergence that cannot be traced back to a rule and a legacy line is not
    actionable, so those three fields are part of the template rather than an
    optional extra.
    """
    lines = [
        "# Divergence Report",
        "",
        f"- System: `{report.system_id}`",
        f"- Generated: {report.generated_at or _now()}",
        f"- Divergences: {len(report.records)} ({report.blocking} blocking)",
    ]
    lines.extend(f"- {note}" for note in report.notes)
    lines.append("")

    if not report.records:
        lines.append("No divergence was detected between the Golden Master and the new system.")
        lines.append("")
        lines.append(
            "This is not a claim that the systems agree everywhere: it means the "
            "curated cases were all decidable and all matched."
        )
        return "\n".join(lines) + "\n"

    for record in report.records:
        lines.extend(
            [
                f"## {record.id}",
                "",
                "## Rule",
                "",
                f"`{record.rule_id or 'unattributed'}`",
                "",
                "## Flow",
                "",
                f"`{record.flow_id or 'unattributed'}`",
                "",
                "## Legacy",
                "",
                f"```\n{record.legacy or 'not recorded'}\n```",
                "",
                "## New System",
                "",
                f"```\n{record.new_system or 'not produced'}\n```",
                "",
                "## Source",
                "",
                f"`{record.source or 'no source recorded'}`",
                "",
                "## Status",
                "",
                f"`{record.status.value}`"
                + ("" if record.blocking else " (advisory: not blocking)"),
                "",
            ]
        )
        if record.summary:
            lines.extend(["> " + record.summary.replace("\n", "\n> "), ""])
    return "\n".join(lines) + "\n"


def render_verification_table(results: list[VerifyResult]) -> str:
    rows = [
        [
            result.case_id,
            f"`{result.flow_id or '-'}`",
            result.status.value,
            _yes(bool(result.matches)),
            str(len(result.divergences)),
            (result.detail or "-").split(";")[0],
        ]
        for result in results
    ]
    return _table(["Case", "Flow", "Status", "Matches", "Divergences", "Note"], rows)


def render_rule_table(rules: list[BusinessRule]) -> str:
    rows = [
        [
            f"`{rule.id}`",
            rule.status.value,
            rule.confidence.value,
            rule.name,
            f"`{rule.sources[0].file}:{rule.sources[0].line}`"
            if rule.sources and rule.sources[0].file
            else "-",
        ]
        for rule in sorted(rules, key=lambda r: (r.status.value, r.id))
    ]
    return _table(["Rule", "Status", "Confidence", "Name", "Source"], rows)


def render_graph_section(system: LegacySystem, graph: SystemGraph) -> str:
    clusters = [c for c in graph.clusters if c.node_ids]
    hubs = graph.hubs
    lines = [
        "## Structural Metrics",
        "",
        _table(
            ["Metric", "Value"],
            [
                ["Components", str(len(system.components))],
                ["Procedures", str(len(system.procedures))],
                ["SQL statements", str(len(system.sql_statements))],
                ["Database objects", str(len(system.database_objects))],
                ["Nodes", str(len(graph.nodes))],
                ["Edges", str(len(graph.edges))],
            ],
        ),
        "",
        "## Graph Metrics",
        "",
        _table(
            ["Metric", "Value"],
            [
                ["Algorithm", graph.algorithm],
                ["Clusters", str(len(clusters))],
                ["Largest cluster", f"{max((c.size for c in clusters), default=0)} node(s)"],
                [
                    "Largest share of system",
                    f"{max((c.share_of_system for c in clusters), default=0.0):.1%}",
                ],
                ["Hubs", str(len(hubs))],
                ["Flows", str(len(graph.flows))],
            ],
        ),
    ]
    if graph.warnings:
        lines.extend(["", "### Graph warnings", ""])
        lines.extend(f"- {warning}" for warning in graph.warnings)
    lines.extend(
        [
            "",
            "## Hubs",
            "",
            _table(
                ["Hub", "Node", "Degree", "Flows through", "Why it is a hub"],
                [
                    [
                        f"`{h.id}`",
                        h.name,
                        str(h.degree),
                        str(len(h.flows_through)),
                        h.reason or "-",
                    ]
                    for h in hubs
                ],
            ),
        ]
    )
    return "\n".join(lines) + "\n"


def render_legacy_report(
    system: LegacySystem,
    graph: SystemGraph,
    rules: list[BusinessRule],
    results: list[VerifyResult],
    divergences: DivergenceReport | None,
    cases: list[GoldenMasterCase],
    contract_summary: str = "not generated",
    codegen_summary: str = "not run",
    notes: list[str] | None = None,
) -> str:
    """The single ``legacy-report.md`` a reviewer reads.

    Sections appear even when empty, with an explicit "not run" instead of
    silence: a reader must be able to tell the difference between "there was
    nothing to report" and "this stage was skipped".
    """
    validated = [r for r in rules if r.status is RuleStatus.VALIDATED]
    candidates = [r for r in rules if r.status is RuleStatus.CANDIDATE]
    failed = [r for r in results if r.status is VerifyStatus.FAILED]
    skipped = [r for r in results if r.status is VerifyStatus.SKIPPED]

    system_id = system.id
    lines = [
        "# Legacy Refactoring Report",
        "",
        f"- System: **{system.name}** (`{system_id}`)",
        f"- Generated: {_now()}",
        "",
        "## System Overview",
        "",
        _table(
            ["Attribute", "Value"],
            [
                ["Name", system.name],
                ["Id", f"`{system_id}`"],
                ["Source files", str(len(system.files))],
                ["Calls", str(len(system.calls))],
                ["Adapter", system.adapter or "-"],
            ],
        ),
        "",
        render_graph_section(system, graph),
        "",
        "## Extracted Rules",
        "",
        f"{len(rules)} rule(s) extracted: {len(validated)} validated, {len(candidates)} candidate.",
        "",
        render_rule_table(rules),
        "",
        "## Validation Status",
        "",
        "A rule reaches the contract only through human review. Candidates below are "
        "excluded from the API contract until someone approves them.",
        "",
        _table(
            ["Status", "Count"],
            [
                [RuleStatus.VALIDATED.value, str(len(validated))],
                [RuleStatus.CANDIDATE.value, str(len(candidates))],
            ],
        ),
        "",
        "## API Contracts",
        "",
        f"{contract_summary}",
        "",
        "## Generated Code",
        "",
        f"{codegen_summary}",
        "",
        "## Verification Results",
        "",
        f"{len(cases)} curated case(s); {len(results)} compared; "
        f"{len(failed)} diverged, {len(skipped)} not decidable from the evidence.",
        "",
        render_verification_table(results),
        "",
        "## Divergences",
        "",
    ]

    if divergences is None:
        lines.append("No divergence report was produced: verification did not run.")
    elif not divergences.records:
        lines.append(f"Verification ran against {len(results)} case(s) and found no divergence.")
        if skipped:
            lines.append("")
            lines.append(
                f"{len(skipped)} case(s) could not be decided from the recorded evidence, "
                f"so this is a narrower result than it looks. They need a real legacy run "
                f"or a more precise rule."
            )
    else:
        lines.append(f"{len(divergences.records)} divergence(s), {divergences.blocking} blocking.")
        lines.append("")
        lines.append(
            _table(
                ["Id", "Rule", "Flow", "Status", "Summary", "Source"],
                [
                    [
                        f"`{r.id}`",
                        f"`{r.rule_id or '-'}`",
                        f"`{r.flow_id or '-'}`",
                        r.status.value + ("" if r.blocking else " (advisory)"),
                        r.summary,
                        f"`{r.source or '-'}`",
                    ]
                    for r in divergences.records
                ],
            )
        )
        if DivergenceStatus.REQUIRES_REVIEW.value in divergences.by_status:
            lines.append("")
            lines.append(
                "Each divergence above closes the loop back to its rule: fix the rule or "
                "the implementation, re-verify, and the difference either disappears or "
                "becomes a reviewed, recorded decision."
            )

    lines.extend(["", "## Traceability", ""])
    lines.append("Rule to legacy source, and the endpoint that carries the rule.")
    lines.append("")
    lines.append(
        _table(
            ["Rule", "Status", "Legacy source", "Endpoint"],
            [
                [
                    f"`{rule.id}`",
                    rule.status.value,
                    f"`{rule.sources[0].file}:{rule.sources[0].line}`"
                    if rule.sources and rule.sources[0].file
                    else "-",
                    "yes" if rule.status is RuleStatus.VALIDATED else "not in contract",
                ]
                for rule in sorted(rules, key=lambda r: r.id)
            ],
        )
    )

    if notes:
        lines.extend(["", "## Notes", ""])
        lines.extend(f"- {note}" for note in notes)
    return "\n".join(lines) + "\n"


def write_divergence_report(report: DivergenceReport, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_divergence_report(report), encoding="utf-8")
    return path


def write_legacy_report(text: str, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def summarise(records: list[DivergenceRecord]) -> str:
    if not records:
        return "no divergences"
    counts: dict[str, int] = {}
    for record in records:
        counts[record.status.value] = counts.get(record.status.value, 0) + 1
    return ", ".join(f"{count} {status}" for status, count in sorted(counts.items()))
