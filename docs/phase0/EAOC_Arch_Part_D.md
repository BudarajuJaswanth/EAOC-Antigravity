# EAOC — Detailed System Architecture Specification
# Part 0.3-D: Sections 16–18 + ADRs + Risks + Open Decisions + Checklist
# Phase 0 · Part 0.3 · Version 1.0

---

# SECTION 16 — TRACEABILITY

## 16.1 Architecture Components to Part 0.1 Requirements

| Architecture Component | Part 0.1 Requirements |
|---|---|
| API Layer (FastAPI) | FR-REQ-001, FR-REQ-002, FR-REQ-003, FR-REQ-004, FR-ERR-001, FR-ERR-002, FR-RESP-002 |
| Auth Module | FR-AUTH-001, FR-AUTH-002, FR-AUTH-003, NFR-SEC-001, NFR-SEC-002 |
| RBAC Module | FR-RBAC-001, FR-RBAC-002, FR-RBAC-003, NFR-SEC-004 |
| Orchestrator (LangGraph) | FR-ORCH-001, FR-ORCH-002, FR-ORCH-003, FR-AGT-ORCH-001 |
| Planner | FR-PLAN-001, FR-PLAN-002, FR-PLAN-003, FR-AGT-PLAN-001, FR-ADAP-001 |
| LangGraph State Layer | FR-STATE-001, FR-STATE-002, NFR-REL-002 |
| Logical Agent Nodes | FR-AGT-* (all 12), FR-RBAC-003, FR-PERM-001 |
| Tool Layer + Interceptor | FR-PERM-001, FR-PERM-002, FR-TOOL-001, FR-TOOL-002 |
| Data Access Layer | FR-DATA-001, FR-DATA-002, NFR-SEC-004 |
| RAG Layer + pgvector | FR-RAG-001, FR-RAG-002, FR-AGT-RAG-001 |
| SQL/DB Layer | FR-SQL-001, FR-SQL-002, FR-AGT-SQL-001, FR-DATA-001 |
| Document Storage | FR-DOC-001, FR-DOC-002, FR-AGT-DOC-001 |
| Reviewer/Validation | FR-REV-001, FR-REV-002, FR-REV-003, FR-AGT-REV-001 |
| Security/Policy Layer | FR-AGT-SEC-001, FR-PERM-001, FR-PERM-002, NFR-FAULT-001 |
| Risk Engine | FR-RISK-001, FR-RISK-002, NFR-FAULT-001 |
| Human Approval Queue | FR-HITL-001, FR-HITL-002, FR-HITL-003 |
| Audit Logging | FR-AUDIT-001, FR-AUDIT-002, NFR-AUDIT-001, NFR-AUDIT-002 |
| Provenance Store | FR-PROV-001, FR-PROV-002, NFR-TRACE-001, NFR-TRACE-002 |
| Observability (logs, metrics, health) | FR-OBS-001, FR-OBS-002, FR-OBS-003, NFR-OBS-001 |
| State Management (Redis + PG) | FR-STATE-001, FR-STATE-002, NFR-REL-001, NFR-REL-002 |
| Failure/Recovery Subsystem | FR-FAIL-001, FR-FAIL-002, FR-RETR-001, FR-RETR-002, NFR-REL-001 |
| Deployment Topology (Docker) | NFR-SCALE-001, NFR-AVAIL-001, NFR-AVAIL-002, NFR-MAINT-001 |
| Adaptive Orchestration | FR-ADAP-001, FR-ADAP-002 |
| Final Response Assembly | FR-RESP-001, FR-RESP-002 |
| API Error Handling | FR-ERR-001, FR-ERR-002 |

## 16.2 Architecture Components to Part 0.2 Agent Specifications

