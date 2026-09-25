# Claim register

Every performance claim AxonFDE makes — in the README, the UI, a case study, a
CV bullet or an interview — is registered here together with the evidence
required to support it.

**The rule is absolute:**

> No number appears anywhere unless a stored benchmark run produced it, and
> that run is identified by `(git_sha, prompt_version, model_id, pack_version,
> config_hash)`.

Claims move through four states:

| State | Meaning |
|---|---|
| `PLACEHOLDER` | Registered, not yet measured. May not be stated anywhere. |
| `MEASURED` | A stored run supports it. Must cite the run ID. |
| `REFUTED` | Measured and not supported. **Published anyway.** |
| `INSUFFICIENT_DATA` | A grader ran and produced a number, but over fewer cases than the claim's own method requires. **The number may not be stated as the claim.** |

`INSUFFICIENT_DATA` was added when AxonBench first ran. It is the honest
answer to a real situation: a lead time computed from one scenario is a real
measurement and is not evidence for a claim whose method says forty. Calling
it `MEASURED` is precisely the failure invariant I7 exists to prevent, and
leaving it `PLACEHOLDER` discards both the run and the record of what is
missing. `docs/evaluation/results.md` lists every such claim with its
shortfall.

A `REFUTED` claim is not a failure of the project. An ablation that shows a
capability does not help is a real finding, and reporting it is worth more than
quietly deleting the experiment. Two claims below (C4, C5) are explicitly
expected to have a meaningful chance of landing there.

---

## Forbidden claims

These can never be truthfully made by this project, whatever the benchmark
says, because the system has no access to the underlying reality:

- Any statement about **real-world** accuracy, spoilage, or dollars saved.
- Any reference to real customers, real users, production scale, or uptime.
- Any claim that the risk model generalises beyond the simulator it was
  trained on.
- Any claim that the intervention simulator predicts physical outcomes.

Anything framed in dollars is a **model-based estimate under stated
assumptions** and must be labelled as such at every point of use.

Measured numbers are regenerated into `docs/evaluation/results.md` by
`poe bench report`, and the runs behind published figures are committed under
`benchmarks/results/published/` so that anybody can check them.

---

## Register

### C1 — Lead time over threshold detection

| | |
|---|---|
| **Claim** | AxonFDE raises an actionable incident before a threshold alarm fires. |
| **Metric** | Median lead time and IQR, in minutes, **reported jointly with false-alarm rate**. |
| **Baseline** | `BaselineDetector` (threshold alarm) consuming the identical event stream. |
| **Dataset** | ≥40 AxonBench scenarios in which a breach actually occurs. |
| **Method** | `lead_time = t(threshold_alarm) − t(axon_alert)`, computed only over true-breach scenarios. |
| **Status** | `MEASURED` — median **49 min** (IQR 49), **false-alarm rate 0.30** against the baseline's 0.20. Over the 40 true-breach scenarios of pack v1.1.0, with 20 controls supplying the rate. |
| **Run** | `run-9d16820ec3b3` · `git_sha=7ac6005` · `model_id=none` · `prompt_version=none` · `pack_version=1.1.0` · `config_hash=60b1fb3864a5cca8` |

> **Why the pairing is mandatory.** Lead time alone is trivially gameable: a
> detector that alerts constantly has infinite lead time and zero value. The
> metric is meaningless unless quoted with the false-alarm rate at the same
> operating threshold. Any presentation of C1 that omits it is a misuse.

> **The honest form of this result, in one line:** *49 minutes of median
> warning, bought at a false-alarm rate of 30% against the threshold alarm's
> 20%.* Ten points of extra false alarms is not free — Axon's dispatchers
> already mute alert categories — and the number is not quotable without it.

> **A second median, and why it is here.** On 22.5% of the breach scenarios
> the predictive detector fired **before the causal fault had started**. That
> is not skill. The cause is specific and worth stating: a load begins at
> setpoint, a proportional controller needs steady-state error to produce
> output, so in hot ambient the cargo genuinely climbs for half an hour before
> levelling off — and linear extrapolation cannot tell that curve from a slow
> excursion. Restricted to alerts that followed their fault, the median is
> **35 min**, which is also the flagship's long-quoted figure. Both numbers are
> in the stored run. 49 is the headline for one reason only: it is what the
> **Method** row above computes, over the population the **Dataset** row
> defines. Narrowing that population to a subset picked *after* seeing which
> alerts looked unearned is not the stated method, however defensible the
> reasoning — that is a change to the method, made in the open, not a filter
> applied quietly on the way to a number. 35 is published beside it because the
> headline alone overstates the warning that is real, and note which direction
> it runs: the correction **costs** the claim 14 minutes.
>
> `CONSECUTIVE_READINGS_TO_FIRE = 3` was chosen on the flagship alone, where it
> moved the first alert from minute 29 to minute 102. Across sixty scenarios it
> does not generalise. That is a finding about the detector, not a defect in
> the pack, and it is deliberately **not** tuned away here: retuning a
> parameter against the benchmark that measures it is how a number stops
> meaning anything.

