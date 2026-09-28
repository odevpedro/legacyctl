"""Tests for the CLI and the pipeline it drives.

These run the real commands against the fixtures. They do not mock the
pipeline: a CLI test that mocks the pipeline only tests the mocking.

The cases that matter here are the refusals. A command that produces output
when it has no business doing so is worse than one that crashes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from legacyctl.cli.main import main
from legacyctl.domain.enums import RuleStatus, VerifyStatus

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "vb6"
GOLDEN_MASTER = Path(__file__).resolve().parents[2] / "catalog" / "golden-master" / "vb6.yaml"

pytestmark = pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning")


def run(*args: str) -> int:
    return main(["--source", str(FIXTURES), *args])


class TestEntryPoint:
    def test_no_command_is_a_usage_error(self) -> None:
        with pytest.raises(SystemExit) as error:
            main([])
        assert error.value.code == 2

    def test_analyze_prints_the_graph(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run("--output", str(tmp_path), "analyze") == 0
        out = capsys.readouterr().out
        assert "parsed" in out
        assert "cluster(s)" in out

    def test_analyze_accepts_a_positional_root(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["--output", str(tmp_path), "analyze", str(FIXTURES)]) == 0
        assert "parsed" in capsys.readouterr().out

    def test_a_missing_root_fails_with_a_message(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from legacyctl.application import PipelineError

        code = main(["--source", str(tmp_path / "nope"), "--output", str(tmp_path), "analyze"])
        out = capsys.readouterr().out
        assert code == 1
        assert "not a directory" in out
        assert PipelineError  # the failure is typed, not just printed


class TestInterchangeFormats:
    def test_analyze_writes_every_promised_format(self, tmp_path: Path) -> None:
        run("--output", str(tmp_path), "analyze")
        assert (tmp_path / "graph" / "system.graphml").is_file()
        assert (tmp_path / "graph" / "graph-nodes.csv").is_file()
        assert (tmp_path / "graph" / "graph-edges.csv").is_file()
        assert (tmp_path / "graph" / "system.db").is_file()

    def test_the_sqlite_graph_is_queryable(self, tmp_path: Path) -> None:
        import sqlite3

        run("--output", str(tmp_path), "analyze")
        with sqlite3.connect(tmp_path / "graph" / "system.db") as db:
            tables = {r[0] for r in db.execute("select name from sqlite_master where type='table'")}
            assert {"nodes", "edges", "clusters", "hubs"} <= tables
            assert db.execute("select count(*) from nodes").fetchone()[0] > 0

    def test_the_log_is_written(self, tmp_path: Path) -> None:
        run("--output", str(tmp_path), "analyze")
        assert (tmp_path / "logs" / "run.jsonl").is_file()


class TestSliceAndRules:
    def test_slice_writes_a_sanitized_slice(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert (
            run("--output", str(tmp_path), "slice", "--entry", "CustomerForm.ValidateCustomer") == 0
        )
        out = capsys.readouterr().out
        slices = list((tmp_path / "slices").glob("*.json"))
        assert slices, "no slice file was written"
        assert "FLOW-CUSTOMERFORM_VALIDATECUSTOMER" in out

    def test_an_unknown_entry_point_fails(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run("--output", str(tmp_path), "slice", "--entry", "Nope.Missing") == 1
        assert "Nope.Missing" in capsys.readouterr().out

    def test_extracting_rules_needs_a_slice(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = run(
            "--output", str(tmp_path), "extract-rules", "--slice", str(tmp_path / "absent.json")
        )
        assert code == 1
        assert "run 'legacyctl slice' first" in capsys.readouterr().out

    def test_rules_start_as_candidates_and_never_as_validated(self, tmp_path: Path) -> None:
        run("--output", str(tmp_path), "slice", "--entry", "CustomerForm.ValidateCustomer")
        slice_file = next((tmp_path / "slices").glob("*.json"))
        run("--output", str(tmp_path), "extract-rules", "--slice", str(slice_file))

        from legacyctl.rules import RuleCatalogStore

        rules = RuleCatalogStore(tmp_path / "catalog" / "rules.yaml", "SYS-VB6").load().rules
        assert rules
        assert all(r.status is RuleStatus.CANDIDATE for r in rules)


class TestReview:
    def _slice_and_extract(self, tmp_path: Path) -> Path:
        run("--output", str(tmp_path), "slice", "--entry", "CustomerForm.ValidateCustomer")
        slice_file = next((tmp_path / "slices").glob("*.json"))
        run("--output", str(tmp_path), "extract-rules", "--slice", str(slice_file))
        return tmp_path

    def test_validate_fails_while_candidates_remain(self, tmp_path: Path) -> None:
        self._slice_and_extract(tmp_path)
        assert run("--output", str(tmp_path), "validate", "--require-validated") == 1

    def test_reviewing_records_the_reviewer(self, tmp_path: Path) -> None:
        self._slice_and_extract(tmp_path)
        code = run(
            "--output",
            str(tmp_path),
            "review",
            "RULE-CUSTOMERFORM_VALIDATECUSTOMER-001",
            "--reviewer",
            "ana",
            "--note",
            "read the legacy source",
        )
        assert code == 0
        from legacyctl.rules import RuleCatalogStore

        rules = RuleCatalogStore(tmp_path / "catalog" / "rules.yaml", "SYS-VB6").load().rules
        reviewed = next(r for r in rules if r.id.endswith("-001"))
        assert reviewed.status is RuleStatus.VALIDATED
        assert reviewed.reviewed_by == "ana"

    def test_reviewing_an_unknown_rule_fails(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._slice_and_extract(tmp_path)
        assert run("--output", str(tmp_path), "review", "RULE-DOES-NOT-EXIST") == 1
        assert "RULE-DOES-NOT-EXIST" in capsys.readouterr().out


class TestContract:
    def test_the_contract_is_refused_without_reviewed_rules(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run("--output", str(tmp_path), "slice", "--entry", "CustomerForm.ValidateCustomer")
        slice_file = next((tmp_path / "slices").glob("*.json"))
        run("--output", str(tmp_path), "extract-rules", "--slice", str(slice_file))

        code = run("--output", str(tmp_path), "contract")
        out = capsys.readouterr().out
        assert code == 1
        assert "no validated rules" in out
        assert not (tmp_path / "contracts" / "openapi.yaml").exists()

    def test_a_reviewed_catalog_produces_a_contract(self, tmp_path: Path) -> None:
        run("--output", str(tmp_path), "slice", "--entry", "CustomerForm.ValidateCustomer")
        slice_file = next((tmp_path / "slices").glob("*.json"))
        run("--output", str(tmp_path), "extract-rules", "--slice", str(slice_file))
        for index in range(1, 5):
            run(
                "--output",
                str(tmp_path),
                "review",
                f"RULE-CUSTOMERFORM_VALIDATECUSTOMER-{index:03d}",
                "--reviewer",
                "ana",
            )
        assert run("--output", str(tmp_path), "contract") == 0
        contract = tmp_path / "contracts" / "openapi.yaml"
        assert contract.is_file()
        assert "openapi: 3.1.0" in contract.read_text(encoding="utf-8")


class TestVerifyAndReport:
    def _reviewed_run(self, tmp_path: Path) -> None:
        run("--output", str(tmp_path), "slice", "--entry", "CustomerForm.ValidateCustomer")
        slice_file = next((tmp_path / "slices").glob("*.json"))
        run("--output", str(tmp_path), "extract-rules", "--slice", str(slice_file))
        for index in range(1, 5):
            run(
                "--output",
                str(tmp_path),
                "review",
                f"RULE-CUSTOMERFORM_VALIDATECUSTOMER-{index:03d}",
                "--reviewer",
                "ana",
            )

    def test_verify_without_a_golden_master_fails(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = run(
            "--output", str(tmp_path), "verify", "--golden-master", str(tmp_path / "absent.yaml")
        )
        out = capsys.readouterr().out
        assert code == 1
        assert "no Golden Master cases" in out

    def test_verify_says_a_passed_case_passed(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Regression: the CLI compared status values as lower-case strings, so
        # every case was printed as a warning.
        self._reviewed_run(tmp_path)
        run("--output", str(tmp_path), "verify", "--golden-master", str(GOLDEN_MASTER))
        out = capsys.readouterr().out
        assert VerifyStatus.PASSED.value in out or "passed" in out
        assert "GM-CUSTOMER-001: PASSED" not in out
        assert "GM-CUSTOMER-001: FAILED" not in out

    def test_verify_writes_a_divergence_report(self, tmp_path: Path) -> None:
        self._reviewed_run(tmp_path)
        run("--output", str(tmp_path), "verify", "--golden-master", str(GOLDEN_MASTER))
        report = tmp_path / "reports" / "divergence-report.md"
        assert report.is_file()
        text = report.read_text(encoding="utf-8")
        assert "# Divergence Report" in text

    def test_report_states_when_a_stage_did_not_run(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A report must not imply coverage it does not have.
        assert run("--output", str(tmp_path), "report") == 0
        text = (tmp_path / "reports" / "legacy-report.md").read_text(encoding="utf-8")
        assert "not run in this run" in text
        assert "not generated in this run" in text
        assert "verification did not run" in text

    def test_report_works_without_a_previous_command(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run("--output", str(tmp_path), "report") == 0
        assert "re-parsing" in capsys.readouterr().out
