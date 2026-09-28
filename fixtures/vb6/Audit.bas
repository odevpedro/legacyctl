Attribute VB_Name = "Audit"
Option Explicit

' ---------------------------------------------------------------------------
' Audit.bas
' Regra transversal: toda operacao de cliente aprovada ou rejeitada gera um
' evento de auditoria. Hub transversal do sistema (HUB-AUDIT).
' ---------------------------------------------------------------------------

Public Sub AuditCustomer(ByVal customerId As Long, ByVal operation As String, ByVal result As String)
    Dim sql As String

    sql = "INSERT INTO AUDIT_LOG (CUSTOMER_ID, OPERATION, RESULT, CREATED_AT) VALUES (" & _
           CStr(customerId) & ", '" & operation & "', '" & result & "', " & _
           "CURRENT_TIMESTAMP)"

    Database.Execute sql
End Sub
