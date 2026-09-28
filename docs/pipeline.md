# Pipeline

Eight stages, each with its own artifact on disk, each able to stop the run.
The stages are separate on purpose: the whole point of the tool is that a
person can stop between two of them and read what came out.

```
  sources
     │  ① analyze
     ▼
 LegacySystem ──► SystemGraph ──────────────┬── GraphML / CSV / SQLite
     │              │                      │
     │ ② slice      │                      │
     ▼              ▼                      │
 BusinessFlow ◄─────┘                      │
     │ ③ extract-rules                     │
     ▼                                     │
 rule catalog (CANDIDATE)                   │
     │ ④ review          ◄── the human gate │
     ▼                                     │
 (VALIDATED) ──⑤ contract──► OpenAPI 3.1    │
                          │                 │
                          ⑥ codegen        │
                          ▼                 │
                    .java ── .class         │
                          │                 │
     ⑦ verify ◄───────────┘                 │
        ▼                                   │
     divergence report ──⑧ report───────────┘
                        ▼
                 legacy-report.md
```

## ① `legacyctl analyze <root>`

Parses the sources, builds the graph, clusters it, and writes every interchange
format.

**Artifacts:** `graph/system.graphml`, `graph-nodes.csv`, `graph-edges.csv`,
`graph/system.db` (SQLite, queryable), `logs/run.jsonl`

Refuses a non-directory. Reports parse diagnostics separately from graph
warnings, because a gap in the knowledge and a structural observation are
different things and a reader who cannot tell them apart will ignore both.

## ② `legacyctl slice --entry <E> --depth <N>`

Cuts one flow: the subgraph reachable from a single entry point, plus the source
fragments of exactly those procedures, plus their SQL, plus the state
transitions they perform.

**Artifact:** `slices/flow-<id>.json` (flow, graph, sanitized fragments)

Bounded by design. It follows only edges that exist — a call the parser could
not resolve produces no edge, so it cannot silently widen the slice. Reaching
the depth boundary sets `truncated`, which is a result, not a failure.

## ③ `legacyctl extract-rules --slice <file>`

Proposes candidate rules for one slice. This is the only stage that may be
uncertain, and it is isolated behind `RuleExtractorPort`.

**Artifact:** `catalog/rules.yaml`

Every rule arrives as `CANDIDATE` with mandatory `sources`. The offline
deterministic extractor is the default; a hosted model is opt-in and its
provider, model and token estimates are logged per run.

## ④ `legacyctl review <rule-id>` — the human gate

The only path to `VALIDATED`. Records who decided, when, and why.

`legacyctl review-all` exists for demos and bulk sessions. It does not promote
silently: the reviewer and the note are written onto every rule, so a catalog
always shows whether the gate was opened deliberately.

`make pipeline` stops here on purpose. A Makefile that auto-approves would make
the gate decorative.

## ⑤ `legacyctl contract`

Builds OpenAPI 3.1 from **validated rules only**.

**Artifact:** `contracts/openapi.yaml`

Refuses to run with zero validated rules. Carries `x-business-rules`,
`x-legacy-evidence`, `x-legacy-condition` and `x-legacy-behavior`; the observed
state names become a schema; OUT parameter schemas come from the database
procedures.

## ⑥ `legacyctl codegen [--compile]`

```
OpenAPI ──► openapi-generator ──► .java ──► Maven build ──► .class
```

**Artifacts:** `java/` (11 `.java`), `java/target/classes/` (12 `.class`),
`java/codegen-generator.log`

Pinned generator image, pinned Maven image, named-volume dependency cache.
Runs as the host uid so `output/` stays deletable.

Three checks turn a silent failure into a loud one: a missing contract, an
absent `pom.xml`/`src/main/java`, and a build that reports success while
producing no `.class`.

Business logic is never generated. The output is a contractual surface, and the
rule id plus the legacy `file:line` travel in the Javadoc so whoever implements
it can see what they are implementing.

## ⑦ `legacyctl verify --golden-master <file>`

Compares curated legacy cases against the new system.

**Artifacts:** `reports/divergence-report.md`, results in memory

`--new-system simulator` (default) evaluates the validated rules honestly: a
guard whose behaviour the legacy code never states reports `fired`/`not_fired`,
never a fabricated `accept`/`reject`. `--new-system http` posts the case to a
deployed service via `LEGACYCTL_NEW_SYSTEM_URL`.

Outcomes: `PASSED`, `FAILED`, `SKIPPED`. Only validated rules can answer, so an
unreviewed rule surfaces as *missing* rather than being quietly simulated.

## ⑧ `legacyctl report`

Writes `legacy-report.md`: system overview, structural metrics, graph metrics,
clusters, hubs, flows, extracted rules, validation status, API contracts,
generated code, verification results, divergences and traceability.

Stages that did not run say so in their own section, and artifacts from an
earlier command are read back from disk — the report must neither invent
coverage nor understate work that was done.

## The feedback loop

```
Divergence ──► rule id ──► catalog ──► human decides
     ▲                                    │
     └────── re-verify ◄── fix the rule or the new system ◄─┘
```

A divergence never changes a rule by itself. It points at one, and the decision
to keep or change it is a person's, recorded in the catalog.

## Running it

```bash
make demo     # the whole chain, opening the review gate as "make-demo"
make pipeline # stops at the review gate
make test     # unit tests: no LLM, no Docker, no Java
```

Individual stages:

```bash
legacyctl analyze fixtures/vb6
legacyctl slice --entry CustomerForm.ValidateCustomer --depth 5
legacyctl extract-rules --slice output/vb6/slices/flow-customerform_validatecustomer.json
legacyctl review RULE-CUSTOMERFORM_VALIDATECUSTOMER-002 --reviewer ana --note "..."
legacyctl contract
legacyctl codegen --compile
legacyctl verify --golden-master catalog/golden-master/vb6.yaml
legacyctl report --golden-master catalog/golden-master/vb6.yaml
```

## Exit codes

| Code | Meaning |
|---|---|
| 0 | the stage succeeded |
| 1 | a real failure: a divergence, a refusal, a missing artifact |
| 2 | usage error |

`verify` exits 1 when anything diverged or any divergence is blocking. A run
with `SKIPPED` cases does not exit 0 on a false green: the skipped count is
printed so the result is visibly narrower than it looks.
