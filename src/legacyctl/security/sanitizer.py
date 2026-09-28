"""Redaction of sensitive values before anything reaches an LLM.

Applies to ``source_fragments``, ``graph_edges``, ``sql_references`` and
``hub_references`` — i.e. to the whole composed LLM context, not only to free
text fields of the request.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any, Protocol

PLACEHOLDER = "[REDACTED]"

# key = value / key: value / "key" => "value" for well known secret names.
#
# The key is matched with an arbitrary ``UPPER_SNAKE``/``lowerCamel`` prefix so
# that ``API_TOKEN``, ``dbPassword``, ``CONN_STRING`` and ``aws_secret`` are all
# recognised, not only the bare word ``password``. Under-redaction is the one
# failure mode this framework cannot have; over-redaction only costs context.
_KV = re.compile(
    r"(?P<key>\b[A-Za-z0-9_]*"
    r"(?:pass(?:word|wd)?|pwd|secret|token|api[_-]?key|apikey|"
    r"access[_-]?key|client[_-]?secret|private[_-]?key|credential(?:s)?|"
    r"authorization|auth|bearer|session[_-]?id|connection[_-]?string|conn[_-]?str)"
    r"\b)"
    # ``{"Password": "hunter2"}`` (JSON/YAML) quotes the *key* as well.
    r"(?P<kq>\"?)"
    # ``API_TOKEN As String = "..."`` is the VB6 shape; the type annotation
    # sits between the name and the value, so it is part of the separator.
    r"(?P<sep>(?:\s+As\s+[A-Za-z0-9_()]+)?\s*[:=]\s*)"
    # Quoted values are matched up to the closing quote so that secrets
    # containing spaces survive redaction intact (``"-----BEGIN RSA-----"``,
    # ``"Provider=...;Password=...;Data Source=..."``).
    r"(?:\"(?P<dq>[^\"]*)\"|'(?P<sq>[^']*)'|(?P<bare>[^\s,;&)\"']+))",
    re.IGNORECASE,
)

# Credentials embedded in JDBC / ODBC / ADO style connection strings.
_JDBC = re.compile(
    r"(?P<scheme>jdbc:[a-z0-9]+://[^\s;\"']+?)(?P<sep>;)(?P<rest>(?:password|pwd|user|username)\s*=\s*[^\s;\"']+)+",
    re.IGNORECASE,
)

_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{8,}")

_URL_USERINFO = re.compile(
    r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*://)(?P<userinfo>[^/\s:@]+:[^/\s:@]+@)"
)

# Name-only form of the secret vocabulary, used when the *key of a mapping* is
# the only place the secret is named -- ``{"password": "hunter2"}`` never
# reaches :meth:`RedactingSanitizer.sanitize`, because the text being sanitized
# is just ``"hunter2"``.
_SECRET_NAME = re.compile(
    r"\A[A-Za-z0-9_ ]*"
    r"(?:pass(?:word|wd)?|pwd|secret|token|api[_-]?key|apikey|"
    r"access[_-]?key|client[_-]?secret|private[_-]?key|credential(?:s)?|"
    r"authorization|auth|bearer|session[_-]?id|connection[_-]?string|conn[_-]?str)"
    r"[A-Za-z0-9_ ]*\Z",
    re.IGNORECASE,
)


def names_a_secret(key: str) -> bool:
    """True when a mapping key alone identifies the value as a secret."""
    return bool(_SECRET_NAME.match(key.strip()))


class ContextSanitizer(Protocol):
    """Port: anything able to redact secrets from a payload."""

    def sanitize(self, text: str) -> str: ...

    def sanitize_mapping(self, payload: dict[str, Any]) -> dict[str, Any]: ...


class RedactingSanitizer:
    """Default, dependency-free implementation.

    Conservative by design: it may over-redact, it never under-redacts a
    recognised secret shape. It never attempts to be a general DLP engine --
    the guarantee is "no obvious credential leaves the process".
    """

    def __init__(self, extra_keys: Iterable[str] = ()) -> None:
        keys = "|".join(sorted({k for k in extra_keys if k}))
        self._extra_keys = keys

    def sanitize(self, text: str) -> str:
        if not text:
            return text
        # Order matters. ``Authorization: Bearer <jwt>`` is redacted by
        # ``_BEARER`` *first*; if ``_KV`` ran first it would match the bare word
        # ``Bearer`` as the value of ``Authorization`` and leave the JWT in
        # clear text -- a silent credential leak.
        result = _BEARER.sub(f"Bearer {PLACEHOLDER}", text)
        result = _KV.sub(self._kv_repl, result)
        result = _JDBC.sub(self._jdbc_repl, result)
        result = _URL_USERINFO.sub(rf"\g<scheme>{PLACEHOLDER}@", result)
        return result

    def sanitize_mapping(self, payload: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in payload.items():
            if names_a_secret(key):
                # The key is the only signal available here, so it decides.
                result[key] = self._redacted(value)
            else:
                result[key] = self._sanitize_value(value)
        return result

    def _redacted(self, value: Any) -> Any:
        if isinstance(value, str):
            return PLACEHOLDER
        if isinstance(value, (list, tuple, dict)):
            return f"{PLACEHOLDER} ({type(value).__name__} omitted)"
        return PLACEHOLDER

    def sanitize_all(self, values: Iterable[str]) -> list[str]:
        return [self.sanitize(v) for v in values]

    def _sanitize_value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.sanitize(value)
        if isinstance(value, dict):
            return self.sanitize_mapping(value)
        if isinstance(value, list):
            return [self._sanitize_value(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self._sanitize_value(item) for item in value)
        return value

    def _kv_repl(self, match: re.Match[str]) -> str:
        key = match.group("key")
        prefix = f"{key}{match.group('kq')}{match.group('sep')}"
        if match.group("dq") is not None:
            return f'{prefix}"{PLACEHOLDER}"'
        if match.group("sq") is not None:
            return f"{prefix}'{PLACEHOLDER}'"
        return f"{prefix}{PLACEHOLDER}"

    def _jdbc_repl(self, match: re.Match[str]) -> str:
        return f"{match.group('scheme')};{PLACEHOLDER}"


_default: RedactingSanitizer | None = None


def get_sanitizer() -> RedactingSanitizer:
    global _default
    if _default is None:
        _default = RedactingSanitizer()
    return _default


def redact_secrets(payload: Any) -> Any:
    """Module-level shortcut used by the run log."""
    return get_sanitizer()._sanitize_value(payload)
