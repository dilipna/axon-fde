# Fifteen ideas — AxonFDE redevelopment decisions

October 31, 2026. Feature names are not accomplishment claims. Public release and DB/browser qualification remain pending.

| Idea | Current decision and actual scope |
|---|---|
| 1 Context assembler | Existing deterministic citable digest retained. No new tokenizer or arbitrary context middleware claimed. |
| 2 Durable workflow | New genuine review interrupt/resume, persisted proposal binding, fresh checks and database executor adapter. In-memory reconstruction tested; Postgres reconnection and executor restart tests pending CI. |
| 3 MCP | Deferred: no external scoped consumer requires a new protocol surface in this release. Existing typed tools remain. |
| 4 Triage router | Deferred: two model nodes and offline cassettes do not establish request traffic or routing savings. |
| 5 Corrective RAG | Public recorded-evidence lookup abstains and cites saved steps. This is not a new model-driven hybrid/self-correcting RAG engine. Existing backend grounding is retained. |
| 6 Memory | Browser notes are private UX state tied to evidence version, not a three-tier agent memory engine. |
| 7 Trajectory CI | New versioned order contract, actual workflow-run checks and adversarial gate mutations. Order validity is separate from authorization and effect evidence. |
| 8 Sanitization | Strict same-origin CSP, literal untrusted-text rendering, exact resume schema and scope boundaries. No multi-agent proxy claim. |
| 9 Voice | Deferred: no voice operator task established. |
| 10 DPO | Deferred: no reviewed preference corpus or justified tuning budget. |
| 11 Cost controls | Existing budgets and degradation retained; review waiting is accounted separately from compute. No new vendor fallback chain or measured cost reduction. |
| 12 Shadow routing | Planning parity fixtures compare browser arithmetic with backend arithmetic; they are not shadow model traffic. Existing benchmark arms and negative findings remain unchanged. |
| 13 Computer use | Deferred: no authorized legacy portal gap. No desktop control used for this redevelopment. |
| 14 PII vault | Deferred: all public logistics data is fictional. A token vault without a concrete sensitive-data flow would add unjustified complexity. |
| 15 Architecture teardown | Acceptance, defect/fix boundaries, reproduction and source-bound public proof included. Current measurements must be attributed to current tests; historical runs retain dates and limits. |

Primary SDK reference used for interruption semantics: [LangGraph interrupt reference](https://reference.langchain.com/python/langgraph/types/interrupt). Interrupts require checkpoint persistence and resume the interrupted node; side effects must not happen before the interrupt. Actual DB effects remain in the existing idempotent executor transaction.