> **Where the 3 missing scenarios went.** 37 of the 40 breaches yielded a lead
> time. Three stuck-sensor scenarios produced no threshold alarm at all — the
> instrument froze inside the envelope while the cargo left it — so there is no
> `t(threshold_alarm)` to subtract from, and two of those three the predictive
> arm also missed. They are excluded from the median and named in the run.
> The exclusion is **conservative**: those are cases where the baseline fails
> outright, so counting them would raise the figure, not lower it.

### C2 — Calibrated excursion probability

| | |
|---|---|
| **Claim** | The risk model outputs calibrated probabilities, not just accurate rankings. |
| **Metric** | AUC-PR, Brier score, Expected Calibration Error, reliability diagram. |
| **Baseline** | (1) current-temperature margin rule, (2) linear slope extrapolation, (3) logistic regression. |
| **Dataset** | Held-out scenarios split by **generative regime** — unseen seeds, unseen fault types, unseen ambient ranges. |
| **Method** | Isotonic calibration fitted on a dedicated split; operating threshold chosen by expected cost, not 0.5. |
| **Status** | `PLACEHOLDER` |

> **Stated limitation, mandatory at every point of use:** trained and evaluated
> on synthetic data from a documented lumped-capacitance thermal model.
> Real-world generalisation is unvalidated.
>
> **Stop condition:** if LightGBM does not beat slope extrapolation by ≥5
> points on lead-time-at-fixed-false-alarm-rate after two feature iterations,
> the baseline ships as the production model and the ML result is published as
> a negative finding.

### C3 — Root-cause identification

| | |
|---|---|
| **Claim** | The system identifies the correct root cause of an incident. |
| **Metric** | Top-1 and top-3 accuracy; contributing-cause F1. |
| **Baseline** | `rules_only` arm (no LLM). |
| **Dataset** | AxonBench scenarios with IncidentForge ground truth. |
| **Status** | `PLACEHOLDER` |

### C4 — Multimodal evidence improves outcomes

| | |
|---|---|
| **Claim** | Adding visual and document evidence measurably improves decisions. |
| **Metric** | Δ root-cause accuracy, Δ conflict-detection accuracy, Δ cost, Δ latency. |
| **Baseline** | The `sql + telemetry + sop` arm. |
| **Dataset** | Modality arms A–E, with emphasis on the `sensor_drift` family where visual evidence should be decisive. |
| **Status** | `PLACEHOLDER` — **may be refuted; publish either way** |

> **The ablation must control for compressor response.** Measured on
> `sensor_drift_pharma_01` at minute 100 over a 30-minute window: the reported
> temperature climbs at +0.039 °C/min while compressor RPM is still *rising*
> (+2.0 rpm/min) and no fault code is present. On
> `compressor_degradation_pharma_01` the temperature climbs at +0.016 °C/min
> while RPM is *falling* (−9.2 rpm/min) with `AL17` active.
>
> A compressor winding down while cargo warms is a unit losing the fight; one
> winding up while the reported temperature climbs fast is physically
> incoherent, and the sensor is the thing that is wrong. Either that
> inconsistency or the plain absence of a fault code separates these cases
> **from telemetry alone, with no second modality involved**.
>
> So an arm comparison that adds a photograph on top of a feature set lacking
> compressor response would attribute to the image a discrimination that
> single-modality telemetry already achieves. The `sql + telemetry + sop`
> baseline arm must include the compressor-response feature, or C4 is
> inflated. Recorded 2026-09-19, before the ablation was built, so that the
> baseline cannot be quietly chosen to flatter the result.

### C5 — The LLM adds value over rules alone

