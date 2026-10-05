# EAOC Agent Specification — Part D: Matrices, Test Plan, Traceability, Checklist
# Phase 0 · Part 0.2 · Version 1.0

---

# 1. AGENT RESPONSIBILITY MATRIX

| Responsibility | ORCH | PLAN | RAG | SQL | DOC | ANA | FIN | HR | IT | DEV | REV | SEC |
|---|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| Workflow coordination | X | | | | | | | | | | | |
| Task decomposition | | X | | | | | | | | | | |
| Plan validation | | X | | | | | | | | | | |
| Risk annotation of steps | | X | | | | | | | | | |
| Semantic vector retrieval | | | X | | | | | | | | | |
| Answer synthesis (RAG) | | | X | | | | | | | | | |
| NL-to-SQL generation | | | | X | | | | | | | | |
| SQL execution (read-only) | | | | X | | | | | | | | |
| Document parsing | | | | | X | | | | | | | |
| Structured field extraction | | | | | X | | | | | | | |
| Document summarization | | | | | X | | | | | | | |
| Metric computation | | | | | | X | | | | | | |
| Narrative generation | | | | | | X | | | | | | |
| Finance data operations | | | | | | | X | | | | | |
| HR data operations | | | | | | | | X | | | | |
| IT ticket operations | | | | | | | | | X | | | |
| Code explanation/review | | | | | | | | | | X | | |
| Code generation | | | | | | | | | | X | | |
| Sandbox test execution | | | | | | | | | | X | | |
| Output completeness check | | | | | | | | | | | X | |
| Factual grounding check | | | | | | | | | | | X | |
| Policy compliance check | | | | | | | | | | | X | X |
| Risk level assessment | | X | | | | | | | | | X | X |
| Agent manifest enforcement | | | | | | | | | | | | X |
| Tool permission enforcement | | | | | | | | | | | | X |
| Human approval creation | X | | | | | | | | | | | |
| Workflow state persistence | X | | | | | | | | | | | |
| Provenance recording | X | | | | | | | | | | | |
| Final response assembly | X | | | | | | | | | | | |
| Audit logging | X | X | X | X | X | X | X | X | X | X | X | X |

---

# 2. AGENT-TO-TOOL PERMISSION MATRIX

| Tool | ORCH | PLAN | RAG | SQL | DOC | ANA | FIN | HR | IT | DEV | REV | SEC |
|---|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| State Store (Redis/PG) R/W | X | | | | | | | | | | | |
| RBAC Check Tool | X | | | | | | | | | | | |
| Risk Gate Tool | X | | | | | | | | | | | |
| Approval Queue Tool | X | | | | | | | | | | | |
| LLM Inference Tool | | X | X | X | X | X | X | X | X | X | X | |
| Agent Registry Lookup | | X | | | | | | | | | | |
| Embedding Tool | | | X | | | | | | | | | |
| pgvector Search Tool | | | X | | | | | | | | | |
| PostgreSQL Read-Only Exec | | | | X | | | | | | | | |
| SQL Validation Tool | | | | X | | | | | | | | |
| Doc Store Retrieval Tool | | | | | X | | | | | | | |
| PDF Parser Tool | | | | | X | | | | | | | |
| DOCX Parser Tool | | | | | X | | | | | | | |
| Computation Library | | | | | | X | | | | | | |
| Sandbox Exec Tool | | | | | | | | | | X | | |
| Schema Validation Tool | | | | | | | | | | | X | |
| Policy Rule Engine | | | | | | | | | | | X | X |
| Manifest Lookup Tool | | | | | | | | | | | | X |
| Risk Classification Tool | | X | | | | | | | | | X | X |
| Input Sanitization Tool | | X | | | | | | | | | | X |
| Audit Logger | X | X | X | X | X | X | X | X | X | X | X | X |

