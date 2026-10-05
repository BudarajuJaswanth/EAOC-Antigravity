# EAOC — Detailed System Architecture Specification
# Part 0.3-A: Sections 1–5
# Phase 0 · Part 0.3 · Version 1.0
# Status: DRAFT — Awaiting Part 0.3 approval
# Inputs: Master Spec v1.0, Part 0.1 FRS/NFRS, Part 0.2 Agent Specification

---

# SECTION 1 — SYSTEM CONTEXT

## 1.1 Context Overview

EAOC operates as an enterprise-internal AI operations platform. It sits between human users (or programmatic API callers) and the enterprise data/tool layer. It never directly exposes raw enterprise data; all access is mediated by its agent and permission architecture.

```
┌─────────────────────────────────────────────────────────┐
│                    EAOC SYSTEM BOUNDARY                 │
│                                                         │
│  ┌───────────┐    ┌──────────────────────────────────┐  │
│  │ API Layer │    │   Multi-Agent Orchestration Core │  │
│  │ (FastAPI) │───▶│  Orchestrator / Planner / Agents │  │
│  └───────────┘    └─────────────┬────────────────────┘  │
│        ▲                        │                        │
│        │                        ▼                        │
│  ┌─────┴──────┐    ┌────────────────────────────────┐    │
│  │ Auth/RBAC  │    │      Data & Tool Layer          │    │
│  └────────────┘    │  PostgreSQL / pgvector / Redis  │    │
│                    │  Document Store / LLM Runtime   │    │
│                    └────────────────────────────────┘    │
└─────────────────────────────────────────────────────────┘
         │                        │
         ▼                        ▼
   Human Users /          Human Approval
   API Clients              Reviewers
```

## 1.2 External Actors

| Actor | Type | Interaction |
|---|---|---|
| End User | Human | Submits tasks via REST API; polls status; receives results |
| API Client | System | Programmatic task submission and status polling |
| Human Approver | Human | Reviews and decides on high-risk approval requests |
| Local LLM Runtime | External System | LM Studio or Ollama serving a local model; called via OpenAI-compatible API |
| Cloud LLM API | Optional External | Gemini or equivalent; optional, not mandatory; model-agnostic interface |

## 1.3 Internal System Components

| Component | Role |
|---|---|
| FastAPI API Layer | REST API gateway; request intake; response assembly |
| Auth Module | JWT issuance and validation |
| RBAC Module | Role-to-permission resolution |
| Orchestrator | Workflow coordination (LangGraph) |
| Planner | Task decomposition and plan generation |
| 12 Logical Agents | Domain-specific task execution |
| Security/Policy Agent | Permission enforcement and guardrails |
| Reviewer/Validation Agent | Output quality and compliance gate |
| Tool Layer | Registered tools with permission enforcement |
| PostgreSQL | Relational data, audit log, workflow state, vector store |
| pgvector | Vector similarity search (extension of PostgreSQL) |
| Redis | Fast workflow state cache, task queue |
| Document Store | Enterprise document files (PDF, DOCX, TXT) |
| Local LLM Runtime | Model inference (shared across all agents) |
| Approval Queue | Persistent human approval request store |
| Observability Stack | Structured logging, metrics, health endpoints |

## 1.4 Enterprise Data Domains (Synthetic)

| Domain | Schema | Managed By |
|---|---|---|
| Finance | finance.invoices, finance.budgets, finance.expenses | Finance Agent via SQL Agent |
| HR | hr.employees, hr.departments, hr.org_chart | HR Agent via SQL Agent |
| IT | it.tickets, it.categories, it.sla_definitions | IT Agent via SQL Agent |
| Analytics | analytics.sales, analytics.performance, analytics.kpi | Analytics Agent via SQL Agent |
| Knowledge Base | pgvector (chunked documents) | RAG Agent |
| Documents | Document Store filesystem or object store | Document Agent |
| Workflow State | public.workflow_state, public.approval_requests | Orchestrator |
| Audit Log | public.audit_log | All agents (append-only) |

---

# SECTION 2 — END-TO-END REQUEST FLOW

## 2.1 Primary Success Path

