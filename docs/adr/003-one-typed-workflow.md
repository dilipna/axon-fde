# ADR-003 — One typed workflow, not a multi-agent swarm

- **State:** Accepted
- **Date:** 2026-09-18
- **Deciders:** Principal FDE

## Context

The obvious framing for this problem is a team of agents: a Telemetry Agent, a
Document Agent, a Diagnosis Agent, a Decision Agent, a Safety Agent, each with
its own prompt, conferring to reach a conclusion. It demos well and it maps
neatly onto how the work is described in prose.

It is also, for this problem, the wrong shape.

The investigation is not open-ended exploration. It is a **known procedure**
with a handful of genuinely uncertain steps. For a temperature excursion, the
evidence a dispatcher needs is not a matter of creative discovery — it is a
checklist, and it is the same checklist every time. Where real judgement is
required is narrow: which root causes fit this evidence, which interventions are
worth considering, and how to explain the conclusion.

Handing a fixed procedure to autonomous agents converts a deterministic,
measurable, auditable process into a nondeterministic one, and pays tokens and
latency for the privilege.

There is also an evaluation argument. Required-tool recall, unnecessary-tool
rate and step count are only meaningful if the expected trajectory is
well-defined. A swarm's trajectory varies run to run, which makes trajectory
metrics noisy and makes regression gating nearly impossible.

## Decision

**One LangGraph workflow with typed state, fourteen nodes, and an explicit
transition table.** A second, separate graph handles outcome verification.

Of the fourteen nodes:

| Nodes | Nature |
|---|---|
| 10 | Fully deterministic (triage, evidence planning, gathering, reconciliation, risk, simulation, grounding, policy, approval, execution) |
| 2 | Hybrid — rules produce candidates with priors; the LLM may add to them and propose evidence links (`form_hypotheses`, `generate_actions`) |
| 1 | LLM — narrative composition (`compose_recommendation`) |
| 1 | Optional LLM — evidence-plan gap filling |

Evidence gathering fans out in parallel, but as a deterministic fan-out over a
checklist, not as agents deciding what to look at.

LangGraph is chosen for a specific reason: **durable checkpointing and
interrupt/resume**. Human approval means an investigation must suspend, survive
a process restart, and resume days later against the same typed state. That is
the requirement, and it is not "we want agents."

## Alternatives considered

### A. Multi-agent swarm with a supervisor

Rejected as above: nondeterminism where determinism is available, higher cost
and latency, unmeasurable trajectories, and a much larger prompt-injection
surface (every inter-agent message is an untrusted channel).

### B. A plain Python function calling tools in sequence

Genuinely tempting, and correct for the deterministic 10 nodes.

Rejected because of approval. Suspending mid-procedure, persisting typed state,
and resuming after a human decision is exactly what a checkpointed graph
provides and what hand-rolled code would reimplement badly. The graph earns its
place on durability, not on intelligence.

### C. A single large prompt with all tools available

Rejected. It makes the trajectory entirely emergent, offers no place to insert
deterministic policy or grounding checks between steps, and provides no
mechanism to enforce a step budget before cost is incurred.

## Consequences

### Accepted costs

- **Adding a new incident type requires touching the graph**, not just writing
  a prompt. That is slower, and it is also why behaviour stays predictable.
- **The framework is a dependency** with its own upgrade path. Pinned to a
  major version; the node functions themselves are plain typed Python and would
  survive a framework swap.
- **Less impressive at first glance** than "multi-agent." Mitigated by the fact
  that the ablation (C5) measures what the LLM actually contributes, which is a
  more interesting claim than an architecture diagram.

### Gained

- Every investigation follows the same auditable path.
- Required-tool recall and step count are meaningful metrics.
- Deterministic guards (budgets, grounding, policy) sit *between* nodes, where
  they can actually stop something.
- Token cost is bounded and predictable, which matters on a real budget.

## The honest test

If the `rules_only` ablation (claim C5) shows the LLM contributes little over
the deterministic path, that is a finding about this architecture and it gets
published. The design is deliberately arranged so the question is answerable —
which a swarm architecture would not be.

## Revisit if

- Incident types proliferate to the point where the graph becomes a
  hard-to-maintain switch statement over incident kinds.
- A measured comparison shows exploratory tool selection beating the fixed
  evidence checklist on required-evidence recall.
