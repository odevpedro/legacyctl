# ADR 001 — Python como engine de análise

**Status:** aceito · **Data:** 2026-09-28

## Contexto

O pipeline precisa ler código VB6, normalizar SQL, construir e clusterizar um
grafo de conhecimento, extrair regras de negócio, gerar um contrato OpenAPI e
verificar a modernização. Duas partes rodam fora do Python por escolha
deliberada: o parser VB6 de referência é uma ferramenta Java, e a geração de
código é o OpenAPI Generator. A decisão aqui é sobre a **língua do orchestrator
e de tudo o que ele constrói por conta própria**.

O codebase alvo da modernização é Java 21, o que faz Python parecer a escolha
"errada" à primeira vista.

## Decisão

O engine de análise é Python 3.12. Tudo o que a ferramenta raciocina sobre o
sistema legado — parser, grafo, clustering, slicing, extração, contrato,
verificação, relatório — é Python. Java aparece apenas como processo filho,
atrás de uma fronteira de interoperabilidade.

## Razões

**O gargalo é semântica, não performance.** O trabalho pesado é ler código
herdado e decidir o que ele significa. Nenhuma dessas tarefas é limitada por
CPU. Python dá acesso imediato a parsing (§ ADR 008 para o VB6), SQLGlot para
SQL, e ao ecossistema de análise de grafos.

**Python é mais forte em texto.** A camada de regras é, em essência,
processamento de linguagem sobre fragmentos de código. Manipulação de string,
regex e parsing são mais expressivos e mais curtos em Python do que em Java, e
essa expressividade é a restrição de verdade.

**As bibliotecas certas já existem.** `sqlglot` faz dialeto SQL e lineage,
`networkx` faz grafos e centralidades, `pydantic` valida e serializa o modelo
de domínio. Escrever isso em Java seria meses de trabalho sem acrescentar
nada ao produto.

**O modelo de domínio é o ativo, e pydantic o mantém honesto.** As invariantes
do §ADR 001 do domain model (uma regra sem `sources` não pode existir, um
Golden Master sem evidência é recusado) são declaradas em tipos e validadas na
fronteira. O mesmo em Java exigiria construtores e validadores manuais, e
alguém esqueceria um.

**O time é Python.** Um monolito em Java exige que o time que sabe
VB6-SQL-orquestração também saiba construir serviços Spring. Não é o perfil
disponível.

## Alternativas considered

**Java 21 / Spring Boot como engine.** Alinhamento natural com o alvo, e o
time provavelmente escreve Java todos os dias. Contra: menos expressivo para
manipulação de texto, mais verboso para o tipo de trabalho textual deste
projeto, e o modelo de domínio perde as invariantes declarativas de pydantic.
O custo real: a linguagem obriga a declarar cada invariante à mão, num mundo
onde um validador esquecido falha em silêncio.

**TypeScript.** Razoável, e o `openapi-typescript` é excelente. Contra: o
ecossistema de análise está mais maduro em Python, e o time não é de JS.

**Rust / Go.** Desempenho excelente, irrelevante aqui. Custo de desenvolvimento
muito maior para trabalho que não é CPU-bound.

## Consequências

**Boas**
- O código de análise é curto e legível; as invariantes ficam nos tipos.
- O pipeline roda em qualquer máquina com Python 3.12, sem build step.
- Adicionar um provider de LLM ou um parser é uma dependência, não um
  redesign.

**Ruins**
- Duas linguagens no repositório (Python + o Java gerado). A fronteira entre
  elas é o contrato OpenAPI, que é explícito e versionado.
- `pydantic` é uma dependência estrutural: trocar por dataclasses significaria
  reimplementar a validação que sustenta as garantias.- Type hints precisam discipline: o projeto usa mypy em modo `strict` sobre
  48 arquivos, mas mypy não valida em runtime o que pydantic valida.

**Como isso evolui**
Se o alvo deixasse de ser Java, a decisão teria de ser revisada. Enquanto o
alvo é Java e o trabalho é análise, a troca custaria mais do que renderia.