```
1.  Client submits POST /api/v1/tasks  (TaskRequest + Bearer token)
2.  Auth Middleware validates JWT → extracts UserContext
3.  RBAC Middleware attaches role/permission context to request
4.  Request schema validated (Pydantic TaskRequest)
5.  API Layer assigns task_id (UUID), persists initial state (accepted)
6.  Task enqueued to Redis task queue → returns HTTP 202 immediately
7.  Orchestrator dequeues task → initialises WorkflowContext
8.  Orchestrator dispatches WorkflowContext to Planner
9.  Planner generates ExecutionPlan (LLM + agent registry + risk rules)
10. Planner validates plan → returns to Orchestrator
11. Orchestrator updates state → status: running
12. FOR EACH step in ExecutionPlan:
    a. Orchestrator checks user RBAC for target agent
    b. Security/Policy Agent checks agent permission manifest
    c. Risk Gate evaluates step risk_level:
       - LOW  → execute immediately
       - MED  → execute with enhanced audit logging
       - HIGH → pause; create HumanApprovalRequest; status: awaiting_approval
    d. Orchestrator dispatches to specialized agent
    e. Agent invokes permitted tools (each tool call passes through permission check)
    f. Agent returns structured result
    g. Orchestrator appends provenance entry; updates WorkflowContext
13. All steps complete → Orchestrator forwards to Reviewer/Validation Agent
14. Reviewer checks: completeness, grounding, schema, policy, risk
15. Reviewer returns ReviewResult: passed=true, recommendation=approve
16. Orchestrator assembles FinalResponse
17. State updated → status: completed; FinalResponse stored
18. Client polls GET /api/v1/tasks/{task_id} → receives FinalResponse
```

## 2.2 Alternative Paths

### PATH-A: Permission Denial
```
Step 3/3b: RBAC or manifest check fails
→ HTTP 403 returned immediately (no task created if pre-task)
→ If mid-workflow: step blocked; PERMISSION_DENIED audit entry
→ Orchestrator sets step status: permission_denied
→ Task fails with reason: permission_denied
→ Client receives FinalResponse.status = failed, reason = permission_denied
```

### PATH-B: Validation Failure (Reviewer Rejects)
```
Step 14: Reviewer returns passed=false, recommendation=reject
→ Orchestrator checks if failed steps are re-executable
→ If yes: re-dispatch failing steps (max 1 re-execution per step)
→ If no / second failure: task fails
→ FinalResponse.status = failed, reason = reviewer_rejected, findings included
```

### PATH-C: High-Risk Step — Human Approval Required
```
Step 12c: Risk Gate classifies step as HIGH
→ Orchestrator creates HumanApprovalRequest (approval_id, step_detail, risk_factors, deadline)
→ Task status = awaiting_approval
→ Human Approver receives approval request (polls GET /api/v1/approvals or notified externally)
→ Approver submits POST /api/v1/approvals/{approval_id}/decision
   - approve → Orchestrator resumes step execution → continues normal path
   - reject  → Orchestrator sets task failed, reason = human_approval_rejected
→ Timeout (deadline elapsed without decision):
   → Task fails, reason = approval_timeout (NEVER auto-approve on timeout)
```

### PATH-D: Agent Failure / Tool Failure
```
Step 12d/e: Agent or tool raises failure
→ Orchestrator classifies failure: timeout | llm_failure | tool_error | schema_violation
→ Transient (timeout, llm_failure):
   → Retry up to max_retries (default 2) with exponential backoff
   → If retries exhausted: classify as permanent failure
→ Permanent failure:
   → If step is optional: mark skipped; continue with partial_result
   → If step is required: trigger contextual re-planning (max_replan_attempts = 2)
   → If re-planning fails: task fails with full failure trace
```

### PATH-E: Model (LLM) Failure
```
LLM health check detects 3 consecutive failures → llm_available = false
→ New task submissions return HTTP 503, reason = llm_unavailable
→ In-progress tasks: pause at next LLM call; status = degraded
→ Health endpoint reflects llm: unhealthy
→ When LLM recovers: flag reset; new tasks accepted; paused tasks may resume
```

### PATH-F: Database Failure
```
PostgreSQL unavailable:
→ Workflow state cannot be persisted
→ In-progress tasks: attempt Redis-only persistence
→ New tasks: HTTP 503 (cannot guarantee durability)
→ Orchestrator alert raised; operator intervention required
Redis unavailable:
→ Fall back to PostgreSQL-only for state reads/writes
→ Performance degraded but functionality maintained
```

