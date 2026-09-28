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
| **Method** | Isotonic calibration fitted on a dedicated split; operating threshold chosen by expected cost, not 0.5. **As run:** leave-one-regime-out over the 12 fault regimes (every scenario scored by a model that never saw its regime), a dedicated calibration split (every 4th scenario), a fixed false-alarm budget of 0.30 in place of a cost-chosen threshold (that belongs to the decision layer). |
| **Status** | `MEASURED` for the candidate that would ship — a **logistic regression** on the risk features plus the two baselines' scores: **ECE 0.030**, AUC-PR 0.689, Brier 0.068, out of regime, over 60/60 scenarios. Slope extrapolation: ECE 0.119, AUC-PR 0.284, Brier 0.139. Not the isotonic-calibrated LightGBM the register named; see below. **Stated limitation: synthetic data only.** |
| **Run** | `run-a361f65e424e` · `git_sha=c9a8f9a` · `model_id=none` · `prompt_version=none` · `pack_version=1.1.0` |

> **The pre-registered stop condition triggered, decisively.** LightGBM +
> isotonic failed to beat slope extrapolation after both allowed feature
> iterations: **−57 min** median lead time at FAR ≤ 0.30 (95% interval over
> scenarios [−74.5, −42.0]); ECE 0.069, AUC-PR 0.450. Twelve regimes are
> effectively sixty independent scenarios however many rows there are, and the
> trees fit regime quirks. The plan says to publish that and ship the simpler
> model — which is what the logistic candidate is. `5 points` in the stop
> condition was read as 5 minutes of median lead time, decided before any result.
>
> **What the bootstrap over scenarios establishes for the logistic candidate,
> against slope extrapolation:** AUC-PR +0.36 [+0.09, +0.56] and ECE −0.081
> [−0.184, −0.012] are real; Brier −0.067 [−0.162, +0.003] touches zero; the
> **+11.1 min lead-time gain [−2.5, +25.0] is not established** and must not be
> quoted. That interval holds the thresholds fixed, so it is optimistic.
>
> **Selection caveat.** The v2 features were chosen after seeing iteration 1's
> per-regime failure, and the logistic model was identified as the winner
> afterwards, so the held-out regimes were consulted twice. Confirm on fresh
> scenarios before treating the advantage as settled. Its coefficients are not
> physically interpretable (headroom has a *positive* weight — collinearity with
> the slope-score input), so it is a predictive stack, not a physical model.
>
> **Not shipped as the default.** The detector still uses slope extrapolation:
> swapping primary moves C1 and the demo's minute 102 and is its own block.
> The model is a versioned artifact (`data/models/risk_v1/`) that
> `LinearRiskEstimator` loads and refuses if its feature list has changed.

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
| **Dataset** | AxonBench scenarios with IncidentForge ground truth. **No count is stated**; the harness requires 40 *investigated incidents*, borrowed from C1's forty and not from this register, where an incident is a scenario the predictive detector opened one on (44 of 60: 38 breach scenarios plus 6 non-breach ones — near-miss controls with a real but non-breaching fault — on which the detector also fired). |
| **Status** | `MEASURED` for both arms, over the same 44 investigated incidents. `rules_only`: top-1 **0.50**, top-3 0.70, contributing-cause F1 0.125. `rules_llm` (Groq, `openai/gpt-oss-120b`): top-1 **0.432**, top-3 0.591, contributing-cause F1 0.080 — **worse on all three**, and see C5. |
| **Run** | rules_only: `run-c99db2657550` · `git_sha=cdc615d`. rules_llm: `run-a434370a4627` · `git_sha=cbce97f` · `model_id=openai/gpt-oss-120b (links)` · `prompt_version=v2` · `pack_version=1.1.0` · `config_hash=42da15021752172e` |

> **What the baseline number is made of.** 0.50 top-1 hides a sharp split, by
> true cause: environmental heat **6/6**, compressor degradation **14/15**,
> door left open **2/11**, sensor malfunction **0/4**, reefer fuel exhaustion
> **0/8**. There is no fuel rule, so that cause sits at the 0.02 floor, and on
> door and sensor incidents the rules answer `compressor_degradation`. That is
> a statement about a handful of hand-written rules, not about diagnosis in
> general. Nine of the 44 incidents were opened *before* the causal fault
> began (the start-of-run artefact recorded against C1); on the other 35
> top-1 is 0.46.
>
> **The model cannot fix the fuel cases by design.** The scorer computes
> `confidence = prior × support` with support capped at 1, so a model may
> re-order and drop hypotheses but can never raise one above its rule prior
> (I2). A cause the rules gave the floor stays at the floor whatever the model
> says. If C5 lands at or near zero, that is the architecture working as
> specified, and worth stating that way.

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
| **Dataset** | Not stated. Same 44 investigated incidents as C3, both arms on each. |
| **Status** | `REFUTED` — measured, and the answer is no. Δ top-1 **−0.068** (0.432 vs 0.500), Δ top-3 **−0.114**, Δ contributing-cause F1 **−0.045**, Δ correct-action **0.000** (identical on all 44, as predicted below), Δ judge-scored explanation **+1.45** (1.82 → 3.27 of 5, the one place the model helped). Run `run-a434370a4627`, same provenance as C3. |

