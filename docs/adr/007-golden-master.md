# ADR 007 — Golden Master para verificação

**Status:** aceito · **Data:** 2026-09-28

## Contexto

O pipeline extrai regras e gera um sistema novo. Como saber, automaticamente,
se o sistema novo se comporta como o legado?

Sem verificação, a modernização é uma reorganização de código sem prova — o
timegut believe que "modernizou" e descobre as diferenças em produção.

A pergunta é qual técnica de verificação usar no MVP.

## Decisão

A verificação usa a técnica de **Golden Master**: um conjunto de casos
curados com entradas e saídas esperadas do sistema legado, contra os quais o
sistema novo é comparado. Os casos ficam em `catalog/golden-master/*.yaml`, com
`VerifyResult` por caso e um relatório de divergências.

O `verify` aceita `--new-system simulator` (offline, default) ou
`--new-system http` (um serviço real). Cada caso é `PASSED`, `FAILED` ou
`SKIPPED`.

## Razões

**Golden Master é o padrão daFixtures para "não tenho o sistema novo pronto".**
A técnica de Carlos de la Field famously compara um sistema novo contra um
antigo com casos capturados do antigo, sem exigir que o novo esteja completo.
É exatamente a situação do MVP: a geração produz uma superfície contratual,
não um sistema em produção.

**Um teste unitário não pega regressão de comportamento.** Um teste verifica
se o código novo faz o que o teste diz. Um Golden Master verifica se faz o que
o **legado** fazia — que é o objetivo real da modernização.

**A captura é a parte difícil, e o Golden Master a torna explícita.** Casos
precisam vir de evidência real do sistema legado (ver ADR 009), não de
imaginação. Isso é explícito no modelo: um caso sem evidência aceita é
recusado no carregamento.

**A comparação é determinística e legível.** A verificação é comparação de
saídas, não inferência. Um `FAILED` aponta um caso, um fluxo e uma regra — e o
divergence report diz exatamente o que divergiu.

**O simulator permite verificar antes do sistema novo existir.** O default
offline avalia as regras validadas e reporta honestamente quando não consegue
decidir (`SKIPPED`). Isso dá feedback desde o primeiro dia, sem esperar o time
escrever o sistema novo. O sistema real depois entra via `--new-system http`.

**Divergência é o produto, não o fracasso.** O divergence report é a lista de
onde o novo sistema difere do legado, com regra e fonte. É o artefato que
guia o trabalho de reconciliação. Um Golden Master que passa de primeira é
suspeito; um que falha com divergências precisas é útil.

## Alternativas consideradas

**Testes unitários do sistema novo.** Só verificam o que o teste afirma, não o
comportamento do legado. Não atendem ao objetivo.

**Diff de banco / golden master de SQL.** Compara queries, mas ignora lógica
de aplicação que não chega ao banco. Complementar, não substituto.

**Avaliação por LLM ("o novo parece equivalente").** Contradiz o princípio
central: verificação por interpretação produz falsos positivos. Um verificador
deve comparar, não julgar.

**Não verificar (só gerar contrato).** Mais simples, e foi o estado anterior.
Contra: o contrato pode estar errado e ninguém saberia até a produção. A
existência de um `FAILED` detector é o que torna o resto do pipeline confiável.

## Consequências

**Boas**
- Divergências viram um artefato acionável, com regra, fluxo e fonte.
- Verificação possível antes do sistema novo existir (simulator) e depois
  (http).
- Determinhismo: sem LLM na verificação.

**Ruins**
- Golden Master exige curation: capturar entradas/saídas do legado é trabalho
  humano e é o gargalo real. Mitigado por recusar casos sem evidência, forçando
  honestidade.
- `SKIPPED` precisa de semântica clara: um caso não decidido não é `PASSED`.
  O relatório declara explicitamente que o resultado é mais estreito do que
  parece.
- O simulator não substitui teste de integração real; ele valida as regras que
  ele consegue avaliar, e diz quais não conseguiu.

**Como isso evolui**
O simulator cobre o que é derivável das regras. Conforme o sistema novo
ganha endpoints, `--new-system http` passa a exercitar a superfície real. Os
mesmos casos servem para os dois, o que dá uma ponte contínua entre "regra
extraída" e "endpoint testado".
