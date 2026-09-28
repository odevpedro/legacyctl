Attribute VB_Name = "Rastreio"
Option Explicit

' ---------------------------------------------------------------------------
' Rastreio.bas
' Hub transversal de rastreio de estado. O estado NAO vive no legado apenas
' como regra: e persistido (RASTRO_GRUPO / RASTRO_CONSISTENCIA) e lido de volta.
' Todas as transicoes abaixo sao observacoes literais do codigo.
' ---------------------------------------------------------------------------

Public Sub CriarRastroGrupo(ByVal idGrupo As Long)
    Database.Execute "INSERT INTO RASTRO_GRUPO (ID_GRUPO, ESTADO, CHANGED_AT) VALUES (" & _
                     CStr(idGrupo) & ", 'CRIADO', CURRENT_TIMESTAMP)"
End Sub

Public Sub AtualizarEstadoGrupo(ByVal idGrupo As Long, ByVal state As String)
    Database.Execute "UPDATE RASTRO_GRUPO SET ESTADO = '" & state & _
                     "' WHERE ID_GRUPO = " & CStr(idGrupo)
End Sub

Public Sub CriarRastroConsistencia(ByVal idGrupo As Long, ByVal idConsistencia As Long)
    Database.Execute "INSERT INTO RASTRO_CONSISTENCIA (ID_GRUPO, ID_CONSISTENCIA, ESTADO, CHANGED_AT) " & _
                     "VALUES (" & CStr(idGrupo) & ", " & CStr(idConsistencia) & ", 'EXECUTANDO', CURRENT_TIMESTAMP)"
End Sub

Public Sub AtualizarRastroConsistencia(ByVal idGrupo As Long, ByVal idConsistencia As Long, ByVal state As String)
    Database.Execute "UPDATE RASTRO_CONSISTENCIA SET ESTADO = '" & state & _
                     "' WHERE ID_GRUPO = " & CStr(idGrupo) & _
                     " AND ID_CONSISTENCIA = " & CStr(idConsistencia)
End Sub

' Le o estado persistido. Ausencia de evidencia nao e evidencia de estado:
' a funcao devolve UNKNOWN quando nao ha linha.
Public Function ObterEstadoGrupo(ByVal idGrupo As Long) As String
    Dim sql As String
    Dim rs As Object

    sql = "SELECT ESTADO FROM RASTRO_GRUPO WHERE ID_GRUPO = " & CStr(idGrupo)

    Set rs = Database.Query(sql)
    If rs Is Nothing Then
        ObterEstadoGrupo = "UNKNOWN"
        Exit Function
    End If

    If rs.EOF Then
        ObterEstadoGrupo = "UNKNOWN"
        Exit Function
    End If

    ObterEstadoGrupo = CStr(rs.Fields("ESTADO").Value)
End Function

' Le o usuario do banco (JDBC/ADO). Transversal a todos os fluxos.
Public Function ObterUsuarioDb() As String
    Dim sql As String
    Dim rs As Object

    sql = "SELECT USUARIO FROM OPERADOR WHERE ATIVO = 1"

    Set rs = Database.Query(sql)
    If rs Is Nothing Then
        ObterUsuarioDb = "UNKNOWN"
        Exit Function
    End If

    If rs.EOF Then
        ObterUsuarioDb = "UNKNOWN"
        Exit Function
    End If

    ObterUsuarioDb = CStr(rs.Fields("USUARIO").Value)
End Function