> **What C5 can and cannot show.** The decision engine ranks actions from the
> computed breach probability and never reads a hypothesis, so
> correct-action selection is identical across the arms *by construction*; the
> grader measures the delta anyway and a test turns red if they ever diverge.
> Root-cause accuracy is the only place the model can help, and it is capped
> by the rule priors (see C3). The judge-scored explanation quality uses a
> model from the same vendor as the arm it scores, blind to which summary is
> which; treat it as indicative.
>
> **Measured with a free open-weight model (Groq, `gpt-oss-120b`/`-20b`), not a
> frontier one** — recorded because the OpenAI account had no credit. Root-cause
> accuracy came out *worse* than the deterministic rules, exactly as the design
> predicts it can: the scorer computes `confidence = prior × support`, capped
> at the rule prior, so the model can re-order or drop hypotheses but never
> raise one above what the rules already permit. The correct-action rate is
> **identical across arms on all 44 incidents** — not close, identical — which
> is the I2/decision-engine separation confirmed on real model output rather
> than only asserted. **32% of narratives failed the deterministic grounding
> check** (see C10); those incidents escalate rather than reaching a
> dispatcher, so a fabricated number cannot appear in a shown recommendation,
> but it does mean the model's prose is not yet reliable enough to ship
> unescalated. Explanation quality is the one dimension the model wins by a
> wide margin (1.82 → 3.27 of 5): the rules-only arm's narrative is a fixed
> template, and the judge consistently preferred prose that named the reading
> and the reasoning over one that only named a number.
>
> **Not a verdict on frontier models.** `gpt-oss-20b` is a small open-weight
> model chosen for a free key, not for capability. This result says what a
> deterministic scorer plus a weak model produces; it does not say what the
> ceiling is with `gpt-5` or `claude-sonnet-5`. Re-recording with either would
> need cassettes of its own — different vendor, different cassette key — and
> would answer a different, more interesting question.

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
| **Dataset** | Not stated, but the metric names four modalities (documents, images, SOPs, database values) and a defences-off baseline. |
| **Status** | `INSUFFICIENT_DATA` — **cannot be measured yet, and not for want of an API key.** There is no attack pack, no image modality until B13, and no attack suite until AxonRed (B14). A small hand-written pack would run into the stop condition below: an attack success rate of 0 from weak attacks measures the attacks. The structural defences are real and tested (`ProposedLink` has no confidence field, the scorer ignores prose, the grounding check rejects an untraceable figure) but a structural argument is not a measured rate. |

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
| **Status** | `MEASURED` — unsupported-claim rate **0.318** (14 of 44 narratives), 313 figures checked, over `gpt-oss-20b` narratives written for real incidents (Groq, `run-a434370a4627`). |
| **Run** | `run-a434370a4627` · `git_sha=cbce97f` · `model_id=openai/gpt-oss-20b` · `prompt_version=v2` · `pack_version=1.1.0` · `config_hash=42da15021752172e` |

> **Known leniency and known strictness, both left uncorrected.** Lenient: a
> figure is matched against *every* numeric observation in the bundle within
> 0.05, and a bundle holds over a thousand readings, so a wrong figure can
> pass by landing near an unrelated one — the stored rate is a lower bound in
> this direction. Strict: `gpt-oss-20b` routinely rounds a dollar figure to
> the nearest whole dollar ("−7428 USD" for an exact −7428.34), and the same
> 0.05 *absolute* tolerance that correctly checks a temperature rejects that
> as unsupported — `NUMERIC_TOLERANCE`'s own docstring already documents this
> tolerance as "far too tight" for a dollar figure. All 14 failures are this
> shape. Neither is corrected here: loosening the threshold after seeing this
> run's count would be tuning the detector against the benchmark that
> measures it, which this project does not do (see §1, C1's
> `CONSECUTIVE_READINGS_TO_FIRE`).
>
> **One real bug found and fixed before this run, not papered over.**
> `gpt-oss` writes a negative number with a typographic minus (en dash, minus
> sign or non-breaking hyphen), which the original `[-+]?` did not match, so a
> correct negative reading — any frozen-cargo temperature — was extracted as
> its positive magnitude and flagged as fabricated. That is a parser bug, not
> a threshold, and fixing it moved the rate from 0.341 to 0.318 (15 → 14
> failures) on the identical cassettes. `tests/unit/test_agents.py`'s
> `TestTypographicMinusIsStillAMinus` proves it red on the old regex first.

### C11 — Outcome verification