Notes:
- FIN, HR, IT agents invoke SQL/Data Agent and RAG Agent via delegation, not direct DB connections.
- ANA agent invokes SQL/Data Agent via delegation.
- SEC agent has no enterprise data access; all checks are in-memory against loaded policy definitions.

---

# 3. AGENT-TO-DATA ACCESS MATRIX

| Data Source | ORCH | PLAN | RAG | SQL | DOC | ANA | FIN | HR | IT | DEV | REV | SEC |
|---|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| workflow_state table (PG) | R/W | | | | | | | | | | | |
| approval_requests table | R/W | | | | | | | | | | | |
| rbac_roles (config) | R | R | | | | | | | | | | |
| Agent capability registry | R | R | | | | | | | | | | |
| Risk rules config | | R | | | | | | | | | X | X |
| Policy definitions config | | | | | | | | | | | X | X |
| Agent permission manifests | R | | | | | | | | | | | X |
| pgvector chunks table | | | R | | | | | | | | | |
| Document metadata table | | | R | | R | | | | | | | |
| Enterprise document store | | | | | R | | | | | | | |
| analytics schema (PG) | | | | R | | | | | | | | |
| finance schema (PG) | | | | R* | | | | | | | | |
| hr schema (PG) | | | | R* | | | | | | | | |
| it schema (PG) | | | | R* | | | | | | | | |
| User-provided code (input) | | | | | | | | | | R | | |
| WorkflowContext (read-only) | | | | | | | | | | | R | |
| Provenance chain (read-only) | | | | | | | | | | | R | |
| audit_log table (append) | A | A | A | A | A | A | A | A | A | A | A | A |

Legend: R=Read, W=Write, R/W=Read-Write, A=Append-only
* SQL Agent accesses these schemas only when the calling workflow's manifest scope permits it.
- FIN/HR/IT agents access their respective schemas only via SQL Agent delegation.
- ANA agent has no direct DB access; receives data via SQL Agent delegation.
- DEV agent has no enterprise data access.
- REV agent has no direct DB access; reads WorkflowContext passed by Orchestrator.
- SEC agent has no enterprise data access; uses in-memory policy definitions only.

---

# 4. AGENT INTERACTION / DEPENDENCY MATRIX

| Agent | Invokes | Receives From |
|---|---|---|
| ORCH | PLAN, all 10 domain agents, REV, SEC (policy checks) | API Layer, RBAC Module, Risk Gate, Approval Queue |
| PLAN | LLM, Agent Registry, Risk Classification Tool | ORCH |
| RAG | Embedding Tool, pgvector, LLM | ORCH, HR Agent (delegation), IT Agent (delegation) |
| SQL | LLM, SQL Validation Tool, PostgreSQL R/O | ORCH, ANA (delegation), FIN (delegation), HR (delegation), IT (delegation) |
| DOC | Doc Store, PDF/DOCX/TXT Parsers, LLM | ORCH, FIN (delegation) |
| ANA | SQL Agent (delegation), LLM, Computation Library | ORCH |
| FIN | SQL Agent (delegation), DOC Agent (delegation), LLM | ORCH |
| HR | SQL Agent (delegation), RAG Agent (delegation) | ORCH |
| IT | SQL Agent (delegation), RAG Agent (delegation), LLM | ORCH |
| DEV | LLM, Sandbox Exec Tool | ORCH |
| REV | LLM, Policy Rule Engine, Risk Classification Tool | ORCH |
| SEC | Policy Rule Engine, Manifest Lookup, Risk Classification Tool | Tool Execution Layer, ORCH, REV |

Key dependency chains:
- Finance data: ORCH → FIN → SQL → PostgreSQL
- HR policy: ORCH → HR → RAG → pgvector
- IT resolution: ORCH → IT → [SQL → PostgreSQL] + [RAG → pgvector]
- Analytics: ORCH → ANA → SQL → PostgreSQL
- Document extraction: ORCH → DOC → Document Store
- Validation: ORCH → REV (with full WorkflowContext)
- Every tool call: Agent → SEC (permission check) → Tool

