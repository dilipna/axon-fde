# AxonFDE

**Proactive decision intelligence for cold-chain logistics.**

AxonFDE turns fragmented enterprise data into proactive operational decisions. It continuously
combines live telemetry, legacy enterprise records, multimodal evidence and operational knowledge
to predict incidents *before* a threshold is breached, investigate root causes, compare intervention
options, recommend an evidence-backed action, execute it only under human approval, and then verify
whether the intervention actually worked.

> **Status: Phase 0 (scaffolding).** The system is under active construction. No performance claim
> appears in this README until it is produced by a stored benchmark run. Anything not yet measured
> is explicitly marked `PLACEHOLDER`.

---

## The problem

Axon Truck Services moves temperature-sensitive cargo — pharmaceuticals, vaccines, fresh and frozen
goods. Their dispatchers work across an ERP on SQL Server, a separate telemetry portal, a shared
drive of signed PDFs, and a maintenance system. The stated request was "an AI assistant that can
answer questions about our fleet." That is not the real problem.

The real problems are structural:

| Problem | Why it matters |
|---|---|
| **Detection is lagging by construction.** Every alarm fires on a threshold crossing. For a thermal system, that crossing *is* the failure — not a warning of one. | By the time the dashboard turns red, the only decisions left are damage-control decisions. |
| **Evidence assembly is manual.** Six systems, six logins, 10–20 minutes per decision. | The situation degrades while the dispatcher is still gathering context. |
| **The systems disagree and nobody owns reconciliation.** The ERP says this cargo tolerates 2–10 °C; the signed Bill of Lading says 2–8 °C. | Whichever document the dispatcher happens to open determines the decision. This is a compliance exposure dressed as a data-quality issue. |
| **Judgment is concentrated in a few people.** | Outcome quality varies by who is on shift, and nobody measures it. |
| **No outcome attribution.** Nobody records whether a reroute worked. | The organisation cannot learn, and cannot answer a pharma client's audit question. |

**Product thesis:** *AxonFDE buys the dispatcher time, assembles the evidence, shows its work, and
proves whether the decision worked.*

---

## What makes this different from a RAG chatbot

The LLM appears in exactly three places — proposing hypotheses, proposing actions, and composing the
narrative. It **cannot**:

- author an observation (evidence may only originate from SQL, telemetry, documents, images, external APIs or the risk model),
- emit a confidence score (a deterministic, versioned scorer owns every number),
- compute a risk probability (a separate ML service does, and always reports its baseline alongside),
- decide a policy outcome (a deterministic policy engine does, outside the model),
- cause a side effect (model output is *data*; execution requires a hash-bound human approval).

That constraint is the architecture, not a disclaimer.

### Design commitments

- **Typed, provenance-carrying evidence.** Every observation normalises into one schema over a
  canonical taxonomy, carrying source, timestamp, validity window, computed confidence, freshness,
  and a link back to the original artifact.
- **Deterministic contradiction detection.** Cross-source disagreement is found by comparing typed
  values against declared per-type tolerances — not by asking a model whether two things disagree.
- **Expected-value decisions.** Interventions are compared on expected cost including `do_nothing`,
  with uncertainty bands and a stated decision-flip point.
- **Hash-bound approvals.** An approval is bound to the exact evidence it was granted against. If
  the world moves before execution, the approval goes stale and must be re-sought.
- **Append-only, hash-chained audit.** Tamper-evident by construction; no role can rewrite it.
- **Evaluation is part of the product.** AxonBench ships alongside the system, not after it.

---

## Architecture

See [`docs/architecture/`](docs/architecture/) for the full design and
[`docs/adr/`](docs/adr/) for the decision records.

```
Next.js control tower
        │
FastAPI (authn/authz) ── PolicyEngine (deterministic, outside the LLM)
        │
LangGraph investigation workflow  ──►  verification workflow (scheduled)
        │
Tool layer (typed, policy-gated, budgeted)
        │
   ┌────┴─────────────────────────────────────────┐
SQL Server (legacy, read-only vw_ai_* views)   PostgreSQL + pgvector
                                               (incidents, evidence, audit)
        │
IncidentForge simulator ──► AxonBench evaluation harness
```

