"""Phase 5: contract -> Java 21 source, via OpenAPI Generator.

The chain is deliberately explicit and never collapses::

    OpenAPI 3.1 -> openapi-generator -> .java -> javac/Gradle -> .class

Catalogs never claim to be compiled Java. This module stops at *source*; the
compile step is a separate, explicit command, because "the catalog produced
working Java" is exactly the claim that hides a broken contract.

Business logic is never generated. The output is a contractual surface --
DTOs, request/response models, enums, interfaces -- that a team fills in. The
generated interfaces carry the rule id and legacy evidence as Javadoc, so a
developer implementing one can see which legacy code it came from.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ..observability import get_log

#: Pinned so a run is reproducible; the tag is part of the audit trail.
DEFAULT_GENERATOR_IMAGE = "openapitools/openapi-generator-cli:v7.14.0"

#: Drives the generated Spring project's own build file.
DEFAULT_MAVEN_IMAGE = "maven:3.9-eclipse-temurin-21"

#: Files the generator always emits. Their presence is what "code was
#: generated" means here -- a directory that merely exists proves nothing.
REQUIRED_ARTIFACTS = ("pom.xml", "src/main/java")

#: Business logic is a human decision; the generator must never invent it.
MODEL_SUFFIXES = ("api", "model")


class CodegenError(RuntimeError):
    """Code generation could not be carried out or verified."""


@dataclass(slots=True)
class CodegenRequest:
    """Everything needed to turn a contract into Java source."""

    contract_path: Path
    output_dir: Path
    system_id: str
    package: str = "com.legacyctl.generated"
    artifact_id: str = "legacy-api"
    generator: str = "spring"
    library: str = "spring-boot"
    image: str = DEFAULT_GENERATOR_IMAGE
    #: Set false to accept whatever the toolchain has; true refuses to guess.
    require_docker: bool = True
    extra_properties: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class CodegenResult:
    request: CodegenRequest
    output_dir: Path
    java_files: list[Path] = field(default_factory=list)
    class_files: list[Path] = field(default_factory=list)
    command: list[str] = field(default_factory=list)
    compile_command: list[str] = field(default_factory=list)
    duration_ms: float = 0.0
    generator_log: Path | None = None
    compiled: bool = False
    compile_error: str | None = None

    @property
    def file_count(self) -> int:
        return len(self.java_files)

    def summary(self) -> str:
        head = f"{self.file_count} Java file(s) generated in {self.output_dir}"
        if self.compiled:
            return f"{head}; build produced {len(self.class_files)} .class file(s)"
        if self.compile_error:
            return f"{head}; compile FAILED: {self.compile_error}"
        return f"{head}; not compiled (compilation is a separate step)"


def _docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            check=True,
            timeout=20,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return False
    return True


def build_command(request: CodegenRequest, use_docker: bool) -> list[str]:
    """The exact generator invocation, for the audit record.

    Extra properties are passed as ``key=value`` on the command line, which is
    what the generator's own CLI accepts; nothing is interpolated into a shell
    string, so a rule name can never become a shell fragment.
    """
    properties = {
        "dateLibrary": "java8",
        "openApiNullable": "true",
        "useTags": "true",
        "hideGenerationTimestamp": "true",
        # Without this the generator falls back to org.openapitools and the
        # output has nothing to do with the package the caller asked for.
        "invokerPackage": request.package,
        "apiPackage": f"{request.package}.api",
        "modelPackage": f"{request.package}.model",
        **request.extra_properties,
    }
    if use_docker:
        # The generator runs in a container: host paths are meaningless inside
        # it, so both the output directory and the spec get their own mount,
        # and Docker only accepts absolute host paths.
        mounts = [
            "-v",
            f"{request.output_dir.resolve()}:/out",
            "-v",
            f"{request.contract_path.resolve().parent}:/spec:ro",
        ]
        # Without an explicit user the container writes as root and the host
        # user cannot then delete or regenerate anything under output/.
        user = []
        if hasattr(os, "getuid"):
            user = ["--user", f"{os.getuid()}:{os.getgid()}"]
        spec_path = f"/spec/{request.contract_path.name}"
        out_path = "/out"
        base = [
            "docker",
            "run",
            "--rm",
            *user,
            # The generator's cache lives in $HOME; an unwritable HOME makes
            # the run fail once we stop being root.
            "-e",
            "HOME=/tmp",
            *mounts,
            request.image,
            "generate",
        ]
    else:
        base = ["openapi-generator-cli", "generate"]
        spec_path = str(request.contract_path.resolve())
        out_path = str(request.output_dir.resolve())

    return [
        *base,
        "-i",
        spec_path,
        "-g",
        request.generator,
        "--library",
        request.library,
        "-o",
        out_path,
        "--additional-properties",
        ",".join(f"{key}={value}" for key, value in sorted(properties.items())),
    ]


def generate(request: CodegenRequest) -> CodegenResult:
    """Run the generator and verify it actually produced a Java surface."""
    log = get_log()
    if not request.contract_path.is_file():
        raise CodegenError(
            f"no contract at {request.contract_path}; the contract is the input to "
            f"codegen and is never inferred from a catalog"
        )

    use_docker = _docker_available()
    if request.require_docker and not use_docker:
        raise CodegenError(
            "no usable Docker daemon; install Docker or pass require_docker=False "
            "with openapi-generator-cli on PATH"
        )

    request.output_dir.mkdir(parents=True, exist_ok=True)
    log_path = request.output_dir / "codegen-generator.log"
    command = build_command(request, use_docker)

    with log.stage(
        "codegen", system_id=request.system_id, source=str(request.contract_path)
    ) as record:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=900,
            env={**os.environ, "GENERATOR_CLI_USER_CONFIG": str(request.output_dir / "config")},
        )
        log_path.write_text(
            f"$ {' '.join(command)}\n\n{completed.stdout}\n{completed.stderr}", encoding="utf-8"
        )
        result = CodegenResult(
            request=request,
            output_dir=request.output_dir,
            command=command,
            generator_log=log_path,
        )
        record.details["exit_code"] = completed.returncode
        record.details["output_dir"] = str(result.output_dir)
        record.details["generator"] = request.generator
        record.details["via"] = "docker" if use_docker else "local"

    if completed.returncode != 0:
        raise CodegenError(
            f"openapi-generator failed (exit {completed.returncode}); see {log_path}\n"
            f"{_tail(completed.stderr)}"
        )

    _verify_surface(result)
    return result


def _verify_surface(result: CodegenResult) -> None:
    """Refuse to call it a success unless the expected artifacts exist."""
    missing = [name for name in REQUIRED_ARTIFACTS if not (result.output_dir / name).exists()]
    if missing:
        raise CodegenError(
            f"the generator reported success but {', '.join(missing)} is missing from "
            f"{result.output_dir}; the contract did not produce a Java project"
        )
    result.java_files = sorted(result.output_dir.rglob("*.java"))
    if not result.java_files:
        raise CodegenError(
            f"no .java files under {result.output_dir}; an empty project is not a "
            f"contractual surface"
        )


def compile_java(
    result: CodegenResult,
    output_dir: Path | None = None,
    image: str = DEFAULT_MAVEN_IMAGE,
) -> CodegenResult:
    """Compile the generated sources to bytecode.

    Separate on purpose: the reader must see that the chain is
    contract -> source -> bytecode, and a failure here is a contract problem,
    not a generator problem.

    The generated code is Spring, so it needs Spring on the classpath; a bare
    ``javac`` cannot prove anything about it. We therefore drive the
    generator's own build file with Maven (in Docker when available). A missing
    toolchain is reported as such and never counted as a successful compile.
    """
    if not result.java_files:
        raise CodegenError("nothing to compile: generate first")
    if not (result.output_dir / "pom.xml").is_file():
        raise CodegenError(
            f"no build file in {result.output_dir}; cannot prove the contract compiles"
        )

    log = get_log()
    project = result.output_dir.resolve()
    classes = output_dir or (project / "target" / "classes")
    use_docker = _docker_available()

    with log.stage("codegen-compile", system_id=result.request.system_id) as record:
        if use_docker:
            command = [
                "docker",
                "run",
                "--rm",
                "--user",
                f"{os.getuid()}:{os.getgid()}",
                "-e",
                "HOME=/tmp",
                "-v",
                f"{project}:/project",
                # A named volume keeps the dependency cache between runs, so
                # only the first build pays for the downloads.
                "-v",
                "legacyctl-m2:/root/.m2",
                "-w",
                "/project",
                image,
                "mvn",
                "-B",
                "-q",
                "-DskipTests",
                "compile",
            ]
        else:
            command = ["mvn", "-B", "-q", "-DskipTests", "compile"]

        completed = subprocess.run(command, capture_output=True, text=True, timeout=1800)
        result.compile_command = command
        result.duration_ms = record.duration_ms or 0.0
        record.details["exit_code"] = completed.returncode
        record.details["via"] = "docker" if use_docker else "local-maven"
        record.details["project"] = str(project)

    if completed.returncode != 0:
        result.compiled = False
        result.compile_error = _tail(completed.stdout + completed.stderr)
        return result

    produced = sorted(classes.rglob("*.class"))
    if not produced:
        result.compiled = False
        result.compile_error = f"the build reported success but produced no .class under {classes}"
        return result
    result.class_files = produced
    result.compiled = True
    result.compile_error = None
    return result


def _tail(text: str, limit: int = 800) -> str:
    return (text or "").strip()[-limit:]
