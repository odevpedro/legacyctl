Attribute VB_Name = "ExecutarConsistencia"
Option Explicit

' ---------------------------------------------------------------------------
' ExecutarConsistencia.bas
' Fluxo real de grupos de consistencia, chamado por JDBC/ADO com OUT params.
' O comportamento relevante (resultado, mensagem, payload) vive na procedure
' do banco, nao apenas no cliente: por isso {call ...} e DatabaseProcedure.
' ---------------------------------------------------------------------------

Private Const PROC_ESQUEMA As String = "BANK_CORE"

Private m_resultado As String
Private m_mensagem As String
Private m_payload As String

' Executa uma consistencia do grupo. Registra o rastro antes e depois.
Public Function ExecutarConsistencia(ByVal idGrupo As Long, ByVal idConsistencia As Long) As String
    Dim sql As String

    Rastreio.CriarRastroConsistencia idGrupo, idConsistencia

    sql = "{call " & PROC_ESQUEMA & ".executar_consistencia(?, ?)}"
    Database.BeginCall sql
    Database.SetIn 1, idGrupo
    Database.SetIn 2, idConsistencia
    Database.RegisterOut 1, "P_RESULTADO_NEGOCIO"
    Database.RegisterOut 2, "P_MENSAGEM"
    Database.RegisterOut 3, "P_PAYLOAD_JSON"
    Database.Execute
    Database.CloseCall

    Rastreio.AtualizarRastroConsistencia idGrupo, idConsistencia, "SUCESSO"

    ExecutarConsistencia = m_resultado
End Function

' Mapeia SQLException (codigo ADO) para o estado de consistencia.
' Regra transversal (HUB-SQL-ERROR): a inconsistencia observada no legado e o
' mapeamento do erro do SGBD para o estado persistido.
Public Function MapearErroConsistencia(ByVal sqlError As Long) As String
    If sqlError = -2147217843 Then
        MapearErroConsistencia = "FALHA_AUTORIZACAO"
    ElseIf sqlError = -2147417848 Then
        MapearErroConsistencia = "TIMEOUT"
    Else
        MapearErroConsistencia = "FALHA_EXECUCAO"
    End If
End Function

' Hub transversal de parametros: valida tipo, minimo, maximo e valores
' permitidos da consistencia antes de executa-la.
Public Function ValidarParametros(ByVal idGrupo As Long, ByVal parametros As String) As Boolean
    If Validation.IsBlank(parametros) Then
        ValidarParametros = False
        Exit Function
    End If

    If idGrupo < 1 Then
        ValidarParametros = False
        Exit Function
    End If

    ValidarParametros = True
End Function

' Fluxo completo do grupo de consistencia.
' RASTRO: CRIADO -> VALIDANDO -> EXECUTANDO -> estado final.
Public Function ExecutarGrupo(ByVal idGrupo As Long, ByVal parametros As String) As Boolean
    Dim idConsistencia As Long
    Dim resultado As String
    Dim parcial As Boolean
    Dim sql As String

    Rastreio.CriarRastroGrupo idGrupo
    Rastreio.AtualizarEstadoGrupo idGrupo, "VALIDANDO"

    If Not ValidarParametros(idGrupo, parametros) Then
        Rastreio.AtualizarEstadoGrupo idGrupo, "INCONCLUSIVO"
        ExecutarGrupo = False
        Exit Function
    End If

    Rastreio.AtualizarEstadoGrupo idGrupo, "EXECUTANDO"

    idConsistencia = 1
    parcial = False

    Do While idConsistencia <= 2
        resultado = ExecutarConsistencia(idGrupo, idConsistencia)

        If resultado = "REPROVADO" Then
            parcial = True
        End If

        If resultado = "ERRO" Then
            Rastreio.AtualizarRastroConsistencia idGrupo, idConsistencia, "FALHA_EXECUCAO"
        End If

        idConsistencia = idConsistencia + 1
    Loop

    If parcial Then
        Rastreio.AtualizarEstadoGrupo idGrupo, "FINALIZADO_PARCIAL"
    Else
        Rastreio.AtualizarEstadoGrupo idGrupo, "FINALIZADO_SUCESSO"
    End If

    sql = "SELECT COUNT(*) AS TOTAL FROM RASTRO_CONSISTENCIA WHERE ID_GRUPO = " & CStr(idGrupo) & _
          " AND ESTADO = 'SUCESSO'"
    Database.Query sql

    ExecutarGrupo = Not parcial
End Function
