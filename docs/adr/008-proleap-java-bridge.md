# ADR 008 — ProLeap via subprocesso Java com AST JSON

**Status:** aceito · **Data:** 2026-09-28

## Contexto

VB6 é uma linguagem sem parser native em Python. Para extrair procedures,
chamadas, parâmetros e SQL com confiança, é preciso entender a sintaxe real do
código — não aproximá-la com regex.

Há três caminhos: um parser Python escrito do zero, regex, ou usar o parser de
referência do VB6 — **ProLeap**, que é uma ferramenta Java — através de uma
fronteira de subprocesso, com **AST JSON** como formato de interoperabilidade.

## Decisão

Usar o **ProLeap** (parser VB6 open-source em Java) como parser de referência,
invocado como **subprocesso Java**, com um **AST JSON canônico** como contrato
de interoperabilidade. O Python consome o JSON; nunca fala com o parser
diretamente. Quando o ProLeap não está disponível, há um fallback
estrutural determinístico em Python.

## Razões

**Regex não entende sintaxe.** VB6 tem `If...Then...Else`, `Select Case`,
`On Error`, blocos `With`, chamadas com e sem parênteses, continuação de linha
`_`. Regex erra em construções aninhadas e produz falsos positivos — e um
falso positivo na extração de regra é uma regra errada com confiança alta. A
spec de referência exige parser real justamente por isso.

**Um parser Python do zero seria um projeto paralelo.** VB6 é dialeto
peculiar: tipos `As String` implícitos, `Long` vs `Integer`, parametrização
`Optional`, `ByVal`/`ByRef`, propriedades `Let`/`Set`. Escrever um parser
correto consome meses e seria subtly diferente do VB6 real em casos de borda.
ProLeap já ébattle-tested nesse dialeto.

**AST JSON é a fronteira certa.** Serializar o AST para JSON e tratar em
Python dá: (a) isolamento — o parser não compartilha memória nem estado com a
análise; (b) determinismo — a análise consome uma estrutura de dados pura,
serializável, testável; (c) cache — o `var/ast/<file>.json` permite reexecutar
a análise sem reparsear; (d) testabilidade — fixtures JSON de AST podem ser
versionadas e o parser testado sem Java.

**Subprocesso, não binding.** Comunicar com um processo Java via stdin/stdout
e um arquivo JSON é mais simples e mais robusto que um FFI ou embedding. Sem
dependência nativa, sem crash do parser derrubando o processo Python, sem
gerenciamento de memória cross-language. A fronteira é um arquivo.

**A degradação é honesta.** Quando o ProLeap não está disponível, o fallback
estrutural em Python roda e marca o que não conseguiu resolver como `UNKNOWN`
(ver `CallKind.UNRESOLVED`, `ParseStatus.PARTIAL`). A ferramenta continua
util, mas **não finge** ter understood o que não entendeu. Um parser ausente
degrada a cobertura, nunca a honestidade.

## Alternativas consideradas

**Regex puro.** Simples e sem dependências. Contra: erra em sintaxe aninhada;
não distingue `Call` resolvido de não resolvido; a extração de regra fica
baseada em adivinhação textual. Inaceitável para o objetivo.

**Parser Python do zero (tree-sitter-vb6, ANTLR).** tree-sitter para VB6 não
existe maduro; ANTLR exigiria escrever a gramática, que é o mesmo custo do
parser do zero com menos battle-testing. Contra: diverge do VB6 real em bordas.

**ProLeap via binding nativo (Jep/Py4J).** Chamada direta, sem subprocesso.
Contra: adiciona dependência nativa, complica setup e empacotamento, e um bug
no parser derruba o processo Python inteiro. O custo não se paga para a
margem de robustez que o subprocesso dá.

**Só ProLeap, sem fallback.** Mais simples conceitualmente. Contra: obriga
Java instalado em qualquer máquina que rode o pipeline, inclusive em `make
test`. O fallback mantém o pipeline utilizável sem Java, degradado mas honesto.

## Consequências

**Boas**
- Sintaxe real, não aproximação: as regras extraídas ancoram em AST.
- Análise é determinística e cacheável (`var/ast/`).
- Fixtures de AST JSON versionáveis; o parser é testável sem Java.
- Fallback garante que o pipeline roda sem Java, marcado como `PARTIAL`.

**Ruins**
- Java é necessário para a cobertura **completa**. `make test` e o default do
  pipeline usam o fallback e, portanto, têm cobertura menor do que a
  disponível com ProLeap.
- Latência: invocar Java por arquivo adiciona overhead. O cache em
  `var/ast/` amortiza execuções seguintes.
- O AST JSON é um contrato: mudanças no ProLeap exigem manter o esquema
  canônico alinhado. É um ponto de manutenção, e por isso o formato é
  versionado e os fixtures fixados.

**Como isso evolui**
Se um parser VB6 nativo em Python amadurecer, ele pode substituir o
subprocesso atrás da **mesma** interface `LegacyParser` (o contrato é o
protocolo, não o ProLeap). A troca não toca o resto do pipeline.
