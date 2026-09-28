"""Call extraction from real VB6 shapes.

The extraction of calls is the single place where a silent under-report would
corrupt every downstream artifact: the call graph, the slice, the rule
candidate set and therefore the contract. These tests pin the shapes that the
``fixtures/`` files actually use.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from legacyctl.domain.enums import CallKind, ParseStatus
from legacyctl.domain.models import Call, LegacySystem
from legacyctl.parsers.vb6 import VB6Parser

SOURCE = """\
Option Explicit

Private Const LIMIT As Double = 5000

Public Sub Run(ByVal name As String, ByVal cpf As String, ByVal amount As Double)
    Dim total As Double
    If Not Validation.IsValidCustomerBasics(name, cpf) Then
        SetCustomerState 0, "REJECTED_INVALID_DATA"
        Exit Sub
    End If
    total = CheckCreditLimit(0, amount)
    If Not IsBlank(name) Then
        AuditCustomer 0, "CREATE", "APPROVED"
    End If
    Call Database.BeginCall "{call BANK_CORE.P}"
    Database.Execute "UPDATE T SET A = 1"
    Set rs = Database.Query("SELECT 1")
    label = CStr(Validation.IsBlank(name))
    Debug.Print "hello"
    total = CDbl(rs.Fields("X").Value)
End Sub
"""


def _calls_of_run(system: LegacySystem) -> list[Call]:
    procedure = next(p for p in system.procedures if p.name.lower() == "run")
    return [c for c in system.calls if c.caller_id == procedure.id]


def _callees(calls: list[Call]) -> dict[str, CallKind]:
    """``callee_name`` is the qualified name (``Validation.IsBlank``)."""
    return {c.callee_name.lower(): c.kind for c in calls}


@pytest.fixture(scope="module")
def run_calls(tmp_path_factory: pytest.TempPathFactory) -> list[Call]:
    module = tmp_path_factory.mktemp("vb6") / "run.bas"
    module.write_text(SOURCE, encoding="utf-8")
    system = VB6Parser().parse(module.parent)
    assert all(c.parse_status is ParseStatus.OK for c in system.components)
    return _calls_of_run(system)


def test_call_inside_if_condition_is_found(run_calls: list[Call]) -> None:
    """``If Not Validation.IsValidCustomerBasics(name, cpf) Then``.

    Reading only the statement head loses this call entirely, and with it the
    link from the command to the validation module.
    """
    assert "validation.isvalidcustomerbasics" in _callees(run_calls)


def test_call_on_assignment_right_hand_side_is_found(run_calls: list[Call]) -> None:
    assert "checkcreditlimit" in _callees(run_calls)


def test_qualified_invocation_is_found(run_calls: list[Call]) -> None:
    assert "database.query" in _callees(run_calls)
    assert "rs.fields" in _callees(run_calls)


def test_qualified_statement_call_without_parentheses_is_found(run_calls: list[Call]) -> None:
    call = next(c for c in run_calls if c.callee_name.lower() == "database.begincall")
    assert "Database.BeginCall" in call.call_expression
    assert call.arguments == ["{call BANK_CORE.P}"]
    assert call.kind is CallKind.DYNAMIC


def test_statement_calls_without_parentheses_carry_their_arguments(run_calls: list[Call]) -> None:
    set_state = next(c for c in run_calls if c.callee_name.lower() == "setcustomerstate")
    assert len(set_state.arguments) == 2
    audit = next(c for c in run_calls if c.callee_name.lower() == "auditcustomer")
    assert len(audit.arguments) == 3


def test_nested_argument_does_not_leak_into_the_outer_call(run_calls: list[Call]) -> None:
    """``CStr(Validation.IsBlank(name))`` has one argument, not a spliced string.

    A text-slicing implementation takes the *last* parenthesis pair and reports
    ``Validation .IsBlank(name)`` as the argument of ``CStr``.
    """
    cstr = next(c for c in run_calls if c.callee_name.lower() == "cstr")
    assert cstr.arguments == ["Validation.IsBlank(name)"]


def test_builtin_is_external_and_stays_out_of_the_internal_graph(
    run_calls: list[Call],
) -> None:
    for name in ("cstr", "cdbl"):
        call = next(c for c in run_calls if c.callee_name.lower() == name)
        assert call.kind is CallKind.EXTERNAL, name


def test_debug_print_is_not_a_call(run_calls: list[Call]) -> None:
    assert "print" not in _callees(run_calls)


def test_module_level_constant_does_not_become_a_procedure(tmp_path: Path) -> None:
    (tmp_path / "only.bas").write_text(
        "Option Explicit\nPrivate Const LIMIT As Double = 5000\n", encoding="utf-8"
    )
    assert VB6Parser().parse(tmp_path).procedures == []


def test_sql_inside_a_call_statement_is_still_captured(tmp_path: Path) -> None:
    (tmp_path / "sql.bas").write_text(
        "Public Sub P()\n"
        '    Database.BeginCall "{call BANK_CORE.EXECUTAR_CONSISTENCIA(?, ?)}"\n'
        "End Sub\n",
        encoding="utf-8",
    )
    system = VB6Parser().parse(tmp_path)
    assert [s.procedure_name for s in system.sql_statements] == ["BANK_CORE.EXECUTAR_CONSISTENCIA"]


def test_call_id_is_deterministic(tmp_path: Path) -> None:
    (tmp_path / "p.bas").write_text(
        "Public Sub P()\n    Q 1\nEnd Sub\nPublic Sub Q(ByVal n As Long)\nEnd Sub\n",
        encoding="utf-8",
    )
    first = [c.id for c in VB6Parser().parse(tmp_path).calls]
    second = [c.id for c in VB6Parser().parse(tmp_path).calls]
    assert first == second
