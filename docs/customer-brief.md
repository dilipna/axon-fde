# Customer brief — Axon Truck Services

*Discovery output. Written after stakeholder interviews, a ride-along, and two
weeks of shadowing the dispatch desk.*

> **Note on realism.** Axon Truck Services is a fictional company constructed to
> be a faithful composite of real cold-chain logistics operations. The systems,
> workflows and pain points described here are representative; the company,
> people, and data are invented. Nothing in this repository connects to a real
> carrier.

---

## 1. The company

Axon Truck Services is a mid-sized cold-chain carrier running roughly 120
refrigerated tractor-trailers across regional and long-haul lanes. Cargo splits
into four classes, each with a contractual temperature envelope:

| Cargo class | Envelope | Share of loads | Typical load value |
|---|---|---|---|
| Pharmaceutical | 2–8 °C | 18% | $120k–250k |
| Vaccine | 2–8 °C | 6% | $200k–800k |
| Fresh produce / dairy | 0–4 °C | 47% | $18k–60k |
| Frozen | ≤ −18 °C | 29% | $25k–90k |

Pharmaceutical and vaccine loads are a quarter of volume and the large majority
of financial and reputational exposure. They are also the loads subject to the
strictest documentation expectations: clients performing Good Distribution
Practice audits ask Axon to evidence not just that cargo stayed in range, but
what the company did, and why, when it nearly did not.

## 2. Stakeholders

| Role | Who they are | What they care about | What they fear |
|---|---|---|---|
| **Dispatcher** (primary user) | 9 staff across 3 shifts; 2–15 years tenure | Getting through the shift without a load going out of spec | Being blamed for a decision made with incomplete information |
| **Fleet Manager** | 2 staff | Asset utilisation, maintenance cost, on-time delivery | Unnecessary reroutes that blow the schedule and the budget |
| **Compliance Lead** | 1 staff | Audit readiness, client documentation requests | An auditor asking "why did you continue to destination?" and having no answer |
| **VP Operations** | Sponsor | Claims ratio, client retention, cost per load | Losing a pharma contract over a single excursion |
| **Drivers** | ~140 | Clear instructions, not being second-guessed | Being told to divert 90 minutes for no reason they can see |
| **IT** | 3 staff, heavily loaded | Not owning another system | An AI project that needs write access to the ERP |

**Two stakeholder positions shaped the architecture directly.** IT stated
plainly that no new system gets write access to the ERP — that constraint
produced the read-only, view-scoped integration. The Compliance Lead described
the audit problem unprompted, which is what elevated the audit trail from
engineering hygiene to a product feature.

## 3. The systems as they exist today

| System | Technology | Holds | Access today |
|---|---|---|---|
| **ERP** | SQL Server 2022 | Shipments, customers, vehicles, drivers, routes, facilities, cargo requirements | Read-only reporting logins; ad-hoc SQL by two power users |
| **Telemetry portal** | Vendor SaaS | Live temperature, GPS, door state, reefer status, fuel | Web UI only; CSV export |
| **Maintenance system** | Separate vendor | Service history, fault codes, technician notes | Web UI; nightly CSV drop |
| **Document store** | Shared network drive | Bills of Lading, manifests, inspection forms, SOPs, equipment manuals | Folder browsing and filename search |
| **Weather** | Public API | Forecast and observations | Consulted manually, inconsistently |

Five systems, five logins, no shared identifier discipline, and no system that
holds a view of a shipment's *situation* rather than its *record*.

## 4. The workflow as it exists today

What a dispatcher actually does when a temperature alarm fires:

```
  Alarm fires (threshold already breached)
        ↓
  Open telemetry portal, find the truck, read the last hour
        ↓
  Open ERP, look up the shipment, the cargo, the customer
        ↓
  Walk to the shared drive, find the BOL, check the permitted range
        ↓
  Open the maintenance system, check whether this reefer has history
        ↓
  Check the weather along the remaining route
        ↓
  Find the right SOP (often by asking whoever is nearby)
        ↓
  Decide: continue / call the driver / divert
        ↓
  Execute by phone
        ↓
  Hope
```

Observed elapsed time from alarm to decision: **11 to 24 minutes**, with the
long tail driven by document search and by waiting for a second opinion. During
that window, the cargo continues warming.

Nothing records what was decided or why. Nothing checks afterwards whether it
worked.

## 5. What they asked for

> "We want an AI assistant that lets dispatchers ask questions about our fleet."

Read literally, this is a request to make step 2 through step 7 above faster by
putting a chat box in front of the same five systems.

That would be a real improvement, and it would not touch the actual problem.

## 6. What is actually wrong

### 6.1 Detection is lagging by construction

Every alarm in the current stack fires on a threshold crossing. For a thermal
system, the threshold crossing **is** the failure, not a warning of one. By the
time the dashboard turns red, the cargo is out of spec and the remaining
decisions are damage-control decisions.

The dashboards are not broken. They are correctly reporting a lagging
indicator, and no amount of conversational interface changes that.

Observed during the ride-along: a pharma load reading 5.2, 5.6, 6.0, 6.4,
6.9 °C over 50 minutes against an 8 °C ceiling. Every reading was in spec. The
dashboard was green the entire time. The trend was unmistakable and nothing in
the stack was watching it.

### 6.2 Evidence assembly is the bottleneck, and it is manual

The dispatcher's job is not retrieval. It is judgment under time pressure with
incomplete and sometimes contradictory evidence. The 11–24 minutes is spent
almost entirely on assembly, not on thinking.

### 6.3 The systems disagree, and nobody owns reconciliation

Found during discovery, on a live pharma shipment:

- The ERP recorded a permitted range of **2–10 °C**.
- The signed Bill of Lading specified **2–8 °C**.