| | |
|---|---|
| **Claim** | The language model contributes beyond what deterministic rules achieve. |
| **Metric** | Δ root-cause accuracy, Δ correct-action selection, judge-scored explanation quality. |
| **Baseline** | `rules_only`: rule-derived hypotheses, rule-derived actions, templated narrative. |
| **Status** | `PLACEHOLDER` — **may be refuted; publish either way** |

> This is the ablation most likely to be attacked in an interview and the one
> most worth running early. If the LLM contributes little, that is a finding
> about where LLMs belong in safety-critical workflows — which is a more
> interesting result than a marginal accuracy bump.

### C6 — Cross-source contradiction detection

| | |
|---|---|
| **Claim** | The system automatically detects when enterprise sources disagree. |
| **Metric** | Precision and recall against seeded, known conflicts. |
| **Baseline** | None — this is a new capability, not an improvement on one. |
| **Dataset** | Scenarios seeded with ERP-vs-BOL mismatches and sensor-vs-panel disagreements. |
| **Status** | `MEASURED` — recall **1.00**, precision **1.00**, over 23 seeded conflicts and 60 constructed negatives. |
| **Run** | `run-9d16820ec3b3` · `git_sha=7ac6005` · `model_id=none` · `prompt_version=none` · `pack_version=1.1.0` · `config_hash=60b1fb3864a5cca8` |

> **What makes the precision worth anything is the negative set.** Twenty-three
> seeded conflicts alone would give a precision of 1.00 that could not go down,
> because nothing was offered that the system could wrongly flag. So one
> negative is constructed per scenario, on a conflict-capable channel that
> scenario does not seed, with the two sources placed at **80% of the
> taxonomy's own `conflict_tolerance`** for that observation type — close
> enough to disagree, inside the band that says they do not. For a type whose
> tolerance is zero, such as the contractual limits, the hardest negative
> available is exact agreement, and that is what is used.

> **Read this as the modest claim it is.** Reconciliation is a deterministic
> comparison against a declared tolerance, so scoring 1.00 on both is the
> expected result rather than a surprising one. What the measurement
> establishes is that the 23 conflicts the pack seeds — across two mechanisms
> (ERP-vs-BOL, telemetry-vs-panel) and five observation types — actually reach
> the reconciler and raise a conflict, and that near-misses inside tolerance do
> not. It does not establish behaviour on noisy real-world sources, and no
> tolerance in this system was fitted to data.

### C7 — AI cannot execute unauthorised actions

| | |
|---|---|
| **Claim** | No model output can cause an unauthorised side effect. |
| **Metric** | Unauthorised-action rate and approval-bypass rate. **Target: exactly 0.** |
| **Method** | Full role × action policy matrix, plus the AxonRed escalation attacks. |
| **Gate** | Non-zero fails CI. This is not a tolerance-based metric. |
| **Status** | `MEASURED` — **0** unauthorised actions, **0** approval bypasses, **0** kill-switch leaks across all 50 role × action cells. |
| **Run** | `run-53b8015e9c0b` · `git_sha=1777425` · `model_id=none` · `prompt_version=none` · `pack_version=1.0.0` · `config_hash=60b1fb3864a5cca8` |

> **What this covers and what it does not.** The 50 cells are the whole
> matrix, and the kill switch is probed on every one of them, so the
> *policy-engine* half of the claim is complete. The AxonRed escalation
> attacks named in the method arrive with B14 and are not in this number; the
> execution gate against a live database is covered separately by
> `tests/security/test_execution_gate.py`. A larger adversarial set makes this
> claim stronger — it does not make the present zero less true.

### C8 — Generated SQL cannot mutate the legacy system

| | |
|---|---|
| **Claim** | The AI's database access is provably read-only. |
| **Metric** | Prohibited-operation rate across ~60 adversarial inputs. **Target: exactly 0.** |
| **Method** | Three independent layers, each tested separately: SQLGlot AST allowlist, the `axon_ai_ro` grant (SELECT on six views only), and statement timeouts + row caps. |
| **Gate** | Non-zero fails CI. |
| **Status** | `MEASURED` — **0** of 57 adversarial inputs reached the driver. |
| **Run** | `run-53b8015e9c0b` · `git_sha=1777425` · `model_id=none` · `prompt_version=none` · `pack_version=1.0.0` · `config_hash=60b1fb3864a5cca8` |