### PATH-G: Reviewer Escalation
```
Step 15: Reviewer returns recommendation = escalate
→ Orchestrator creates HumanApprovalRequest for output review
→ Human reviews assembled output and evidence
→ approve → Orchestrator assembles FinalResponse → task completed
→ reject  → task failed with reason = human_escalation_rejected
→ timeout → task failed with reason = escalation_timeout
```

### PATH-H: Contextual Re-planning
```
Step 12d: Required step permanently fails
→ Orchestrator checks replan_count < max_replan_attempts (default 2)
→ Invokes Planner with: partial WorkflowContext + failed step detail + remaining steps
→ Planner generates revised plan for remaining steps
→ Orchestrator resumes with revised plan
→ Revised plan re-enters risk gate and RBAC checks (no permission escalation)
→ If max_replan_attempts exceeded: task fails with full trace
```

---

# SECTION 3 — COMPONENT ARCHITECTURE

## 3.1 API Layer (FastAPI)

**Responsibility**: REST API gateway, request/response handling, middleware chain, async task management.

**Interfaces**:
- `POST /api/v1/tasks` — submit task
- `GET /api/v1/tasks/{task_id}` — poll task status
- `GET /api/v1/tasks/{task_id}/trace` — retrieve provenance trace
- `POST /api/v1/auth/token` — issue JWT
- `GET /api/v1/approvals` — list pending approval requests (approver role)
- `POST /api/v1/approvals/{approval_id}/decision` — submit approval decision
- `GET /api/v1/audit` — query audit log (admin)
- `GET /api/v1/health` — health check (public)
- `GET /metrics` — Prometheus metrics (internal network)

**Middleware Chain** (in order):
1. RequestID Middleware — assign/propagate request_id
2. Auth Middleware — validate JWT; attach UserContext
3. RBAC Middleware — resolve permissions; attach to request context
4. Rate Limit Middleware — per-IP/token rate limiting
5. Logging Middleware — structured JSON log per request

**Key Design Rules**:
- All endpoints (except `/health`, `/metrics`, `/auth/token`) require authenticated JWT.
- All task submissions return 202 immediately; processing is async via background worker.
- No synchronous agent execution in the API request thread.

## 3.2 Authentication Module

**Responsibility**: JWT issuance, validation, and revocation tracking.

**Components**:
- `TokenIssuer`: validates credentials against user store; issues signed JWT
- `TokenValidator`: middleware component; validates signature, expiry, claims
- `TokenRevocationStore`: Redis set of revoked token JTIs (for logout support)
- `UserStore`: PostgreSQL table of users with bcrypt-hashed passwords

**JWT Claims**:
```
{
  "sub": "<user_id>",
  "roles": ["finance_user", "analyst"],
  "jti": "<unique token ID>",
  "iat": <issued_at>,
  "exp": <expires_at>
}
```

**Security**:
- Algorithm: HS256 (symmetric) for initial development; RS256 (asymmetric) recommended for production.
- Secret loaded from environment variable only.
- Token expiry: configurable (default 8 hours).
- Brute-force lockout: 10 consecutive failures → account locked (lockout_until stored in Redis).

## 3.3 RBAC Module

**Responsibility**: Role-to-permission resolution; enforcement of user-level agent access.

**Permission Model**:
```
Role → permitted_agents: [agent_ids]
Role → permitted_data_classes: [data_class_labels]
Role → max_risk_level: low | medium | high
Role → pii_access: bool
```

**Roles (initial)**:
- `admin`: all agents, all data classes, max_risk=high, pii=true
- `analyst`: RAG, SQL, Analytics, Document, Reviewer; max_risk=medium; pii=false
- `finance_user`: Finance, SQL(finance), Document, Analytics; max_risk=medium; pii=false
- `hr_user`: HR, RAG(hr_policy), Document; max_risk=low; pii=false (pii=true if elevated)
- `it_user`: IT, RAG(it_kb), SQL(it); max_risk=low; pii=false
- `developer`: Developer, RAG, SQL(analytics); max_risk=medium; pii=false
- `viewer`: status polling and audit read only; no agent invocation

**Enforcement Points**:
1. Pre-task RBAC check (by Orchestrator before dispatching each step)
2. Agent-level manifest check (by Security/Policy Agent for every tool call)
3. Data-class check (by domain agents before delegating to SQL/RAG)

**Fail-Closed Rule**: Any RBAC check failure (module unavailable, unknown role, etc.) → deny. Never allow on failure.

## 3.4 Orchestrator

**Technology**: LangGraph StatefulGraph; runs as a background worker process.

