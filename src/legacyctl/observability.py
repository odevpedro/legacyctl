"""Structured observability for every pipeline stage.

Each stage emits one JSON line carrying ``execution_id``, ``system_id``,
``flow_id``, ``rule_id``, ``source`` and ``timestamp``. When an LLM is involved
the record also carries ``provider``, ``model``, input/output token estimates
and duration.

Secrets are never logged: values pass through
:func:`legacyctl.security.sanitizer.redact_secrets` before being written.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from .security.sanitizer import redact_secrets


def _utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


@dataclass(slots=True)
class RunRecord:
    """One auditable pipeline event."""

    stage: str
    execution_id: str
    system_id: str | None = None
    flow_id: str | None = None
    rule_id: str | None = None
    source: str | None = None
    timestamp: str = field(default_factory=_utcnow)
    duration_ms: float | None = None
    provider: str | None = None
    model: str | None = None
    prompt_version: str | None = None
    input_token_estimate: int | None = None
    output_token_estimate: int | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        if not payload["details"]:
            payload.pop("details")
        return cast(dict[str, Any], redact_secrets(payload))

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False)


class ObservabilityLog:
    """Append-only JSONL run log plus an in-memory list for reports/tests."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self.records: list[RunRecord] = []
        self._sequence = 0

    def next_execution_id(self, system_id: str, stage: str) -> str:
        self._sequence += 1
        return f"EXEC-{system_id}-{stage}-{self._sequence:04d}"

    def emit(self, record: RunRecord) -> RunRecord:
        self.records.append(record)
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(record.to_json() + "\n")
        return record

    def stage(self, stage: str, *, system_id: str | None = None, **kwargs: Any) -> _StageContext:
        return _StageContext(self, stage, system_id, kwargs)

    def by_stage(self, stage: str) -> list[RunRecord]:
        return [r for r in self.records if r.stage == stage]

    def llm_records(self) -> list[RunRecord]:
        return [r for r in self.records if r.provider is not None]


class _StageContext:
    def __init__(
        self, log: ObservabilityLog, stage: str, system_id: str | None, kwargs: dict[str, Any]
    ) -> None:
        self._log = log
        self._stage = stage
        self._system_id = system_id
        self._kwargs = kwargs
        self._start = 0.0
        self.record: RunRecord | None = None

    def __enter__(self) -> RunRecord:
        self._start = time.perf_counter()
        self.record = RunRecord(
            stage=self._stage,
            execution_id=self._log.next_execution_id(self._system_id or "SYSTEM", self._stage),
            system_id=self._system_id,
            **self._kwargs,
        )
        return self.record

    def __exit__(self, exc_type: type[BaseException] | None, *_: object) -> None:
        assert self.record is not None
        self.record.duration_ms = round((time.perf_counter() - self._start) * 1000, 3)
        if exc_type is not None:
            self.record.details.setdefault("error", exc_type.__name__)
        self._log.emit(self.record)


_default_log: ObservabilityLog | None = None


def get_log() -> ObservabilityLog:
    """Process-wide log, backed by ``LEGACYCTL_LOG_FILE`` when set."""
    global _default_log
    if _default_log is None:
        env_path = os.environ.get("LEGACYCTL_LOG_FILE")
        _default_log = ObservabilityLog(Path(env_path) if env_path else None)
    return _default_log


def reset_log() -> None:
    global _default_log
    _default_log = None


@contextmanager
def timed() -> Iterator[float]:
    start = time.perf_counter()
    holder: list[float] = [0.0]
    try:
        yield holder[0]
    finally:
        holder[0] = round((time.perf_counter() - start) * 1000, 3)