---

# 5. RISK / APPROVAL MATRIX

| Operation | Risk Level | Auto-Approve | Requires Reviewer | Requires Human Approval |
|---|:---:|:---:|:---:|:---:|
| Read-only analytics query | Low | Yes | Yes (standard) | No |
| RAG policy lookup | Low | Yes | Yes (standard) | No |
| IT ticket query | Low | Yes | Yes (standard) | No |
| HR headcount query (aggregate) | Low | Yes | Yes (standard) | No |
| Code explanation / review / doc | Low | Yes | Yes (standard) | No |
| Finance invoice read | Medium | Yes (enhanced log) | Yes (standard) | No |
| HR employee info (non-PII) | Medium | Yes (enhanced log) | Yes (standard) | No |
| SQL query on finance schema | Medium | Yes (enhanced log) | Yes (standard) | No |
| Code generation (sandbox test) | Medium | Yes (enhanced log) | Yes (standard) | No |
| HR PII data access (authorized) | High | No | Yes | Yes |
| Financial write operation | High | No | Yes | Yes |
| Any data modification operation | High | No | Yes | Yes |
| Restricted document access | High | No | Yes | Yes |
| Policy or RBAC configuration change | High | No | Yes | Yes |
| Production deployment | High | No | Yes | Yes (not in current scope) |

Notes:
- "Auto-Approve" means the Orchestrator proceeds without a human approval gate.
- Medium-risk steps proceed with full audit logging and mandatory Reviewer check.
- High-risk steps pause execution and create a HumanApprovalRequest.
- LLM is never the sole authority for risk assessment; rule-based classification governs.

---

# 6. FAILURE / RECOVERY MATRIX

| Failure Event | Detecting Agent | Failure Class | Retry? | Re-plan? | Human Escalation? | Task Outcome |
|---|---|---|:---:|:---:|:---:|---|
| LLM timeout (any agent) | Orchestrator | llm_failure | Yes (1x) | After retries | No | Failed if retries exhausted |
| DB unavailable (SQL Agent) | SQL Agent | database_unavailable | Yes (1x) | After retries | No | Failed if retries exhausted |
| pgvector unavailable (RAG) | RAG Agent | vector_store_unavailable | Yes (1x) | After retries | No | Failed if retries exhausted |
| Document not found (DOC) | Doc Agent | document_not_found | No | Yes | No | Re-plan or fail |
| Plan validation failure | Planner | plan_validation_failed | Yes (1x replan) | Yes (1x) | No | Failed after 2 plan attempts |
| Policy violation (SEC) | Security Agent | policy_violation | No | No | No | Step blocked; task fails or re-plans |
| Reviewer rejects output | Reviewer | review_failed | No | Yes (failing steps) | Escalate option | Re-execute or escalate |
| Reviewer LLM fails | Reviewer | llm_failure | Yes (1x) | No | Yes (escalate to human) | Human review |
| Human approval timeout | Orchestrator | approval_timeout | No | No | No | Task failed |
| Human approval rejected | Orchestrator | approval_rejected | No | No | No | Task failed |
| State store unavailable | Orchestrator | state_store_failure | Yes (3x) | No | No | Degraded or failed |
| Sandbox unavailable (DEV) | Dev Agent | sandbox_unavailable | No | No | No | Step fails; no code exec |
| Optional step failure | Orchestrator | step_skipped | No | No | No | Partial result; task completes |
| Orchestrator crash mid-task | Restart + PG recovery | n/a | n/a | Resume from last step | No | Recovery within 30s |

---

# 7. PROVENANCE REQUIREMENTS (END-TO-END CHAIN)

Every completed task must support a full provenance reconstruction from final output back to the original user request through this chain:

## Provenance Chain Schema

