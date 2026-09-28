"""``legacyctl`` -- one command per pipeline stage.

Design rules that shaped this CLI:

* **No hidden state.** Commands read and write files under ``--output``; a
  command that needs a previous stage says which artifact is missing instead of
  silently reparsing.
* **No colour as information.** Colour is decoration only; every conclusion is
  a word.
* **Exit codes mean something.** 0 is success, 1 is a real failure (a divergence,
  a refused rule, a missing artifact), 2 is a usage error.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from ..application import Pipeline, PipelineError, PipelinePaths
from ..codegen import CodegenError
from ..domain.enums import RuleStatus, VerifyStatus
from ..domain.models import BusinessFlow, SourceFragment, SystemGraph
from ..observability import ObservabilityLog, get_log
from ..slicing import SliceResult

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2

_GREEN = "\033[32m"
_RED = "\033[31m"
_YELLOW = "\033[33m"
_DIM = "\033[2m"
_RESET = "\033[0m"


def _colour(text: str, code: str, enabled: bool) -> str:
    return f"{code}{text}{_RESET}" if enabled else text


def _say(text: str, code: str = "", colour: bool = True) -> None:
    print(_colour(text, code, colour) if code else text)


def _pipeline(args: argparse.Namespace) -> Pipeline:
    source = getattr(args, "path", None) or args.source
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    # One log per run, so a later report can point at the run that produced it.
    log = ObservabilityLog(output / "logs" / "run.jsonl")
    import legacyctl.observability as obs

    obs._default_log = log
    return Pipeline(source_root=Path(source), paths=PipelinePaths(output=output))


def cmd_analyze(args: argparse.Namespace) -> int:
    pipeline = _pipeline(args)
    system = pipeline.analyze()
    graph = pipeline.graph
    assert graph is not None  # analyze() always produces the graph
    _say(f"parsed {len(system.components)} component(s), {len(system.procedures)} procedure(s)")
    _say(
        f"graph: {len(graph.nodes)} node(s), {len(graph.edges)} edge(s), "
        f"{len(graph.clusters)} cluster(s), {len(graph.hubs)} hub(s)"
    )
    for name, path in sorted(pipeline.graph_formats.items()):
        _say(f"  {name}: {path}", _DIM)
    for diagnostic in pipeline.diagnostics:
        _say(f"  ! {diagnostic}", _YELLOW)
    for warning in graph.warnings:
        _say(f"  ! {warning}", _YELLOW)
    return EXIT_OK


def cmd_clusters(args: argparse.Namespace) -> int:
    pipeline = _pipeline(args)
    _require_analyzed(pipeline, args)
    graph = pipeline.graph
    assert graph is not None
    _say(f"{len(graph.clusters)} cluster(s), algorithm {graph.algorithm}")
    for cluster in sorted(graph.clusters, key=lambda c: -c.size):
        share = f"{cluster.share_of_system:.1%}"
        _say(
            f"  {cluster.id}: {cluster.size} node(s), {share} of the system"
            + (" [giant]" if cluster.is_giant else "")
        )
    for hub in graph.hubs:
        _say(f"  hub {hub.id}: {hub.name} (degree {hub.degree}, {hub.reason})", _YELLOW)
    return EXIT_OK


def cmd_slice(args: argparse.Namespace) -> int:
    pipeline = _pipeline(args)
    _require_analyzed(pipeline, args)
    try:
        result = pipeline.slice_flow(args.entry, args.depth)
    except Exception as error:
        _say(str(error), _RED)
        return EXIT_FAILURE
    path = pipeline.write_slice(result, args.name)
    flow = result.flow
    _say(
        f"slice {flow.id}: {len(flow.node_ids)} node(s), {len(flow.procedure_ids)} "
        f"procedure(s), {len(flow.sql_ids)} SQL, depth {flow.depth}"
    )
    if flow.truncated:
        _say("  ! the depth limit stopped this slice; it is bounded, not complete", _YELLOW)
    for hub in result.warnings:
        _say(f"  ! {hub}", _YELLOW)
    _say(f"written: {path}")
    return EXIT_OK


def cmd_extract_rules(args: argparse.Namespace) -> int:
    pipeline = _pipeline(args)
    _require_analyzed(pipeline, args)
    slice_file = Path(args.slice_file)
    if not slice_file.is_file():
        _say(f"no slice at {slice_file}; run 'legacyctl slice' first", _RED)
        return EXIT_FAILURE
    result = _load_slice(slice_file)
    rules = pipeline.extract_rules(result)
    _say(f"extracted {len(rules)} candidate rule(s) into {pipeline.paths.catalog}")
    _say("candidates are not in the contract until a human reviews them", _YELLOW)
    return EXIT_OK


def _load_slice(path: Path) -> SliceResult:
    """Rehydrate a slice written by ``legacyctl slice``.

    The flow and its graph travel together so rule extraction can run in a
    later process without re-reading the legacy sources.
    """
    payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return SliceResult(
        flow=BusinessFlow.model_validate(payload["flow"]),
        graph=SystemGraph.model_validate(payload["graph"]),
        fragments=[SourceFragment.model_validate(f) for f in payload.get("fragments", [])],
        warnings=list(payload.get("warnings", [])),
    )


def cmd_rules_list(args: argparse.Namespace) -> int:
    pipeline = _pipeline(args)
    _require_analyzed(pipeline, args)
    rules = pipeline.load_rules()
    if not rules:
        _say("no rules yet; run 'legacyctl extract-rules' first", _YELLOW)
        return EXIT_OK
    for rule in sorted(rules, key=lambda r: r.id):
        source = f"{rule.sources[0].file}:{rule.sources[0].line}" if rule.sources else "-"
        colour = _GREEN if rule.status is RuleStatus.VALIDATED else _YELLOW
        _say(
            f"{rule.id}  {rule.status.value:<10} {rule.name}  "
            + _colour(f"[{source}]", _DIM, True),
            colour,
        )
    return EXIT_OK


def cmd_rule_review(args: argparse.Namespace) -> int:
    pipeline = _pipeline(args)
    _require_analyzed(pipeline, args)
    try:
        rule = pipeline.review(args.rule_id, args.decision, args.reviewer, args.note or "")
    except Exception as error:
        _say(str(error), _RED)
        return EXIT_FAILURE
    _say(f"{rule.id} is now {rule.status.value} (reviewed by {rule.reviewed_by})")
    return EXIT_OK


def cmd_review_all(args: argparse.Namespace) -> int:
    """Decide every candidate at once.

    This exists for demos and for a reviewer who has read the whole catalog in
    one sitting. It never promotes silently: the decision, the reviewer name
    and the note are written into every rule, so a later reader can see that
    the gate was opened deliberately rather than by a bug.
    """
    pipeline = _pipeline(args)
    _require_analyzed(pipeline, args)
    candidates = [r for r in pipeline.load_rules() if r.status is RuleStatus.CANDIDATE]
    if not candidates:
        _say("no candidate rules to decide")
        return EXIT_OK
    _say(
        f"deciding {len(candidates)} candidate rule(s) as {args.decision} "
        f"on behalf of {args.reviewer}",
        _YELLOW,
    )
    for rule in candidates:
        pipeline.review(rule.id, args.decision, args.reviewer, args.note or "")
        _say(f"  {rule.id} {args.decision}d")
    return EXIT_OK


def cmd_validate(args: argparse.Namespace) -> int:
    pipeline = _pipeline(args)
    _require_analyzed(pipeline, args)
    rules = pipeline.load_rules()
    candidates = [r for r in rules if r.status is RuleStatus.CANDIDATE]
    if candidates:
        _say(
            f"{len(candidates)} rule(s) await review; they are excluded from the contract:",
            _YELLOW,
        )
        for rule in candidates:
            _say(f"  {rule.id}  {rule.name}")
    if args.require_validated and candidates:
        _say("review them with 'legacyctl rule review <id> --decision approve'", _YELLOW)
        return EXIT_FAILURE
    _say(f"{len(rules) - len(candidates)} rule(s) validated")
    return EXIT_OK


def cmd_contract_generate(args: argparse.Namespace) -> int:
    pipeline = _pipeline(args)
    _require_analyzed(pipeline, args)
    try:
        contract = pipeline.generate_contract()
    except PipelineError as error:
        _say(str(error), _RED)
        return EXIT_FAILURE
    _say(f"contract: {pipeline.paths.contract}")
    _say(f"  {len(contract.operations)} operation(s) from validated rules only")
    _say(f"  {len(contract.states)} state(s) observed in the legacy system")
    return EXIT_OK


def cmd_codegen(args: argparse.Namespace) -> int:
    pipeline = _pipeline(args)
    _require_analyzed(pipeline, args)
    try:
        result = pipeline.generate_code(compile_result=args.compile)
    except (CodegenError, PipelineError) as error:
        _say(str(error), _RED)
        return EXIT_FAILURE
    _say(result.summary())
    if result.compiled:
        _say(f"  {len(result.class_files)} .class file(s) produced", _GREEN)
    elif result.compile_error:
        _say(f"  {result.compile_error}", _RED)
    return EXIT_OK


def cmd_verify(args: argparse.Namespace) -> int:
    pipeline = _pipeline(args)
    _require_analyzed(pipeline, args)
    try:
        run = pipeline.verify(Path(args.golden_master), args.new_system)
    except PipelineError as error:
        _say(str(error), _RED)
        return EXIT_FAILURE
    _say(
        f"{run.passed} passed, {run.failed} failed, {run.skipped} not decidable "
        f"(new system: {run.provider}"
        + ("" if run.provider_is_real else ", a simulation of the validated rules")
        + ")"
    )
    # Compare against the enum, not a string: the values are upper case, and
    # a string comparison silently printed every case as a warning.
    for result in run.results:
        if result.status is VerifyStatus.PASSED:
            continue
        colour = _RED if result.status is VerifyStatus.FAILED else _YELLOW
        _say(f"  {result.case_id}: {result.status.value}", colour)
        for divergence in result.divergences:
            _say(f"    - {divergence}")
    if run.report is not None:
        _say(f"divergence report: {pipeline.paths.divergence}")
    if not run.ok:
        return EXIT_FAILURE
    return EXIT_OK


def cmd_report(args: argparse.Namespace) -> int:
    pipeline = _pipeline(args)
    _require_analyzed(pipeline, args)
    golden_master = Path(args.golden_master) if args.golden_master else None
    path = pipeline.report(golden_master, args.new_system)
    _say(f"report: {path}")
    _say(f"run log: {pipeline.paths.run_log}")
    return EXIT_OK


def _require_analyzed(pipeline: Pipeline, args: argparse.Namespace) -> None:
    """Make the knowledge available, saying so when it has to be rebuilt.

    Parsing is cheap and deterministic, so a standalone command re-parses
    rather than reading a possibly stale artifact. The rebuild is announced:
    a command that silently used yesterday's graph would be worse than useless.
    """
    if pipeline.system is not None:
        return
    _say(
        f"re-parsing {pipeline.source_root}: this command runs on its own and no "
        f"knowledge was in memory",
        _DIM,
    )
    pipeline.analyze()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="legacyctl",
        description=(
            "Turn a VB6 system into knowledge, an OpenAPI contract and Java 21 "
            "source -- and check the new system against the legacy behaviour."
        ),
    )
    parser.add_argument("--source", default="fixtures/vb6", help="legacy source root")
    parser.add_argument("--output", default="output", help="artifact directory")
    parser.add_argument("--no-colour", action="store_true", help="plain text output")
    sub = parser.add_subparsers(dest="command", required=True)

    an = sub.add_parser("analyze", help="parse the sources and build the graph")
    an.add_argument("path", nargs="?", help="legacy source root (overrides --source)")
    an.set_defaults(func=cmd_analyze)

    clusters = sub.add_parser("clusters", help="list clusters and hubs")
    clusters.add_argument("path", nargs="?", help="legacy source root (overrides --source)")
    clusters.set_defaults(func=cmd_clusters)

    sl = sub.add_parser("slice", help="cut one flow out of the graph")
    sl.add_argument("--entry", required=True, help="entry point, e.g. Form.Procedure")
    sl.add_argument("--depth", type=int, default=5, help="traversal depth (default 5)")
    sl.add_argument("--name", help="file stem for the slice JSON")
    sl.set_defaults(func=cmd_slice)

    ex = sub.add_parser("extract-rules", help="propose candidate rules for a slice")
    ex.add_argument("--slice", dest="slice_file", required=True, help="slice JSON from 'slice'")
    ex.set_defaults(func=cmd_extract_rules)

    sub.add_parser("rules", help="list the rule catalog").set_defaults(func=cmd_rules_list)

    ra = sub.add_parser("review-all", help="decide every candidate at once (record who, and why)")
    ra.add_argument("--decision", choices=["approve", "reject"], default="approve")
    ra.add_argument("--reviewer", required=True, help="who is deciding")
    ra.add_argument("--note", default="bulk review", help="recorded on every rule")
    ra.set_defaults(func=cmd_review_all)

    rv = sub.add_parser("review", help="approve or reject a candidate rule")
    rv.add_argument("rule_id")
    rv.add_argument("--decision", choices=["approve", "reject"], default="approve")
    rv.add_argument("--reviewer", default="cli", help="who is deciding")
    rv.add_argument("--note", help="why, recorded in the catalog")
    rv.set_defaults(func=cmd_rule_review)

    va = sub.add_parser("validate", help="check the catalog is reviewable")
    va.add_argument(
        "--require-validated",
        action="store_true",
        help="exit non-zero while candidates remain",
    )
    va.set_defaults(func=cmd_validate)

    sub.add_parser("contract", help="generate the OpenAPI contract").set_defaults(
        func=cmd_contract_generate
    )

    cg = sub.add_parser("codegen", help="contract -> Java 21 source")
    cg.add_argument("--compile", action="store_true", help="also build the generated project")
    cg.set_defaults(func=cmd_codegen)

    vf = sub.add_parser("verify", help="Golden Master vs the new system")
    vf.add_argument("--golden-master", required=True, help="curated cases YAML")
    vf.add_argument(
        "--new-system",
        choices=["simulator", "http"],
        default="simulator",
        help="where to ask the new system (default: the rule simulator)",
    )
    vf.set_defaults(func=cmd_verify)

    rp = sub.add_parser("report", help="write legacy-report.md")
    rp.add_argument("--golden-master", help="curated cases YAML, if available")
    rp.add_argument("--new-system", choices=["simulator", "http"], default="simulator")
    rp.set_defaults(func=cmd_report)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "no_colour", False):
        global _GREEN, _RED, _YELLOW, _DIM, _RESET
        _GREEN = _RED = _YELLOW = _DIM = _RESET = ""
    try:
        return int(args.func(args))
    except SystemExit as error:
        return int(error.code or 0)
    except (PipelineError, CodegenError) as error:
        _say(str(error), _RED, colour=not getattr(args, "no_colour", False))
        return EXIT_FAILURE
    finally:
        _emit_log(args)


def _emit_log(args: argparse.Namespace) -> None:
    log = get_log()
    if log.records:
        print(_colour(f"  ({len(log.records)} run record(s) logged)", _DIM, True), file=sys.stderr)


def run() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    run()