| Architecture Component | Part 0.2 Agent Spec |
|---|---|
| Orchestrator LangGraph node | AGT-ORCH (all fields) |
| Planner LangGraph node | AGT-PLAN (all fields) |
| RAG Agent node + pgvector SearchService | AGT-RAG (all fields) |
| SQL Agent node + PostgreSQL R/O tool | AGT-SQL (all fields) |
| Document Agent node + parsers | AGT-DOC (all fields) |
| Analytics Agent node + Computation Library | AGT-ANA (all fields) |
| Finance Agent node + SQL delegation | AGT-FIN (all fields) |
| HR Agent node + SQL + RAG delegation | AGT-HR (all fields) |
| IT Agent node + SQL + RAG delegation | AGT-IT (all fields) |
| Developer Agent node + Sandbox tool | AGT-DEV (all fields) |
| Reviewer node + Policy Rule Engine | AGT-REV (all fields) |
| Security/Policy node + ManifestStore | AGT-SEC (all fields) |
| Agent responsibility matrix | Part 0.2 Section 1 |
| Agent-to-tool permission matrix | Part 0.2 Section 2 |
| Agent-to-data access matrix | Part 0.2 Section 3 |
| Agent interaction matrix | Part 0.2 Section 4 |
| Risk/approval matrix | Part 0.2 Section 5 |
| Failure/recovery matrix | Part 0.2 Section 6 |
| Provenance chain schema | Part 0.2 Section 7 |

---

# SECTION 17 — ARCHITECTURAL DECISION RECORDS (ADRs)

## ADR-001: Logical Agents as LangGraph Nodes (Not Microservices)

| Field | Detail |
|---|---|
| Status | DECIDED |
| Decision | All 12 logical agents are implemented as LangGraph nodes within a single Python worker process. |
| Context | Part 0.2 AMB-007 identified three options: in-process LangGraph nodes, separate FastAPI sub-services, or hybrid. |
| Rationale | (1) Simplifies Phase 1 implementation significantly. (2) Avoids network overhead and distributed state complexity. (3) Logical agent isolation via permission manifests is architecturally sufficient for the research evaluation phase. (4) Migrating to sub-services is a Phase 4+ evolution if required. |
| Consequences | Agent isolation is logical (permission-enforced) not physical (process-boundary). A bug in one agent node could theoretically affect others. Mitigated by: strict permission manifest enforcement, input/output schema validation, and the Security/Policy Agent running as a cross-cutting enforcement layer. |
| Review Trigger | If agent isolation requirements become security-critical in Phase 5, revisit this decision. |

## ADR-002: LangGraph for Orchestration

| Field | Detail |
|---|---|
| Status | DECIDED (Master Specification) |
| Decision | LangGraph is used as the orchestration framework for the Orchestrator's workflow state machine. |
| Context | Master Specification v1.0 mandates LangGraph. |
| Rationale | LangGraph provides: built-in state management with checkpointing, conditional edge routing, cycle support (for retry loops), and compatibility with LangChain components. |
| Consequences | Orchestrator workflow logic is expressed as a LangGraph StatefulGraph. State is persisted via LangGraph's checkpoint mechanism (PostgreSQL backend). |
| Constraints | LangGraph version pinned in requirements.txt. Any LangGraph upgrade must be tested against the full workflow execution path. |

## ADR-003: Shared Local LLM Runtime

| Field | Detail |
|---|---|
| Status | DECIDED |
| Decision | All agents share one local LLM runtime instance (Ollama or LM Studio). No agent gets a dedicated model or API key. |
| Context | Master Specification v1.0: "Do NOT assume each agent needs a separate model or API key." |
| Rationale | (1) Reduces hardware requirements for local development and evaluation. (2) Model-agnostic architecture means the underlying model can be swapped without changing agent logic. (3) Agent differentiation is achieved via system prompts, tool access, and output schemas — not separate models. |
| Consequences | LLM is a shared resource; concurrent agent invocations queue at the LLM runtime. Latency for complex multi-agent tasks will be higher than if parallel LLM calls were possible. Acceptable for research evaluation; can be revisited for production scale. |
| Open Sub-Decision | Which local model to use is OPEN (AMB-005, see ARCH-OPEN-002). |

## ADR-004: PostgreSQL + pgvector (Single Database)

