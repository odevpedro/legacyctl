"""Runtime configuration and filesystem layout.

All paths are relative to the project root by default, but every one of them is
overridable through environment variables prefixed with ``LEGACYCTL_`` so the
CLI never hard-codes a location.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_PATH_DEFAULTS: tuple[tuple[str, str], ...] = (
    ("db_path", "output/legacy.db"),
    ("ast_dir", "var/ast"),
    ("output_dir", "output"),
    ("catalog_path", "catalog/rules.yaml"),
    ("contract_path", "contracts/openapi.yaml"),
    ("generated_dir", "generated"),
    ("adapters_dir", "adapters"),
    ("golden_master_dir", "verification/golden-master"),
    ("report_path", "output/legacy-report.md"),
    ("divergence_path", "output/divergence-report.md"),
    ("demo_java_dir", "demo-new-system"),
    ("generated_demo_dir", "demo-new-system/generated-api"),
)


def _default_root() -> Path:
    return Path.cwd()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LEGACYCTL_", env_file=".env", extra="ignore", case_sensitive=False
    )

    root: Path = Field(default_factory=_default_root, description="Project root directory")
    db_path: Path = Field(default=Path(), description="SQLite graph database path")
    ast_dir: Path = Field(default=Path(), description="Cache for external parser AST JSON")
    output_dir: Path = Field(default=Path(), description="Pipeline output directory")
    catalog_path: Path = Field(default=Path(), description="Rule catalog YAML path")
    contract_path: Path = Field(default=Path(), description="OpenAPI contract path")
    generated_dir: Path = Field(default=Path(), description="Generated code directory")
    adapters_dir: Path = Field(default=Path(), description="Language adapters directory")
    golden_master_dir: Path = Field(default=Path(), description="Golden Master fixtures")
    report_path: Path = Field(default=Path(), description="Markdown report path")
    divergence_path: Path = Field(default=Path(), description="Divergence report path")

    proleap_jar: Path | None = Field(default=None, description="Path to the ProLeap CLI JAR")
    proleap_timeout: int = Field(default=120, description="ProLeap subprocess timeout (s)")
    proleap_java: str = Field(default="java", description="Java executable for ProLeap")

    openapi_generator_jar: Path | None = Field(
        default=None, description="Path to the openapi-generator-cli JAR"
    )
    openapi_generator_version: str = Field(default="7.10.0", description="Generator version")
    openapi_generator_timeout: int = Field(default=600, description="Generator timeout (s)")

    demo_java_dir: Path = Field(default=Path(), description="Hand written demo Spring Boot app")
    generated_demo_dir: Path = Field(
        default=Path(), description="Where the generated Java surface is copied"
    )

    @model_validator(mode="before")
    @classmethod
    def _apply_path_defaults(cls, data: Any) -> Any:
        """Resolve every output path from ``root`` unless it was set explicitly.

        Done *before* validation so the fields are plain ``Path`` for callers:
        a pipeline stage must never have to handle "this directory might be
        ``None``", and an unset path must never silently become the CWD.
        """
        if not isinstance(data, dict):
            return data
        resolved = dict(data)
        root_value = resolved.get("root")
        root = root_value if isinstance(root_value, Path) else Path(root_value or Path.cwd())
        for name, suffix in _PATH_DEFAULTS:
            value = resolved.get(name)
            if value is None or value == Path():
                resolved[name] = root / suffix
        return resolved

    def ensure_dirs(self) -> None:
        for path in (
            self.output_dir,
            self.ast_dir,
            self.catalog_path.parent,
            self.contract_path.parent,
            self.generated_dir,
            self.report_path.parent,
        ):
            path.mkdir(parents=True, exist_ok=True)

    @property
    def slices_dir(self) -> Path:
        return self.output_dir / "slices"

    @property
    def graphml_path(self) -> Path:
        return self.output_dir / "system.graphml"


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings() -> None:
    """Test hook: force a fresh settings load."""
    global _settings
    _settings = None
