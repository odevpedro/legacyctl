"""Human-readable reports: the pipeline summary and the divergence report."""

from .markdown import (
    render_divergence_report,
    render_graph_section,
    render_legacy_report,
    render_rule_table,
    render_verification_table,
    summarise,
    write_divergence_report,
    write_legacy_report,
)

__all__ = [
    "render_divergence_report",
    "render_graph_section",
    "render_legacy_report",
    "render_rule_table",
    "render_verification_table",
    "summarise",
    "write_divergence_report",
    "write_legacy_report",
]