| | |
|---|---|
| **Claim** | The system verifies whether an intervention actually worked. |
| **Metric** | Verification-verdict accuracy against known post-action trajectories. |
| **Method** | *Pre-registered 2026-09-28, before any post-action trajectory was generated.* IncidentForge gains an intervention hook that changes the physics from the minute an action takes effect: **cold storage** (reroute: cargo on shore power in a cold room at the setpoint, door closed, 45 min after the action - the flagship's detour to CS-11), **unit swap** (a healthy reefer, door closed, 60 min after), **driver contact** (an open door is closed 20 min after; nothing else changes). For each breach scenario in pack v1.1.0 and each of the three actions, the action is taken at minute *A* = 30 minutes before the reported breach (the true breach when the sensor never reports one), floored at 30. The shipped verifier (`verification.outcome.evaluate_effect`) grades the **reported** readings over the window the shipped action catalogue declares (90 / 120 / 90 min). The **label** is the same rule applied to the simulator's **true** temperatures over the same window. A trajectory whose window runs past the scenario's end is excluded and counted. |
| **Dataset** | Up to 120 trajectories (40 breach scenarios x 3 actions); the ClaimSpec's 20 is the floor. |
| **What it does and does not measure** | Whether a verifier that can only see the sensor reaches the verdict it would reach if it could see the truth - so its errors come from the sensor (noise, drift, a stuck reading). It does **not** measure whether "back in the envelope within 90 minutes" is the right definition of success; the label shares that rule by construction. **Prediction, stated before running:** agreement is high on honest-sensor scenarios and breaks on the lying-sensor ones, where a drifting or stuck reading can confirm a recovery that did not happen. The dangerous error - **confirming an intervention that did not work** - is reported separately as a companion, not averaged away. |
| **Status** | `PLACEHOLDER` |

### C12 — Operating cost and latency

| | |
|---|---|
| **Claim** | An incident is investigated end to end for $X at p95 latency Y seconds. |
| **Metric** | Cost p50/p95 and latency p50/p95, aggregated from `model_invocation` rows and OTel spans. |
| **Dataset** | Not stated. Same 44 investigated incidents. |
| **Status** | `MEASURED` — **$0.00063 p95** ($0.00052 p50) per incident, 2 model calls each, p50 latency **8.5 s** / p95 **11.4 s**, over 44 incidents. Run `run-a434370a4627`. Aggregated from each workflow call's recorded invocation (tokens, cost, latency), not from `model_invocation` rows or OTel spans, which nothing writes yet. Covers the model calls only; the judge (88 calls, $0.0105 total) is excluded, since it is grading infrastructure, not part of an incident's own cost. |
>
> **This is a free-tier open-weight model's cost, stated as such.** `gpt-oss`
> is priced here at what the *paid* tier of the same tokens would cost
> (`$0` was the actual spend). Prices are checked against
> `console.groq.com/docs/model/...` — `PRICES_AS_OF` in `pricing.py`, same
> discipline as the OpenAI rows — and checking them caught a real error:
> `gpt-oss-120b`'s output price was first recorded at $0.75/Mtok from memory;
> the page says $0.60/Mtok. This register's number is the corrected one.
> Latency is Groq's on the day this was recorded, replayed from the cassette —
> not a live measurement, and not comparable to a frontier model's latency,
> which was not measured.

### C13 — Safe degradation

| | |
|---|---|
| **Claim** | The system degrades safely when dependencies fail, and never fabricates a missing observation. |
| **Metric** | Correct-degradation rate per failure mode; **fabrication rate, target exactly 0**. |
| **Method** | Failure-injection suite covering every row of the failure matrix. |
| **Dataset** | The 11 graded rows of [`failure_matrix.md`](failure_matrix.md), pre-registered and committed (`0013f12`) before the suite existed. |
| **Status** | `MEASURED` — **fabrication rate 0** over **11/11** failure-matrix rows, **correct-degradation rate 1.00** (every row, every injected variant). Before the fix the same suite measured **6/11 correct, 0 fabricated**: an LLM outage of any kind crashed the workflow (F1-F3), an unusable answer ran silently as `FULL` (F4), and missing facility data produced a recommendation 7.6x worse in expected value (F11). The system failed safe but not gracefully; the `NO_LLM` rung that fixed it is in `backend/app/agents/degradation.py`. |
| **Run** | rules_llm: `run-3bf20da5bf32` · rules_only: `run-e5395fd01838` · both `git_sha=fe1a050`, `pack_version=1.1.0` |

> **Read the scope before quoting it.** The suite runs the real workflow with
> failures injected at the provider boundary and in the evidence bundle; it
> does not kill real containers. Database and network failures below the
> workflow (Postgres down during execution, a dropped SQL Server connection)
> are covered by the integration and execution-gate suites, not by this
> number. `NO_VLM` is registered and not graded: there is no vision model yet.

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
