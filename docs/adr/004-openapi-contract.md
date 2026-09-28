# ADR 004 — OpenAPI 3.1 como contrato

**Status:** aceito · **Data:** 2026-09-28

## Contexto

Entre o sistema legado e o sistema novo há um contrato. Ele precisa:

1. descrever as operações que a modernização expõe;
2. carregar as evidências que justificam cada operação (regra, arquivo, linha);
3. ser consumível por geração automática de código;
4. ser legível por uma pessoa que vai implementar a regra à mão.

A escolha de formato é uma decisão estruturante porque tudo o que vem depois
— geração, verificação, relatório — consome esse contrato.

## Decisão

O contrato é **OpenAPI 3.1**, gerado em `contracts/openapi.yaml`, contendo
apenas operações derivadas de regras com `RuleStatus.VALIDATED`. Cada operação
carrega extensões `x-business-rules`, `x-legacy-evidence`,
`x-legacy-condition` e `x-legacy-behavior`. Nomes de estado observados no
sistema legado viram um schema; schemas de parâmetros OUT vêm das stored
procedures.

## Razões

**É o contrato que a geração automática já consome.** OpenAPI é o ponto de
acordo universal entre "descrever uma API" e "gerar código". Escolher outro
formato exigiria escrever o gerador ou aceitar um round-trip imperfeito.

**3.1 é JSON Schema nativo.** `3.1` embute JSON Schema 2020-12, o que
significa que os schemas do contrato não são um dialeto proprietário: são
schemas que outras ferramentas já entendem.

**A evidência viaja junto da operação.** As extensões `x-*` mantêm a
proveniência (regra, fonte, condição, comportamento) **dentro** do contrato, e
não em um documento paralelo. Quem abre o `openapi.yaml` vê a regra e o
`arquivo:linha` do VB6 sem precisar de outra fonte. Se a proveniência morasse
fora, ela se perderia no primeiro commit que movesse o arquivo.

**O contrato é gerado de regras revisadas, não de candidatos automáticos.** O
gerador recusa rodar sem `VALIDATED` rules. O contrato é, portanto, uma
afirmação revisada por humanos — não um rascunho de máquina. Isso é o que
torna o contrato confiável o bastante para gerar código.

**Estende em vez de inventar.** `x-*` é a forma padrão de adicionar metadados
a um OpenAPI sem quebrar validadores. O contrato continua sendo OpenAPI válido.

## Alternativas consideradas

**JSON Schema puro.** Descreve dados, não operações. Perde a noção de método,
código de resposta e operação nomeada.

**Protobuf / gRPC.** Melhor contrato para serviço interno, mas não carrega
documentação legível por geradores OpenAPI nem descreve o caso de uso
principal (uma API HTTP gerada a partir de regras).

**Contrato customizado (YAML próprio).** Máximo controle, mas obriga a
escrever e manter um gerador, e ninguém mais entende o formato.

**OpenAPI 3.0.** Amplamente suportado, mas 3.0 usa um dialeto de JSON Schema
restrito e próprio; 3.1 remove essa camada de tradução.

## Consequências

**Boas**
- Geração de código e verificação consomem o mesmo contrato.
- A proveniência é legível no próprio contrato, inclusive no Javadoc gerado.
- O contrato valida como OpenAPI padrão.

**Ruins**
- As extensões `x-*` não são padronizadas: cada consumidor precisa saber que
  `x-business-rules` existe. Mitigado porque só esta ferramenta lê.
- OpenAPI descreve a **superfície** da API, não a lógica de negócio. O
  comportamento fica em `x-legacy-behavior` como texto, porque decidir o que o
  código deve fazer é decisão humana (ver ADR 006 sobre por que lógica não é
  gerada).
- OpenAPI 3.1 nem toda ferramenta antiga valida; o gerador usado é fixado por
  versão para contornar.

**Como isso evolui**
Se a API evoluir para além REST, OpenAPI não modela mensageria ou streaming e
um contrato paralelo seria necessário. Para o alvo REST do demo, é a escolha
mais direta.