| Field | Detail |
|---|---|
| Status | DECIDED (Master Specification) |
| Decision | PostgreSQL with the pgvector extension is used as the primary database for both relational data and vector storage. |
| Context | Master Specification v1.0 mandates PostgreSQL and pgvector initially. |
| Rationale | (1) Reduces operational complexity (one database to manage). (2) pgvector is mature and sufficient for the synthetic corpus size (5K-20K chunks). (3) Single DB means vector search and relational queries can be combined in one transaction if needed. |
| Consequences | pgvector performance degrades at very large scale (millions of chunks). Architecture provides a migration path to Qdrant (ADR-005) but the trigger is not yet defined. |

## ADR-005: pgvector to Qdrant Migration Path (Open Trigger)

| Field | Detail |
|---|---|
| Status | OPEN — ARCH-DECISION-PENDING-001 |
| Decision | Migration path documented but trigger condition NOT decided. |
| Context | Master Specification says "pgvector initially, with the architecture allowing Qdrant if required." AMB-004 from Part 0.1. |
| What is decided | The SearchService interface is backend-agnostic (dependency-injected). The migration procedure is documented in Section 6.6. |
| What is NOT decided | The specific trigger condition (corpus size threshold, performance SLA breach, feature requirement). |
| Action Required | Decide trigger condition before Phase 7 evaluation design is finalized. |

## ADR-006: Redis as State Cache and Task Queue

| Field | Detail |
|---|---|
| Status | DECIDED (Master Specification) |
| Decision | Redis serves as: (1) fast task state cache, (2) task queue (Redis List), (3) token revocation store, (4) circuit breaker state. |
| Context | Master Specification v1.0 mandates Redis. |
| Rationale | Redis provides sub-millisecond read/write for task state polling (NFR-PERF-001). The task queue (Redis List) enables decoupling of API layer from worker process. |
| Consequences | Redis is a single point of failure for the task queue. Mitigated by: PostgreSQL as durable state backup; architecture supports Redis Sentinel for HA in production. |

## ADR-007: Deterministic Risk Classification (Not LLM-Based)

| Field | Detail |
|---|---|
| Status | DECIDED |
| Decision | Risk classification is implemented as a deterministic rule engine loaded from policy configuration. LLM commentary is advisory only and never overrides the rule engine. |
| Context | Part 0.2 specified: "Do not let an LLM be the sole authority for authorization." AMB-002 from Part 0.1. |
| Rationale | (1) Deterministic rules are auditable, reproducible, and testable. (2) LLM outputs are non-deterministic and cannot be the authoritative gate for security-relevant decisions. (3) Research validity: the evaluation must be based on predictable, rule-governed behavior, not LLM whims. |
| Consequences | Risk rules must be explicitly maintained in policy configuration files. New operation types require explicit rule additions (not automatic LLM generalization). |

## ADR-008: Human Approval via API Queue (No Notification Channel Yet)

| Field | Detail |
|---|---|
| Status | PARTIALLY DECIDED — ARCH-OPEN-001 |
| Decision | Human approval uses a PostgreSQL-backed approval_requests table with an API polling endpoint. Notification channel is NOT decided. |
| Context | AMB-001 from Part 0.1: "notification mechanism not specified." |
| What is decided | Database schema, API endpoints (GET /approvals, POST /approvals/{id}/decision), timeout handling, audit events. |
| What is NOT decided | How approvers are notified of pending requests (polling vs. webhook vs. email vs. Slack). |
| Implication | For Phase 4 testing, approvers will poll the API endpoint. Notification channel deferred to Phase 5 or Phase 8 frontend integration. |

## ADR-009: Append-Only Audit Log in PostgreSQL

| Field | Detail |
|---|---|
| Status | DECIDED |
| Decision | Audit log is stored in PostgreSQL as an append-only table using a DB role with INSERT-only grants. No UPDATE or DELETE is ever granted on this table. |
| Rationale | (1) Immutability is a legal and compliance requirement. (2) PostgreSQL INSERT-only role is a well-established pattern. (3) Keeps the audit log in the same DB as other data — reduces operational complexity. |
| Consequences | Audit log table will grow continuously. Archival strategy (e.g. partition by month, archive old partitions) must be defined before Phase 7 evaluation runs. |

