# FDE case-study presentation

## Editorial basis

The owner requested a recruiter-facing, end-to-end FDE story and confirmed a real engagement context named AXON TRUCK SERVICES in Hyderabad, India. The owner instructed us to infer the technical narrative from the existing website. The repository's original customer brief explicitly invents its interviews, ride-along, fleet/headcount figures and observations. These are not evidence of real customer events and must not be republished as such. The named company/location are owner-provided context, not independently verified corporate records. No customer quote, meeting, fleet size, measured customer improvement, commercial relationship date, successful pilot or production deployment is added.

The visible technical problem and solution are reconstructed from the existing implementation and synthetic scenario. The opening states my engineering scope, then provides three cards: problem, solution and proof. Detailed pages share the case-study framing. Public records remain visibly synthetic; actual customer systems and data are not connected to this public website.

## Claim-to-source map

| Visible claim | Source / scope |
| --- | --- |
| Legacy SQL Server integration with read-only views / SQL AST guard | backend/app/db/legacy/sqlguard.py; existing architecture docs; disposable-database CI |
| Source-backed requirements and conflicting limits | existing workbench data, source manifest and recorded compressor chapter 1 |
| Durable human review and revalidation | backend/app/agents/review.py and review_runtime.py; docs/decision-workspace.md |
| Outcome verification with known limitations | docs/verification-release-protocol.md and apps/control_tower/verification-release.json |
| 35-minute earlier warning | Single synthetic compressor case, minute 102 vs 137; not customer/fleet performance |
| 24-case pack; false confirmations 6→2, missed recoveries 0→4 | Before/after fields in original frozen verification artifact; nonconstant lies remain unresolved |
| Compressor incident unresolved | Original synthetic trajectory, failed verification and reopened incident; no simulated reroute success is invented |
| Public deployment | Existing GitHub Pages; source identity and production browser/HTTP release records |

## Acceptance

Preserve midnight/cobalt/lime theme, geometric fonts, pausable illustration, notes, exports, backend decisions and original evidence. Three opening cards must appear early in the first viewport; use a readable responsive layout. Keep legacy #problem routing to engineering by naming the new anchor #brief-problem. Native details expose integration, reconciliation, human control and verification choices. Proof links must lead to actual original evidence and source docs. Existing browser suite gains one case-study journey (33 total), while responsive checks assert opening context/cards and balanced evidence language. Real browser/screenshot qualification runs only on GitHub after exact-source approval. Do not claim it passed based on local static tests.

This iteration is local until its exact public-release approval. No desktop/local-browser control, paid services, hosting-policy changes, actual customer-data integration or unrelated project edits.
