"""Tests for phase 5: contract -> Java source -> bytecode.

The point of these tests is the chain, and specifically that a failure anywhere
in it is reported as a failure. A generator that exits 0 while producing no
Java is a broken contract, not a success.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from legacyctl.codegen import (
    DEFAULT_GENERATOR_IMAGE,
    DEFAULT_MAVEN_IMAGE,
    CodegenError,
    CodegenRequest,
    build_command,
    compile_java,
    generate,
)
from legacyctl.codegen.generator import CodegenResult

CONTRACT = """openapi: 3.1.0
info:
  title: Customer API
  version: 1.0.0
paths:
  /rules/branch:
    post:
      operationId: branch
      summary: Branch when credit > limit
      description: 'Rule: RULE-TEST-001. Legacy source: Customer.bas:28.'
      responses:
        '200':
          description: The decision the legacy system took.
          content:
            application/json:
              schema:
                type: object
                properties:
                  decision:
                    type: string
"""


@pytest.fixture
def request_for(tmp_path: Path) -> CodegenRequest:
    contract = tmp_path / "openapi.yaml"
    contract.write_text(CONTRACT, encoding="utf-8")
    return CodegenRequest(
        contract_path=contract,
        output_dir=tmp_path / "java",
        system_id="SYS-TEST",
    )


def _fake_generated(output: Path, files: int = 2) -> Path:
    """Mimic what the generator leaves behind, so tests need no network."""
    (output / "src" / "main" / "java" / "com" / "example").mkdir(parents=True, exist_ok=True)
    (output / "pom.xml").write_text("<project/>", encoding="utf-8")
    for index in range(files):
        (output / "src" / "main" / "java" / "com" / "example" / f"Model{index}.java").write_text(
            f"package com.example;\npublic class Model{index} {{}}\n", encoding="utf-8"
        )
    return output


class TestRequest:
    def test_defaults_target_java_and_a_spring_project(self) -> None:
        request = CodegenRequest(
            contract_path=Path("a.yaml"), output_dir=Path("out"), system_id="s"
        )
        assert request.generator == "spring"
        assert request.library == "spring-boot"
        assert request.image == DEFAULT_GENERATOR_IMAGE

    def test_the_generator_image_is_pinned(self) -> None:
        # A floating tag would make two runs produce different code, which
        # would quietly destroy reproducibility of the whole pipeline.
        assert ":" in DEFAULT_GENERATOR_IMAGE
        assert "@" not in DEFAULT_GENERATOR_IMAGE
        assert DEFAULT_MAVEN_IMAGE.endswith("21")


class TestCommand:
    def test_docker_mounts_the_output_and_the_spec(self, request_for: CodegenRequest) -> None:
        command = build_command(request_for, use_docker=True)
        joined = " ".join(command)
        assert "/spec:ro" in joined
        assert f"/spec/{request_for.contract_path.name}" in command
        assert str(request_for.output_dir.resolve()) in joined

    def test_docker_uses_the_host_user(self, request_for: CodegenRequest) -> None:
        # Root-owned output cannot be regenerated or deleted by the user later.
        command = build_command(request_for, use_docker=True)
        assert command[command.index("--user") + 1].startswith(str(__import__("os").getuid()))

    def test_local_invocation_uses_host_paths(self, request_for: CodegenRequest) -> None:
        command = build_command(request_for, use_docker=False)
        assert command[0] == "openapi-generator-cli"
        assert str(request_for.contract_path.resolve()) in command
        assert "/spec" not in command

    def test_the_requested_package_is_passed_through(self, request_for: CodegenRequest) -> None:
        # Without invokerPackage the generator emits org.openapitools and the
        # caller's package is ignored without any error.
        command = build_command(request_for, use_docker=True)
        properties = command[command.index("--additional-properties") + 1]
        assert "invokerPackage=com.legacyctl.generated" in properties

    def test_extra_properties_win_over_the_defaults(self, request_for: CodegenRequest) -> None:
        request_for.extra_properties["hideGenerationTimestamp"] = "false"
        command = build_command(request_for, use_docker=False)
        properties = command[command.index("--additional-properties") + 1]
        assert "hideGenerationTimestamp=false" in properties
        assert "hideGenerationTimestamp=true" not in properties


class TestGenerate:
    def test_a_missing_contract_is_refused(self, tmp_path: Path) -> None:
        request = CodegenRequest(
            contract_path=tmp_path / "absent.yaml", output_dir=tmp_path / "out", system_id="s"
        )
        with pytest.raises(CodegenError, match="no contract at"):
            generate(request)

    def test_an_empty_project_is_not_a_success(
        self, request_for: CodegenRequest, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("legacyctl.codegen.generator._docker_available", lambda: True)
        monkeypatch.setattr(
            "legacyctl.codegen.generator.subprocess.run",
            lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "", ""),
        )
        with pytest.raises(CodegenError, match="did not produce a Java project"):
            generate(request_for)

    def test_a_generator_that_writes_no_java_is_refused(
        self, request_for: CodegenRequest, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Exit code 0, a pom, but no .java: still a broken contract.
        monkeypatch.setattr("legacyctl.codegen.generator._docker_available", lambda: True)

        def fake_run(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
            _fake_generated(request_for.output_dir, files=0)
            return subprocess.CompletedProcess([], 0, "", "")

        monkeypatch.setattr("legacyctl.codegen.generator.subprocess.run", fake_run)
        with pytest.raises(CodegenError, match=r"no \.java files"):
            generate(request_for)

    def test_a_failing_generator_reports_its_own_words(
        self, request_for: CodegenRequest, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("legacyctl.codegen.generator._docker_available", lambda: True)
        monkeypatch.setattr(
            "legacyctl.codegen.generator.subprocess.run",
            lambda *a, **k: subprocess.CompletedProcess(a[0], 127, "", "invalid spec at line 3"),
        )
        with pytest.raises(CodegenError, match="invalid spec at line 3"):
            generate(request_for)

    def test_successful_generation_lists_the_java_files(
        self, request_for: CodegenRequest, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("legacyctl.codegen.generator._docker_available", lambda: True)

        def fake_run(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
            _fake_generated(request_for.output_dir, files=3)
            return subprocess.CompletedProcess([], 0, "", "")

        monkeypatch.setattr("legacyctl.codegen.generator.subprocess.run", fake_run)
        result = generate(request_for)
        assert result.file_count == 3
        assert result.compiled is False
        assert "not compiled" in result.summary()

    def test_generation_never_claims_business_logic(
        self, request_for: CodegenRequest, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("legacyctl.codegen.generator._docker_available", lambda: True)

        def fake_run(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
            _fake_generated(request_for.output_dir, files=1)
            return subprocess.CompletedProcess([], 0, "", "")

        monkeypatch.setattr("legacyctl.codegen.generator.subprocess.run", fake_run)
        command = build_command(request_for, use_docker=True)
        assert "skipDefaultInterface" not in " ".join(command)
        # The generator is never asked to invent implementations.
        assert "generateSupportingFiles" not in " ".join(command)


class TestCompile:
    def _result(
        self, request_for: CodegenRequest, monkeypatch: pytest.MonkeyPatch
    ) -> CodegenResult:
        monkeypatch.setattr("legacyctl.codegen.generator._docker_available", lambda: True)

        def fake_run(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
            _fake_generated(request_for.output_dir, files=2)
            return subprocess.CompletedProcess([], 0, "", "")

        monkeypatch.setattr("legacyctl.codegen.generator.subprocess.run", fake_run)
        return generate(request_for)

    def test_compiling_before_generating_is_refused(self, request_for: CodegenRequest) -> None:
        result = CodegenResult(request=request_for, output_dir=request_for.output_dir)
        with pytest.raises(CodegenError, match="nothing to compile"):
            compile_java(result)

    def test_a_project_without_a_build_file_cannot_be_proven(
        self, request_for: CodegenRequest, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = self._result(request_for, monkeypatch)
        (result.output_dir / "pom.xml").unlink()
        with pytest.raises(CodegenError, match="no build file"):
            compile_java(result)

    def test_a_failing_build_is_reported_with_its_output(
        self, request_for: CodegenRequest, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = self._result(request_for, monkeypatch)
        monkeypatch.setattr("legacyctl.codegen.generator._docker_available", lambda: True)
        monkeypatch.setattr(
            "legacyctl.codegen.generator.subprocess.run",
            lambda *a, **k: subprocess.CompletedProcess(
                a[0], 1, "cannot find symbol: class RegraDecision", ""
            ),
        )
        compile_java(result)
        assert result.compiled is False
        assert "RegraDecision" in (result.compile_error or "")
        assert "compile FAILED" in result.summary()

    def test_a_build_with_no_class_files_is_not_a_success(
        self, request_for: CodegenRequest, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = self._result(request_for, monkeypatch)
        monkeypatch.setattr("legacyctl.codegen.generator._docker_available", lambda: True)
        monkeypatch.setattr(
            "legacyctl.codegen.generator.subprocess.run",
            lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "", ""),
        )
        compile_java(result)
        # Exit code 0 but nothing was produced: that is a failure, not a pass.
        assert result.compiled is False
        assert "no .class" in (result.compile_error or "")

    def test_a_successful_build_reports_its_bytecode(
        self, request_for: CodegenRequest, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = self._result(request_for, monkeypatch)
        monkeypatch.setattr("legacyctl.codegen.generator._docker_available", lambda: True)

        def fake_run(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
            classes = result.output_dir / "target" / "classes"
            (classes / "com" / "example").mkdir(parents=True)
            (classes / "com" / "example" / "Model0.class").write_bytes(b"\xca\xfe\xba\xbe")
            return subprocess.CompletedProcess([], 0, "", "")

        monkeypatch.setattr("legacyctl.codegen.generator.subprocess.run", fake_run)
        compile_java(result)
        assert result.compiled is True
        assert result.compile_error is None
        assert len(result.class_files) == 1
        assert "1 .class" in result.summary()

    def test_the_source_and_bytecode_chains_are_recorded_separately(
        self, request_for: CodegenRequest, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = self._result(request_for, monkeypatch)
        assert result.command
        assert result.compile_command == []
        assert result.generator_log is not None
        assert result.generator_log.is_file()
