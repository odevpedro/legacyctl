"""The sanitizer is the last line of defence before an LLM call.

Every test here encodes a leak that the current patterns do close, so a
regression in the regexes fails loudly instead of shipping credentials.
"""

from __future__ import annotations

import pytest

from legacyctl.security.sanitizer import PLACEHOLDER, RedactingSanitizer


@pytest.fixture(scope="module")
def sanitizer() -> RedactingSanitizer:
    return RedactingSanitizer()


@pytest.mark.parametrize(
    "secret",
    [
        "LegacyS3cr3t",
        "abc123def456",
        "eyJhbGciOi.JIUzI1NiJ9.eyJzdWIiOiIxIn0.sig",
        "-----BEGIN RSA PRIVATE KEY-----",
    ],
)
@pytest.mark.parametrize(
    "line",
    [
        'Public Const API_TOKEN As String = "{secret}"',
        'Private Const CONN_STRING As String = "Provider=PostgreSQL;Password={secret}"',
        "dbPassword = {secret}",
        "Authorization: Bearer {secret}",
        'url = "jdbc:postgresql://host/db;user=app;password={secret}"',
        "session_id={secret}",
    ],
)
def test_recognised_secret_shapes_are_redacted(
    sanitizer: RedactingSanitizer, line: str, secret: str
) -> None:
    assert secret not in sanitizer.sanitize(line.format(secret=secret))
    assert PLACEHOLDER in sanitizer.sanitize(line.format(secret=secret))


def test_bearer_is_redacted_before_key_value(sanitizer: RedactingSanitizer) -> None:
    """``Authorization: Bearer <jwt>`` must not leave the JWT behind.

    ``_KV`` alone would match the bare word ``Bearer`` as the *value* of
    ``Authorization`` and then the JWT would survive untouched.
    """
    out = sanitizer.sanitize("Authorization: Bearer eyJhbGciOi.JIUzI1NiJ9.body.sig")
    assert "eyJ" not in out
    assert out.count("[REDACTED]") == 2


def test_redaction_keeps_string_delimiters_balanced(sanitizer: RedactingSanitizer) -> None:
    """Redaction must not eat a closing quote and unbalance the source line."""
    for line in (
        'Set oConn = "Server=x;Password=p"',
        'Private Const CONN_STRING As String = "Provider=PostgreSQL;Password=s3cr3t"',
    ):
        out = sanitizer.sanitize(line)
        assert out.count('"') % 2 == 0, out


def test_business_logic_survives_redaction(sanitizer: RedactingSanitizer) -> None:
    """Over-redaction is tolerated; *erasing the rule* is not.

    A sanitizer that blanks every quoted string would pass the leak tests while
    destroying the slice the LLM is supposed to reason about.
    """
    samples = [
        'sql = "SELECT CREDIT_LIMIT FROM CUSTOMER_CREDIT WHERE CUSTOMER_ID = " & CStr(id)',
        "If requestedCredit > creditLimit Then",
        "Public Function ValidateCustomer(ByVal name As String) As Boolean",
        "If Validation.IsValidCustomerBasics(name, cpf) Then",
        'SetCustomerState customerId, "REJECTED_CREDIT_LIMIT"',
        "Dim requestedCredit As Double",
    ]
    for sample in samples:
        assert sanitizer.sanitize(sample) == sample, sample


def test_sanitize_mapping_is_recursive(sanitizer: RedactingSanitizer) -> None:
    payload = {
        "sql": '... password="hunter2"',
        "nested": [{"Password": "hunter2"}, {"safe": "SELECT 1"}],
        "number": 7,
    }
    out = sanitizer.sanitize_mapping(payload)
    assert "hunter2" not in str(out)
    assert out["nested"][1]["safe"] == "SELECT 1"
    assert out["number"] == 7