```
ProvenanceChain [
  {
    step_index: int,
    agent_id: string,           // which agent produced this result
    tool_used: string,          // which tool was invoked
    data_source: string,        // which data source was accessed
    action_summary: string,     // brief description of action taken
    input_ref: string,          // reference to input data (not raw data)
    result_ref: string,         // reference to output stored in WorkflowContext
    timestamp: datetime,
    status: "completed" | "failed" | "skipped"
  },
  ...
]
```

## Required Provenance Fields Per Agent

| Agent | Required Provenance Fields |
|---|---|
| ORCH | task_id, user_id, plan_id, step_sequence, final_status |
| PLAN | plan_id, plan_type, agent_sequence, risk_annotations, planning_rationale |
| RAG | namespace, query (sanitized), doc_ids_retrieved, chunk_ids_retrieved, scores, model_used |
| SQL | query_goal (sanitized), sql_generated, sql_executed, schemas_accessed, row_count, truncated |
| DOC | document_id, format, operation, fields_extracted_names, missing_fields, model_used |
| ANA | analysis_goal, data_source_ref, sql_agent_ref, metric_names, pii_aggregated |
| FIN | entity_type, filters_applied, sql_agent_ref, doc_agent_ref (if used) |
| HR | query_type, department_filter, pii_redacted, sql_agent_ref, rag_agent_ref (if used) |
| IT | operation, ticket_id (if used), sql_agent_ref, rag_agent_ref (if used) |
| DEV | operation, language, code_snippet_hash, model_used, sandbox_id (if used), fix_verified |
| REV | review_id, findings_count, risk_level, recommendation, model_used |
| SEC | check_id, agent_evaluated, tool_evaluated, allowed, violation_count, policies_applied |

## Trace Reconstruction Path

```
GET /api/v1/tasks/{task_id}/trace
  → WorkflowContext.provenance_chain
  → For each step: agent_id → tool_used → data_source → result_ref
  → Enables: final_output ← reviewer ← agent_results ← tools ← data_sources ← user_request
```

---

# 8. AGENT-LEVEL TEST PLAN

## Test Categories Per Agent

### T1 — Happy Path Tests
Verify the agent completes its primary responsibility correctly for a valid input.

### T2 — Permission Boundary Tests
Verify the agent cannot access tools or data outside its manifest.

### T3 — Failure / Error Tests
Verify the agent handles each failure condition correctly and returns structured errors.

### T4 — Security Tests
Verify security constraints: no credential leakage, no PII exposure, no unauthorized access.

### T5 — Provenance Tests
Verify that the provenance entry produced by the agent contains all required fields.

### T6 — Audit Tests
Verify that all declared audit events are produced for each scenario.

### T7 — Integration Tests
Verify the agent operates correctly within a multi-agent workflow.

## Test Coverage Summary Per Agent

| Agent | T1 | T2 | T3 | T4 | T5 | T6 | T7 | Scenarios Defined |
|---|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| ORCH | X | X | X | X | X | X | X | 7 |
| PLAN | X | X | X | X | X | X | X | 7 |
| RAG | X | X | X | X | X | X | X | 7 |
| SQL | X | X | X | X | X | X | X | 7 |
| DOC | X | X | X | X | X | X | X | 7 |
| ANA | X | X | X | X | X | X | X | 7 |
| FIN | X | X | X | X | X | X | X | 5 |
| HR | X | X | X | X | X | X | X | 5 |
| IT | X | X | X | X | X | X | X | 5 |
| DEV | X | X | X | X | X | X | X | 7 |
| REV | X | X | X | X | X | X | X | 7 |
| SEC | X | X | X | X | X | X | X | 7 |
| **Total** | | | | | | | | **76** |

---

# 9. TRACEABILITY TO PART 0.1 REQUIREMENTS

