"""Per-language guidance loaded from ``adapters/<lang>/``.

Each adapter directory holds a ``SKILL.md`` (how to read this language without
inventing behaviour) and a ``schema.yaml`` (which node and edge types the
adapter may emit). The specification says the SKILL text is injected into the
``RuleExtractor`` alongside the slice, so the language's own warnings travel
with the code being read.

Nothing here is a runtime parser. If a language's ``SKILL.md`` is missing, the
guidance is simply absent and the run says so — it does not invent a default,
because a wrong default would steer rule extraction in a direction no one
approved.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

#: Project root: the directory that holds ``src/`` and ``adapters/``.
#: ``__file__`` is ``<root>/src/legacyctl/adapters/__init__.py``, so the root is
#: three levels up. A source checkout is assumed; an installed wheel has no
#: ``adapters/`` tree and degrades to "no guidance" (see ``load_adapter``).
_ROOT = Path(__file__).resolve().parents[3]
_ADAPTERS_DIR = _ROOT / "adapters"


@dataclass(frozen=True, slots=True)
class AdapterGuidance:
    """The read-only guidance for one language."""

    language: str
    skill: str
    schema: dict[str, Any]
    path: Path

    @property
    def implemented(self) -> bool:
        """False when the adapter is documented but has no parser yet.

        A ``planned`` schema is still loaded: it documents intent, and the
        distinction between "no guidance" and "guidance for an adapter that
        does not run yet" is worth keeping in the context.
        """
        status: object = self.schema.get("status", "active")
        return status != "planned"


def adapter_dir(language: str) -> Path:
    return _ADAPTERS_DIR / language


def _read_schema(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    return loaded if isinstance(loaded, dict) else {}


def load_adapter(language: str) -> AdapterGuidance | None:
    """Load one adapter's guidance, or ``None`` when it is not present.

    A missing ``SKILL.md`` yields ``None`` rather than a fallback file: the
    extractor then has no language guidance, which is an honest state, instead
    of generic advice presented as if it were the language's.
    """
    directory = adapter_dir(language)
    skill_path = directory / "SKILL.md"
    if not skill_path.is_file():
        return None
    return AdapterGuidance(
        language=language,
        skill=skill_path.read_text(encoding="utf-8"),
        schema=_read_schema(directory / "schema.yaml"),
        path=directory,
    )


def available_adapters() -> list[str]:
    """Every language directory that carries a ``SKILL.md``."""
    if not _ADAPTERS_DIR.is_dir():
        return []
    return sorted(
        entry.name
        for entry in _ADAPTERS_DIR.iterdir()
        if entry.is_dir() and (entry / "SKILL.md").is_file()
    )


__all__ = ["AdapterGuidance", "adapter_dir", "available_adapters", "load_adapter"]
