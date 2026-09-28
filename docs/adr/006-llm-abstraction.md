# ADR 006 — O LLM isolado atrás de uma interface

**Status:** aceito · **Data:** 2026-09-28

## Contexto

A extração de regras de negócio é a única etapa do pipeline que exige
interpretação semântica: transformar "se `IsValidCustomerBasics` for falso
então mostrar mensagem e sair" em uma afirmação sobre o negócio. Isso é
reconhecimento de linguagem, não sintaxe.

Um LLM é a ferramenta natural para isso. Mas um LLM também é não-determinístico,
pode alucinar, e — se a fonte legada contiver segredos — exfiltra dados se
chamado sem cuidado.

## Decisão

O LLM é **opcional, opt-in e isolado atrás de `RuleExtractorPort`**. O
default do pipeline é `DeterministicRuleExtractor`, que roda offline e sem
modelo. Nenhuma chamada de rede ocorre a menos que o operador peça
explicitamente (`--provider` ou `LEGACYCTL_LLM_PROVIDER`).

Todo contexto enviado a um modelo passa por `security/sanitizer.py` antes.

## Razões

**Só a extração precisa de um modelo.** Parse, grafo, clustering, slicing,
contrato, codegen e verificação são determinísticos e mais confiáveis sem
modelo. Colocar um LLM em qualquer um deles introduziria variabilidade onde
não há ambiguidade. O LLM fica confinado ao único ponto onde a interpretação é
realmente o trabalho.

**O default determinístico torna o pipeline reproduzível e auditável sem
modelo.** Com `DeterministicRuleExtractor`, o `make demo` inteiro roda sem
chave de API, sem rede, e produz o mesmo resultado a cada execução. O LLM é
uma melhoria opcional, não um pré-requisito. Isso é o oposto do desenho em
que um LLM é o motor e o pipeline é uma casca.

**A porta explícita é uma barreira de segurança.** Se o LLM só é acionado com
`--provider`, então ninguém pode, por acidente, mandar código legado para
um serviço remoto. A decisão de enviar dados para fora é explícita e
documentada.

**Proveniência sobrevive ao modelo.** Uma regra, venha de onde vier, precisa de
`sources` com `arquivo:linha`, e a extração se dá sobre um slice já
sanonitizado e delimitado. O modelo propõe; a fonte justifica. Se a proposta
não puder ser ancorada no código, ela não é uma regra.

**A porta também é testabilidade.** O default offline significa que a suíte de
testes não depende de rede, chave ou comportamento de modelo. Testes de
regressão rodam em qualquer máquina.

## Alternativas consideradas

**LLM como motor obrigatório.** Mais simples de vender ("a IA entende o
legado"). Contra: torna o pipeline não-reproduzível, dependente de rede e de
chave, e cada regra passa a custar uma chamada. Para um artefato que precisa
ser revisado por humanos, menos automação determinística é um ativo.

**Chamar um modelo por etapa.** Tentador para "enriquecer" o grafo com
semântica. Contra: multiplica fontes de alucinação em etapas que já são
determinísticas, e cada chamada é um ponto onde o pipeline pode divergir entre
execuções. Mantido fora.

**Modelo local (Ollama).** Remove a questão do dado sair da máquina, e há um
`ollama.service` disponível. Contra: exige um modelo baixado e rodando, o que
quebra a premissa de "roda em qualquer máquina sem setup". Fica como opção
natural para quando o requisito de não-enviar-dados passar a ser mandatório —
o ponto de extensão (`RuleExtractorPort`) já está lá.

## Consequências

**Boas**
- Pipeline roda offline, determinístico e testável por padrão.
- Nenhum dado legado sai da máquina sem pedido explícito e registrado.
- O ponto de extensão é uma interface pequena, com implementações
  determinística e OpenAI-compatible.

**Ruins**
- A extração determinística é mais fraca que um modelo: ela reconhece padrões
  (guardas, chamadas a stored procedures, transições de estado) e marca o
  resto como `UNKNOWN`, em vez de inventar. Isso é intencional — cobertura
  menor, nenhuma alucinação.
- Suporte a múltiplos providers está incompleto: hoje há
  `DeterministicRuleExtractor` e `OpenAICompatibleExtractor`; Anthropic e Ollama
  são pontos de extensão, não implementações.
- Quando o LLM é usado, a saída ainda precisa de revisão humana; automatizar a
  etapa de interpretação não elimina a revisão.

**Como isso evolui**
A porta `RuleExtractorPort` é o ponto de extensão. Adicionar Anthropic, Ollama
ou um modelo local é implementar o protocolo e registrá-lo em
`build_extractor`, sem tocar no resto do pipeline.
