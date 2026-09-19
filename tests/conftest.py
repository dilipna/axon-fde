"""Shared test configuration.

Holds one thing: the rule for what happens when a test's dependency is
missing.

**Why this exists.** A test that skips is a test that did not run, and a
suite reporting "312 passed, 40 skipped" is indistinguishable from one
reporting "312 passed" unless somebody reads the summary. Locally that is the
right trade - a developer with no containers running still wants a useful
signal from the unit tests. In CI it is not: an integration job that starts
no database, skips every test that needed one and reports success is a green
tick that means nothing.

So the behaviour is environment-dependent and explicit. Set
``AXON_REQUIRE_INTEGRATION=1`` and a missing dependency becomes a failure
naming what was absent. CI sets it; developers do not.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import NoReturn

import pytest

__all__ = [
    "GENERATED_DIR",
    "integration_required",
    "missing_dependency",
    "recording",
]

#: Scenario recordings are generated, not committed (`data/generated/` is
#: gitignored), so anything reading one has to cope with its absence.
GENERATED_DIR = Path(__file__).resolve().parents[1] / "data" / "generated"

_REQUIRE_FLAG = "AXON_REQUIRE_INTEGRATION"


def integration_required() -> bool:
    """Whether a missing dependency should fail rather than skip."""
    return os.environ.get(_REQUIRE_FLAG, "").strip().lower() in {"1", "true", "yes"}


def missing_dependency(what: str, detail: str = "") -> NoReturn:
    """Skip locally, fail in CI.

    The message names the command that would fix it, because the most common
    reader of this is someone who has just cloned the repository.
    """
    message = f"{what} is unavailable." + (f" Detail: {detail}" if detail else "")
    if integration_required():
        pytest.fail(
            f"{message}\n\n"
            f"{_REQUIRE_FLAG} is set, so this is a failure rather than a skip: "
            "the environment was supposed to provide this and did not. A run "
            "that skipped it would report success while testing nothing."
        )
    pytest.skip(message)


def recording(scenario_id: str, filename: str = "telemetry.parquet") -> Path:
    """The path to a generated scenario recording.

    Skips locally when it has not been generated, and fails in CI, for the
    same reason as everything else in this module: a headline assertion that
    quietly does not run in CI is not protecting anything. The minute the
    flagship scenario breaches its envelope is exactly such an assertion.
    """
    path = GENERATED_DIR / scenario_id / filename
    if not path.exists():
        missing_dependency(
            f"The recording for {scenario_id} (`poe forge run {scenario_id}`)",
            f"{path} does not exist",
        )
    return path
