"""Shared fixtures.

Tests read the real legacy fixtures on purpose: the parser claims to handle VB6
as it is written, so the assertions are about the shipped fixtures, not about
mocks that merely agree with the implementation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "fixtures"


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def vb6_root() -> Path:
    root = FIXTURES / "vb6"
    if not root.is_dir():
        pytest.skip(f"missing fixture root: {root}")
    return root


@pytest.fixture(scope="session")
def consistency_root() -> Path:
    root = FIXTURES / "consistency"
    if not root.is_dir():
        pytest.skip(f"missing fixture root: {root}")
    return root


@pytest.fixture(scope="session")
def vb6_system(vb6_root: Path):  # type: ignore[no-untyped-def]
    from legacyctl.parsers.vb6 import VB6Parser

    return VB6Parser().parse(vb6_root)


@pytest.fixture(scope="session")
def vb6_graph(vb6_system):  # type: ignore[no-untyped-def]
    from legacyctl.clustering import ClusterAnalyzer
    from legacyctl.graph import LegacyGraphBuilder

    return ClusterAnalyzer().analyze(LegacyGraphBuilder().build(vb6_system))
