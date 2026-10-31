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