## ADR-010: Provenance as First-Class Data (Not Log-Only)

| Field | Detail |
|---|---|
| Status | DECIDED |
| Decision | Provenance is stored both within WorkflowContext (JSON column) AND in a dedicated provenance_chain table. It is queryable via API endpoint, not just recoverable from logs. |
| Rationale | (1) Provenance is a core research feature (evidence provenance as research mechanism). (2) Log-only provenance is not queryable for the /trace API endpoint. (3) Dual storage provides both fast access and durability. |
| Consequences | Every agent step write involves a provenance table INSERT. Small performance cost; considered acceptable given the research value. |

## ADR-011: SQL Agent Schema Scope via Per-Invocation Config

| Field | Detail |
|---|---|
| Status | DECIDED — resolves AMB-008 from Part 0.2 |
| Decision | The SQL Agent receives a permitted_schemas list as part of its AgentTaskInput for every invocation. The calling workflow context (via Orchestrator) sets this list. When Finance Agent delegates to SQL Agent, the Orchestrator passes permitted_schemas=["finance"]. When HR Agent delegates, permitted_schemas=["hr"]. The SQL Agent's permission manifest defines the CEILING of permitted schemas; the per-invocation list must be a subset. |
| Rationale | (1) A single SQL Agent node with per-invocation scope is simpler than multiple SQL Agent variants. (2) The manifest ceiling + per-invocation subset pattern enforces least-privilege without requiring separate agent instances. |
| Consequences | The Orchestrator is responsible for correctly setting permitted_schemas when composing AgentTaskInput for delegated SQL calls. This must be tested explicitly. |

---

# SECTION 18 — ARCHITECTURAL RISKS

## ARCH-RISK-001: LLM Latency and Sequential Bottleneck

| Field | Detail |
|---|---|
| Risk | All agents share one LLM runtime. Multi-agent tasks execute LLM calls sequentially (not in parallel). |
| Impact | High latency for complex multi-agent workflows (e.g. 5-step plan with each step requiring LLM → total LLM time may exceed 5 minutes). |
| Mitigation | (1) Design tasks to minimize unnecessary LLM calls. (2) Use LLM only for generation; use deterministic tools for data retrieval and computation. (3) LLM response caching for identical prompts (future). (4) Accept higher latency as a research-phase trade-off. |
| Validation Required | Measure end-to-end latency for representative multi-agent tasks in Phase 7 evaluation. |

## ARCH-RISK-002: LangGraph State Machine Complexity

| Field | Detail |
|---|---|
| Risk | Complex conditional routing logic in LangGraph may become difficult to debug and maintain as more agents and failure paths are added. |
| Impact | Medium — incorrect routing logic could cause tasks to silently fail or enter incorrect states. |
| Mitigation | (1) Comprehensive unit tests for each LangGraph conditional edge. (2) State transition diagram (Diagram C) kept updated. (3) Structured logging at every node transition. |
| Validation Required | Full workflow integration tests covering every alternative path defined in Section 2.2. |

## ARCH-RISK-003: PostgreSQL as Single Point of Failure

| Field | Detail |
|---|---|
| Risk | PostgreSQL is used for all persistent state, audit, provenance, and relational data. Its unavailability brings the system to a halt. |
| Impact | High — no task can complete durably without PostgreSQL. |
| Mitigation | (1) Redis provides short-term state continuity during brief PG outages. (2) Docker volume-backed PG data (survives container restart). (3) Production deployment should add PG replication (outside current scope). |
| Validation Required | Test Redis-only operation during PG unavailability in Phase 6 reliability testing. |

## ARCH-RISK-004: Agent Permission Bypass via Prompt Injection