> **One of the three layers.** This number is the SQLGlot AST allowlist. The
> `axon_ai_ro` grant is exercised against a real SQL Server in
> `tests/integration/test_legacy_access.py`, because a claim about a grant
> cannot be tested without one; the timeouts and row caps are configuration
> asserted there too. The 57 inputs are grouped by *mechanism* — direct
> mutation, stacked statements, privilege manipulation, procedural execution,
> reaching outside the six views, session and server state, malformed input,
> injection shapes — rather than padded with variations of one idea, because
> the claim's strength comes from covering distinct routes to a write.

### C9 — Resistance to prompt injection across modalities

| | |
|---|---|
| **Claim** | Injected instructions in documents, images, SOPs and database values do not alter system behaviour. |
| **Metric** | Attack success rate, policy-violation rate, leakage rate, **and benign-task degradation**. |
| **Baseline** | The same scenarios with defences disabled. |
| **Status** | `PLACEHOLDER` |

> Benign-task degradation is part of the claim, not a footnote. A defence that
> costs eight points of root-cause accuracy is a bad defence, and reporting
> attack-success-rate alone would hide that.
>
> **Stop condition:** an attack success rate of 0 on the first run means the
> attack pack is too weak, not that the system is secure. Strengthen the
> attacks before reporting anything.

### C10 — Recommendations are grounded in retrieved evidence

| | |
|---|---|
| **Claim** | The system does not assert anything its evidence does not support. |
| **Metric** | Unsupported-claim rate. |
| **Method** | **Deterministic**, not LLM-judged: every cited evidence ID must exist in the context bundle, and every numeric claim in the narrative must match an evidence value within tolerance. |
| **Status** | `PLACEHOLDER` |

### C11 — Outcome verification

| | |
|---|---|
| **Claim** | The system verifies whether an intervention actually worked. |
| **Metric** | Verification-verdict accuracy against known post-action trajectories. |
| **Status** | `PLACEHOLDER` |

### C12 — Operating cost and latency

| | |
|---|---|
| **Claim** | An incident is investigated end to end for $X at p95 latency Y seconds. |
| **Metric** | Cost p50/p95 and latency p50/p95, aggregated from `model_invocation` rows and OTel spans. |
| **Status** | `PLACEHOLDER` |

### C13 — Safe degradation

| | |
|---|---|
| **Claim** | The system degrades safely when dependencies fail, and never fabricates a missing observation. |
| **Metric** | Correct-degradation rate per failure mode; **fabrication rate, target exactly 0**. |
| **Method** | Failure-injection suite covering every row of the failure matrix. |
| **Status** | `PLACEHOLDER` |

### C14 — Workflow improvement over the legacy process

| | |
|---|---|
| **Claim** | Dispatchers reach a correct decision faster with AxonFDE than with the legacy workflow. |
| **Metric** | Time to diagnosis, correct-action rate, manual step count, self-reported confidence. |
| **Baseline** | Legacy Mode UI (static tables + threshold alarms) over identical scenarios. |
| **Method** | Counterbalanced within-subject study, ≥8 participants. |
| **Status** | `PLACEHOLDER` (Phase 8) |

> If ≥8 participants are not available, this downgrades to a structured
> self-comparison with the limitation stated prominently — not dropped, and not
> presented as a user study.

### C15 — Estimated avoided loss

| | |
|---|---|
| **Claim** | Under stated assumptions, the system avoids an estimated $X of cargo loss per N shipments. |
| **Metric** | Expected avoided loss with a sensitivity band. |
| **Baseline** | A do-nothing policy over identical scenarios. |
| **Status** | `PLACEHOLDER` — must always be labelled a model-based estimate |

> Every input (cargo value, spoilage fraction, intervention cost, delay
> penalty) lives in a versioned assumptions file and is reproduced next to the
> number wherever it appears.

---

## Review checklist

Before any write-up, demo or CV bullet ships:

- [ ] Every number traces to a stored benchmark run ID.
- [ ] Every claim with a baseline states the baseline alongside the result.
- [ ] C1 is never quoted without its false-alarm rate, and not without the
      second median over alerts that followed their fault. The README and
      `results.md` both carry all three; anything derived from them must too.
- [ ] C2 is never quoted without the synthetic-data limitation.
- [ ] C15 is never quoted without "model-based estimate" and its assumptions.
- [ ] C4's baseline arm includes the compressor-response feature, so the
      multimodal delta is not credited with a single-modality result.
- [ ] Refuted claims are present and visible, not removed.
- [ ] No claim from the forbidden list appears in any form.
