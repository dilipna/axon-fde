"""Running an arm and storing what it measured.

`poe bench`. One arm, every grader that can reach its claim, one JSON file per
run under `benchmarks/results/`.

**Results are stored, not printed.** That is invariant I7: no number appears
anywhere unless a stored run produced it. A benchmark that only printed to a
terminal would make every number in the README unverifiable the moment the
scrollback was lost, and "we measured 35 minutes" would rest on somebody's
memory of a number they saw once.

**The exit code is the safety gate and nothing else.** A quality metric that
came out worse than hoped does not fail the build; a non-zero safety metric
does. Blending them would mean a disappointing lead-time number could be
"fixed" by relaxing the same threshold that guards approval bypass.

The arm is named in the result. `rules_only` consults no model and records
``model_id: none`` rather than the name of a model it never called - which is
the only thing that keeps the two arms distinguishable in stored results, and
the C5 ablation rests entirely on that distinction.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from benchmarks.axonbench.claims import CLAIMS, ClaimStatus
from benchmarks.axonbench.graders.base import GraderResult, Measurement
from benchmarks.axonbench.graders.degradation import DegradationGrader
from benchmarks.axonbench.graders.detection import (
    DEFAULT_PACK_DIR,
    ConflictGrader,
    LeadTimeGrader,
)
from benchmarks.axonbench.graders.diagnosis import (
    CaseOutcome,
    CostLatencyGrader,
    DiagnosisGrader,
    GroundingGrader,
    LlmValueGrader,
)
from benchmarks.axonbench.graders.risk import RiskCalibrationGrader
from benchmarks.axonbench.graders.safety import PolicyMatrixGrader, SqlGuardGrader
from benchmarks.axonbench.provenance import RunProvenance, current_provenance
from simulator.incidentforge.scenarios import load_pack

__all__ = ["ARMS", "RESULTS_DIR", "BenchRun", "main", "run_arm"]

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESULTS_DIR = PROJECT_ROOT / "benchmarks" / "results"

#: The arms the ablation compares. `rules_only` has run in CI since B7.
#: `rules_llm` replays recorded cassettes (B10b) - no key, no network, and a
#: missing recording raises rather than re-recording.
ARMS = ("rules_only", "rules_llm")


#: Every parameter that could change a number here, hashed into the run id.
#: Listed explicitly rather than scraped from Settings: a config hash over
#: everything would change when an unrelated setting moved, and two runs that
#: should share an id would not.
def _config(scenario_filter: frozenset[str] | None = None) -> dict[str, Any]:
    from backend.app.actions.effects import MIN_SLOPE_WINDOW_MINUTES
    from backend.app.incidents.detection import CONSECUTIVE_READINGS_TO_FIRE
    from backend.app.risk.baselines import DEFAULT_HORIZON_MINUTES
    from backend.app.risk.features import DEFAULT_WINDOW_MINUTES, MIN_WINDOW_COVERAGE
    from backend.app.verification.outcome import (
        RECOVERY_HOLD_READINGS,
        STOPPED_RISING_SLOPE_C_PER_MIN,
    )
    from benchmarks.axonbench.graders.diagnosis import CONTRIBUTING_THRESHOLD
    from benchmarks.axonbench.llm_arm import CASE_HISTORY_MINUTES

    return {
        # A filtered run measures a different dataset and must not share an id
        # with the full one.
        "scenario_filter": sorted(scenario_filter) if scenario_filter else "all",
        "case_history_minutes": CASE_HISTORY_MINUTES,
        "contributing_threshold": CONTRIBUTING_THRESHOLD,
        "risk_horizon_minutes": DEFAULT_HORIZON_MINUTES,
        "risk_window_minutes": DEFAULT_WINDOW_MINUTES,
        "risk_window_coverage": MIN_WINDOW_COVERAGE,
        "detector_consecutive_readings": CONSECUTIVE_READINGS_TO_FIRE,
        "verification_slope_window_minutes": MIN_SLOPE_WINDOW_MINUTES,
        "verification_slope_threshold": STOPPED_RISING_SLOPE_C_PER_MIN,
        "verification_recovery_hold": RECOVERY_HOLD_READINGS,
    }


def _pack_version() -> str:
    """The version of the pack the graders will actually read.

    Read from the pack rather than passed in or defaulted. The scenarios are
    half of what a number means, and a provenance tuple naming a pack that was
    not used is worse than one naming none: it puts a false dataset beside the
    result and, because the run id is a digest of the tuple, makes runs over
    two different packs share an identifier.
    """
    return load_pack(DEFAULT_PACK_DIR).pack_version


@dataclass(frozen=True, slots=True)
class BenchRun:
    """One arm's results, and whether the safety gates held."""

    arm: str
    provenance: RunProvenance
    results: tuple[GraderResult, ...]
    #: What the arm did that is not a claim: which scenarios were not
    #: investigated and why, the models, what the judge cost.
    notes: dict[str, Any] = field(default_factory=dict)

    @property
    def gate_failures(self) -> tuple[GraderResult, ...]:
        """Safety metrics that came out non-zero. Any of these fails the build."""
        return tuple(result for result in self.results if not result.passed_gate)

    @property
    def measured(self) -> tuple[GraderResult, ...]:
        return tuple(r for r in self.results if r.status is ClaimStatus.MEASURED)

    @property
    def insufficient(self) -> tuple[GraderResult, ...]:
        return tuple(r for r in self.results if r.status is ClaimStatus.INSUFFICIENT_DATA)

    def as_payload(self) -> dict[str, Any]:
        return {
            "arm": self.arm,
            "provenance": self.provenance.as_payload(),
            "completed_at": datetime.now(UTC).isoformat(),
            "arm_notes": self.notes,
            "results": [result.as_payload() for result in self.results],
            "summary": {
                "measured": len(self.measured),
                "insufficient_data": len(self.insufficient),
                "gate_failures": [r.claim_id for r in self.gate_failures],
                # Claims in the register that this harness does not touch at
                # all. Reported so the gap between "graded" and "registered"
                # is visible in every result rather than inferred.
                "claims_graded": sorted(r.claim_id for r in self.results),
                "claims_registered_here": sorted(CLAIMS),
            },
        }

    def store(self, directory: Path | None = None) -> Path:
        target = (directory or RESULTS_DIR) / f"{self.arm}-{self.provenance.run_id}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.as_payload(), indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
        return target


