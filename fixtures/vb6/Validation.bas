Attribute VB_Name = "Validation"
Option Explicit

' ---------------------------------------------------------------------------
' Validation.bas
' Hub transversal de validacao de dados. Chamado por todos os fluxos de
' negocio; as regras extraidas daqui recebem IDs HUB-VALIDATION-*.
' Nao duplicar estas regras em cada fluxo.
' ---------------------------------------------------------------------------

Private Const DATE_ZERO As Double = 0

Public Function IsBlank(ByVal value As Variant) As Boolean
    IsBlank = IsNull(value) Or Len(Trim$(CStr(value))) = 0
End Function

' Data no legado e serial (0 = 30/12/1899). Nao derivar regra de calendario
' sem evidencia adicional.
Public Function ToDateSerial(ByVal value As Variant) As Double
    If IsDate(value) Then
        ToDateSerial = CDbl(CDate(value))
    Else
        ToDateSerial = DATE_ZERO
    End If
End Function

' Validacao de CPF: 11 digitos, dois digitos verificadores.
' Regra transversal (HUB-VALIDATION-CPF-001).
Public Function IsValidCPF(ByVal cpf As String) As Boolean
    Dim i As Long
    Dim soma As Long
    Dim digito As Long
    Dim rest As Long

    cpf = Replace(Trim$(cpf), ".", "")
    cpf = Replace(cpf, "-", "")

    If Len(cpf) <> 11 Then
        IsValidCPF = False
        Exit Function
    End If

    If Not IsNumeric(cpf) Then
        IsValidCPF = False
        Exit Function
    End If

    If cpf = "00000000000" Or cpf = "11111111111" Then
        IsValidCPF = False
        Exit Function
    End If

    soma = 0
    For i = 1 To 9
        digito = CLng(Mid$(cpf, i, 1))
        soma = soma + digito * (11 - i)
    Next i
    rest = (soma * 10) Mod 11
    If rest = 10 Then rest = 0
    If rest <> CLng(Mid$(cpf, 10, 1)) Then
        IsValidCPF = False
        Exit Function
    End If

    soma = 0
    For i = 1 To 10
        digito = CLng(Mid$(cpf, i, 1))
        soma = soma + digito * (12 - i)
    Next i
    rest = (soma * 10) Mod 11
    If rest = 10 Then rest = 0
    IsValidCPF = (rest = CLng(Mid$(cpf, 11, 1)))
End Function

' Validacao transversal de campos obrigatorios de um cliente.
Public Function IsValidCustomerBasics(ByVal name As String, ByVal cpf As String) As Boolean
    If IsBlank(name) Then
        IsValidCustomerBasics = False
        Exit Function
    End If

    If IsBlank(cpf) Then
        IsValidCustomerBasics = False
        Exit Function
    End If

    IsValidCustomerBasics = IsValidCPF(cpf)
End Function
