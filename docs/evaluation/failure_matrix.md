# Failure matrix — the rows C13 is graded against

**Pre-registered 2026-09-28, before any failure-injection code was written.**
This file is committed on its own, ahead of the suite, so the history shows
the matrix was not drawn around whatever the injections happened to cover.
Changing a row after the suite exists is allowed, but it must say so here,
with the date and the reason.

C13's claim (`claims.md`): *the system degrades safely when dependencies fail,
and never fabricates a missing observation.* Its method: *failure-injection
suite covering every row of the failure matrix.* Until today no such matrix
existed; the nearest thing was the four-rung degradation ladder in
`docs/architecture/overview.md` §7. This file is that matrix.

## What "correct" and "fabricated" mean, for every row

- **Correct degradation** — the run ends in the row's *expected outcome*, and
  the reason it gives names the failure. A run that crashes (raises out of the
  workflow) when the row expects a degraded answer or an escalation is
  **incorrect**, even if nothing was fabricated, because a thrown workflow
  loses everything it had established.
- **Fabrication** — any of: evidence present at the end that was not supplied
  at the start (I1); a temperature envelope when none was derivable; a risk
  probability when the trajectory was not fittable; a narrative that failed
  the grounding check reaching an approval request; a model-authored narrative
  presented while the run says the model was unavailable. **Target: exactly 0
  across every row.**

## The rows

The ladder's rungs are `FULL`, `NO_VLM`, `NO_LLM`, `DETECT_ONLY`.
"Escalate" means: the run stops before `request_approval`, hands over to a
person with a stated reason, and keeps what it had established.

| Row | Dependency | Injected failure | Expected outcome | Notes |
|---|---|---|---|---|
| **F1** | LLM | provider unreachable during `propose_links` (connection error, timeout, HTTP 5xx, HTTP 429) | **`NO_LLM`**: no further model calls; rule-derived cause ranking; a templated narrative over computed figures, citing real evidence, that passes the *same* grounding check; the run reaches `request_approval` recommending the same action a healthy run would | the ladder's `NO_LLM` rung. **Believed not implemented today** — the model nodes have no error handling |
| **F2** | LLM | provider unreachable during `narrate` (after links succeeded) | **`NO_LLM`** from that node: model-linked hypotheses kept, templated narrative, run reaches `request_approval` | |
| **F3** | LLM | daily spend ceiling reached (`SpendLimitExceededError`) | **`NO_LLM`**, as F1 | the ladder's "rate-limited or refusing"; the system keeps working without the model rather than stopping |
| **F4** | LLM | model returns unusable output (no parseable structure / empty narrative) | **`NO_LLM`**, as F1 / F2 | distinct from F5: an empty answer is a failure to answer, not a fabrication |
| **F5** | LLM | model cites evidence that does not exist, or states a figure nobody computed | **Escalate** at `check_grounding`; the narrative is never shown and no approval is requested | exists since B9 |
| **F6** | LLM (replay harness) | a cassette miss in replay mode (`CassetteMissError`) | **Raise** out of the workflow. Never degrade | the one row where crashing is correct: degrading here would let a benchmark silently measure the rules-only arm under the LLM arm's name. Same for a missing API key / unknown model — configuration errors fail loudly |
| **F7** | Documents + ERP | no contractual temperature envelope in the evidence | **Escalate** (`DETECT_ONLY`): no envelope assumed, no risk estimated, no recommendation | I6; exists since B9 |
| **F8** | Telemetry | sensor dropout — too few readings, or readings not covering the trend window | **Escalate** (`DETECT_ONLY`): no risk probability, no interpolated readings | exists since B3/B9 |
| **F9** | Compute | per-incident budget exhausted (steps, tokens, wall clock) | **Escalate** with the spent budget named; what was established survives | exists since B9 |
| **F10** | Legacy ERP | ERP unreachable, so no ERP evidence in the bundle, only the signed document | **`FULL` on the document**: the envelope comes from the document, no ERP value appears, the conflict list is empty rather than invented, the run reaches `request_approval` | absence represented as absence |
| **F11** | Facility data | no cold-storage facility data available | **Escalate**, stating facility data is unavailable. The run must **not** recommend `do_nothing` because the options it would have compared could not be priced | **Expectation stated before checking what the code does** |
| — | Vision provider | `NO_VLM` | **Not graded.** There is no vision model in the system until B13, so there is nothing to fail | registered so its absence is visible, not forgotten |

**Refinement of the ladder, stated here rather than discovered later.**
`overview.md` §7 triggers `DETECT_ONLY` on "multiple dependencies down". The
recommendation, though, is computed by the deterministic core and needs
neither the LLM nor the ERP when the signed document is present. So "LLM down
and ERP down" still yields a recommendation (F1 + F10), and `DETECT_ONLY` is
reached exactly when the core cannot compute one: no envelope (F7), no
fittable trajectory (F8), no priceable options (F11).

**Graded rows: 11 (F1-F11).** `ClaimSpec.required_cases` for C13 was 8, a
number derived from nothing written down; it becomes 11, the row count of this
matrix. Each row is graded pass/fail; F1 is run once per injected error kind
and passes only if every kind passes.