Whichever document the dispatcher happened to open would have determined the
decision. No process notices the disagreement. The BOL is the legally operative
document, so the ERP value is not merely different — relying on it is a
compliance failure waiting to be discovered by an auditor rather than by Axon.

This is not an edge case: it is a predictable consequence of two systems
maintained by different teams with no reconciliation control between them.

### 6.4 Judgment is concentrated in a few people

One senior dispatcher knows that fault code AL17 on a particular reefer model,
combined with a rising delta-T, usually means compressor degradation rather
than a sensor fault. That knowledge is not written down, not transferable, and
not available at 03:00. Outcome quality therefore varies by who is on shift —
which is measurable, and which nobody measures.

### 6.5 There is no outcome attribution

After a reroute, nobody records whether the reroute worked. Consequently:

- The organisation cannot learn which interventions are effective.
- The intervention playbook stays folkloric rather than empirical.
- The Compliance Lead cannot answer an audit question about a specific decision.

### 6.6 Alarm fatigue is already present

Dispatchers described muting categories of alert. Any system that adds alerts
without a precision budget makes this worse and will be switched off inside a
week, whatever its offline accuracy.

**This is a hard requirement, not an aspiration:** predictive alerts must be
quoted and tuned against a false-alarm rate, and the operating threshold must
be chosen by expected cost rather than by a default 0.5.

## 7. Opportunities the customer did not ask for

| # | Opportunity | Rationale |
|---|---|---|
| **O1** | **Sell lead time, not answers.** Reframe the product around minutes bought before a breach. Minutes are the only thing that converts a loss into a save, and lead time is directly measurable. |
| **O2** | **The decision record is a compliance product.** An immutable, evidence-linked, tamper-evident record of what was known, what was decided, by whom, and what happened next is something Axon can show to pharma clients and auditors. It may be worth more than the spoilage savings and costs almost nothing extra to build alongside the core loop. |
| **O3** | **Decision economics, not risk scores.** The dominant cost is not only spoiled cargo — it is *unnecessary* interventions on loads that would have been fine. Comparing options by expected value, always including "do nothing", captures both sides and is what justifies probability calibration over raw accuracy. |
| **O4** | **Contradiction detection as a standing control.** Once evidence is typed and normalised, ERP-vs-BOL-vs-sensor disagreement becomes detectable across the whole fleet continuously, not only during incidents. That is a data-quality capability Axon did not know was available to them. |
| **O5** | **An empirical intervention playbook.** With outcomes recorded, intervention effectiveness becomes rankable over time instead of anecdotal. |

## 8. Requirements

### 8.1 Functional

| ID | Requirement |
|---|---|
| F1 | Detect developing excursions before a threshold is crossed, and quantify the lead time gained |
| F2 | Assemble the evidence for an incident automatically from all five systems |
| F3 | Detect and surface contradictions between sources rather than silently choosing a value |
| F4 | Produce root-cause hypotheses with explicit supporting and contradicting evidence |
| F5 | Compare candidate interventions, including doing nothing, on expected cost |
| F6 | Require human approval for every consequential action |
| F7 | Verify after the fact whether the intervention worked, and reopen if it did not |
| F8 | Record an immutable, auditable trail of the whole decision |
| F9 | Answer ad-hoc operational questions in natural language (the original request, scoped to a sidebar) |

### 8.2 Non-functional

| ID | Requirement | Target |
|---|---|---|
| N1 | AI access to the ERP is read-only and provably so | Zero write capability, enforced at three independent layers |
| N2 | Predictive alerts respect a precision budget | Operating threshold chosen by expected cost; false-alarm rate always reported |
| N3 | Incident investigation completes fast enough to be useful | p95 well inside the current 11-minute floor |
| N4 | The system degrades safely when a dependency fails | Never fabricates a missing observation; escalates to a human |
| N5 | Every consequential action is authorised outside the language model | Deterministic policy engine |
| N6 | The audit trail is tamper-evident | Hash-chained, append-only, no role may rewrite it |
| N7 | Operating cost per incident is known and bounded | Tracked per invocation; hard daily spend ceiling |
| N8 | No claim about the system is made without a reproducible measurement | Enforced by the claim register |

### 8.3 Explicit non-goals

- Replacing the dispatcher. The system recommends; a human decides.
- Autonomous action. Nothing consequential executes without approval.
- Replacing the ERP, the telemetry portal, or the maintenance system.
- Write access to any existing system of record.

## 9. Success metrics

Measured against the legacy workflow on identical scenarios:

| Metric | Today (observed) | Target |
|---|---|---|
| Time from signal to decision | 11–24 min | Substantially reduced; measured, not asserted |
| Warning before threshold breach | 0 min (by construction) | Positive median lead time at an acceptable false-alarm rate |
| Cross-source contradictions detected | 0% (no process exists) | Measured precision and recall |
| Decisions with a recorded rationale | ~0% | 100% |
| Interventions with a verified outcome | 0% | 100% |
| Unauthorised actions by the AI | n/a | Exactly zero |

Targets are deliberately stated as *directions with measurement methods* rather
than as numbers. The numbers go in [`claims.md`](evaluation/claims.md) only once
a benchmark run has produced them.

## 10. Operating model change

The engagement is not "add AI to dispatch." It is a change in how the operation
runs:

```
  TODAY                          AXONFDE
  ─────                          ───────
  detect (too late)              understand
  manually investigate           predict
  search for documents           investigate
  decide alone                   decide between costed options
  execute                        approve
  hope                           act
                                 verify
                                 learn
```

The measurable difference is where the loop closes. Today it ends at "execute."
The proposed model ends at "learn," and every step in between leaves a record.
