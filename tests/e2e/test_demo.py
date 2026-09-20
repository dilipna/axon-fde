"""`poe demo` - the rules-only closed loop, run as a test.

**Why the demo is a test and not only a script.** A demo that CI does not
execute rots silently, and this one is load-bearing twice over: it is the
artefact somebody is shown, and it is the ``rules_only`` ablation arm for
claim C5. An ablation arm that has quietly stopped running is worse than none,
because the number it produced last time is still sitting in the report.

The assertions are on the *beats* rather than on exact wording, so the
narrative can be reworded freely - but the beats are the B7 acceptance
criteria, and those cannot be reworded away.
"""

from __future__ import annotations

import io
from contextlib import redirect_stdout

import pytest

from scripts.demo import TOTAL_STEPS, main
from tests.conftest import recording

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def demo(app_db_ready: None, legacy_ready: None) -> tuple[int, str]:
    """Run the demo once; every assertion reads the same transcript.

    Once rather than per test. The loop replays 240 minutes of telemetry
    through the whole pipeline, and paying that per assertion would make the
    suite slow enough that somebody eventually marks it skip - which is the
    failure mode this file exists to prevent.
    """
    # Both guards are session-scoped, so a module-scoped fixture may request
    # them. Without them a stopped database makes the demo exit 1 and this
    # suite report an assertion failure on the transcript - which reads as
    # "the loop is broken" rather than "nothing was running". The recording
    # check is called directly for the same reason. All three skip locally and
    # fail in CI.
    recording("compressor_degradation_pharma_01")

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = main([])
    return code, buffer.getvalue()


def test_the_demo_runs_the_whole_loop(demo: tuple[int, str]) -> None:
    code, output = demo
    assert code == 0, output
    for step in range(1, TOTAL_STEPS + 1):
        assert f"[{step}/{TOTAL_STEPS}]" in output, f"step {step} never ran"


def test_it_consults_no_model(demo: tuple[int, str]) -> None:
    """The whole point of the arm.

    Asserted on the transcript rather than by mocking the Anthropic client,
    because the claim is about the run as a whole rather than about one call
    site - and an import-linter contract already forbids the decision layer
    from reaching `anthropic` at all.
    """
    _, output = demo
    assert "no model was consulted" in output


class TestTheAcceptanceCriteria:
    def test_the_predictive_detector_fires_before_the_breach(self, demo: tuple[int, str]) -> None:
        """Minute 102, against a threshold baseline that waits until 137."""
        _, output = demo
        assert "incident opened at minute 102" in output

    def test_the_document_wins_the_envelope_conflict(self, demo: tuple[int, str]) -> None:
        """8.0 C from the Bill of Lading, not 10.0 C from the ERP.

        Judged against the ERP's number this load never looks at risk at all,
        so this line is the difference between a demo and a demo that matters.
        """
        _, output = demo
        assert "2.0 to 8.0 C" in output
        assert "permitted_temp_max_c" in output

    def test_the_approval_stale_branch_is_exercised(self, demo: tuple[int, str]) -> None:
        """Not described - exercised, against readings that really arrived.

        The B7 acceptance criterion. The refusal must be `approval_stale`
        specifically: a demo that refused for any other reason would look
        identical on screen and prove nothing about I5.
        """
        _, output = demo
        assert "execution refused: approval_stale" in output
        assert "new readings arrived" in output
        # The risk number moving is what makes this more than a hash mismatch
        # on a list of evidence ids.
        assert "risk.probability" in output

    def test_a_retry_does_not_dispatch_a_second_truck(self, demo: tuple[int, str]) -> None:
        _, output = demo
        assert "no second truck dispatched" in output
        assert "attempt 2" in output

    def test_a_failed_verification_reopens_the_incident(self, demo: tuple[int, str]) -> None:
        """The loop closes, and it closes honestly.

        The recording is the trajectory of a truck that was *not* rerouted, so
        `failed` is the truthful verdict. A demo that synthesised a recovery to
        end happily would be fabricating an observation.
        """
        _, output = demo
        assert "verdict: failed" in output
        assert "incident reopened: investigating" in output

    def test_the_audit_chain_verifies_at_the_end(self, demo: tuple[int, str]) -> None:
        _, output = demo
        assert "audit chain verified" in output


def test_the_demo_leaves_no_residue(demo: tuple[int, str]) -> None:
    """Rolled back, which is what makes it repeatable.

    A committing demo works once: the second run finds the first run's
    incident still open on the same correlation key, deduplicates into it, and
    fails on a transition that was already made. This assertion is the reason
    the suite can run twice in a row.
    """
    _, output = demo
    assert "Rolled back" in output