| Part 0.2 Section | Traced-From Part 0.1 Requirements |
|---|---|
| Orchestrator spec (AGT-ORCH) | FR-ORCH-001, FR-ORCH-002, FR-ORCH-003, FR-AGT-ORCH-001, FR-HITL-001, FR-PROV-001 |
| Planner spec (AGT-PLAN) | FR-PLAN-001, FR-PLAN-002, FR-PLAN-003, FR-AGT-PLAN-001, FR-ADAP-001, FR-RISK-001 |
| RAG Agent spec (AGT-RAG) | FR-AGT-RAG-001, FR-RAG-001, FR-RAG-002, FR-PERM-001, FR-PERM-002 |
| SQL Agent spec (AGT-SQL) | FR-AGT-SQL-001, FR-SQL-001, FR-SQL-002, FR-DATA-001, FR-DATA-002, FR-PERM-001 |
| Document Agent spec (AGT-DOC) | FR-AGT-DOC-001, FR-DOC-001, FR-DOC-002, FR-PERM-001 |
| Analytics Agent spec (AGT-ANA) | FR-AGT-ANA-001, FR-ANA-001, FR-ANA-002, NFR-PRIV-001 |
| Finance Agent spec (AGT-FIN) | FR-AGT-FIN-001, FR-FIN-001, FR-FIN-002, FR-RBAC-002, FR-AUDIT-001 |
| HR Agent spec (AGT-HR) | FR-AGT-HR-001, FR-HR-001, FR-HR-002, NFR-PRIV-001, FR-AUDIT-001 |
| IT Agent spec (AGT-IT) | FR-AGT-IT-001, FR-IT-001, FR-IT-002, FR-PERM-001 |
| Developer Agent spec (AGT-DEV) | FR-AGT-DEV-001, FR-HITL-001 (HITL model), FR-TOOL-001, FR-TOOL-002 |
| Reviewer spec (AGT-REV) | FR-AGT-REV-001, FR-REV-001, FR-REV-002, FR-REV-003, FR-RISK-002 |
| Security/Policy spec (AGT-SEC) | FR-AGT-SEC-001, FR-PERM-001, FR-PERM-002, FR-RBAC-003, NFR-FAULT-001 |
| Agent responsibility matrix | FR-RBAC-003, FR-PERM-001, FR-AGT-* (all 12) |
| Tool permission matrix | FR-PERM-001, FR-PERM-002, FR-TOOL-001, FR-TOOL-002 |
| Data access matrix | FR-DATA-001, FR-DATA-002, FR-RBAC-003, NFR-SEC-004 |
| Interaction/dependency matrix | FR-ORCH-002, FR-ADAP-001, FR-ADAP-002 |
| Risk/approval matrix | FR-RISK-001, FR-RISK-002, FR-HITL-001, FR-HITL-002, FR-HITL-003 |
| Failure/recovery matrix | FR-FAIL-001, FR-FAIL-002, FR-RETR-001, FR-RETR-002, NFR-REL-001 |
| Provenance requirements | FR-PROV-001, FR-PROV-002, NFR-TRACE-001, NFR-TRACE-002 |
| Agent test plan | NFR-MAINT-002, FR-AUDIT-001 |

---

# 10. AMBIGUITIES REQUIRING LATER DECISIONS

## AMB-007 · Agent Isolation Implementation Model [Pre-Phase 3]
All 12 agents are specified as logical components. The physical isolation model is not yet decided:
- Option A: LangGraph nodes (in-process, same Python process)
- Option B: Separate FastAPI sub-services (network boundary, stronger isolation)
- Option C: Hybrid (Orchestrator/Planner/SEC in-process; domain agents as sub-services)
This choice affects implementation of FR-RBAC-003 (agent-level permission scoping) and FR-PERM-002 (tool call interception). Must be decided before Phase 3 begins.

## AMB-008 · SQL Agent Schema Scope Per Delegation Context [Pre-Phase 3]
The SQL/Data Agent is shared by FIN, HR, IT, and ANA agents via delegation. When Finance Agent delegates to SQL Agent, the SQL Agent must restrict itself to finance schema. The mechanism for passing the per-invocation schema scope (via context parameter vs. separate agent instances vs. manifest variant) must be defined before Phase 3.

