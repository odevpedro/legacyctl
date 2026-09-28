---
name: cobol-legacy
description: >-
  Análise de código COBOL no legacyctl. Use para mapear COBOL (batch, copybooks,
  programas *PROCEDURE DIVISION*); não use para outras linguagens.
status: planejado
---

# COBOL Adapter

**Status: adapter planejado, não implementado.** Não existe parser COBOL em
`src/legacyctl/parsers/`. Este documento registra a orientação prevista para
quando o adapter existir, e o que um parser COBOL precisaria decidir sem
mentir.

Os pontos abaixo são as armadilhas conhecidas de COBOL. Estão aqui porque são
justamente as que fariam um parser COBOL inventar comportamento, que é o modo
de falha que o projeto existe para evitar.

## Toolchain (parser)

- **Referência:** a definir. O caminho óbvio é o GnuCOBOL em modo
  `generate-c_source` / análise sintática, ou um parser de terceiros com
  suporte a dialectos — a escolha depende do dialeto real do cliente
  (COBOL-85 vs Enterprise COBOL vs Micro Focus), e essa decisão precisa vir
  antes do parser.
- **Contrato de interoperabilidade:** o mesmo `AST JSON` canônico usado pelo
  ProLeap em VB6, para que o grafo, o slicing e o RuleExtractor não tenham um
  caminho paralelo por linguagem.

## Mapeamento para o modelo de domínio

| COBOL | Tipo de domínio |
|---|---|
| Copybook (`01`, `05`) | `Variable` (grupos e elementos) |
| `PROCEDURE DIVISION`, `PARAGRAPH` | `Procedure` |
| `PERFORM ... THRU` | `Call` (`STATIC` quando o alvo resolve) |
| `CALL "LITERAL"` | `Call` (`EXTERNAL` ou `DYNAMIC`) |
| `EXEC SQL` | `SqlStatement` |
| `WORKING-STORAGE` / `FILE SECTION` | `Component` + variáveis |
| `FILE-CONTROL` / `SELECT` | `DatabaseObject` ou dataset |

## Indicações

- **Copybooks são o contrato de dados.** A estrutura `01`/`05` com `PIC` e
  `OCCURS` descreve o layout; `OCCURS DEPENDING ON` é dinámico e precisa ser
  marcado.
- **`PERFORM` aninhado** é a unidade de fluxo. `PERFORM A THRU B` executa um
  intervalo de parágrafos, e a análise precisa expandir o intervalo — tratá-lo
  como uma chamada só perde a lógica entre `A` e `B`.
- **`EXEC SQL` com host variables** é o caminho de acesso a dados; passar por
  normalização antes de qualquer interpretação.

## Restrições e armadilhas

O que **nunca** assumir em COBOL:

- **Arredondamento e `PIC`:** `PIC S9(9)V99 COMP-3` (packed decimal) tem
 semântica de escala e arredondamento própria. Não derivar regra de valor sem
  considerar a `PIC`; um `COMP-3` truncado é um bug clássico.
- **Copybook `REDEFINES`:** o mesmo byte pode ter layouts diferentes conforme o
  contexto. Um tipo único para o grupo é uma simplificação; marcar a
  ambiguidade em vez de escolher um layout.
- **`COMP-3` e `COMP` vs `DISPLAY`:** mesma Gama de valores, representações
  distintas. Confundir as duas ao ler um arquivo é um erro silencioso.
- **Código FIXED (colunas 1–72):** coluna 73 em diante é ignorada pelo
  compilador, mas presente no arquivo. Ler sem truncar produz código que nunca
  executou.
- **Nomes de 8 caracteres e abreviação:** `MOVE X TO Y` onde `Y` foi declarado
  num copybook com nome abreviado pode resolver errado sem `PICTURE` explícita.
  Marcar `UNRESOLVED` em vez de adivinhar.
- **`GO TO` e `ALTER`:** o mesmo problema de fluxo não-linear do VB6, mais
  `ALTER`, que modifica o destino de um `GO TO` em tempo de execução.
- **`ON SIZE ERROR`:** aritmética com overflow tem caminho de erro explícito;
  o tratamento é parte da regra, não boilerplate.
- **Preprocessor (`CBL`) e `COPY`:** o código efetivo depende da preprocessação.
  Se o preprocessor não for executado, o que se analisou não é o que compila —
  isso é um limite de cobertura, e precisa ser declarado.

## SQL e banco de dados

- **`EXEC SQL ... END-EXEC`:** SQL embutido é o caso comum; normalizar antes de
  interpretar.
- **`CALL ... USING` / `LINKAGE SECTION`:** parâmetros por referência. A
  direção (entrada/saída) depende da `COPY` da interface ou do catalogo, e não
  é óbvia do `USING` — que é exatamente a informação que não pode ser
  inventada.
- **`SELECT` do `FILE SECTION` vs tabela relacional:** um dataset não é uma
  tabela. Modelar `FileObject` explicitamente evita tratar um arquivo
  sequencial como tabela consultável.
- **Host variables em `EXEC SQL`:** são os parâmetros de verdade; concatenações
  dinâmicas precisam de `is_dynamic` quando não reconstruíveis.

## Exemplos canônicos

Trechos que disparariam as heurísticas:

```cobol
       WORKING-STORAGE SECTION.
       01  WS-CLIENTE.
           05  WS-ID         PIC 9(9) COMP-3.
           05  WS-VALOR      PIC S9(9)V99 COMP-3.
           05  WS-TIPO       PIC X.
       01  WS-STATUS       PIC XX VALUE "EXECUTANDO".
```

```cobol
       EXEC SQL
           SELECT ID, NOME
             INTO :WS-ID, :WS-NOME
             FROM CLIENTE
            WHERE ID = :WS-ID-ENTRADA
       END-EXEC.
```

## O que esta orientação proíbe

Se o dialecto, o preprocessor ou a direção de um `USING` não puderem ser
determinados a partir da evidência, o resultado é `UNKNOWN` — não um layout
provável. E, como em VB6, o Golden Master de um adapter COBOL tem que vir de
evidência real de execução (JCL output, trace do DB2, dump de arquivo), nunca
de expectativa gerada.
