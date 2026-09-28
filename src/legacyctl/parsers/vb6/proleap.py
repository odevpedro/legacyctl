"""ProLeap bridge: the reference VB6 parser, invoked as a Java subprocess.

Contract (ADR 008)::

    proleap_cli analyze <arquivo> --output <ast.json>

Rules that the framework depends on:

1. ProLeap is **not** a runtime requirement for unit tests. Tests consume frozen
   ``AST JSON`` fixtures; ProLeap is exercised by the integration suite.
2. If ProLeap fails for a file, the failure is surfaced. The caller degrades to
   the built-in structural fallback and records
   ``component_parse_status=PARTIAL`` plus a diagnostic -- it never invents
   dependencies.
3. Every produced AST is persisted under ``var/ast/<file>.json`` so runs are
   reproducible and debuggable.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .ast import AstDocument, translate_native

CACHE_VERSION = 1


class ProleapError(RuntimeError):
    """Raised when the ProLeap subprocess fails."""


@dataclass(slots=True)
class ProleapResult:
    document: AstDocument
    cached: bool
    used_proleap: bool
    diagnostics: list[str]


class ProleapBridge:
    """Thin, dependency-free subprocess wrapper."""

    def __init__(
        self,
        jar: Path | None,
        *,
        cache_dir: Path,
        java_executable: str = "java",
        timeout: int = 120,
    ) -> None:
        self.jar = jar
        self.cache_dir = cache_dir
        self.java_executable = java_executable
        self.timeout = timeout

    def available(self) -> bool:
        if self.jar is None or not Path(self.jar).is_file():
            return False
        return shutil.which(self.java_executable) is not None

    def cache_path(self, source: Path) -> Path:
        return self.cache_dir / f"{source.name}.json"

    def read_cache(self, source: Path, digest: str) -> AstDocument | None:
        path = self.cache_path(source)
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if payload.get("source_sha256") not in (None, digest):
            return None
        if "document" not in payload:
            return None
        return AstDocument.from_dict(payload["document"])

    def write_cache(self, source: Path, document: AstDocument, digest: str) -> Path:
        path = self.cache_path(source)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "cache_version": CACHE_VERSION,
                    "source": str(source),
                    "source_sha256": digest,
                    "document": document.model_dump(mode="json"),
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        document.write(path.with_name(f"{path.stem}.ast.json"))
        return path

    def analyze(self, source: Path, output: Path) -> AstDocument:
        """Run ``proleap_cli analyze <file> --output <ast.json>``."""
        if self.jar is None:
            raise ProleapError("LEGACYCTL_PROLEAP_JAR is not configured")
        if not Path(self.jar).is_file():
            raise ProleapError(f"ProLeap jar not found: {self.jar}")
        if shutil.which(self.java_executable) is None:
            raise ProleapError(f"java executable not found: {self.java_executable}")

        output.parent.mkdir(parents=True, exist_ok=True)
        command = [
            self.java_executable,
            "-jar",
            str(self.jar),
            "analyze",
            str(source),
            "--output",
            str(output),
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ProleapError(f"proleap timed out after {self.timeout}s: {source.name}") from exc
        except OSError as exc:
            raise ProleapError(f"cannot spawn java: {exc}") from exc

        if completed.returncode != 0:
            stderr = (completed.stderr or completed.stdout or "").strip()[:500]
            raise ProleapError(f"proleap exit={completed.returncode}: {stderr}")
        if not output.is_file():
            raise ProleapError(f"proleap produced no output for {source.name}")

        try:
            payload = json.loads(output.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProleapError(f"proleap output is not valid JSON: {exc}") from exc
        return translate_native(payload, file=source.name)


def source_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
