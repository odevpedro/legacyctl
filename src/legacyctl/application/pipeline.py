"""The pipeline as one object: parse -> graph -> slice -> rules -> contract -> code -> verify.

Each stage is a separate call with its own artifact on disk, because the whole
point of the tool is that a reviewer can stop between two stages and look at
what came out. The convenience methods here chain stages, but they never skip
one silently -- in particular nothing generates a contract from a catalog, and
nothing reports a compile that did not happen.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from ..clustering import ClusterAnalyzer
from ..codegen import CodegenError, CodegenRequest, CodegenResult, compile_java, generate
from ..contracts import ApiContract, ContractError, build_contract, write_contract
from ..domain.enums import RuleStatus
from ..domain.models import BusinessRule, GoldenMasterCase, LegacySystem, SystemGraph
from ..graph import LegacyGraphBuilder, export
from ..observability import get_log
from ..parsers.vb6 import VB6Parser
from ..reporting import (
    render_legacy_report,
    write_divergence_report,
    write_legacy_report,
)
from ..rules import RuleCatalogStore, RuleExtractor
from ..slicing import FlowSlicer, SliceResult
from ..verification import GoldenMasterStore, VerificationRun, Verifier, build_new_system


class PipelineError(RuntimeError):
    """A stage could not be carried out."""


@dataclass(slots=True)
class PipelinePaths:
    """Where each stage's artifact lives. One root, so a run is self-contained."""

    output: Path
    log_file: Path | None = None

    @property
    def catalog(self) -> Path:
        return self.output / "catalog" / "rules.yaml"

    @property
    def contract(self) -> Path:
        return self.output / "contracts" / "openapi.yaml"

    @property
    def java(self) -> Path:
        return self.output / "java"

    @property
    def slices(self) -> Path:
        return self.output / "slices"

    @property
    def divergence(self) -> Path:
        return self.output / "reports" / "divergence-report.md"

    @property
    def legacy_report(self) -> Path:
        return self.output / "reports" / "legacy-report.md"

    @property
    def run_log(self) -> Path:
        return self.log_file or (self.output / "logs" / "run.jsonl")

    @property
    def system_file(self) -> Path:
        return self.output / "system.json"

    @property
    def graph_dir(self) -> Path:
        return self.output / "graph"

    @property
    def graph_db(self) -> Path:
        return self.output / "graph" / "system.db"


