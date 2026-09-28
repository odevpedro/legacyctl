"""Verification of the modernized system against curated legacy behaviour."""

from .golden_master import (
    ACCEPTABLE_EVIDENCE,
    GoldenMasterError,
    GoldenMasterStore,
    advisory_cases,
    blocking_cases,
    load_cases,
)
from .new_system import (
    HttpNewSystem,
    NewSystemPort,
    RuleSimulator,
    build_new_system,
    coerce,
)
from .verifier import VerificationRun, Verifier

__all__ = [
    "ACCEPTABLE_EVIDENCE",
    "GoldenMasterError",
    "GoldenMasterStore",
    "HttpNewSystem",
    "NewSystemPort",
    "RuleSimulator",
    "VerificationRun",
    "Verifier",
    "advisory_cases",
    "blocking_cases",
    "build_new_system",
    "coerce",
    "load_cases",
]
