"""The metrics C2 is judged on, and the stop condition's metric.

**Rank metrics and calibration metrics answer different questions.** AUC-PR
says whether a model orders risky moments above safe ones; Brier and ECE say
whether its numbers can be read as probabilities. The decision layer multiplies
the probability by a cargo value, so the second matters more than the first
and a model can win the first while failing the second.

**Lead time at a fixed false-alarm rate** is the metric the stop condition is
written on, and it is defined here *before any model was trained*:

1. An estimator scores every minute of every scenario.
2. An alert fires at the first minute the score has been at or above a
   threshold for `HOLD` consecutive minutes - the same three-reading hold the
   shipped detector uses, so a spiky score cannot buy lead time.
3. The false-alarm rate is the fraction of *non-breach* scenarios that ever
   alert.
4. The threshold is the lowest (most sensitive) one whose false-alarm rate is
   at most `TARGET_FAR`.
5. Lead time is the true breach minute minus the alert minute, **with a missed
   breach counted as zero minutes** rather than dropped. Dropping misses would
   let a timid detector look good by only ever alerting on the easy ones.

`TARGET_FAR = 0.30` is C1's current operating point, chosen so the comparison
is at the false-alarm rate the shipped detector already lives with.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray
from sklearn.metrics import average_precision_score

from benchmarks.riskmodel.dataset import Row, ScenarioMeta

__all__ = [
    "HOLD",
    "TARGET_FAR",
    "LeadTimeResult",
    "auc_pr",
    "bootstrap_difference",
    "brier",
    "ece",
    "lead_time_at_far",
    "leads_at_threshold",
    "reliability",
]

HOLD = 3
TARGET_FAR = 0.30
_THRESHOLD_CANDIDATES = 400


def auc_pr(labels: NDArray[np.int_], scores: NDArray[np.float64]) -> float:
    return float(average_precision_score(labels, scores))


def brier(labels: NDArray[np.int_], scores: NDArray[np.float64]) -> float:
    return float(np.mean((scores - labels) ** 2))


def reliability(
    labels: NDArray[np.int_], scores: NDArray[np.float64], *, bins: int = 10
) -> list[dict[str, float]]:
    """Per-bin mean predicted probability against observed frequency."""
    edges = np.linspace(0.0, 1.0, bins + 1)
    index = np.clip(np.digitize(scores, edges[1:-1]), 0, bins - 1)
    out: list[dict[str, float]] = []
    for b in range(bins):
        mask = index == b
        if not mask.any():
            continue
        out.append(
            {
                "bin_low": float(edges[b]),
                "bin_high": float(edges[b + 1]),
                "mean_predicted": float(scores[mask].mean()),
                "observed_frequency": float(labels[mask].mean()),
                "count": float(mask.sum()),
            }
        )
    return out


def ece(labels: NDArray[np.int_], scores: NDArray[np.float64], *, bins: int = 10) -> float:
    """Expected calibration error: bin-weighted gap between predicted and observed."""
    total = len(labels)
    return float(
        sum(
            b["count"] / total * abs(b["mean_predicted"] - b["observed_frequency"])
            for b in reliability(labels, scores, bins=bins)
        )
    )


@dataclass(frozen=True, slots=True)
class LeadTimeResult:
    threshold: float
    false_alarm_rate: float
    detection_rate: float
    #: Median over every breach scenario, a miss counting as 0 minutes.
    median_lead_min: float
    #: Median over only the breaches that were detected. Reported so the
    #: cost of the misses is visible, never as the headline.
    median_lead_detected_min: float
    breach_scenarios: int
    non_breach_scenarios: int


def _first_alert(above: NDArray[np.bool_], minutes: NDArray[np.int_], hold: int) -> int | None:
    """The minute the score has been high for `hold` consecutive minutes."""
    if len(above) < hold:
        return None
    run = np.convolve(above.astype(int), np.ones(hold, dtype=int), mode="valid")
    hits = np.nonzero(run >= hold)[0]
    return None if len(hits) == 0 else int(minutes[hits[0] + hold - 1])


def lead_time_at_far(
    rows: list[Row],
    scores: NDArray[np.float64],
    meta: dict[str, ScenarioMeta],
    *,
    target_far: float = TARGET_FAR,
    hold: int = HOLD,
) -> LeadTimeResult:
    by_scenario: dict[str, tuple[NDArray[np.int_], NDArray[np.float64]]] = {}
    grouped: dict[str, list[int]] = {}
    for index, row in enumerate(rows):
        grouped.setdefault(row.scenario_id, []).append(index)
    for scenario_id, indices in grouped.items():
        indices.sort(key=lambda i: rows[i].minute)
        by_scenario[scenario_id] = (
            np.array([rows[i].minute for i in indices]),
            scores[indices],
        )

    breaches = [s for s in by_scenario if meta[s].breach_minute is not None]
    non_breaches = [s for s in by_scenario if meta[s].breach_minute is None]

    candidates = np.unique(np.quantile(scores, np.linspace(0.0, 1.0, _THRESHOLD_CANDIDATES)))
    chosen: LeadTimeResult | None = None
    for threshold in candidates:  # ascending, so the first feasible one is the lowest
        alerts = {
            s: _first_alert(by_scenario[s][1] >= threshold, by_scenario[s][0], hold)
            for s in by_scenario
        }
        far = sum(alerts[s] is not None for s in non_breaches) / max(1, len(non_breaches))
        if far > target_far:
            continue
        leads = [
            (meta[s].breach_minute - alerts[s]) if alerts[s] is not None else 0  # type: ignore[operator]
            for s in breaches
        ]
        detected = [lead for s, lead in zip(breaches, leads, strict=True) if alerts[s] is not None]
        chosen = LeadTimeResult(
            threshold=float(threshold),
            false_alarm_rate=far,
            detection_rate=len(detected) / max(1, len(breaches)),
            median_lead_min=float(statistics.median(leads)) if leads else 0.0,
            median_lead_detected_min=float(statistics.median(detected)) if detected else 0.0,
            breach_scenarios=len(breaches),
            non_breach_scenarios=len(non_breaches),
        )
        break
    if chosen is None:
        # No threshold, however high, met the false-alarm budget. Reported as
        # zero detection rather than raising: it is a result about the scorer.
        return LeadTimeResult(
            threshold=float("inf"),
            false_alarm_rate=0.0,
            detection_rate=0.0,
            median_lead_min=0.0,
            median_lead_detected_min=0.0,
            breach_scenarios=len(breaches),
            non_breach_scenarios=len(non_breaches),
        )
    return chosen


def leads_at_threshold(
    rows: list[Row],
    scores: NDArray[np.float64],
    meta: dict[str, ScenarioMeta],
    threshold: float,
    *,
    hold: int = HOLD,
) -> tuple[dict[str, float], dict[str, bool]]:
    """Per-breach lead (a miss is 0) and per-non-breach alert flag, at a fixed threshold."""
    grouped: dict[str, list[int]] = {}
    for index, row in enumerate(rows):
        grouped.setdefault(row.scenario_id, []).append(index)
    leads: dict[str, float] = {}
    alerted: dict[str, bool] = {}
    for scenario_id, indices in grouped.items():
        indices.sort(key=lambda i: rows[i].minute)
        minutes = np.array([rows[i].minute for i in indices])
        alert = _first_alert(scores[indices] >= threshold, minutes, hold)
        breach = meta[scenario_id].breach_minute
        if breach is None:
            alerted[scenario_id] = alert is not None
        else:
            leads[scenario_id] = float(breach - alert) if alert is not None else 0.0
    return leads, alerted


def bootstrap_difference(
    rows: list[Row],
    meta: dict[str, ScenarioMeta],
    scores_a: NDArray[np.float64],
    scores_b: NDArray[np.float64],
    labels: NDArray[np.int_],
    *,
    threshold_a: float,
    threshold_b: float,
    resamples: int = 300,
    seed: int = 0,
) -> dict[str, dict[str, float]]:
    """95% intervals for (A minus B) on Brier, AUC-PR and lead time, resampling scenarios.

    Scenarios are the resampling unit because rows within a scenario are
    near-copies of each other: resampling rows would report an interval far
    narrower than the data supports. **Optimistic in one respect, stated:** the
    lead-time thresholds are the ones chosen on the full pool and held fixed
    across resamples, so that interval does not include threshold-selection
    variance.
    """
    rng = np.random.default_rng(seed)
    by_scenario: dict[str, list[int]] = {}
    for index, row in enumerate(rows):
        by_scenario.setdefault(row.scenario_id, []).append(index)
    ids = sorted(by_scenario)
    lead_a, _ = leads_at_threshold(rows, scores_a, meta, threshold_a)
    lead_b, _ = leads_at_threshold(rows, scores_b, meta, threshold_b)

    diffs: dict[str, list[float]] = {"brier": [], "ece": [], "auc_pr": [], "median_lead_min": []}
    for _ in range(resamples):
        picked = rng.choice(len(ids), size=len(ids), replace=True)
        chosen = [ids[i] for i in picked]
        idx = np.concatenate([by_scenario[s] for s in chosen])
        y = labels[idx]
        if y.sum() == 0:
            continue
        diffs["brier"].append(brier(y, scores_a[idx]) - brier(y, scores_b[idx]))
        diffs["ece"].append(ece(y, scores_a[idx]) - ece(y, scores_b[idx]))
        diffs["auc_pr"].append(auc_pr(y, scores_a[idx]) - auc_pr(y, scores_b[idx]))
        leads_a = [lead_a[s] for s in chosen if s in lead_a]
        leads_b = [lead_b[s] for s in chosen if s in lead_b]
        if leads_a and leads_b:
            diffs["median_lead_min"].append(
                float(statistics.median(leads_a) - statistics.median(leads_b))
            )
    out: dict[str, dict[str, float]] = {}
    for name, values in diffs.items():
        arr = np.array(values)
        out[name] = {
            "mean": float(arr.mean()),
            "ci95_low": float(np.percentile(arr, 2.5)),
            "ci95_high": float(np.percentile(arr, 97.5)),
            "share_a_better": float(
                (arr < 0).mean() if name in ("brier", "ece") else (arr > 0).mean()
            ),
        }
    return out
