"""C3, C5 and C12: what the investigation concluded, and what it cost.

**These graders read outcomes; they do not produce them.** The import-linter
contract for this package forbids a model, so the code that runs the workflow
against a provider lives in `benchmarks.axonbench.llm_arm` and hands this
module plain records. That split is the contract doing its job rather than
getting in the way: a grader that could call a model could be argued with, and
what it measures could change between two runs of the same recording.

**One ground truth, two arms.** `CaseOutcome` carries the rules-only ranking
and the rules+LLM ranking for the *same* incident, both graded against the
scenario's declared root cause. C3 grades whichever arm it is handed; C5 is
the difference between them.

**What C5 can and cannot show, said up front.** The scorer computes
`confidence = prior x support` with support capped at 1, so the model can
re-order and drop hypotheses but can never raise one above what the rules
allowed (I2). The decision engine ranks actions from the computed breach
probability and never reads a hypothesis. Root-cause accuracy is therefore the
only place the model can help, and action selection is unchanged *by
construction*. The grader still measures the action delta rather than asserting
it, because "by construction" is a claim about the code and this is the check
that the code still says so.
"""

from __future__ import annotations

import statistics
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from benchmarks.axonbench.claims import ClaimStatus
from benchmarks.axonbench.graders.base import GraderResult, Measurement, judge

__all__ = [
    "CONTRIBUTING_THRESHOLD",
    "CaseOutcome",
    "CostLatencyGrader",
    "DiagnosisGrader",
    "GroundingGrader",
    "LlmValueGrader",
]

#: A hypothesis other than the top one counts as a *predicted contributing
#: cause* at or above this confidence. Chosen before any result was seen and
#: not revisited: it sits between the rule priors' floor (0.02) and their
#: mid-range values (0.40-0.45), and tuning it against F1 would make the F1 a
#: measurement of the threshold.
CONTRIBUTING_THRESHOLD = 0.30


@dataclass(frozen=True, slots=True)
class CaseOutcome:
    """Everything a grader needs about one investigated incident."""

    scenario_id: str
    truth_root_cause: str
    truth_contributing: tuple[str, ...]
    correct_action: str
    #: True when the predictive detector fired before the causal fault began -
    #: the start-of-run settling artefact recorded against C1. Diagnosis on
    #: those incidents is reported separately, because there is no fault yet
    #: for a diagnosis to be right or wrong about.
    alert_preceded_fault_onset: bool

    rules_ranking: tuple[str, ...]
    rules_contributing: tuple[str, ...]
    rules_action: str | None
    rules_explanation_score: int | None = None

    #: `None` in a run of the rules-only arm.
    llm_ranking: tuple[str, ...] | None = None
    llm_contributing: tuple[str, ...] = ()
    llm_action: str | None = None
    llm_explanation_score: int | None = None
    llm_narrative_grounded: bool | None = None
    #: `GroundingFailure` values from the deterministic check; empty when grounded.
    llm_grounding_failures: tuple[str, ...] = ()
    llm_figures_checked: int = 0
    llm_escalation_reason: str = ""
    llm_handles_dropped: int = 0
    #: One dict per model call: node, model, tokens, cost_usd, latency_ms.
    invocations: tuple[dict[str, Any], ...] = field(default_factory=tuple)


def _top1(ranking: tuple[str, ...] | None, truth: str) -> bool:
    return bool(ranking) and ranking[0] == truth  # type: ignore[index]


def _top3(ranking: tuple[str, ...] | None, truth: str) -> bool:
    return bool(ranking) and truth in ranking[:3]  # type: ignore[index]


def _rate(hits: int, total: int) -> float:
    return hits / total if total else 0.0


def _contributing_f1(pairs: list[tuple[tuple[str, ...], tuple[str, ...]]]) -> float:
    """Micro-averaged F1 over (predicted, true) contributing-cause sets.

    Micro rather than a mean of per-incident F1s: most incidents have one or
    no contributing cause, and a per-incident F1 of an empty set against an
    empty set is undefined - averaging conventions for that case would decide
    the number.
    """
    tp = fp = fn = 0
    for predicted, truth in pairs:
        p, t = set(predicted), set(truth)
        tp += len(p & t)
        fp += len(p - t)
        fn += len(t - p)
    denominator = 2 * tp + fp + fn
    return (2 * tp / denominator) if denominator else 0.0


