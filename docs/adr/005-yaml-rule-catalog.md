# ADR 005 — Catálogo de regras em YAML versionado

**Status:** aceito · **Data:** 2026-09-28

## Contexto

As regras de negócio extraídas do sistema legado são a.joia do trabalho. Elas
passam por um ciclo de vida — `CANDIDATE` → `REVIEW` → `VALIDATED` ou
`REJECTED` — e cada transição é uma decisão humana que precisa ser registrada
(quem, quando, por quê).

A pergunta é onde esse catálogo vive: banco de dados, ou um arquivo de texto
versionado no repositório?

## Decisão

O catálogo é um **arquivo YAML versionado** (`catalog/rules.yaml`), com um
campo `version` e um `system_id`. É lido e escrito por
`RuleCatalogStore`, validado contra os modelos pydantic, e cada regra
carrega seu status, confiança, fontes e histórico de revisão.

## Razões

**Git review é o processo de revisão que o time já tem.** Uma regra de negócio
é uma afirmação que alguém precisa ler e julgar. O diff de um YAML é legível:
"essa regra passou de `CANDIDATE` para `VALIDATED`" aparece como uma linha.
Com um banco, essa revisão exigiria uma ferramenta nova e a decisão ficaria
opaca para quem não tem acesso.

**O histórico é o valor.** Um banco guarda o estado atual; um arquivo
versionado guarda a *sequência* de decisões. Saber que alguém aprovou a
regra `RULE-...-002` e depois a rejeitou, e o porquê, é parte do produto. O
`review_note` e o `reviewed_by` vivem no arquivo, em cada transição.

**Diffável e mergeável.** YAML é texto. Disputas de merge sobre regras
revisadas são conflitos textuais, visíveis. Em banco, seriam conflitos de
escrita, invisíveis.

**Auditável e portável.** O catálogo é um artefato autocontido: dá para
arquivar junto com o relatório, anexar a um bug, ou entregar a um cliente. Sem
servidor, sem credencial.

**Validado, não confiável.** Embora o formato seja texto livre para o Git, ele
não é texto livre para a máquina: `RuleCatalogStore.load()` valida contra o
modelo e **recusa** um catálogo cuja regra não tem `sources`, ou cujo status é
`VALIDATED` sem `reviewed_by`. A revisão humana é uma invariante do modelo, não
uma convenção.

## Alternativas consideradas

**SQLite (o mesmo banco do grafo).** Já existe, e é consultável. Contra: um
`git diff` sobre regras de negócio é peça fundamental da revisão; binário não
faz diff. E o histórico por commit é gratuito num arquivo, difícil num banco.

**PostgreSQL com auditoria.** Robusto, com triggers de auditoria. Contra:
serviço para o caso de uso principal, e nenhum ganho proporcional ao custo.

**Markdown.** Ligeiro e legível. Contra: mais difícil de validar
programaticamente sem um parser; YAML já tem estrutura e um parser confiável
(PyYAML) além de ser commentável.

## Consequências

**Boas**
- Revisão e histórico de decisões via Git, sem ferramenta extra.
- Artefato autocontido e portátil.
- Validação forte no carregamento, independente do formato.

**Ruins**
- Em sistemas com milhares de regras, o YAML fica grande e o diff fica ruidoso.
  O escopo do MVP não tem esse volume; se tiver, a mitigação é dividir por
  sistema.
- Sem concorrência segura para escrita simultânea — aceitável, o fluxo é
  sequencial e git-based.
- YAML não é o formato mais rápido de parse, mas o volume é pequeno.

**Como isso evolui**
Se o volume de regras crescer muito, a mitigação é separar em arquivos por
sistema ou por componente, mantendo o mesmo modelo e store. O formato interno
continua sendo uma lista de `BusinessRule` validadas.
