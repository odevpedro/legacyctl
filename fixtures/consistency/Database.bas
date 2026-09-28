Attribute VB_Name = "Database"
Option Explicit

' ---------------------------------------------------------------------------
' Database.bas (fixture de consistencia)
' Camada de acesso a dados compartilhada com o fixture vb6.
' ---------------------------------------------------------------------------

Private Const CONN_STRING As String = "Provider=PostgreSQL;User ID=legacy_app;Password=LegacyS3cr3t;Data Source=bank_core"

Private m_cnn As Object
Private m_rs As Object
Private m_bOpened As Boolean

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

Public Function Query(sql As String) As Object
    On Error Resume Next

    If Not OpenConnection() Then
        Set Query = Nothing
        Exit Function
    End If

    Set Query = m_cnn.Execute(sql)
End Function

Public Sub Execute(sql As String)
    On Error Resume Next

    If OpenConnection() Then
        m_cnn.Execute sql, , adAffectNothing
    End If
End Sub

Public Sub BeginCall(sql As String)
    On Error Resume Next
    Set m_rs = m_cnn.Execute(sql)
End Sub

Public Sub SetIn(ByVal position As Long, ByVal value As Variant)
End Sub

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