@dataclass(frozen=True, slots=True)
class DiagnosisGrader:
    """C3: does the investigation name the right root cause?"""

    outcomes: tuple[CaseOutcome, ...]
    arm: str  # "rules" or "llm"
    claim_id: str = "C3"

    def _ranking(self, outcome: CaseOutcome) -> tuple[str, ...] | None:
        return outcome.rules_ranking if self.arm == "rules" else outcome.llm_ranking

    def _contributing(self, outcome: CaseOutcome) -> tuple[str, ...]:
        return outcome.rules_contributing if self.arm == "rules" else outcome.llm_contributing

    def measure(self) -> Measurement:
        outcomes = self.outcomes
        n = len(outcomes)
        top1 = [_top1(self._ranking(o), o.truth_root_cause) for o in outcomes]
        top3 = [_top3(self._ranking(o), o.truth_root_cause) for o in outcomes]

        after_onset = [
            (o, hit)
            for o, hit in zip(outcomes, top1, strict=True)
            if not o.alert_preceded_fault_onset
        ]
        misses = Counter(
            (o.truth_root_cause, (self._ranking(o) or ("(no hypothesis)",))[0])
            for o, hit in zip(outcomes, top1, strict=True)
            if not hit
        )
        return Measurement(
            value=_rate(sum(top1), n),
            cases=n,
            companions={
                "top3_accuracy": _rate(sum(top3), n),
                "contributing_cause_f1": _contributing_f1(
                    [(self._contributing(o), o.truth_contributing) for o in outcomes]
                ),
                # The subset with a fault to diagnose. Reported because the
                # headline includes incidents opened by the start-of-run
                # artefact, where no cause exists yet.
                "top1_accuracy_after_fault_onset": _rate(
                    sum(1 for _, hit in after_onset if hit), len(after_onset)
                ),
                "incidents_after_fault_onset": float(len(after_onset)),
                "incidents_before_fault_onset": float(n - len(after_onset)),
            },
            detail={
                "arm": self.arm,
                "contributing_threshold": CONTRIBUTING_THRESHOLD,
                "wrong_by_truth_and_prediction": [
                    {"truth": truth, "predicted": predicted, "count": count}
                    for (truth, predicted), count in sorted(misses.items())
                ],
                "per_scenario": [
                    {
                        "scenario_id": o.scenario_id,
                        "truth": o.truth_root_cause,
                        "ranking": list(self._ranking(o) or ()),
                        "top1": hit,
                    }
                    for o, hit in zip(outcomes, top1, strict=True)
                ],
            },
        )

    def grade(self) -> GraderResult:
        return judge(self.claim_id, self.measure())


@dataclass(frozen=True, slots=True)
class LlmValueGrader:
    """C5: what the model adds over the rules, on the same incidents.

    Publishes the delta whichever way it points. The claim's own text says it
    may be refuted, and a grader that could only report a positive would be a
    claim, not a measurement.
    """

    outcomes: tuple[CaseOutcome, ...]
    claim_id: str = "C5"

    def measure(self) -> Measurement:
        outcomes = self.outcomes
        n = len(outcomes)
        rules = DiagnosisGrader(outcomes, arm="rules").measure()
        llm = DiagnosisGrader(outcomes, arm="llm").measure()

        rules_action = _rate(sum(1 for o in outcomes if o.rules_action == o.correct_action), n)
        llm_action = _rate(sum(1 for o in outcomes if o.llm_action == o.correct_action), n)
        actions_identical = sum(1 for o in outcomes if o.rules_action == o.llm_action)

        def mean_score(scores: list[int]) -> float:
            return statistics.fmean(scores) if scores else 0.0

        rules_scores = [
            o.rules_explanation_score for o in outcomes if o.rules_explanation_score is not None
        ]
        llm_scores = [
            o.llm_explanation_score for o in outcomes if o.llm_explanation_score is not None
        ]
        paired = [
            (o.rules_explanation_score, o.llm_explanation_score)
            for o in outcomes
            if o.rules_explanation_score is not None and o.llm_explanation_score is not None
        ]

        return Measurement(
            value=llm.value - rules.value,
            cases=n,
            companions={
                "rules_only_top1_accuracy": rules.value,
                "rules_llm_top1_accuracy": llm.value,
                "delta_top3_accuracy": llm.companions["top3_accuracy"]
                - rules.companions["top3_accuracy"],
                "delta_contributing_cause_f1": llm.companions["contributing_cause_f1"]
                - rules.companions["contributing_cause_f1"],
                "rules_only_correct_action_rate": rules_action,
                "rules_llm_correct_action_rate": llm_action,
                "delta_correct_action_rate": llm_action - rules_action,
                "incidents_with_identical_action": float(actions_identical),
                "rules_only_explanation_score": mean_score(rules_scores),
                "rules_llm_explanation_score": mean_score(llm_scores),
                "delta_explanation_score": mean_score([b for _, b in paired])
                - mean_score([a for a, _ in paired]),
                "explanations_judged": float(len(paired)),
                "llm_narratives_grounded_rate": _rate(
                    sum(1 for o in outcomes if o.llm_narrative_grounded), n
                ),
                "llm_incidents_escalated": float(
                    sum(1 for o in outcomes if o.llm_escalation_reason)
                ),
            },
            detail={
                "headline": "delta in top-1 root-cause accuracy, rules+LLM minus rules-only",
                "explanation_judge_caveat": (
                    "the judge is a model from the same vendor as the arm it scores, "
                    "reading both summaries blind; treat the score as indicative"
                ),
                "llm_wins": sorted(
                    o.scenario_id
                    for o in outcomes
                    if _top1(o.llm_ranking, o.truth_root_cause)
                    and not _top1(o.rules_ranking, o.truth_root_cause)
                ),
                "llm_losses": sorted(
                    o.scenario_id
                    for o in outcomes
                    if _top1(o.rules_ranking, o.truth_root_cause)
                    and not _top1(o.llm_ranking, o.truth_root_cause)
                ),
            },
        )

    def grade(self) -> GraderResult:
        return judge(self.claim_id, self.measure())