**Responsibility**: Workflow lifecycle management — not domain reasoning.

**LangGraph Graph Structure**:
```
START
  └─▶ [initialise_workflow]
        └─▶ [dispatch_to_planner]
              └─▶ [validate_plan]
                    ├─▶ [execute_step]  ◀─────────────┐
                    │     ├─▶ [risk_gate]              │
                    │     │     ├─▶ [create_approval]  │
                    │     │     └─▶ [dispatch_agent]   │
                    │     │           ├─▶ [step_ok] ───┘ (loop to next step)
                    │     │           └─▶ [step_fail]
                    │     └─▶ [handle_failure]
                    │           ├─▶ [retry]
                    │           ├─▶ [replan]
                    │           └─▶ [fail_task]
                    └─▶ [run_reviewer]
                          ├─▶ [assemble_response]
                          ├─▶ [create_escalation_approval]
                          └─▶ [fail_task]
END
```

**State Object** (LangGraph state):
```python
WorkflowState:
  task_id: str
  user_context: UserContext
  task_description: str
  execution_plan: ExecutionPlan | None
  current_step_index: int
  step_results: dict[int, AgentResult]
  provenance_chain: list[ProvenanceEntry]
  replan_count: int
  retry_counts: dict[int, int]
  approval_id: str | None
  status: TaskStatus
  final_response: FinalResponse | None
  error: ErrorDetail | None
```

## 3.5 Planner

**Technology**: LangGraph node; uses LLM via the LLM inference abstraction layer.

**Responsibility**: Task decomposition, agent selection, risk annotation, plan validation.

**Interfaces**:
- Input: `WorkflowState` (task_description, user_context, agent_registry)
- Output: `ExecutionPlan` (validated Pydantic model)

**LLM Prompt Structure**:
```
SYSTEM: You are the EAOC Planner. You decompose enterprise tasks into
        ordered agent execution steps. Available agents: {agent_registry}.
        User roles: {user_roles}. Risk rules: {risk_rules}.
        Output ONLY valid JSON matching ExecutionPlan schema.
USER:   Task: {sanitized_task_description}
        Context: {workflow_context_summary}
```

**Validation Pipeline** (after LLM output):
1. JSON parse → fail if not valid JSON
2. Pydantic validation against `ExecutionPlan` schema
3. Agent registry check — all agent_ids must exist
4. RBAC filter — remove steps with agents the user cannot invoke
5. Dependency DAG — detect circular dependencies (topological sort)
6. Risk annotation — apply risk classification rules to each step

## 3.6 LangGraph Workflow / State Layer

**Responsibility**: Manages the state machine for each task workflow; persists state checkpoints.

**State Persistence**:
- LangGraph checkpoint stored in PostgreSQL (`langgraph_checkpoints` table)
- Redis used as fast checkpoint cache (TTL = 24h)
- On LangGraph node execution: state updated and persisted atomically

**Concurrency**:
- Multiple tasks run concurrently as separate LangGraph graph invocations
- Each task has an isolated WorkflowState instance
- No shared mutable state between concurrent tasks

**Recovery**:
- On Orchestrator restart: LangGraph reads last checkpoint from PostgreSQL
- Resumes from last successfully persisted node

## 3.7 Logical Agents (LangGraph Nodes)

All 12 agents are implemented as LangGraph nodes within the same Python process initially (see ADR-003 for isolation decision).

**Agent Node Interface** (standard contract):
```python
def agent_node(state: WorkflowState, config: AgentConfig) -> WorkflowState:
    # 1. Extract relevant inputs from state
    # 2. Invoke permitted tools (via tool execution layer)
    # 3. Return updated state with AgentResult
```

**AgentConfig** (per node):
```
AgentConfig:
  agent_id: str
  permitted_tools: list[str]
  permitted_schemas: list[str]
  permitted_namespaces: list[str]
  max_data_sensitivity: str
  llm_config: LLMConfig
```

Each agent node receives only the slice of WorkflowState relevant to its step inputs (principle of minimal context exposure).

## 3.8 Tool Layer

**Responsibility**: Registered, permission-enforced, schema-validated tool execution.

**Tool Execution Pipeline**:
```
Agent requests tool call
  → Tool Interceptor:
      1. Lookup tool_id in Tool Registry (must exist)
      2. Verify agent_id has tool permission (manifest check via AGT-SEC)
      3. Validate input against tool input schema (Pydantic)
      4. Audit log: TOOL_CALL_ATTEMPTED
  → Execute tool function
  → Validate output against tool output schema (Pydantic)
  → Audit log: TOOL_CALL_COMPLETED | TOOL_CALL_FAILED
  → Return ToolResult to agent
```