#: Claims only the rules+LLM arm can produce. Listed for the rules-only arm as
#: `INSUFFICIENT_DATA` rather than left out: a report that omits them reads as
#: a system that never asked. Held here and not in `CLAIMS.blocked_by`, because
#: that field blocks a claim in *every* arm and these are reachable in one.
_NEEDS_THE_LLM_ARM = {
    "C5": "the ablation compares two arms; this run is the rules-only baseline",
    "C10": "the rules-only arm generates no model narrative to check for unsupported claims",
    "C12": "the rules-only arm makes no model calls, so there is no cost or latency",
}


def _insufficient(claim_id: str, reason: str) -> GraderResult:
    return GraderResult(
        claim_id=claim_id,
        measurement=Measurement(value=0.0, cases=0),
        status=ClaimStatus.INSUFFICIENT_DATA,
        reason=reason,
    )


def _arm_notes(outcomes: list[CaseOutcome], meta: Any) -> dict[str, Any]:
    return {
        "investigated": len(outcomes),
        "no_incident_opened": sorted(meta.not_detected),
        "stopped_before_the_model": dict(sorted(meta.not_investigated.items())),
        "llm_mode": meta.llm_mode,
        "models": meta.models,
        "prompt_version": meta.prompt_version,
        # The judge scores both arms' summaries for C5. Its spend is grading
        # infrastructure, not part of an incident's cost, so it is reported
        # here and kept out of C12.
        "judge_calls": meta.judge_calls,
        "judge_cost_usd": round(meta.judge_cost_usd, 6),
    }


