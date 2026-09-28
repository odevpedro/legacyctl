# ADR 009 — Golden Master curado a partir de evidência real

**Status:** aceito · **Data:** 2026-09-28

## Contexto

A verificação depende de casos com entradas e saídas esperadas (ADR 007). Surge
a pergunta: **de onde vêm essas saídas esperadas?**

Há três fontes possíveis: (a) gerar as expectativas com um LLM a partir do
código; (b) inventar casos "razonáveis" e escrever a saída esperada à mão sem
rodar o legado; (c) **capturar a saída real do sistema legado em execução** —
via trace do SGBD, observação manual do comportamento, ou executando o fluxo.

A escolha da fonte da verdade determina a confiança em toda a verificação.

## Decisão

Os casos do Golden Master são **curados a partir de evidência real** do sistema
legado: traces do banco, observação manual, ou execução real do fluxo. Um caso
deve declarar uma fonte de evidência aceita, e `GoldenMasterStore` **recusa**
um caso sem evidência ou com evidência não aceita. Casos de baixa confiança são
marcados e não bloqueiam.

## Razões

**A saída esperada do legado é um fato, não uma previsão.** O Golden Master
compara novo contra velho. O lado "velho" tem que ser o comportamento real
observado. Se a expectativa vem de um LLM ou de imaginação, o teste passa a
verificar "o novo faz o que alguém imaginou que o legado fazia" — que é
exatamente a alucinação que o resto do projeto evita.

**A evidência é verificável; a imaginação não.** Um trace de banco tem
timestamp, query e resultado. Observação manual tem quem observou e quando. Um
caso gerado por LLM não tem como ser auditado. O loader exige a evidência
como uma **invariante do modelo**, não uma convenção.

**Alinhado com o princípio "UNKNOWN em vez de palpite".** Se não há evidência
de como o legado se comporta num dado caso, a resposta honesta é
`UNKNOWN`/`SKIPPED`, não "provavelmente faz X". A curadoria com evidência é o
princípio aplicado ao Golden Master.

**Confiança baixa é centrada no caso, não no resultado.** Quando a evidência é
fraca (ex.: observação informal), o caso é marcado `low confidence` e a
divergência vira *advisory* — reportada, mas não bloqueia. Isso evita dois
extremos: tratar uma opinião como fato, ou descartar toda verificação por
causa de um caso fraco.

**A separação extractor/curador é deliberada.** O LLM pode **propor** casos
(extrair um caso candidato de um fluxo), mas o caso só entra no Golden Master
com evidência real e curadoria humana. A proposta é automatizável; a verdade
exige evidência.

## Alternativas consideradas

**Gerar expectativas com LLM.** Tentador: rápido, escala, "já que temos LLM".
Contra: produz expectativas que podem estar erradas; o Golden Master passaria
a validar contra a alucinação. Pior que não ter verificação, porque dá confiança
falsa. Descartado explicitamente.

**Escrever casos à mão sem rodar o legado.** Mais rápido que capturar traces.
Contra: a saída esperada é inventada; o teste passa a codificar o que o
desenvolvedor *acredita* que o legado faz, não o que ele faz. Aceitável
apenas como hipótese a ser confirmada — nunca como verdade.

**Usar o sistema novo como fonte da verdade.** Circular: verificaria o novo
contra si mesmo. Não tem valor de verificação.

**Só traces de banco.** Evidência forte, mas parcial: não cobre lógica que não
chega ao banco (validação em tela, decisão de fluxo). Precisa ser complementado
por observação manual.

## Consequências

**Boas**
- A verificação se apoia em fatos observados; a confiança é rastreável até a
  evidência.
- `SKIPPED` e advisory honestos: caso sem evidência ou de baixa confiança não
  gera falso verde nem falso vermelho.
- A separação propose/curate impede que automação contorne a evidência.

**Ruins**
- Curation é o gargalo real do MVP. Capturar evidência exige acesso ao banco e
  execução do legado; sem isso, os casos ficam limitados. É o custo de não
  mentir.
- Casos de baixa confiança não bloqueiam: há um risco de um comportamento real
  divergente "escapar" num caso fraco. Mitigado por ser reportado como
  advisory, visível no relatório.
- Não é automatizável de ponta a ponta; precisa de alguém que conheça o legado.

**Como isso evolui**
Conforme a automação de captura de traces amadurecer, a curadoria manual pode
ser assistida por extração automática de evidência, mantendo a exigência de que
a evidência exista. A invariante do loader permanece.
