# ADR 003 — SQLite para o grafo no MVP

**Status:** aceito · **Data:** 2026-09-28

## Contexto

O grafo de conhecimento precisa ser persistido e consultado: clusters, hubs,
graus de centralidade, e a relação entre fluxo, regra e divergência. A
especificação menciona Neo4j como opção de persistência de grafo.

A pergunta é se o grafo merece um banco de grafo dedicado no MVP.

## Decisão

Persistir o grafo em **SQLite**, no arquivo `graph/system.db`, com tabelas
`nodes`, `edges`, `clusters`, `hubs`, `flows` e `meta`. Além do SQLite, o
pipeline também exporta GraphML, CSV e JSON, para consumo por ferramentas
externas. Nenhum servidor de banco é necessário.

## Razões

**O grafo é um artefato, não um serviço.** O pipeline produz o grafo em uma
execução e o consulta. Não há aplicação de longa duração que precise de
consultas concorrentes de baixa latência — que é exatamente o caso em que um
banco de grafo se paga.

**SQLite é embutido e queryable.** O `.db` é um arquivo. Rodar o pipeline não
requer Docker, servidor, credencial, nem porta. Isso mantém o `make test`
rápido e o ambiente reprodutível.

**As consultas são simples.** O que o pipeline faz com o grafo é percorrer
vizinhos, agregar graus, e junentar fluxo–regra–divergência. SQL lida com
isso; o diferencial do Neo4j (traversões de profundidade arbitrária com
variáveis) não é exigido aqui.

**Consultar por SQL abre o caminho para ferramentas existentes.** Com o `.db`,
qualquer pessoa com `sqlite3` pode responder "quais procedimentos leem
`CUSTOMER`?" sem instalar nada. Com GraphML, seria necessário Gephi ou
similar.

**O custo de sair é baixo.** Se a escala grow até justificar Neo4j, o
`SystemGraph` em memória já é a fonte da verdade e a escrita em SQLite é uma
projeção. Trocar o backend de persistência não toca o domain model.

## Alternativas consideradas

**Neo4j.** A escolha "correta" para um banco de grafo, e o que a
especificação sugeria. Contra: exige um servidor em execução (Docker ou
instalação) para o pipeline funcionar, adiciona um serviço à infra, e traz um
modelo de dados (labels, relacionamentos) que teria de duplicar o que
`networkx` já dá em memória. Para o volume de um sistema VB6 de porte médio, o
custo operacional não se justifica.

**NetworkX apenas em memória, sem persistência.** O mais simples possível.
Contra: obriga a reexecutar o parser para qualquer consulta, e o artefacto
de grafo é justamente o que se quer versionar e distribuir.

**PostgreSQL com `ltree`.** Mais próximo do "banco de verdade" corporativo.
Contra: mesmo custo de servidor do Neo4j, sem o diferencial de travessias.

## Consequências

**Boas**
- Zero infraestrutura para rodar o pipeline inteiro.
- O `.db` é portátil e auto-contido; dá para anexar a um bug report.
- Consultas de verificação são feitas com SQL padrão.

**Ruins**
- Não escala aabytes de arestas; irrelevant para o escopo, mas é o limite
  caso o alvo cresça muito.
- Sem acesso concorrente: o SQLite é single-writer. Como o pipeline é
  sequencial, não há impacto hoje.
- Consultas de travessia profunda são menos naturais em SQL que em Cypher.

**Como isso evolui**
Se um dia um sistema legado for grande o suficiente para justificar um banco
de grafo, o `SystemGraph` em memória já é a fonte da verdade. O passo é
escrever um `SqliteRepository` alternativo ao lado, não reescrever o pipeline.
