# ADR 010 — Procedures legadas (OUT params) e máquina de estados no modelo e contrato

**Status:** aceito · **Data:** 2026-09-28

## Contexto

Duas características estruturam o VB6 analisado e aparecem em quase todo
sistema legado desse tipo:

1. **Procedures de banco com parâmetros OUT.** Stored procedures no SQL Server
   que não retornam um result set, mas preenchem parâmetros de saída
   (`@erro`, `@mensagem`, `@codigo`) e emitem um estado.
2. **Máquina de estados implícita.** O sistema transita entre estados
   (`EXECUTANDO`, `FALHA_AUTORIZACAO`, `TIMEOUT`, `SUCESSO`...) dados por
   essas procedures e registrados em log de estado.

A pergunta é se o modelo de conhecimento e o contrato devem incorporar essas
estruturas, ou se devem focar só em "regras" de alto nível (validação de
cliente, por exemplo) e tratar o resto como detalhe de implementação.

## Decisão

**Sim: procedures com OUT params e a máquina de estados são parte de primeira
classe do modelo de domínio e do contrato.**

- Parâmetros OUT ficam modelados **na stored procedure** (`DatabaseProcedure`
  com `in_params`/`out_params`), e não na assinatura VB6 que a chama.
- A máquina de estados é represented por `StateTransition`
  (`from_state`, `to_state`, `trigger`, `evidence_kind`), com
  `evidence_kind` distinguindo `OBSERVATION` de `INFERENCE`.
- Os nomes de estado observados no legado entram como **schema** no contrato
  OpenAPI.
- O SQL com `{call ...}` e sem schema declarado é gravado com `db_schema=None`
  e `ParseStatus.PARTIAL`, sem inventar identidade.

## Razões

**São o comportamento real, não detalhe.** Em sistemas como esse, a lógica de
negócio costuma estar *dentro* do fluxo procedure→SQL→estado. Se o modelo só
captura regras de alto nível e ignora OUT params e estados, ele perde
exatamente onde o comportamento mora. A verificação de uma procedure que
preenche `@erro` com um código que o front-end interpreta é o caso central,
não um caso de borda.

**Os OUT params são o contrato de retorno.** Uma procedure que sinaliza erro
via `@erro`/`@mensagem` não retorna um valor: ela preenche saídas. Se o modelo
tratar isso como uma chamada qualquer, a comparação com o sistema novo erra,
porque o valor a comparar está nos OUT params, não no retorno. Modelá-los
explicitamente é o que torna a comparação possível.

**A máquina de estados é o resultado observável.** O sistema legado mais
confiável é o seu log de estados: ele diz, para uma operação real, qual
transição aconteceu. Sem transições no modelo, o Golden Master perde a
sequência (`state_trace`) que é parte da saída esperada.

**A distinção observação/inferência é obrigatória.** Uma transição deduzida de
nomes de procedure (`FinalizarComSucesso` → estado `SUCESSO`) é uma hipótese;
uma observada em log é um fato. Colocá-las no mesmo tipo sem `evidence_kind`
faria a verificação tratar hipótese como fato — o modo de falha que o projeto
existe para evitar. `evidence_kind` é o que mantém a distinção viva até o
relatório.

**A identidade do schema é dado, não inferência.** Quando o código VB6 chama
`{call dbo.executar_grupo(?)}` sem declarar o schema, o schema é `None` e o
parse é `PARTIAL`. Inventar um schema seria fabricar uma identidade de objeto
de banco, que se propagaria para o contrato OpenAPI — e um schema errado no
contrato é pior do que um schema ausente.

**Estado no contrato dá forma ao sistema novo.** Os estados observados
viram schema no OpenAPI, para que o sistema novo exponha os mesmos estados
com os mesmos nomes. A modernizeção não só move a lógica; ela preserva o
vocabulário de estados observável.

## Alternativas consideradas

**Só regras de alto nível.** Mais simples e mais próximo do discurso de
"regras de negócio". Contra: perde o comportamento em que a lógica realmente
reside; a verificação fica cega ao canal de retorno real (OUT params) e à
sequência de estados. Seria uma descrição incompleta com aparência de
completa — o pior resultado.

**Tratar OUT params e estados como metadados do procedure VB6, sem modelo
próprio.** Contra: os OUT params não pertencem à assinatura VB6; pertencem ao
contrato da procedure de banco. Anexá-los ao chamador VB6 inventa um acoplamento
que o código legado não tem, e o contrato resultante mentiria sobre a fronteira
real.

**Normalizar estados para um conjunto canônico.** Interessante, mas perde os
nomes reais do legado, e a modernizeção precisa falar a língua do legado. Os
nomes observados são o vocabulário que os logs, as telas e os operadores já
usam.

## Consequências

**Boas**
- O modelo cobre onde o comportamento realmente está (procedure→SQL→estado).
- OUT params modelados enable comparação correta de retorno.
- `state_trace` no Golden Master dá uma saída esperada mais rica.
- `evidence_kind` mantém hipótese e fato distinguíveis até o relatório.
- Estados do legado viram schema do contrato, preservando vocabulário.

**Ruins**
- O modelo é maior e mais espesso que o de "só regras". Aceito: a complexidade
  reflete a do sistema legado.
- Múltiplas transições inferidas podem gerar ruído. Mitigado por
  `evidence_kind` e por revisão humana.
- A semântica de `evidence_kind` precisa ser respeitada por todos os
  consumidores (verificação, relatório); um consumidor que ignore o campo
  reintroduz o problema.

**Como isso evolui**
Proceduras de banco podem ganhar detalhe (assinaturas completas, tipos de
parâmetro) conforme o dialeto for supportado em profundidade. A separação
OUT-param-sobre-a-procedure e a exigência de `evidence_kind` permanecem
invariantes.
