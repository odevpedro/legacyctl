"""Phase 5: contract -> Java 21 source, via OpenAPI Generator."""

from .generator import (
    DEFAULT_GENERATOR_IMAGE,
    DEFAULT_MAVEN_IMAGE,
    REQUIRED_ARTIFACTS,
    CodegenError,
    CodegenRequest,
    CodegenResult,
    build_command,
    compile_java,
    generate,
)

__all__ = [
    "DEFAULT_GENERATOR_IMAGE",
    "DEFAULT_MAVEN_IMAGE",
    "REQUIRED_ARTIFACTS",
    "CodegenError",
    "CodegenRequest",
    "CodegenResult",
    "build_command",
    "compile_java",
    "generate",
]
