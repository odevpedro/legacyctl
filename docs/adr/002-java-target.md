# ADR 002 — Java 21 / Spring Boot como alvo do demo

**Status:** aceito · **Data:** 2026-09-28

## Contexto

O pipeline gera código a partir do contrato OpenAPI. A escolha do alvo
determina o gerador, o build e o formato da evidência de que o código gerado
realmente compila.

O codebase VB6 de origem provavelmente roda sobre SQL Server e, no contexto
da modernização, o time alvo é Java.

## Decisão

O demo gera **Java 21 com Spring Boot**, via `openapi-generator` com o
generator `spring`, e compila em bytecode real com
`maven:3.9-eclipse-temurin-21`. A evidência de sucesso é a produção de
arquivos `.class`, não a ausência de erro do gerador.

### Divergência registrada: Maven em vez de Gradle

O enunciado do MVP lista **Gradle** como build do alvo Java. Este MVP usa
Maven. A escolha é deliberada e fica registrada aqui em vez de ser silenciosa:

- `maven:3.9-eclipse-temurin-21` é a imagem oficial do build, com cache de
  dependências auditável e sem wrapper gerado por tooling local.
- O `openapi-generator` emite `pom.xml` por padrão para o generator `spring`;
  usar Gradle exigiria `openapi-generator-gradle-plugin`, que roda o gerador
  dentro do build em vez de no container que já produz a evidência de
  compilação.
- O ponto que o ADR quer provar — que o contrato gera código que **compila de
  verdade** — é indiferente ao build. `.class` em disco é `.class` em disco.

Trocar para Gradle é trabalho pequeno e mecânico (imagem do container, plugin
do gerador, `build.gradle.kts` no lugar do `pom.xml`), e não está bloqueado por
nada aqui. Fica como o próximo passo natural, não como uma descoberta.


## Razões

**O alvo tem que ser o destino real da modernização.** Gerar para uma
plataforma que ninguém vai adotar torna o contrato um exercício acadêmico. O
ponto do pipeline é ser a ponte entre o VB6 e o sistema que vai substituí-lo.

**A cadeia completa é verificável em Java.** OpenAPI Generator é a
implementação mais madura de geração a partir de contrato, e o ecosystem
Spring é onde a maioria dos sistemas Java legados é reescrita. Compilar de
verdade prova que o contrato é consistente: um schema inválido vira um erro de
compilação, não um `.java` que ninguém jamais executou.

**Java 21 é LTS e moderno.** Records, sealed interfaces e pattern matching
deixam a superfície gerada mais limpa, e o suporte da Oracle é longo.

**O bytecode é a prova honesta.** O tool verifica que existem `.class` no
`target/classes`. Um gerador que "tem sucesso" mas não produz nada é falha,
não sucesso — e o build é separado da geração justamente para tornar essa
distinção observável.

## Alternativas consideradas

**.NET / C#.** Forte em Windows, e VB6 é Windows. Mas o time alvo e o
ambiente de deploy não são .NET, e isso reintroduziria a pergunta sobre
qual runtime está disponível.

**Node.js / TypeScript.** Geração simples e rápida, mas a pergunta "isso
compila?" fica mais difícil de responder com o mesmo rigor, e o time não é de
Node.

**Gerar apenas fonte, sem compilar.** Mais rápido, e foi o estado anterior do
projeto. Contra: perde a única verificação barata de que o contrato é válido.
A compilação é o que transforma "geramos um contrato" em "geramos algo que
roda".

## Consequências

**Boas**
- A existência de `.class` é um critério de sucesso objetivo e automatizável.
- Trocar o alvo é trocar dois parâmetros: `generator` e a imagem de build.
- O time vê Java familiar, que é onde o trabalho manual acontece.

**Ruins**
- Java build em container é lento na primeira execução (cache Maven é um
  volume nomeado para amortizar).
- O gerador é uma dependência externa pesada: a imagem
  `openapitools/openapi-generator-cli:v7.14.0` é fixada por tag, nunca
  `latest`, para que uma atualização do upstream não mude a saída entre
  execuções.
- Docker é necessário para o stage de codegen, o que impede rodar o pipeline
  completo sem daemon.

**Como isso evolui**
Se o alvo do projeto mudar, o contrato OpenAPI é o que fica — mudar o gerador
é configuração. Nada do domain model depende de Java.