| Layer | Choice | Why |
|---|---|---|
| Legacy system | SQL Server 2022 | A genuinely different engine is what makes dialect handling, ODBC integration, view-scoped least-privilege reads and AST validation real rather than decorative. |
| Application DB | PostgreSQL 16 + pgvector | One store for relational data, JSONB, full-text, vectors and agent checkpoints. |
| Agent | LangGraph | Chosen for durable checkpointing and interrupt/resume — which is exactly what human approval needs. Not for "multi-agent". |
| Risk | LightGBM + isotonic calibration | Safety-critical probability stays out of the LLM. Always reported against a non-ML baseline. |
| LLM | Anthropic (`claude-opus-5`) | Structured outputs enforce typed extraction; prompt caching and the Batch API keep cost per incident bounded. |
| SQL safety | SQLGlot | Real AST allowlisting. Regex-based SQL guards are security theatre. |

---

## Quick start

**Prerequisites:** Docker Desktop (running), [uv](https://docs.astral.sh/uv/), Node 20+.

```bash
git clone <repo> && cd axonfde
cp .env.example .env          # defaults work for local development
uv sync                       # creates .venv with a pinned Python 3.12
uv run poe up                 # postgres + mssql + minio
uv run poe seed               # seed the legacy ERP and the app database
uv run poe demo               # end-to-end incident, offline, no API key required
```

`poe demo` runs the full loop from cassettes, so it costs nothing and is deterministic.

Common tasks (`uv run poe --help` for the full list):

| Task | What it does |
|---|---|
| `poe up` / `poe down` | Start / stop core services |
| `poe check` | Everything CI runs: lint, typecheck, module contracts, tests |
| `poe test` | All tests except those that call the real LLM API |
| `poe demo` | The end-to-end incident walkthrough |
| `poe bench` | Run AxonBench |
| `poe forge` | IncidentForge scenario CLI |

---

## Evaluation

Every claim this project makes is registered in [`docs/evaluation/claims.md`](docs/evaluation/claims.md)
with the benchmark, baseline, metric and methodology required to support it. The rule is absolute:

> **No number appears in this README, the UI, or any write-up unless a stored benchmark run produced it.**

| Claim | Metric | Baseline | Result |
|---|---|---|---|
| Detects excursions before threshold alarms | median lead time, reported with false-alarm rate | threshold detector on the identical stream | `PLACEHOLDER` |
| Calibrated excursion probability | Brier, ECE, reliability diagram | slope extrapolation, logistic regression | `PLACEHOLDER` |
| Accurate root-cause identification | top-1 / top-3 accuracy | rules-only arm | `PLACEHOLDER` |
| Multimodal evidence improves outcomes | Δ accuracy across modality arms | telemetry + SOP arm | `PLACEHOLDER` |
| The LLM adds value over rules alone | Δ accuracy, Δ action selection | rules-only arm | `PLACEHOLDER` |
| AI cannot execute unauthorised actions | unauthorised-action rate (target: 0) | — | `PLACEHOLDER` |
| Generated SQL cannot mutate the legacy system | prohibited-operation rate (target: 0) | — | `PLACEHOLDER` |

Two of those ablations may come back negative. If multimodality or the LLM does not move a metric,
that result gets published as-is. A measured negative is worth more than an unfalsifiable positive.

### Known limitations

- The risk model is trained and evaluated on **synthetic data** from a documented lumped-capacitance
  thermal model. Real-world generalisation is unvalidated, and no claim here asserts otherwise.
- The what-if simulator shares physics with the ground-truth generator, so intervention predictions
  are *consistent by construction*. The benchmark measures **decision quality given a model**, not
  physical forecast accuracy.
- All actions are simulated. Nothing in this system dispatches a real truck.

---

## Repository layout

| Path | Contents |
|---|---|
| `backend/app/` | FastAPI application (modular monolith) |
| `simulator/incidentforge/` | Seeded fleet simulator, thermal physics, fault injection |
| `benchmarks/axonbench/` | Evaluation harness, graders, scenario packs |
| `benchmarks/axonred/` | Adversarial / prompt-injection attack pack |
| `ml/` | Risk model training, calibration, evaluation |
| `apps/web/` | Next.js operations control tower |
| `data/` | Seed data, observation taxonomy, knowledge corpus, scenarios |
| `docs/` | Architecture, ADRs, threat model, evaluation, case study |
| `infra/terraform/` | Ephemeral AWS demo environment |

---

## License

`PLACEHOLDER` — to be chosen before the repository is made public.