| Field | Detail |
|---|---|
| Risk | A malicious or miscrafted task_description could attempt to override agent system prompts and cause agents to act outside their permitted scope. |
| Impact | High — could lead to unauthorized data access or policy violations. |
| Mitigation | (1) Input sanitization before all LLM-bound prompts (Section 8.7). (2) Tool interceptor enforces permissions independently of LLM output. (3) Output guardrails check agent outputs before returning results. (4) Security/Policy Agent is not LLM-based and cannot be prompt-injected. |
| Validation Required | Security testing with adversarial inputs in Phase 5. |

## ARCH-RISK-005: Model Hallucination in RAG Synthesis

| Field | Detail |
|---|---|
| Risk | The LLM may generate factually incorrect answers in the RAG Agent that do not match the retrieved documents. |
| Impact | Medium — incorrect enterprise information could lead to bad decisions if not caught. |
| Mitigation | (1) Reviewer factual grounding check validates numerical claims against provenance. (2) Source citations are mandatory in RAG responses. (3) Reviewer LLM failure → escalate to human (never auto-approve). |
| Validation Required | RAG accuracy evaluation in Phase 7 against ground truth synthetic data. |

## ARCH-RISK-006: Audit Log Write Performance Under Load

| Field | Detail |
|---|---|
| Risk | High-frequency audit log writes (every tool call, every agent step) may create a write bottleneck on the audit_log table under load. |
| Impact | Low-Medium — audit writes are append-only and should be fast; risk increases at high concurrent task volumes. |
| Mitigation | (1) Audit writes are async (fire-and-forget with error logging). (2) PostgreSQL table partitioning by month reduces index scan costs. (3) Separate eaoc_audit_append DB role avoids lock contention with state reads. |
| Validation Required | Load test audit write throughput in Phase 7 evaluation. |

## ARCH-RISK-007: Research Validity — Evaluation Baseline Ambiguity

| Field | Detail |
|---|---|
| Risk | The single-agent and basic multi-agent baselines for evaluation are not yet specified (AMB-006). Without well-defined baselines, the research comparison may be invalid. |
| Impact | High — invalid baseline comparison undermines the entire research contribution. |
| Mitigation | Resolve AMB-006 (baseline architecture and metrics) before Phase 7 design. Record in ARCH-OPEN-003. |
| Validation Required | Baseline architecture reviewed and approved before Phase 7 implementation begins. |

## ARCH-RISK-008: Sandbox Security for Developer Agent

| Field | Detail |
|---|---|
| Risk | The Developer Agent sandbox (for debug_with_test) must be truly isolated. A misconfigured sandbox could allow code to access enterprise resources or the host system. |
| Impact | High — sandbox escape would be a critical security breach. |
| Mitigation | (1) Sandbox technology must provide network isolation (no enterprise network access). (2) Sandbox must be ephemeral (destroyed after each execution). (3) Sandbox must have no mounted enterprise volumes. (4) Technology choice requires security review (ADR-009 sub-decision, AMB-009). |
| Validation Required | Penetration test of sandbox in Phase 5 security testing. |

---

# OPEN ARCHITECTURAL DECISIONS

## ARCH-OPEN-001: Human Approval Notification Channel
**Corresponds to**: AMB-001 (Part 0.1)
**Status**: OPEN
**Decision needed**: How human approvers are notified of pending approval requests.
**Options**: (a) Polling API only, (b) Webhook callback, (c) Email, (d) Slack/Teams integration.
**Needed before**: Phase 4 (Multi-Agent Orchestration) evaluation or Phase 5 at latest.

## ARCH-OPEN-002: Local LLM Model Selection
**Corresponds to**: AMB-005 (Part 0.1)
**Status**: OPEN
**Decision needed**: Which local model family and variant to use (Llama 3.1, Mistral, Qwen 2.5, Phi-3, etc.).
**Impact**: Affects context window size (FR-STATE-002), embedding dimension (FR-RAG-001), and performance targets (NFR-PERF-002).
**Needed before**: Phase 1 (Backend Foundation) begins.

## ARCH-OPEN-003: Evaluation Metrics and Baseline Architecture
**Corresponds to**: AMB-006 (Part 0.1)
**Status**: OPEN
**Decision needed**: (a) Specific evaluation metrics (latency, accuracy, error rate, provenance completeness, human approval rate), (b) Single-agent baseline architecture design, (c) Basic multi-agent baseline architecture design.
**Needed before**: Phase 7 (Research Evaluation) design phase.

