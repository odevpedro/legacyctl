# Architecture

## The problem this tool exists to solve

A VB6 system does not say what it does. It is a folder of `.bas` files whose
behaviour is spread across call chains, SQL statements, an implicit state
machine, and procedure OUT parameters. To modernize it, someone has to write
down *what it does* first, and every claim about behaviour has to point at the
code that justifies it.

The risk in automating that step is specific: a tool that guesses confidently
produces a confident, wrong specification, and the mistake surfaces months
later as a production incident. So the design goal is not "extract rules
automatically". It is **make every claim traceable, and refuse to claim what
cannot be shown**.

## Layering

Dependencies point downwards only. Nothing below knows about the layer above it.

```
                        cli/            legacyctl <command>
                                     one command per stage
                          |
                     application/      orchestration, artifact paths
                          |
   +----------+-----------+-----------+------------+-----------+
   |          |           |           |            |           |
parsers/  graph/     clustering/   slicing/     rules/     contracts/
   |          |           |           |            |           |
   +----------+-----------+-----------+------------+-----------+
                          |
                       domain/     models, enums, deterministic ids
                          ^
                          |
        observability.py    security/sanitizer.py     config/settings.py
        (cross-cutting: every stage logs; every LLM input is redacted)
```

Below `domain/` there is no framework. `domain/models.py` is pydantic and
nothing else. The layers that are genuinely hard to get right (ids, enums,
provenance) sit at the bottom where they can be depended upon rather than
re-implemented.

## Where the intelligence is allowed to live

The thesis of the tool is that **most of the pipeline is deterministic and only
one step needs interpretation**.

| Stage | Deterministic? | Why |
|---|---|---|
| Parse (VB6 → AST) | yes | syntax, not meaning |
| SQL analysis (SQLGlot) | yes | grammar, not meaning |
| Graph, clusters, hubs | yes | edges already exist as evidence |
| Slicing | yes | bounded traversal of real edges |
| **Rule extraction** | **no** | this is where a condition becomes a *statement about the business* |
| Contract | yes | only validated rules go in |
| Codegen | yes | the contract already decided everything |
| Verification | yes | comparison, not interpretation |

Only `rules/extractor.py` is allowed to be uncertain, and it is isolated behind
`RuleExtractorPort` so the default can be a deterministic implementation that
needs no model at all. See [ADR 006](adr/006-llm-abstraction.md).

## Provenance is a type, not a comment

`BusinessRule` cannot exist without `sources: list[RuleSource]`, and
`RuleSource` carries `file`, `line` and `procedure`. A rule with no source is
rejected at construction, so "where did this come from" is answerable by the
type system rather than by reading the YAML and hoping.

The same idea governs the rest of the model: `RuleStatus` cannot be
`VALIDATED` without `reviewed_by` and `reviewed_at` being set (see
`RuleCatalogStore.review`), and a `GoldenMasterCase` without `evidence` is
refused by the loader. **The gates are in the data model, not in a
convention that a contributor can forget.**

## Determinism

Every id is derived from content via `domain/ids.py`, never from a counter or a
timestamp. Two runs over the same sources produce the same rule ids, which is
what makes a catalog diffable in a pull request and makes "did anything change?"
answerable without rerunning the pipeline.

Third-party code that could introduce nondeterminism is pinned: the OpenAPI
Generator image is a tagged version, never `latest`, and the Maven build uses a
named volume for its dependency cache so a run is not affected by what happens
to be in a local `~/.m2`.

## Failure is visible

Three mechanisms, all deliberate:

1. **`UNKNOWN` instead of a guess.** An unresolved callee, an unasserted
   parameter type, a behaviour the extractor could not determine. The absence of
   knowledge is a value in the model, not an empty string.
2. **Refusals, not defaults.** No validated rules means no contract. The
   generator reports success with an empty project, that is a hard error.
3. **The divergence report.** A verification that cannot be decided is
   `SKIPPED`, not `PASSED` — a green light built on nothing is worse than a
   visible gap.

The rule simulator enforces the same discipline: it evaluates `field <op>
operand` and returns `unknown` for anything else, because a simulator that
guesses produces false passes, and a false pass in a verification tool is the
worst possible outcome.

## Security boundary

Legacy code is full of hard-coded connection strings and passwords. The
context handed to a model is passed through `security/sanitizer.py` first, and
the log pipeline redacts again on the way out — two independent layers, because
one mistake in a redaction regex is enough to leak a password into a file that
gets committed. The slice written to disk is already sanitized, so the file
itself is safe to hand to a reviewer or a third party.

## What is deliberately not here

- **No business logic generation.** The generated Java is a contractual
  surface: DTOs, request/response models, enums, interfaces. Deciding what the
  code should *do* is the team's job; automating that guess is the failure mode
  this project exists to avoid.
- **No automatic promotion.** A model can propose a rule; only a person can
  validate one. `make pipeline` stops at that gate on purpose.
- **No hosted model by default.** The offline deterministic extractor is the
  default. A hosted model is opt-in and every run records which provider and
  which model produced each rule.

## Reading order

1. [domain-model.md](domain-model.md) — what the tool knows and why each type exists
2. [pipeline.md](pipeline.md) — the stages, their artifacts, and their refusals
3. [adr/](adr/) — why each of these choices was made over the alternative