**Tool Registry Entry**:
```
ToolRegistryEntry:
  tool_id: str
  name: str
  description: str
  input_schema: PydanticModel
  output_schema: PydanticModel
  required_permission: str
  risk_level: str
  timeout_seconds: int
  external_boundary: bool  # true if calls outside EAOC boundary
```

## 3.9 Data Access Layer

**Responsibility**: Abstracted, permission-enforced access to PostgreSQL schemas.

**Connection Pool**: SQLAlchemy async connection pool.

**DB Users (PostgreSQL roles)**:
- `eaoc_read_analytics` — SELECT on analytics schema only
- `eaoc_read_finance` — SELECT on finance schema only
- `eaoc_read_hr` — SELECT on hr schema only
- `eaoc_read_it` — SELECT on it schema only
- `eaoc_state_rw` — SELECT/INSERT/UPDATE on workflow_state, approval_requests
- `eaoc_audit_append` — INSERT only on audit_log
- `eaoc_admin` — used for migrations only; never used at runtime by agents

The SQL/Data Agent receives a connection string for the appropriate read-only role based on the `permitted_schemas` in its invocation config.

## 3.10 RAG / Retrieval Layer

**Responsibility**: Embedding generation, vector similarity search, document chunk management.

**Components**:
- `EmbeddingService`: wraps local embedding model; exposes `embed(text) → vector`
- `pgvector SearchService`: executes cosine similarity queries in PostgreSQL
- `IngestionPipeline`: background process for chunking + embedding new documents

**pgvector Table Schema**:
```sql
CREATE TABLE document_chunks (
  chunk_id      UUID PRIMARY KEY,
  document_id   UUID NOT NULL,
  chunk_index   INT NOT NULL,
  text          TEXT NOT NULL,
  embedding     VECTOR(1536),  -- dimension depends on embedding model
  access_level  VARCHAR(20) NOT NULL,  -- public | internal | restricted
  namespace     VARCHAR(50) NOT NULL,
  source_title  TEXT,
  created_at    TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX ON document_chunks USING ivfflat (embedding vector_cosine_ops);
```

**Retrieval Query** (server-side access filter):
```sql
SELECT chunk_id, document_id, text, source_title,
       1 - (embedding <=> $1::vector) AS score
FROM document_chunks
WHERE namespace = $2
  AND access_level <= $3
ORDER BY embedding <=> $1::vector
LIMIT $4;
```

## 3.11 Document Storage

**Responsibility**: Storage and retrieval of enterprise document files.

**Initial Implementation**: Local filesystem volume (mounted in Docker).

**Document Metadata Table** (PostgreSQL):
```sql
CREATE TABLE documents (
  document_id   UUID PRIMARY KEY,
  title         TEXT NOT NULL,
  filename      TEXT NOT NULL,
  format        VARCHAR(10) NOT NULL,  -- pdf | docx | txt
  access_level  VARCHAR(20) NOT NULL,
  namespace     VARCHAR(50) NOT NULL,
  storage_path  TEXT NOT NULL,
  created_at    TIMESTAMPTZ DEFAULT NOW()
);
```

**Access Control**: Document Agent checks `documents.access_level` against user's authorized ceiling before loading file bytes.

## 3.12 Reviewer / Validation Agent (Component View)

**Position in pipeline**: Always the final step before FinalResponse assembly.
**Invocation**: Orchestrator calls AGT-REV node with the complete WorkflowState.

**Validation Pipeline** (in order):
1. Schema conformance check (Pydantic — deterministic)
2. Completeness check (required fields from ExecutionPlan — deterministic)
3. Policy compliance check (AGT-SEC rule engine — deterministic)
4. Factual grounding check (LLM-assisted, rule-anchored — see AMB-010)
5. Internal consistency check (LLM-assisted)
6. Risk assessment (rule-based risk classification)
7. Recommendation generation (approve | escalate | reject)

## 3.13 Security / Policy Layer (Component View)

**Components**:
- `PolicyEngine`: rule-based evaluation against loaded policy definitions
- `ManifestStore`: in-memory map of agent_id → AgentPermissionManifest (loaded at startup)
- `InputSanitizer`: prompt injection pattern detection for LLM-bound inputs
- `OutputGuardrail`: PII detection, sensitive data detection in outputs

