Attribute VB_Name = "Customer"
Option Explicit

' ---------------------------------------------------------------------------
' Customer.bas
' Regras de negocio do cadastro de cliente.
' Fluxo principal: CreateCustomer -> CheckCreditLimit -> AuditCustomer
' ---------------------------------------------------------------------------

' Carrega o cliente e o limite de credito vigente.
Public Function LoadCustomer(ByVal customerId As Long) As Object
    Dim sql As String
    Dim rs As Object

    sql = "SELECT c.CUSTOMER_ID, c.NAME, c.CPF, cc.CREDIT_LIMIT " & _
           "FROM CUSTOMER c " & _
           "INNER JOIN CUSTOMER_CREDIT cc ON cc.CUSTOMER_ID = c.CUSTOMER_ID " & _
           "WHERE c.CUSTOMER_ID = " & CStr(customerId)

    Set rs = Database.Query(sql)
    Set LoadCustomer = rs
End Function

' Regra de negocio central do fluxo de cadastro:
'   requested_credit > credit_limit  ->  rejeitar a operacao.
' Evidencia: leitura de CUSTOMER_CREDIT.CREDIT_LIMIT e comparacao com o valor
' solicitado (Customer.bas, procedure CheckCreditLimit).
Public Function CheckCreditLimit(ByVal customerId As Long, ByVal requestedCredit As Double) As Boolean
    Dim sql As String
    Dim rs As Object
    Dim creditLimit As Double

    sql = "SELECT CREDIT_LIMIT FROM CUSTOMER_CREDIT WHERE CUSTOMER_ID = " & CStr(customerId)

    Set rs = Database.Query(sql)
    If rs Is Nothing Then
        CheckCreditLimit = False
        Exit Function
    End If

    If rs.EOF Then
        CheckCreditLimit = False
        Exit Function
    End If

    creditLimit = CDbl(rs.Fields("CREDIT_LIMIT").Value)

    If requestedCredit > creditLimit Then
        CheckCreditLimit = False
    Else
        CheckCreditLimit = True
    End If
End Function

' Regra de transicao de estado do cliente (RASTRO -> CRIADO -> VALIDANDO ->
' EXECUTANDO -> final). Estadosliterais observados no legado.
Public Sub SetCustomerState(ByVal customerId As Long, ByVal state As String)
    Dim sql As String

    sql = "UPDATE CUSTOMER SET STATE = '" & state & "' WHERE CUSTOMER_ID = " & CStr(customerId)
    Database.Execute sql
End Sub

' Grava o cadastro do cliente.
Public Function InsertCustomer(ByVal name As String, ByVal cpf As String, ByVal creditLimit As Double) As Long
    Dim sql As String
    Dim newId As Long

    sql = "INSERT INTO CUSTOMER (NAME, CPF, STATE, CREATED_AT) VALUES (" & _
           "'" & name & "', '" & cpf & "', 'CRIADO', CURRENT_TIMESTAMP) RETURNING CUSTOMER_ID"

    SetCustomerState 0, "CRIADO"
    Database.Execute sql

    newId = 0
    InsertCustomer = newId
End Function

' Fluxo principal do cadastro de cliente.
Public Function CreateCustomer(ByVal name As String, ByVal cpf As String, ByVal requestedCredit As Double) As Boolean
    Dim customerId As Long

    If Not Validation.IsValidCustomerBasics(name, cpf) Then
        AuditCustomer 0, "CREATE", "REJECTED_INVALID_DATA"
        CreateCustomer = False
        Exit Function
    End If

    customerId = InsertCustomer(name, cpf, requestedCredit)

    SetCustomerState customerId, "VALIDANDO"

    If Not CheckCreditLimit(customerId, requestedCredit) Then
        AuditCustomer customerId, "CREATE", "REJECTED_CREDIT_LIMIT"
        SetCustomerState customerId, "FINALIZADO_PARCIAL"
        CreateCustomer = False
        Exit Function
    End If

    SetCustomerState customerId, "EXECUTANDO"
    Database.Execute "UPDATE CUSTOMER_CREDIT SET CREDIT_LIMIT = " & CStr(requestedCredit) & _
                     " WHERE CUSTOMER_ID = " & CStr(customerId)
    SetCustomerState customerId, "FINALIZADO_SUCESSO"

    AuditCustomer customerId, "CREATE", "APPROVED"
    CreateCustomer = True
End Function

' Fluxo de atualizacao de limite: mesma regra de credito, outro fluxo.
Public Function UpdateCreditLimit(ByVal customerId As Long, ByVal requestedCredit As Double) As Boolean
    If Not CheckCreditLimit(customerId, requestedCredit) Then
        AuditCustomer customerId, "UPDATE_CREDIT", "REJECTED_CREDIT_LIMIT"
        UpdateCreditLimit = False
        Exit Function
    End If

    Database.Execute "UPDATE CUSTOMER_CREDIT SET CREDIT_LIMIT = " & CStr(requestedCredit) & _
                     " WHERE CUSTOMER_ID = " & CStr(customerId)

    AuditCustomer customerId, "UPDATE_CREDIT", "APPROVED"
    UpdateCreditLimit = True
End Function