def run_arm(arm: str = "rules_only", *, scenarios: frozenset[str] | None = None) -> BenchRun:
    """Grade every claim this harness can reach, for one arm.

    ``scenarios`` restricts the incident-level claims to a subset. It exists for
    pilots and tests, and a restricted run is honest about it: the config hash
    changes, and the case count falls below the claim's requirement, so it
    reports `INSUFFICIENT_DATA` rather than a smaller `MEASURED`.

    Raises:
        ValueError: An arm name that is not in ``ARMS``. Refused rather than
            recorded, because a result file naming an arm nobody defined would
            silently not participate in the ablation it was run for.
    """
    if arm not in ARMS:
        raise ValueError(f"unknown arm {arm!r}; expected one of {', '.join(ARMS)}")

    from benchmarks.axonbench.llm_arm import run_llm_arm, run_rules_arm

    # C1 and C6 joined the list with scenario pack v1.1.0. They run the
    # simulator in process over sixty scenarios, which is a few seconds, and
    # need neither a database nor a key - the same two properties that let the
    # safety graders run in CI.
    graders = (
        PolicyMatrixGrader(),
        SqlGuardGrader(),
        LeadTimeGrader(),
        ConflictGrader(),
        # C2 trains and evaluates out of regime; deterministic, no key, ~15 s.
        RiskCalibrationGrader(),
    )
    results = [grader.grade() for grader in graders]
    # C13 runs the real workflow with failures injected at the provider
    # boundary. Arm-independent, deterministic, no key and no database.
    from benchmarks.axonbench.failure_injection import run_all

    results.append(DegradationGrader(tuple(run_all())).grade())

    if arm == "rules_only":
        outcomes, meta = run_rules_arm(only=scenarios)
        results.append(DiagnosisGrader(tuple(outcomes), arm="rules").grade())
        results.extend(_insufficient(claim, why) for claim, why in _NEEDS_THE_LLM_ARM.items())
        provenance = current_provenance(config=_config(scenarios), pack_version=_pack_version())
    else:
        outcomes, meta = run_llm_arm(only=scenarios)
        frozen = tuple(outcomes)
        results.extend(
            [
                DiagnosisGrader(frozen, arm="llm").grade(),
                LlmValueGrader(frozen).grade(),
                GroundingGrader(frozen).grade(),
                CostLatencyGrader(frozen).grade(),
            ]
        )
        provenance = current_provenance(
            config=_config(scenarios),
            pack_version=_pack_version(),
            prompt_version=meta.prompt_version,
            model_id=(
                f"{meta.models['propose_links']} (links), {meta.models['narrate']} (narrative), "
                f"{meta.models['judge']} (judge)"
            ),
        )

    # Every claim this harness knows about but cannot yet reach gets a
    # recorded blocker rather than being left out. A report listing two green
    # claims and silently omitting six reads as a system with two claims, and
    # the next person rediscovers that C1 needs forty scenarios by reading the
    # method again. `blocked_by` is carried on the claim precisely so the
    # reason travels with it.
    graded = {result.claim_id for result in results}
    for claim_id, spec in sorted(CLAIMS.items()):
        if claim_id in graded or not spec.blocked_by:
            continue
        results.append(_insufficient(claim_id, spec.blocked_by))

    return BenchRun(
        arm=arm, provenance=provenance, results=tuple(results), notes=_arm_notes(outcomes, meta)
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run AxonBench for one arm.")
    parser.add_argument("--arm", default="rules_only", choices=ARMS)
    parser.add_argument(
        "--results-dir",
        type=Path,
        default=None,
        help="Where to store the run. Defaults to benchmarks/results/.",
    )
    parser.add_argument(
        "--scenarios",
        default="",
        help="Comma-separated scenario ids to restrict the incident-level claims to "
        "(pilots and tests). A restricted run reports INSUFFICIENT_DATA, not a smaller MEASURED.",
    )
    args = parser.parse_args(argv)

    only = frozenset(part for part in args.scenarios.split(",") if part) or None
    run = run_arm(args.arm, scenarios=only)
    path = run.store(args.results_dir)

    print(f"AxonBench - arm={run.arm} run={run.provenance.run_id}")
    if not run.provenance.reproducible:
        print(
            f"  WARNING: git_sha={run.provenance.git_sha} - this number was measured "
            "against code that is not committed, so nobody else can reproduce it."
        )
    for result in run.results:
        spec = CLAIMS[result.claim_id]
        # Three marks, not two. A blocked claim is neither passing nor
        # failing, and printing PASS beside "insufficient data" invites
        # reading the row as a green result.
        if not result.passed_gate:
            mark = "FAIL"
        elif result.status is ClaimStatus.MEASURED:
            mark = "PASS"
        else:
            mark = "----"
        print(
            f"  [{mark}] {result.claim_id} {result.status.value:<18} "
            f"{spec.metric} = {result.measurement.value:g} "
            f"over {result.measurement.cases}/{spec.required_cases} {spec.case_unit}"
        )
        if result.status is not ClaimStatus.MEASURED:
            print(f"         {result.reason}")
    try:
        shown = path.relative_to(PROJECT_ROOT)
    except ValueError:  # --results-dir pointed outside the repository
        shown = path
    print(f"  stored: {shown}")

    # Only the safety gates decide the exit code.
    if run.gate_failures:
        print("\nFAILED: " + ", ".join(f"{r.claim_id} ({r.reason})" for r in run.gate_failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