## ARCH-OPEN-004: pgvector to Qdrant Migration Trigger
**Corresponds to**: AMB-004 (Part 0.1), ADR-005
**Status**: OPEN
**Decision needed**: Specific criterion that triggers migration (chunk count threshold, latency SLA breach, feature requirement).
**Needed before**: Phase 7 at latest; earlier if corpus grows rapidly in Phase 2.

## ARCH-OPEN-005: Reviewer Grounding Check Boundary (LLM vs. Rule-Based)
**Corresponds to**: AMB-010 (Part 0.2)
**Status**: OPEN
**Decision needed**: Precise specification of which grounding checks are fully deterministic vs. LLM-assisted, and how LLM-assisted checks are anchored to prevent pure LLM authority.
**Needed before**: Phase 3 (Individual Agents) — Reviewer Agent implementation.

## ARCH-OPEN-006: Developer Agent Sandbox Technology
**Corresponds to**: AMB-009 (Part 0.2)
**Status**: OPEN
**Decision needed**: Specific sandbox implementation (Docker container, gVisor, subprocess with resource limits, etc.).
**Needed before**: Phase 3 — Developer Agent implementation.

---

# PART 0.3 COMPLETION CHECKLIST

- [x] Section 1: System Context — actors, components, data domains defined
- [x] Section 2: End-to-End Request Flow — primary path + 8 alternative paths defined
- [x] Section 3: Component Architecture — all 19 components specified with interfaces
- [x] Section 4: Agent Communication — schemas, IDs, context passing, execution control defined
- [x] Section 5: State Management — all state types, Redis vs PostgreSQL, lifecycle transitions defined
- [x] Section 6: Data Architecture — PostgreSQL schemas, pgvector, Redis, documents, synthetic data
- [x] Section 7: Tool Architecture — registry, interception pipeline, MCP placement, timeout/error handling
- [x] Section 8: Security Architecture — Auth != AuthZ != Policy != Risk explicitly distinguished
- [x] Section 9: Risk-Aware Execution — deterministic rule engine, no LLM authority, all risk levels
- [x] Section 10: Human-in-the-Loop — approval queue, request schema, lifecycle, timeout, audit
- [x] Section 11: Provenance — end-to-end chain, minimum metadata per stage, retrieval API
- [x] Section 12: Failure and Recovery — classification, retry (no infinite loops), circuit breaker, re-planning, graceful failure
- [x] Section 13: Observability — structured logs, 6 metric types, health check, correlation IDs
- [x] Section 14: Deployment Topology — Docker Compose services, agent deployment model, scalability
- [x] Section 15: Architectural Diagrams — 9 diagrams (A through I) in Mermaid + ASCII
- [x] Section 16: Traceability — all components traced to Part 0.1 requirements and Part 0.2 agent specs
- [x] Section 17: Architectural Decision Records — 11 ADRs (7 decided, 4 open)
- [x] Section 18: Architectural Risks — 8 risks with impact, mitigation, validation requirement
- [x] Key interface schemas defined: AgentTaskInput, AgentResult, ErrorDetail, EvidenceRecord, ProvenanceEntry, WorkflowState, ApprovalRequest
- [x] ADR-011: SQL Agent schema scope decision resolves AMB-008 from Part 0.2
- [x] No silent ambiguity resolutions — all open items recorded as ARCH-OPEN-001 through ARCH-OPEN-006
- [x] No implementation code written
- [x] No frontend created
- [x] No new agents added
- [x] No approved technologies replaced
- [x] No architectural conflicts found with Master Specification v1.0, Part 0.1, or Part 0.2
- [ ] USER APPROVAL OF PART 0.3 PENDING
- [ ] Part 0.4 scope to be defined after approval

---
Part 0.3 Complete. Stopped as instructed. Awaiting user approval before proceeding to Part 0.4.
