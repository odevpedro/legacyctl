Attribute VB_Name = "Database"
Option Explicit

' ---------------------------------------------------------------------------
' Database.bas
' Camada de acesso a dados do legado.
' Toda SQL do sistema passa por este modulo: este e o principal HUB de dados.
' ---------------------------------------------------------------------------

Private Const CONN_STRING As String = "Provider=PostgreSQL;User ID=legacy_app;Password=LegacyS3cr3t;Data Source=bank_core"

Private m_cnn As Object
Private m_rs As Object
Private m_bOpened As Boolean

' Abre a conexao com o banco. Hub transversal: chamada por todos os fluxos.
Public Function OpenConnection() As Boolean
    On Error GoTo TratarErro

    If m_cnn Is Nothing Then
        Set m_cnn = CreateObject("ADODB.Connection")
    End If

    m_cnn.Open CONN_STRING
    m_bOpened = True
    OpenConnection = True
    Exit Function

TratarErro:
    m_bOpened = False
    OpenConnection = False
End Function

' Executa uma query de leitura e devolve o recordset.
Public Function Query(sql As String) As Object
    On Error Resume Next

    If Not OpenConnection() Then
        Set Query = Nothing
        Exit Function
    End If

    Set Query = m_cnn.Execute(sql)
End Function

' Executa uma instrucao de escrita.
Public Sub Execute(sql As String)
    On Error Resume Next

    If OpenConnection() Then
        m_cnn.Execute sql, , adAffectNothing
    End If
End Sub

' Prepara a chamada de uma stored procedure ({call schema.proc(?, ?)}).
Public Sub BeginCall(sql As String)
    On Error Resume Next
    Set m_rs = m_cnn.Execute(sql)
End Sub

' Parametro de entrada posicional do comando.
Public Sub SetIn(ByVal position As Long, ByVal value As Variant)
End Sub

' Parametro de saida: e aqui que o nome real do OUT param e conhecido.
Public Sub RegisterOut(ByVal position As Long, ByVal parameterName As String)
End Sub

Public Sub CloseCall()
    On Error Resume Next
    Set m_rs = Nothing
End Sub

Public Sub CloseConnection()
    On Error Resume Next
    If m_bOpened Then
        m_cnn.Close
    End If
    Set m_cnn = Nothing
    m_bOpened = False
End Sub