**Enforcement Points** (in request lifecycle):
1. Input sanitization (before Planner LLM call)
2. Agent manifest check (before every tool call via tool interceptor)
3. Output guardrail (before Reviewer receives assembled output)
4. Audit all violations (append-only, never suppressed)

## 3.14 Risk Engine

**Responsibility**: Deterministic risk classification of plan steps and outputs.

**Classification Rules** (loaded from policy config, immutable at runtime):
```
Rule: operation_type IN [INSERT, UPDATE, DELETE] → HIGH
Rule: data_class IN [financial_write, hr_pii] → HIGH
Rule: agent_id = AGT-FIN AND operation = write → HIGH
Rule: data_class IN [financial_read] → MEDIUM
Rule: data_class IN [hr_personal_non_pii] → MEDIUM
Rule: data_class IN [analytics_aggregate, it_ticket_read, hr_headcount] → LOW
Rule: DEFAULT → LOW
```

**Risk Engine is NOT LLM-based.** It is a deterministic rule evaluation engine. LLM risk commentary (from Reviewer) is advisory only and never overrides the rule engine's classification.

## 3.15 Human Approval Mechanism

**Responsibility**: Persistent approval request queue for high-risk and escalated operations.

**Approval Request Schema** (see Section 10 for full detail):
```sql
CREATE TABLE approval_requests (
  approval_id    UUID PRIMARY KEY,
  task_id        UUID NOT NULL,
  step_index     INT,
  approval_type  VARCHAR(20) NOT NULL,  -- risk_gate | reviewer_escalation
  requested_by   UUID NOT NULL,          -- user_id of task submitter
  risk_level     VARCHAR(10) NOT NULL,
  step_summary   TEXT NOT NULL,
  evidence_ref   TEXT,
  risk_factors   JSONB,
  status         VARCHAR(20) DEFAULT 'pending',
  decision       VARCHAR(10),
  decided_by     UUID,
  decision_notes TEXT,
  expires_at     TIMESTAMPTZ NOT NULL,
  created_at     TIMESTAMPTZ DEFAULT NOW(),
  decided_at     TIMESTAMPTZ
);
```

## 3.16 Audit Logging

**Responsibility**: Append-only, immutable record of all auditable system events.

**Audit Log Schema**:
```sql
CREATE TABLE audit_log (
  audit_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  event_type  VARCHAR(50) NOT NULL,
  task_id     UUID,
  request_id  UUID,
  actor_id    UUID,          -- user_id or agent_id or system
  target_id   VARCHAR(100),  -- agent_id, tool_id, resource_id, etc.
  outcome     VARCHAR(20) NOT NULL,  -- success | failure | blocked | denied
  detail      JSONB,
  timestamp   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- No UPDATE or DELETE permissions granted on this table
-- INSERT only via eaoc_audit_append role
```

## 3.17 Provenance Store

**Responsibility**: Immutable provenance chain per task, enabling full trace reconstruction.

**Provenance entries are stored within WorkflowState** (PostgreSQL JSON column) and are also written to a dedicated `provenance_chain` table for efficient querying:

```sql
CREATE TABLE provenance_chain (
  provenance_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id       UUID NOT NULL,
  step_index    INT NOT NULL,
  agent_id      VARCHAR(20) NOT NULL,
  tool_used     VARCHAR(50),
  data_source   VARCHAR(100),
  action_summary TEXT NOT NULL,
  input_ref     TEXT,
  result_ref    TEXT,
  timestamp     TIMESTAMPTZ NOT NULL,
  status        VARCHAR(20) NOT NULL
);
```

## 3.18 Observability Components

- **Structured Logger**: Python `structlog`; JSON format; all entries include `task_id`, `request_id`, `agent_id`, `timestamp`, `level`.
- **Metrics Collector**: `prometheus_client`; counters and histograms per agent, tool, and endpoint.
- **Health Checker**: Background coroutine; polls DB, Redis, LLM every 10 seconds; sets health flags.
- **Trace Correlator**: `request_id` and `task_id` propagated as structlog context vars across all calls.

## 3.19 Failure / Recovery Subsystem

