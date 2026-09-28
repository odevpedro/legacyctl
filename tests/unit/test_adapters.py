from __future__ import annotations

import pytest

from legacyctl.adapters import available_adapters, load_adapter
from legacyctl.domain.models import BusinessFlow
from legacyctl.rules.extractor import compose_context


def test_both_adapters_ship_a_skill_file() -> None:
    assert available_adapters() == ["cobol", "vb6"]


@pytest.mark.parametrize("language", ["vb6", "cobol"])
def test_skill_and_schema_parse(language: str) -> None:
    adapter = load_adapter(language)
    assert adapter is not None
    assert adapter.language == language
    assert adapter.skill.lstrip().startswith("---"), "SKILL.md needs YAML front matter"
    assert adapter.schema["language"] == language
    assert adapter.schema["node_types"], "a schema must declare node types"


def test_vb6_is_implemented_and_cobol_is_not() -> None:
    """A documented adapter that has no parser must not look runnable."""
    assert load_adapter("vb6").implemented is True
    assert load_adapter("cobol").implemented is False


def test_cobol_declares_no_fallback_parser() -> None:
    """Without a parser a COBOL adapter must not run, rather than guess."""
    fallback = load_adapter("cobol").schema["fallback_parser"]
    assert fallback["value"] == "none"


def test_unknown_language_has_no_guidance() -> None:
    assert load_adapter("fortran") is None


def test_the_vb6_skill_names_every_required_heuristic() -> None:
    """The specification's five required VB6 cautions must all be present."""
    skill = load_adapter("vb6").skill
    for required in (
        "Option Explicit",
        "On Error Resume Next",
        "&",
        "serial",
        "OUT params",
    ):
        assert required.lower() in skill.lower(), required


def test_context_carries_the_vb6_skill(vb6_system, vb6_graph) -> None:  # type: ignore[no-untyped-def]
    """The skill travels with the slice, per the specification."""
    from legacyctl.slicing import FlowSlicer

    result = FlowSlicer(vb6_system, vb6_graph).slice("CustomerForm.ValidateCustomer", 2)
    context = compose_context(result.flow, list(result.fragments))
    adapter = context["adapter"]
    assert adapter["guidance_loaded"] is True
    assert adapter["implemented"] is True
    assert "Option Explicit" in adapter["skill"]
    assert adapter["schema"]["language"] == "vb6"


def test_missing_guidance_is_reported_not_invented() -> None:
    """A language with no SKILL.md must not get a plausible default."""
    context = compose_context(_fake_flow(), [], language="fortran")
    adapter = context["adapter"]
    assert adapter["guidance_loaded"] is False
    assert "do not assume" in adapter["notes"]
    assert "skill" not in adapter


def _fake_flow() -> BusinessFlow:
    return BusinessFlow(
        id="FLOW-X",
        name="x",
        entry_point_id="EP-X",
        entry_label="X",
        depth=1,
        node_ids=["N1"],
        edge_ids=[],
        procedure_ids=[],
        sql_ids=[],
        database_object_ids=[],
    )
