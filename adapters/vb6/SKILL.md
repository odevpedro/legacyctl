---
name: vb6-legacy
description: >-
  Análise de código VB6 no legacyctl. Use para mapear VB6 (Visual Basic 6,
  forms .frm, modules .bas, projects .vbp); não use para outras linguagens.
---

# VB6 Adapter

Contexto e orientação para o `RuleExtractor`. Não é runtime: o parser produz os
fatos, este documento diz como *interpretá-los* sem inventar.

## Toolchain (parser)

- **Referência:** ProLeap, parser VB6 em Java, invocado como subprocesso, com
  `AST JSON` canônico como contrato de interoperabilidade
  (`LEGACYCTL_PROLEAP_JAR`).
- **Fallback documentado:** parser estrutural determinístico em Python, quando o
  ProLeap não está disponível. Reduz a cobertura e marca o que não resolveu
  como `UNKNOWN` (`CallKind.UNRESOLVED`, `ParseStatus.PARTIAL`). Cobre menos;
  não mente.
- **SQL:** todo SQL passa por SQLGlot para normalização, antes de qualquer
  interpretação.

## Mapeamento para o modelo de domínio

| VB6 | Tipo de domínio |
|---|---|
| `.frm` / `.bas` / `.cls` | `Component` (`ComponentKind.FORM` / `MODULE` / `CLASS`) |
| `Sub` / `Function` | `Procedure` (`ProcedureKind.PROCEDURE` / `FUNCTION`) |
| `Dim` / `Public` / parâmetros | `Variable` (com `is_out`, `is_return`, `type_confidence`) |
| Qualquer call site | `Call` (`CallKind.STATIC` / `DYNAMIC` / `EXTERNAL` / `UNRESOLVED`) |
| String SQL embutida | `SqlStatement` |
| `Form_Load`, `cmdX_Click`, `Menu_Click` | `EntryPoint` |
| `CallState` / `SetState` | `StateTransition` |

## Indicações

O que o VB6 permite extrair com segurança:

- **Procedures e assinaturas:** nomes, tipos declarados, `Optional`, `ByVal` /
  `ByRef`. Uma `Function` retorna valor e **não** tem OUT params — não confundir
  os dois, o que inventaria um contrato inexistente.
- **SQL embutido:** literais de string e concatenações que produzem
  `SELECT` / `INSERT` / `UPDATE` / `DELETE` são normalizados por SQLGlot;
  tabela, view e colunas saem da árvore normalizada, não de regex.
- **Chamadas estáticas:** um call site cujo `callee_name` resolve para uma
  procedure do próprio sistema vira `CallKind.STATIC` com aresta no grafo.
- **Estados:** chamadas que escrevem estado viram `StateTransition` com
  `evidence_kind` explícito.

## Restrições e armadilhas

O que **nunca** assumir em VB6:

- **Variáveis implícitas (`Option Explicit` ausente) não provam tipo.** Sem
  `Option Explicit`, um `Dim x` é Variant. Declarar `uncertain`
  (`type_confidence`), nunca `String` por conveniência.
- **`On Error Resume Next`:** ponto de atenção, não detalhe. O erro é engolido e
  o fluxo continua; um branch depois dele pode parecer uma regra quando é
  comportamento sob falha silenciosa.
- **`On Error GoTo`:** ramo de interceptação de erro. Mesma leitura: o que o
  handler faz é parte da regra, não boilerplate.
- **Concatenação com `&` gera SQL dinâmico.** A string final pode não ser
  visível estaticamente. Passar por SQLGlot quando reconstruível; marcar
  `is_dynamic` quando não.
- **`Date` é serial** (0 = 30/12/1899). Não derivar regra de calendário,
  prazo ou idade sem evidência de que o código realmente converte.
- **`GoTo`:** punha a leitura sequencial. Marcar como ponto de atenção; não
  assumir que o fluxo é linear.
- **Injeção de string em SQL:** concatenar valor em SQL é vulnerabilidade
  *e* ambiguidade de intenção. Não assumir que a concatenação é parametricizada.

## SQL e banco de dados

- **Embutido:** SQL em string literal é o caso mais comum.
- **`{call proc(?, ?)}`:** chamada de stored procedure via ADO/DAO. Quando há
  parâmetros de saída, modelar como `DatabaseProcedure` com `out_params` — os
  OUT params pertencem ao **contrato da procedure de banco**, não à assinatura
  VB6 que a chama.
- **Sem schema declarado:** `{call .executar_grupo(?)}` fica com
  `db_schema=None` e `ParseStatus.PARTIAL`. Inventar um schema fabricaria a
  identidade do objeto e se propagaria para o contrato OpenAPI.
- **Estados via procedure:** `@erro` / `@mensagem` preenchidos pela procedure
  são o canal de retorno real; o comportamento está aqui, não no retorno da
  chamada.

## Exemplos canônicos

Trechos que disparam as heurísticas do parser:

```vb
' Option Explicit ausente -> Variable(type_confidence=uncertain)
Dim valor

' guarda de validação -> RuleCondition
If Not CustomerForm.IsValidCustomerBasics(txtNome.Text) Then

' escrita de estado -> StateTransition(evidence_kind=observation)
CallState grupo, "EXECUTANDO"

' SQL embutido -> SqlStatement (SQLGlot)
sql = "SELECT * FROM CLIENTE WHERE ID = " & idCliente

' stored procedure com OUT params -> DatabaseProcedure
rs.Execute "{call dbo.executar_grupo(?, ?)}", idGrupo, msg

' ponto de atenção -> attention_points
On Error Resume Next
```

## O que esta orientação proíbe

Um `RuleBehavior.type` desconhecido é um valor de primeira classe. Quando o
código legado não declara o que a procedure decide, o comportamento é
`unknown` — não uma decisão plausível inventada pelo extrator. A regra só entra
no contrato como `VALIDATED` por decisão humana.