## AMB-009 · Developer Agent Sandbox Technology [Pre-Phase 3]
The Developer Agent sandbox is specified as "isolated container." The specific technology (Docker container, gVisor, Firecracker microVM, subprocess with resource limits) has not been decided. This affects NFR-SEC-002 (secret management in sandbox) and the sandbox_id provenance field. Must be decided before Phase 3 Agent 10 implementation.

## AMB-010 · Reviewer Grounding Check — LLM vs. Rule-Based [Pre-Phase 3]
The Reviewer's factual grounding check uses LLM assistance. The boundary between LLM-assessed and rule-based grounding checks is not fully specified. If LLM is used for grounding, it cannot be the sole authority (FR-RISK-001 principle). The exact rules that constitute a deterministic grounding check vs. LLM-assisted check need to be defined before Phase 3 Reviewer implementation.

## AMB-011 · pgvector Namespace / Collection Taxonomy [Pre-Phase 2]
RAG Agent accesses namespaces in pgvector. The taxonomy of namespaces (e.g. hr_policy, it_kb, company_docs, finance_policy) and the mapping of access levels to roles has not been formally defined. Must be defined before Phase 2 (Data & Knowledge Layer).

## AMB-012 · Adaptive Orchestration Routing Algorithm [Pre-Phase 4]
FR-ADAP-001 specifies dynamic agent selection. The Planner uses the agent capability registry and LLM to determine the agent sequence. The exact format of capability descriptions in the registry (embeddings, keyword tags, structured capability schema) needs to be defined before Phase 4 implementation.

---

# 11. PART 0.2 COMPLETION CHECKLIST

- [x] Agent 1 (Orchestrator): all 25 fields (A-Y) defined
- [x] Agent 2 (Planner): all 25 fields defined
- [x] Agent 3 (Research/RAG): all 25 fields defined
- [x] Agent 4 (SQL/Data): all 25 fields defined
- [x] Agent 5 (Document): all 25 fields defined
- [x] Agent 6 (Analytics): all 25 fields defined
- [x] Agent 7 (Finance): all 25 fields defined
- [x] Agent 8 (HR): all 25 fields defined
- [x] Agent 9 (IT/Support): all 25 fields defined
- [x] Agent 10 (Developer - HITL): all 25 fields defined; HITL model specified
- [x] Agent 11 (Reviewer/Validation): all 25 fields defined; independent validation layer specified
- [x] Agent 12 (Security/Policy): all 25 fields defined; not an unrestricted administrator
- [x] CAN/CANNOT explicitly stated for every agent
- [x] Permitted tools listed per agent
- [x] Permitted data sources listed per agent
- [x] Data modification permissions stated per agent (all read-only by default)
- [x] Human approval conditions stated per agent
- [x] Evidence/provenance requirements stated per agent
- [x] Orchestrator and Planner responsibilities clearly distinguished
- [x] Adaptive orchestration decision inputs defined (FR-ADAP-001, FR-PLAN-003)
- [x] Risk: LLM not sole authority; rule-based classification governs
- [x] Least-privilege principle applied across all agents
- [x] Agent responsibility matrix produced (12x28)
- [x] Agent-to-tool permission matrix produced (12x20)
- [x] Agent-to-data access matrix produced (12x17)
- [x] Agent interaction/dependency matrix produced
- [x] Risk/approval matrix produced (14 operation types)
- [x] Failure/recovery matrix produced (14 failure events)
- [x] End-to-end provenance chain schema defined
- [x] Required provenance fields per agent defined
- [x] Agent-level test plan produced (76 scenarios across 7 test categories)
- [x] Traceability to Part 0.1 requirements produced
- [x] 6 ambiguities requiring later decisions documented (AMB-007 to AMB-012)
- [ ] USER APPROVAL OF PART 0.2 PENDING
- [ ] Part 0.3 scope to be defined after approval

---
Part 0.2 Complete. Stopped as instructed. Awaiting user approval before proceeding to Part 0.3.
