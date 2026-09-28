# AxonFDE — case study

Written for: engineers and hiring managers deciding whether this project is
evidence of how its author works. Every number below comes from a stored,
reproducible benchmark run committed under `benchmarks/results/published/`;
the register that defines each one is [`evaluation/claims.md`](evaluation/claims.md).

---

## The problem

Refrigerated trucks carry vaccines, medicines and food that must stay inside a
narrow temperature range — often 2–8 °C. The alarms fleets rely on fire when
the cargo has **already** left that range. By then the load is usually lost.

Three things make this hard to fix with a dashboard or a chatbot:

1. **The signal is a trend, not a reading.** A failing compressor shows up as a
   slow climb long before any single reading is out of spec.
2. **The sources disagree.** The company's order system (a legacy SQL Server
   ERP) and the signed shipping document can state different limits. Trust the
   wrong one and a shipment at risk looks fine.
3. **Acting has consequences.** Rerouting a truck costs money and time. An
   AI that can act on its own is a liability, not a feature.

## What was built

A closed-loop system that reads sensor telemetry, the legacy ERP (read-only)
and shipping documents; reconciles them; predicts a breach; prices every
option including doing nothing; asks a human to approve the best one; executes
it exactly once; and checks afterwards whether it worked.

- **Python 3.12**, FastAPI, PostgreSQL + pgvector, SQL Server (legacy ERP, in a
  container), LangGraph 1.x for the investigation workflow, a static
  no-build-step web front end.
- **IncidentForge**, a physics-based simulator (lumped-capacitance thermal
  model, seeded, deterministic) generating 60 scenarios — 40 breaches, 20
  controls, 23 planted source conflicts.
- **AxonBench**, a benchmark harness where every public number has a
  pre-registered method, a baseline, and a stored run.
- **An LLM, deliberately boxed in.** It proposes which evidence bears on which
  cause and writes the explanation. It never creates an observation, never
  sets a confidence, never chooses the action. Each model step is followed by
  a deterministic check that can reject it.

Ten invariants are enforced by tests, database constraints or import
contracts — for example, *no action executes without a valid, unexpired
approval that matches exactly what the approver saw*.

## What it measured

| | Result |
|---|---|
| Early warning (C1) | **49 min** median lead time over the threshold alarm, **at a 30% false-alarm rate against the alarm's 20%**. Never quoted alone. |
| Source conflicts (C6) | recall **1.00**, precision **1.00** over 23 planted conflicts and 60 look-alikes |
| Unauthorised actions (C7) | **0** across all 50 role × action cells |
| Forbidden SQL (C8) | **0** of 57 adversarial queries |
| Calibration (C2) | ECE **0.030** vs 0.119 for slope extrapolation, out of regime |
| Verifying outcomes (C11) | **0.94** agreement with ground truth over 100 post-action trajectories; **3 false confirmations, all from stuck sensors** |
| Safe degradation (C13) | **0** fabrications, **11/11** rows of a pre-registered failure matrix |
| Does the LLM help? (C5) | **Refuted.** Root cause 50% → 43% (worse); chosen action identical on all 44 incidents; explanation quality 1.8 → 3.3 of 5 |

## What went wrong, and what caught it

These are the most useful part of the project, and each is documented where
it happened.

- **The append-only audit table wasn't.** The app connected as a PostgreSQL
  superuser, and superusers ignore table privileges. Split into an owner role
  and a restricted runtime role; a test now asserts the runtime role is not a
  superuser.
- **Concurrent audit writes lost events rather than forking the chain** — the
  plan predicted a fork, the measurement showed silent loss, which is worse.
- **CI had failed on every run since the first commit** and nobody noticed,
  because the local gate was green. Five unrelated causes, including a
  security scanner action that silently ignored its own configuration.
- **The detector fires before the fault on 23% of breaches.** Widening the
  scenario pack from 3 to 60 exposed it: in hot weather cargo genuinely warms
  while the unit settles. It was **published, not tuned away** — the honest
  median over alerts that followed their fault is 35 minutes.
- **A trained model failed its own stop condition.** LightGBM lost 57 minutes of
  lead time against the slope baseline; the pre-registered rule said ship the
  baseline, so it did (ADR-008).
- **The LLM made root-cause accuracy worse.** Measured on a small free model
  and reported as `REFUTED`, with the caveat that it is not a verdict on
  frontier models.
- **An LLM outage crashed the whole investigation.** The architecture promised
  a rules-only fallback; a failure-injection suite, written against a failure
  matrix committed *before* the suite, found it had never been built — along
  with missing facility data being read as "no capacity", which produced a
  recommendation 7.6× worse in expected value. Both fixed; the suite went red
  with the fix reverted.
- **A test passed against the bug it was written to catch.** It compared a
  count that the bug did not change. Now every load-bearing test is proven by
  breaking the code and watching it fail.

## How the work was run

- **Claims are registered before they are measured**, with a method and a
  baseline. A claim whose dataset is too small reports `INSUFFICIENT_DATA`,
  not a weaker `MEASURED`.
- **Negative results are published.** Two of the headline findings (the LLM,
  the trained model) are negative.
- **Safety metrics gate CI** and are never averaged with quality metrics.
- **Nothing is tuned against the benchmark that measures it.**

## What is not done

- The physics is simulated; real-world generalisation is unvalidated.
- Two claims are still open: multimodal evidence (needs a vision model) and
  prompt-injection resistance (needs an attack pack and images).
- Actions are simulated; nothing dispatches a real truck. Approving an action
  from the web UI is not built — the UI is read-only over recorded runs.
- No cloud deployment yet.

Source: [github.com/dilipna/axon-fde](https://github.com/dilipna/axon-fde).