**Components**:
- `RetryPolicy`: configurable max_retries, backoff_factor; per-failure-class policy
- `ReplanTrigger`: invoked by Orchestrator when a required step permanently fails
- `CircuitBreaker`: per-dependency (LLM, PostgreSQL, Redis); opens after N consecutive failures
- `RecoveryManager`: on Orchestrator startup, scans PostgreSQL for in-progress tasks; resumes from last checkpoint

---

# SECTION 4 — AGENT COMMUNICATION

## 4.1 Communication Model

**Agents do NOT communicate directly with each other.**

All inter-agent communication is mediated by the Orchestrator via the LangGraph state machine. There is no unrestricted agent-to-agent messaging bus.

**Delegation pattern** (HR→RAG, FIN→SQL, etc.): A domain agent (e.g. HR Agent) calls a lower-level service agent (e.g. SQL Agent) as a tool call — not as a peer message. The call passes through the tool interception layer and produces a logged, permission-checked, schema-validated result.

## 4.2 Task Input Schema (AgentTaskInput)

```python
AgentTaskInput:
  task_id: str            # workflow correlation ID
  step_index: int         # position in ExecutionPlan
  agent_id: str           # target agent ID
  operation: str          # operation type (agent-specific)
  inputs: dict            # step-specific input parameters
  permitted_tools: list[str]
  permitted_schemas: list[str]
  permitted_namespaces: list[str]
  user_context: UserContext
  context_summary: str    # summarized prior step context (token-managed)
  full_context_ref: str   # reference to full WorkflowContext in state store
```

## 4.3 Agent Result Schema (AgentResult)

```python
AgentResult:
  task_id: str
  step_index: int
  agent_id: str
  status: "completed" | "failed" | "partial"
  output: dict              # agent-specific structured output
  provenance: ProvenanceEntry
  evidence_refs: list[str]  # references to source documents, SQL results, etc.
  warnings: list[str]       # non-fatal issues
  error: ErrorDetail | None
  completed_at: datetime
```

## 4.4 Error Schema (ErrorDetail)

```python
ErrorDetail:
  error_code: str       # machine-readable code (e.g. llm_timeout, schema_violation)
  error_message: str    # human-readable description (sanitized; no internal details)
  failure_class: str    # timeout | llm_failure | tool_error | schema_violation | permission_denied
  retryable: bool
  step_index: int
  agent_id: str
  timestamp: datetime
```

## 4.5 Evidence Schema (EvidenceRecord)

```python
EvidenceRecord:
  evidence_id: str
  evidence_type: str    # sql_result | rag_chunk | document_extract | computation_output
  source_ref: str       # table/schema, document_id, chunk_id, etc.
  summary: str          # brief description of what was retrieved
  data_hash: str        # SHA-256 of raw data (for integrity verification)
  created_at: datetime
  agent_id: str
  tool_id: str
```

EvidenceRecords are stored in the WorkflowContext and referenced by ProvenanceEntries.

## 4.6 Provenance Metadata Schema (ProvenanceEntry)

```python
ProvenanceEntry:
  provenance_id: str
  task_id: str
  step_index: int
  agent_id: str
  tool_used: str | None
  data_source: str | None   # schema, document_id, namespace, etc.
  action_summary: str
  input_ref: str | None     # hash or reference to input data
  result_ref: str           # reference to AgentResult in WorkflowContext
  evidence_ids: list[str]   # linked EvidenceRecords
  timestamp: datetime
  status: "completed" | "failed" | "skipped"
```

## 4.7 Workflow / Correlation IDs

| ID | Scope | Format | Generated By |
|---|---|---|---|
| `task_id` | Full task lifecycle | UUID v4 | API Layer on task creation |
| `request_id` | Single HTTP request | UUID v4 | RequestID Middleware |
| `plan_id` | Single plan generation | UUID v4 | Planner |
| `step_id` | Single agent step | `{task_id}-step-{index}` | Orchestrator |
| `tool_call_id` | Single tool invocation | UUID v4 | Tool Interceptor |
| `review_id` | Single review operation | UUID v4 | Reviewer Agent |
| `approval_id` | Single approval request | UUID v4 | Orchestrator |
| `audit_id` | Single audit entry | UUID v4 | Audit Logger |

All IDs propagated through structlog context variables; present in every log entry within their scope.

## 4.8 Context Passing Strategy

**Context size management** (FR-STATE-002):
- Full WorkflowState stored in PostgreSQL (source of truth)
- Each agent node receives:
  - Its specific `AgentTaskInput` (targeted, minimal)
  - A `context_summary` string (summarized prior step outputs, token-limited)
  - A `full_context_ref` pointing to the full WorkflowState in the state store
