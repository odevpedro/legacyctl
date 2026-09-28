"""LLM integration for the rule-extraction phase.

Only the extraction step is delegated to a model, and only ever a *slice*.
Nothing else in the pipeline calls out to a network.
"""

from .provider import (
    DeterministicRuleExtractor,
    ExtractionResult,
    OpenAICompatibleExtractor,
    RuleExtractorPort,
    build_extractor,
    load_prompt_template,
    parse_rule_payload,
    render_prompt,
)

__all__ = [
    "DeterministicRuleExtractor",
    "ExtractionResult",
    "OpenAICompatibleExtractor",
    "RuleExtractorPort",
    "build_extractor",
    "load_prompt_template",
    "parse_rule_payload",
    "render_prompt",
]
