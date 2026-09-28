VERSION 5.00
Begin {C62A69F0-16DC-11CE-9E98-00AA00574A4F} VB5 Form
Caption         = "Cadastro de Cliente"
ClientHeight    = 6000
ClientLeft      = 120
ClientTop       = 465
ClientWidth     = 9000
StartUpPosition = 1 "CenterOwner"
Attribute VB_Name = "CustomerForm"
Attribute VB_GlobalNameSpace = False
Attribute VB_Creatable = False
Attribute VB_PredeclaredId = True
Attribute VB_Exposed = False
Begin VB.CommandButton cmdValidate
   Caption         = "Validar"
   Height          = 500
   Left            = 120
   TabIndex        = 0
   Top             = 500
   Width           = 1200
End
Begin VB.TextBox txtName
   Height          = 400
   Left            = 1400
   TabIndex        = 1
   Top             = 500
   Width           = 3000
End
Begin VB.TextBox txtCpf
   Height          = 400
   Left            = 1400
   TabIndex        = 2
   Top             = 1000
   Width           = 2000
End
Begin VB.CommandButton cmdSave
   Caption         = "Salvar"
   Height          = 500
   Left            = 120
   TabIndex        = 3
   Top             = 1600
   Width           = 1200
End
End
Option Explicit

' ---------------------------------------------------------------------------
' Customer.frm
' Formulario de cadastro: ponto de entrada do fluxo principal.
' ---------------------------------------------------------------------------

Private Sub cmdValidate_Click()
    Dim name As String
    Dim cpf As String
    Dim requestedCredit As Double

    name = Trim$(txtName.Text)
    cpf = Trim$(txtCpf.Text)
    requestedCredit = CDbl(Validation.IsBlank(name))

    Call ValidateCustomer(name, cpf, requestedCredit)
End Sub

Private Sub cmdSave_Click()
    Dim name As String
    Dim cpf As String

    name = Trim$(txtName.Text)
    cpf = Trim$(txtCpf.Text)

    If CreateCustomer(name, cpf, 5000) Then
        MsgBox "Cliente cadastrado com sucesso."
    Else
        MsgBox "Cadastro rejeitado."
    End If
End Sub

Private Sub Form_Load()
    Database.OpenConnection
End Sub

Private Sub Form_Unload(Cancel As Integer)
    Database.CloseConnection
End Sub

' Regra de negocio acessada pelo formulario: valida os dados e consulta o
' limite de credito antes de permitir a gravacao.
Public Function ValidateCustomer(ByVal name As String, ByVal cpf As String, ByVal requestedCredit As Double) As Boolean
    On Error Resume Next

    If Not Validation.IsValidCustomerBasics(name, cpf) Then
        ValidateCustomer = False
        Exit Function
    End If

    ValidateCustomer = CheckCreditLimit(0, requestedCredit)
End Function