@dataclass(slots=True)
class Pipeline:
    """Stateful pipeline over one source root.

    Stages share the parsed system and graph so a command like
    ``legacyctl report`` can run alone, after a previous command produced them.
    """

    source_root: Path
    paths: PipelinePaths
    system: LegacySystem | None = None
    graph: SystemGraph | None = None
    slices: list[SliceResult] = field(default_factory=list)
    contract: ApiContract | None = None
    codegen: CodegenResult | None = None
    graph_formats: dict[str, str] = field(default_factory=dict)
    #: What the parser could not resolve. Kept because a silent gap in the
    #: knowledge is indistinguishable from a system that has no such code.
    diagnostics: list[str] = field(default_factory=list)

    # -- stage 1-3: knowledge -------------------------------------------

    def analyze(self) -> LegacySystem:
        """Parse the source root and build the graph with clusters."""
        if not self.source_root.is_dir():
            raise PipelineError(f"{self.source_root} is not a directory")
        log = get_log()
        with log.stage("analyze", source=str(self.source_root)) as record:
            parser = VB6Parser()
            self.system = parser.parse(self.source_root)
            self.diagnostics = list(parser.diagnostics)
            self.graph = ClusterAnalyzer().analyze(LegacyGraphBuilder().build(self.system))
            # The interchange formats are part of the promised output, so a
            # run produces them whether or not anyone asked for them.
            self.graph_formats = export(
                self.graph, self.paths.graph_dir, db_path=self.paths.graph_db
            )
            record.details["components"] = len(self.system.components)
            record.details["procedures"] = len(self.system.procedures)
            record.details["nodes"] = len(self.graph.nodes)
            record.details["edges"] = len(self.graph.edges)
            record.details["formats"] = sorted(self.graph_formats)
            record.details["diagnostics"] = len(self.diagnostics)
        return self.system

    def slice_flow(self, entry_point: str, depth: int) -> SliceResult:
        """Cut one flow out of the graph, sanitizing the context it carries."""
        system, graph = self._require_knowledge()
        with get_log().stage("slice", system_id=system.id, source=entry_point) as record:
            result = FlowSlicer(system, graph).slice(entry_point, depth)
            record.details["nodes"] = len(result.flow.node_ids)
            record.details["procedures"] = len(result.flow.procedure_ids)
            record.details["truncated"] = result.flow.truncated
        self.slices.append(result)
        return result

    def write_slice(self, result: SliceResult, name: str | None = None) -> Path:
        """Persist a slice: the flow, its graph and its sanitized fragments.

        The fragments on disk are already sanitized, so this file is safe to
        hand to an LLM or to a reviewer without re-checking for secrets.
        """
        stem = name or f"flow-{result.flow.id.split('-', 1)[-1].lower()}"
        path = self.paths.slices / f"{stem}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "flow": result.flow.model_dump(mode="json"),
            "graph": result.graph.model_dump(mode="json"),
            "fragments": [f.model_dump(mode="json") for f in result.fragments],
            "warnings": result.warnings,
        }
        path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
        )
        return path

    # -- stage 4: rules --------------------------------------------------

    def extract_rules(self, result: SliceResult) -> list[BusinessRule]:
        """Propose rules for a slice. These are candidates, never validated."""
        system = self.system or self._require_knowledge()[0]
        store = RuleCatalogStore(self.paths.catalog, system.id)
        report = RuleExtractor(store).extract(result.flow, result.fragments)
        return report.rules

    def load_rules(self) -> list[BusinessRule]:
        system = self.system or self._require_knowledge()[0]
        return RuleCatalogStore(self.paths.catalog, system.id).load().rules

    def review(self, rule_id: str, decision: str, reviewer: str, note: str = "") -> BusinessRule:
        """Record a human decision. This is the only path to VALIDATED."""
        system = self.system or self._require_knowledge()[0]
        store = RuleCatalogStore(self.paths.catalog, system.id)
        return store.review(rule_id, decision, reviewer, note)

    @property
    def validated_rules(self) -> list[BusinessRule]:
        return [r for r in self.load_rules() if r.status is RuleStatus.VALIDATED]

    # -- stage 5: contract and code -------------------------------------

    def generate_contract(self) -> ApiContract:
        system = self.system or self._require_knowledge()[0]
        try:
            contract = build_contract(system, self.validated_rules)
        except ContractError as error:
            raise PipelineError(str(error)) from error
        self.contract = contract
        write_contract(contract, system, self.paths.contract)
        return contract

    def generate_code(self, compile_result: bool = False) -> CodegenResult:
        """Contract -> Java source, and optionally to bytecode.

        Compilation is opt-in and reported separately, because "the catalog
        produced working Java" is the claim this tool must never make.
        """
        system = self.system or self._require_knowledge()[0]
        if self.contract is None:
            self.generate_contract()
        result = generate(
            CodegenRequest(
                contract_path=self.paths.contract,
                output_dir=self.paths.java,
                system_id=system.id,
            )
        )
        if compile_result:
            try:
                compile_java(result)
            except CodegenError as error:
                result.compiled = False
                result.compile_error = str(error)
        self.codegen = result
        return result

    # -- stage 6: verification and reporting ----------------------------

    def verify(self, golden_master: Path, provider: str | None = None) -> VerificationRun:
        """Compare curated legacy cases against the new system."""
        system, _ = self._require_knowledge()
        cases = GoldenMasterStore(golden_master).load()
        if not cases:
            raise PipelineError(
                f"no Golden Master cases at {golden_master}; verification without "
                f"curated evidence proves nothing"
            )
        run = Verifier(build_new_system(provider)).run(system.id, cases, self.load_rules())
        if run.report is not None:
            write_divergence_report(run.report, self.paths.divergence)
        return run

    def report(self, golden_master: Path | None = None, provider: str | None = None) -> Path:
        """Write ``legacy-report.md`` from whatever stages have been run.

        Stages that never ran say so in their own section; the report is not
        allowed to imply coverage it does not have. Artifacts from an earlier
        command are read back from disk, because a report that claimed
        "not generated" for a contract that exists on disk would be as wrong as
        one that claimed coverage it never had.
        """
        system, graph = self._require_knowledge()
        run: VerificationRun | None = None
        cases: list[GoldenMasterCase] = []
        if golden_master is not None and golden_master.is_file():
            run = self.verify(golden_master, provider)
            cases = GoldenMasterStore(golden_master).load()

        text = render_legacy_report(
            system=system,
            graph=graph,
            rules=self.load_rules() if self.paths.catalog.is_file() else [],
            results=run.results if run else [],
            divergences=run.report if run else None,
            cases=cases,
            contract_summary=self._contract_summary(),
            codegen_summary=self._codegen_summary(),
            notes=[f"run log: `{self.paths.run_log}`"],
        )
        return write_legacy_report(text, self.paths.legacy_report)

    def _contract_summary(self) -> str:
        if self.contract is not None:
            return f"`{self.paths.contract}` with {len(self.contract.operations)} operation(s)"
        if self.paths.contract.is_file():
            operations = self.paths.contract.read_text(encoding="utf-8").count("operationId:")
            return (
                f"`{self.paths.contract}` with {operations} operation(s), "
                f"generated by an earlier run of this pipeline"
            )
        return "not generated in this run"

    def _codegen_summary(self) -> str:
        if self.codegen is not None:
            return self.codegen.summary()
        java_files = sorted(self.paths.java.rglob("*.java"))
        if not java_files:
            return "not run in this run"
        classes = sorted(self.paths.java.rglob("*.class"))
        return (
            f"{len(java_files)} Java file(s) present in `{self.paths.java}` "
            f"({len(classes)} .class file(s) built), from an earlier run"
        )

    # -- internals -------------------------------------------------------

    def _require_knowledge(self) -> tuple[LegacySystem, SystemGraph]:
        if self.system is None or self.graph is None:
            raise PipelineError("no knowledge yet: run analyze first")
        return self.system, self.graph
