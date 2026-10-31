# AxonFDE decision-workbench redevelopment

Authorized October 31, 2026. Active isolated branch: improve/axon-decision-workbench, based on current remote main 2df65165699f65549168804ebac492dfdf04cb10. Prior release branches and frozen benchmark artifacts are preserved. Remote history was rewritten, but the fetched main tree differs from the previous local release only in CONTINUE.md/docs/DEMO.md. No reset or force push.

## Product and engineering acceptance

1. Real review checkpoints: orchestration must actually interrupt before approval-dependent continuation. Resume data alone is never authorization. Reuse the existing bound-approval and executor gates; rejection/stale/wrong-thread/replayed approval and restart cases must fail closed. Existing benchmark simulation stays explicitly separated and frozen results remain historical.
2. Trajectory CI: a versioned deterministic contract grades real graph traces as well as adversarial mutations. Catch skipped grounding/approval, reorderings, unknown/repeated nodes and continuation after escalation. A trace proves observed workflow order, not that an approver was authentic or an external effect occurred.
3. Decision workbench: preserve the original brand and story while giving users a concise incident inbox, cited evidence drilldown, comparison of bounded interventions and downloadable decision packets. Static demonstration, recorded backend output and browser-only what-if controls must be clearly distinguished. No fake live fleet, fabricated model responses or business gains.
4. Bounded context and evidence security: audit the existing digest/budget/grounding path before extending it. Integrate only the useful fifteen ideas, with an implementation/deferral ledger; no extra framework solely for its name.
5. Release evidence: baseline and changed unit/agent/security/integration checks; strict types/lint/architecture; hosted browser tests and screenshots; immutable existing historical evidence. Database fixtures only use fresh isolated instances or GitHub services. No personal files/volumes touched, no desktop control, no paid model/hosting calls.

## Fifteen-idea fit

Prioritize #2 durable workflow, #7 trajectory CI and #15 architecture teardown. Inspect #1 context assembly, #5 corrective retrieval, #8 trust-boundary sanitization and #11 budget controls against existing implementations. MCP requires an actual read-only consumer and protocol/auth validation; do not replace working APIs with a ceremonial endpoint. Defer voice, DPO, computer-use, arbitrary multi-agent swarms and a PII vault without real data-flow need. Shadow comparison must be recorded side-effect-free evaluation, never an invented live traffic claim.

## Frozen boundaries

Historical C11 and the 24-case verification release remain unchanged. Nonconstant lying sensors and honest constant-sensor abstention remain limitations. Graph execution markers are not proof of external execution; real ActionExecutor retains authorization, binding, idempotency and persistence checks.

Public changes still require the user's exact-release approval. Existing GitHub Pages is the intended destination; inspect current policy before proposing release. Do all local implementation and recovery preparation before requesting approval. No publication or deployment yet.

## Local implementation checkpoint

- Versioned trajectory contract now checks real graph runs and missing/reordered/unknown/repeated-node mutations. It distinguishes actual suspended review from incomplete execution.
- ReviewGate persists a proposal binding before a genuine LangGraph interrupt, rejects boolean resume grants, reloads current context/approval, refuses stale/expired responses and uses checkpoint-bound incident lookup. Human waiting is excluded from compute wall time without resetting steps/tokens/tool counts.
- DatabaseReviewRuntime uses the existing authenticated Principal, ApprovalService, ActionExecutor and audit chain. It commits an executor refusal before propagating it and relies on the approval-derived idempotency key. Three new integration cases are prepared for disposable CI: Postgres saver reconnection, effect-commit/adapter-restart replay and refusal-audit persistence. These have not run locally. The historical graph without ReviewGate remains explicitly advisory/cassette-only; it does not gain a fictional approval from a marker.
- The public decision workspace uses already published synthetic recordings and the frozen verifier pack. Cost rehearsal is explicitly hypothetical and checked against backend-generated parity fixtures. Notes and rehearsal review remain browser-only; they do not issue backend approvals or side effects.
- A non-finite cargo-value edge case found while building cross-language planning parity now rejects NaN/infinity before ranking.
- Current verified local results: 792 full unit/cassette-agent tests passed before the later snapshot/planning additions; 80 affected Python checks and 57 Node checks subsequently passed. Full updated regression, database integration, hosted browser/visual and deployed qualification remain pending. Strict backend typing and all eleven architecture contracts passed at the backend checkpoint.

## Operational limits retained

A source fingerprint is consistency evidence, not an authenticity signature. The graph's review flag is not an authenticated transport. Application wiring must supply a verified Principal and fresh trusted context loader. A known expired/stale checkpoint refuses automatic continuation; an uncertain previously committed effect must be reconciled through the execution ledger, not inferred absent because review refused. The new runtime idempotency integration test covers a committed effect retried with its still-valid approval. No real carrier integration, multi-tenant access model, field sensor truth, production SLO or business savings is claimed.

## Pre-merge qualification record

User approved publication, full CI with corrections confined to this release, and existing-Pages merge/deployment only after all gates pass. PR #2: https://github.com/dilipna/axon-fde/pull/2. Initial candidate dd6a8a52fe120f1d4cb4dda146aeaf16366b9fa2 passed 803 local unit/cassette-agent tests, 57 Node checks, strict types, lint and all eleven architecture contracts. Its disposable-database CI passed 125 integration/end-to-end cases, including saver reconnection, effect-commit/adapter-restart replay and refusal-audit persistence. Its runner browser suite passed all 18 cases with zero skips/retries; seven screenshots at desktop/tablet/mobile widths were inspected. These results belong to that exact source, not an invented field deployment. A release-documentation correction removes stale local-only wording; the corrected exact tree must pass full CI again before merge. Production identity and public browser checks remain separate gates. Historical recordings and metrics are unchanged.