- If context_summary would exceed the configured token budget:
  - Orchestrator invokes LLM to summarize earlier steps
  - Summarization event recorded in provenance chain
  - Full context still accessible via full_context_ref

## 4.9 Orchestrator Execution Control

The Orchestrator controls the execution graph through LangGraph conditional edges:

```
After each step node:
  ├── step.status == completed AND more steps → route to next execute_step node
  ├── step.status == completed AND no more steps → route to run_reviewer
  ├── step.status == failed AND retryable AND retry_count < max_retries → route to retry_step
  ├── step.status == failed AND not retryable AND step.optional → route to next execute_step (skipped)
  ├── step.status == failed AND not retryable AND not optional AND replan_count < max → route to replan
  └── step.status == failed (exhausted) → route to fail_task

After reviewer:
  ├── review.recommendation == approve → route to assemble_response
  ├── review.recommendation == escalate → route to create_approval (human escalation)
  └── review.recommendation == reject → route to replan OR fail_task
```

---

# SECTION 5 — STATE MANAGEMENT

## 5.1 State Taxonomy

| State Type | Location | Persistence | TTL / Retention |
|---|---|---|---|
| Workflow State (WorkflowContext) | Redis (cache) + PostgreSQL (durable) | Dual-write | Redis: 24h TTL; PG: permanent |
| Task Status | Redis (cache) + PostgreSQL | Dual-write | Same as workflow state |
| LangGraph Checkpoint | PostgreSQL `langgraph_checkpoints` table | Durable | Permanent (until purged by admin) |
| Approval State | PostgreSQL `approval_requests` table | Durable | Permanent |
| Retry State | WorkflowContext (in-memory + PG) | Part of workflow state | Per-task |
| Failure State | WorkflowContext (in-memory + PG) | Part of workflow state | Per-task |
| Audit State | PostgreSQL `audit_log` table | Durable, append-only | 90+ day retention (NFR-AUDIT-002) |
| Provenance Chain | WorkflowContext JSON + `provenance_chain` table | Durable | Permanent |
| Auth Token Revocation | Redis set | Redis | Per-token TTL |
| LLM/DB Circuit State | In-memory (per process) | Not persisted | Resets on restart |

## 5.2 Redis Usage

**What belongs in Redis**:
- Task status cache (fast lookup for polling)
- WorkflowContext cache (avoids PostgreSQL reads on every poll)
- LangGraph checkpoint cache (fast resume)
- Task queue (pending tasks awaiting Orchestrator pickup)
- Token revocation set
- Brute-force lockout counters
- Circuit breaker counters (in-memory fallback; Redis preferred)

**What does NOT belong in Redis**:
- Long-term audit log entries (PostgreSQL only)
- Provenance chain (PostgreSQL only — must be durable)
- Approval requests (PostgreSQL only — legally significant)
- User and role definitions (PostgreSQL only)

## 5.3 PostgreSQL Usage

**What belongs in PostgreSQL**:
- All durable business state (workflow_state, approval_requests, provenance_chain)
- Audit log (append-only, 90+ day retention)
- LangGraph checkpoints (recovery source of truth)
- User store, RBAC role definitions
- Document metadata
- pgvector document chunks
- Tool registry (if DB-backed; alternatively config file)
- Agent permission manifests (if DB-backed; alternatively config file)

## 5.4 Task Lifecycle State Transitions

```
accepted
  → planning        (Orchestrator initialised WorkflowContext)
  → running         (first step dispatched)
  → awaiting_approval  (high-risk step or Reviewer escalation)
  → running         (approval received → resume)
  → completed       (FinalResponse assembled and stored)
  → partial_result  (completed but with skipped optional steps)
  → failed          (unrecoverable failure)
  → degraded        (running but state persistence issues)
```

## 5.5 Approval State Transitions

```
pending
  → approved  (human submits approve decision before deadline)
  → rejected  (human submits reject decision before deadline)
  → expired   (deadline elapsed without decision → task fails)
  → cancelled (task failed before approval was decided)
```

## 5.6 Retry State

Stored within WorkflowContext (not a separate table):
```python
retry_counts: dict[int, int]   # step_index → retry_attempt_count
replan_count: int               # number of replanning cycles for this task
last_failure: dict | None       # most recent StepFailureEvent
```

Retry state is persisted with every WorkflowContext checkpoint write.