@dataclass(frozen=True, slots=True)
class GroundingGrader:
    """C10: how often the narrative asserts something the evidence does not support.

    The method is the register's own and is deterministic: every cited id must
    be in the bundle and every quoted figure must match an observation or a
    computed value. The verdict is `check_grounding`'s, taken from the run - a
    model is not asked whether a model was grounded.

    **A known leniency, reported rather than hidden.** A figure is matched
    against *every* numeric observation in the bundle within 0.05, and a bundle
    holds over a thousand readings, so a wrong figure can pass by landing near
    an unrelated one. The unsupported-claim rate is therefore a lower bound, and
    the count of figures checked rides with it so the reader can see how much
    was tested.
    """

    outcomes: tuple[CaseOutcome, ...]
    claim_id: str = "C10"

    def measure(self) -> Measurement:
        graded = [o for o in self.outcomes if o.llm_narrative_grounded is not None]
        failing = [o for o in graded if not o.llm_narrative_grounded]
        counted = Counter(f for o in failing for f in o.llm_grounding_failures)
        return Measurement(
            value=_rate(len(failing), len(graded)),
            cases=len(graded),
            companions={
                "narratives_grounded_rate": _rate(len(graded) - len(failing), len(graded)),
                "figures_checked": float(sum(o.llm_figures_checked for o in graded)),
            },
            detail={
                "failure_counts": dict(sorted(counted.items())),
                "ungrounded": sorted(o.scenario_id for o in failing),
                "leniency": (
                    "figures are matched against every numeric observation in the bundle, "
                    "so this rate is a lower bound"
                ),
            },
        )

    def grade(self) -> GraderResult:
        return judge(self.claim_id, self.measure())


def _p95(values: list[float]) -> float:
    if len(values) < 2:
        return values[0] if values else 0.0
    return statistics.quantiles(values, n=20, method="inclusive")[-1]


@dataclass(frozen=True, slots=True)
class CostLatencyGrader:
    """C12: what one investigation costs and how long it takes.

    Summed per incident across its model calls, which run one after the other
    in the workflow, so latency adds. **Latency is the recording session's,
    replayed from the cassette** - the network and the vendor's load on the day
    - and is not something a replay can re-measure. Cost is recomputed from the
    stored token counts at the prices in `pricing.py`.
    """

    outcomes: tuple[CaseOutcome, ...]
    claim_id: str = "C12"

    def measure(self) -> Measurement:
        with_calls = [o for o in self.outcomes if o.invocations]
        costs = [sum(float(c["cost_usd"]) for c in o.invocations) for o in with_calls]
        latencies = [sum(float(c["latency_ms"]) for c in o.invocations) / 1000 for o in with_calls]
        cache_read = sum(int(c["cache_read_tokens"]) for o in with_calls for c in o.invocations)
        total_in = sum(
            int(c["input_tokens"]) + int(c["cache_read_tokens"])
            for o in with_calls
            for c in o.invocations
        )
        models = sorted({str(c["model"]) for o in with_calls for c in o.invocations})

        return Measurement(
            value=_p95(costs),
            cases=len(with_calls),
            companions={
                "cost_usd_p50": statistics.median(costs) if costs else 0.0,
                "cost_usd_p95": _p95(costs),
                "cost_usd_total": sum(costs),
                "latency_s_p50": statistics.median(latencies) if latencies else 0.0,
                "latency_s_p95": _p95(latencies),
                "model_calls_per_incident": (
                    statistics.fmean(len(o.invocations) for o in with_calls) if with_calls else 0.0
                ),
                "cached_input_share": _rate(cache_read, total_in),
            },
            detail={
                "headline": "p95 model cost per investigated incident, USD",
                "models": models,
                "scope": (
                    "model calls only; the deterministic nodes are milliseconds and cost "
                    "nothing. Latency is the recording session's, replayed from cassettes."
                ),
            },
        )

    def grade(self) -> GraderResult:
        measurement = self.measure()
        if not measurement.cases:
            return GraderResult(
                claim_id=self.claim_id,
                measurement=measurement,
                status=ClaimStatus.INSUFFICIENT_DATA,
                reason=(
                    "no incident recorded a model call, so there is no cost to take a percentile of"
                ),
            )
        return judge(self.claim_id, measurement)
