# EAOC — Detailed System Architecture Specification
# Part 0.3-B: Sections 6–10
# Phase 0 · Part 0.3 · Version 1.0

---

# SECTION 6 — DATA ARCHITECTURE

## 6.1 Data Store Overview

| Store | Technology | Purpose | Access Pattern |
|---|---|---|---|
| Relational DB | PostgreSQL 15+ | Business data, state, audit, provenance | Read-heavy agents; write by Orchestrator/Audit |
| Vector Store | pgvector (PG extension) | Document chunk embeddings for semantic search | Read by RAG; write by Ingestion Pipeline |
| State Cache | Redis 7+ | Fast task state, queue, revocation | Read/write by Orchestrator; TTL-managed |
| Document Files | Filesystem volume (Docker) | Binary document files | Read by Document Agent |
| Optional Vector Store | Qdrant | Scale-out vector search if needed | OPEN — see ARCH-DECISION-PENDING-001 |

## 6.2 PostgreSQL Schema Layout

```
PostgreSQL
├── public schema
│   ├── users
│   ├── rbac_roles
│   ├── rbac_role_permissions
│   ├── workflow_state
│   ├── langgraph_checkpoints
│   ├── approval_requests
│   ├── audit_log
│   ├── provenance_chain
│   ├── document_chunks  (pgvector)
│   └── documents
├── finance schema
│   ├── invoices
│   ├── budgets
│   ├── expenses
│   └── reports
├── hr schema
│   ├── employees
│   ├── departments
│   └── org_chart
├── it schema
│   ├── tickets
│   ├── ticket_categories
│   └── sla_definitions
└── analytics schema
    ├── sales
    ├── performance
    └── kpi
```

## 6.3 Key Table Definitions

### workflow_state
```sql
CREATE TABLE workflow_state (
  task_id         UUID PRIMARY KEY,
  user_id         UUID NOT NULL,
  status          VARCHAR(30) NOT NULL,
  task_description TEXT NOT NULL,
  execution_plan  JSONB,
  step_results    JSONB DEFAULT '{}',
  provenance_ref  JSONB DEFAULT '[]',
  retry_counts    JSONB DEFAULT '{}',
  replan_count    INT DEFAULT 0,
  final_response  JSONB,
  error_detail    JSONB,
  created_at      TIMESTAMPTZ DEFAULT NOW(),
  updated_at      TIMESTAMPTZ DEFAULT NOW()
);
```

### approval_requests
```sql
CREATE TABLE approval_requests (
  approval_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id        UUID NOT NULL REFERENCES workflow_state(task_id),
  step_index     INT,
  approval_type  VARCHAR(20) NOT NULL,
  requested_by   UUID NOT NULL,
  risk_level     VARCHAR(10) NOT NULL,
  step_summary   TEXT NOT NULL,
  evidence_ref   JSONB,
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

### audit_log
```sql
CREATE TABLE audit_log (
  audit_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  event_type  VARCHAR(60) NOT NULL,
  task_id     UUID,
  request_id  UUID,
  actor_id    VARCHAR(100),
  target_id   VARCHAR(100),
  outcome     VARCHAR(20) NOT NULL,
  detail      JSONB DEFAULT '{}',
  timestamp   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- Immutable: no UPDATE/DELETE grants; only eaoc_audit_append INSERT role
```

## 6.4 Synthetic Enterprise Datasets

Each schema will be populated with realistic synthetic data:

| Schema | Tables | Volume Target | Generator |
|---|---|---|---|
| finance | invoices, budgets, expenses | ~5,000 invoices, ~500 budget records | Python Faker + custom seeded generator |
| hr | employees, departments, org_chart | ~500 employees, ~30 departments | Python Faker + org hierarchy generator |
| it | tickets, categories | ~2,000 tickets, ~20 categories | Python Faker + ticket status simulator |
| analytics | sales, performance, kpi | ~10,000 sales records | Python Faker + time-series generator |
| pgvector | document_chunks | ~5,000-20,000 chunks | Document Ingestion Pipeline |

All generators use a fixed random seed (NFR-REPRO-001) for reproducibility.

## 6.5 Redis Data Layout

```
Redis key namespaces:
  task:{task_id}:status          → string (task status, TTL 24h)
  task:{task_id}:context         → JSON string (WorkflowContext cache, TTL 24h)
  task:queue                     → Redis List (pending task_ids)
  token:revoked:{jti}            → string "1" (token revocation, TTL = token expiry)
  auth:lockout:{username}        → string lockout_until timestamp (TTL per lockout policy)
  circuit:{dependency}:failures  → int counter (TTL reset on success)
  health:llm_available           → string "true" | "false"
```

## 6.6 pgvector to Qdrant Migration

**ARCH-DECISION-PENDING-001** (AMB-004 from Part 0.1):

The trigger condition for migrating from pgvector to Qdrant has NOT been defined. This document records the migration path as architectural intent without deciding the trigger.

**Intended migration path** (if triggered):
1. Deploy Qdrant as a separate Docker service.
2. Run dual-write mode: Ingestion Pipeline writes to both pgvector and Qdrant.
3. Validate retrieval quality parity between pgvector and Qdrant on the test corpus.
4. Switch RAG Agent `SearchService` implementation from pgvector to Qdrant client.
5. Disable pgvector writes; retain read access for historical queries.
6. Decommission pgvector index after validation.

The `SearchService` interface is designed to be backend-agnostic; the implementation is injected via dependency injection.

---

# SECTION 7 — TOOL ARCHITECTURE

## 7.1 Tool Registry

The Tool Registry is loaded at system startup from a YAML configuration file (or database table). It is immutable at runtime.

**Tool Registry YAML Structure**:
```yaml
tools:
  - tool_id: pgvector_search
    name: "pgvector Semantic Search"
    description: "Performs cosine similarity search in pgvector"
    permitted_agents: [AGT-RAG]
    input_schema: PgvectorSearchInput
    output_schema: PgvectorSearchOutput
    timeout_seconds: 10
    risk_level: low
    external_boundary: false

  - tool_id: postgresql_read
    name: "PostgreSQL Read-Only Query"
    description: "Executes a validated SELECT statement on PostgreSQL"
    permitted_agents: [AGT-SQL]
    input_schema: PostgresqlReadInput
    output_schema: PostgresqlReadOutput
    timeout_seconds: 15
    risk_level: low
    external_boundary: false

  - tool_id: llm_inference
    name: "LLM Inference"
    description: "Calls the local LLM runtime for text generation"
    permitted_agents: [AGT-PLAN, AGT-RAG, AGT-SQL, AGT-DOC, AGT-ANA, AGT-FIN, AGT-HR, AGT-IT, AGT-DEV, AGT-REV]
    input_schema: LLMInferenceInput
    output_schema: LLMInferenceOutput
    timeout_seconds: 120
    risk_level: low
    external_boundary: true

  - tool_id: sandbox_execution
    name: "Sandboxed Code Execution"
    description: "Executes code in an isolated container sandbox"
    permitted_agents: [AGT-DEV]
    input_schema: SandboxExecutionInput
    output_schema: SandboxExecutionOutput
    timeout_seconds: 60
    risk_level: medium
    external_boundary: false

  - tool_id: document_retrieve
    name: "Document Store Retrieval"
    description: "Retrieves a document file from the enterprise document store"
    permitted_agents: [AGT-DOC]
    input_schema: DocumentRetrieveInput
    output_schema: DocumentRetrieveOutput
    timeout_seconds: 5
    risk_level: low
    external_boundary: false
```

## 7.2 Tool Execution Pipeline (Detailed)

```
Agent Node
  │
  ▼
ToolInterceptor.call(agent_id, tool_id, params)
  │
  ├─ 1. Registry Lookup
  │     Tool not found → ToolNotFoundError (logged, returned to agent)
  │
  ├─ 2. Permission Check (AGT-SEC.check_manifest)
  │     agent_id not in tool.permitted_agents → ToolPermissionDenied
  │     → Audit: TOOL_PERMISSION_DENIED
  │     → Returned to agent; agent returns ErrorDetail to Orchestrator
  │
  ├─ 3. Input Validation (Pydantic against tool.input_schema)
  │     Validation failure → ToolInputValidationError
  │     → Audit: TOOL_INPUT_INVALID
  │
  ├─ 4. Audit: TOOL_CALL_ATTEMPTED (task_id, agent_id, tool_id, timestamp)
  │
  ├─ 5. Execute Tool Function (with timeout enforcement)
  │     Timeout → ToolTimeoutError → classify as transient failure
  │     Exception → ToolExecutionError → classify by type
  │
  ├─ 6. Output Validation (Pydantic against tool.output_schema)
  │     Validation failure → ToolOutputSchemaError
  │
  ├─ 7. Audit: TOOL_CALL_COMPLETED | TOOL_CALL_FAILED
  │
  └─ 8. Return ToolResult to agent
```

## 7.3 MCP (Model Context Protocol) Integration

MCP is used **selectively** for tools where the MCP interface provides value (e.g. connecting to external enterprise systems or third-party data sources via MCP servers). MCP is NOT used as the universal tool interface.

**Architecture rule**: Internal EAOC tools (pgvector search, PostgreSQL query, document retrieval) use native Python tool implementations. External integrations (where an MCP server already exists) may use MCP client calls.

**MCP boundary**: An MCP client tool wrapper is registered in the Tool Registry as a standard tool entry. The ToolInterceptor treats it identically to any other tool (same permission check, same input/output validation, same audit logging). The MCP protocol is an implementation detail of that tool's execution, not a privileged bypass.

**External API boundaries**: All tools with `external_boundary: true` are subject to additional circuit breaker protection and enhanced audit logging.

## 7.4 Tool Timeout and Error Handling

| Tool | Timeout | On Timeout | Retryable |
|---|---|---|---|
| llm_inference | 120s | ToolTimeoutError → llm_failure class | Yes (max 2) |
| pgvector_search | 10s | ToolTimeoutError → tool_error class | Yes (max 1) |
| postgresql_read | 15s | ToolTimeoutError → database_unavailable | Yes (max 1) |
| document_retrieve | 5s | ToolTimeoutError → tool_error | Yes (max 1) |
| sandbox_execution | 60s | ToolTimeoutError → sandbox_timeout | No |
| embedding_generate | 30s | ToolTimeoutError → embedding_unavailable | Yes (max 1) |

---

# SECTION 8 — SECURITY ARCHITECTURE

## 8.1 Security Layers (Distinct Responsibilities)

```
AUTHENTICATION  ≠  AUTHORIZATION  ≠  POLICY EVALUATION  ≠  RISK ASSESSMENT

Authentication:       "Who are you?" — JWT validation (Auth Module)
Authorization:        "Are you allowed to do this?" — RBAC role-to-permission check
Policy Evaluation:    "Does this action comply with enterprise policy?" — AGT-SEC rule engine
Risk Assessment:      "How risky is this action?" — Risk Engine (deterministic rules)
```

These four functions are implemented in separate components and are never conflated.

## 8.2 Authentication Architecture

**Token flow**:
```
Client → POST /auth/token (username, password)
  → Auth Module validates credentials (bcrypt compare)
  → Issues JWT: { sub, roles, jti, iat, exp }
  → Client stores JWT locally

Client → POST /api/v1/tasks (Authorization: Bearer <jwt>)
  → Auth Middleware: decode JWT → verify signature → check expiry → check revocation (Redis)
  → Attach UserContext to request state
  → Proceed to RBAC Middleware
```

**Service-to-service**: Orchestrator and agent nodes communicate within the same process (LangGraph). Cross-process calls (if any) use internal service tokens loaded from environment variables.

## 8.3 RBAC Authorization Architecture

**Decision hierarchy**:
```
1. User-level RBAC (can this user's roles invoke this agent?)
   → Checked by Orchestrator before every agent dispatch
   → Enforced by RBAC Module; fail-closed

2. Agent-level permission (is this tool in this agent's manifest?)
   → Checked by ToolInterceptor via AGT-SEC for every tool call
   → Enforced by Security/Policy Agent; fail-closed

3. Data-level permission (can this agent access this schema/namespace/access_level?)
   → Enforced at connection/query level (PostgreSQL roles, namespace filters)
   → Cannot be bypassed by agent logic
```

## 8.4 Agent Permission Manifest (Runtime Enforcement)

```yaml
# Example: AGT-HR manifest
agent_id: AGT-HR
permitted_tools:
  - llm_inference
  - audit_logger
permitted_delegation_targets:
  - AGT-SQL  # can delegate to SQL Agent
  - AGT-RAG  # can delegate to RAG Agent
permitted_schemas: []           # no direct DB access
permitted_namespaces: [hr_policy]
max_data_sensitivity: internal
write_permitted: false
```

Manifests are YAML files, loaded into `ManifestStore` at startup. Changes require service restart.

## 8.5 Secret Handling

| Secret | Storage | Access |
|---|---|---|
| JWT signing secret | Environment variable | Auth Module only |
| PostgreSQL passwords | Environment variable | SQLAlchemy engine only |
| Redis password | Environment variable | Redis client only |
| LLM API key (if cloud) | Environment variable | LLM inference tool only |
| Service tokens | Environment variable | Internal auth only |

**Rules**:
- Secrets never committed to version control (`.env` in `.gitignore`).
- Secrets never logged (structlog masking applied to sensitive keys).
- Secrets never appear in audit log detail fields.
- Docker Compose uses `env_file` or `secrets` mechanism for secret injection.

## 8.6 Security Violation Handling

```
Violation detected (by AGT-SEC or any middleware):
  → Block the action immediately
  → Write POLICY_VIOLATION_DETECTED to audit_log (always, even on system degradation)
  → Return PolicyCheckResult(allowed=false) to the caller
  → Caller (Orchestrator or Tool Interceptor) handles the failure:
      - For tool call: ToolPermissionDenied → agent receives ErrorDetail
      - For agent dispatch: RBAC denial → step fails → task fails
  → No retry on permission failures (non-retryable by definition)
  → No human escalation on policy violations (block is the response)
```

## 8.7 Input Sanitization

Applied at two points:
1. **Pre-Planner**: task_description sanitized before inclusion in LLM prompt (prompt injection patterns stripped).
2. **Pre-SQL**: query_goal sanitized before inclusion in SQL generation prompt.

Sanitization does NOT alter the user's intent — it removes control characters, prompt injection patterns (e.g. "Ignore previous instructions"), and oversized inputs. Rejected inputs return `HTTP 400` with reason `input_sanitization_failed`.

---

# SECTION 9 — RISK-AWARE EXECUTION ARCHITECTURE

## 9.1 Risk Classification Engine

**Type**: Deterministic rule engine (NOT LLM-based).
**Location**: Loaded at Planner time and enforced at Orchestrator step execution time.

**Rule Evaluation Order** (first match wins):
```
1. operation_type IN [write, delete, modify, deploy] → HIGH
2. data_class = financial_write → HIGH
3. data_class = hr_pii → HIGH
4. data_class = restricted_document → HIGH
5. agent_id = AGT-DEV AND operation = sandbox_execution → MEDIUM
6. data_class IN [financial_read, hr_personal_non_pii, cross_domain] → MEDIUM
7. agent_id IN [AGT-FIN, AGT-HR] AND operation = read → MEDIUM
8. DEFAULT → LOW
```

**LLM advisory commentary** from Reviewer is recorded in ReviewResult.risk_assessment but NEVER used to override the deterministic classification. If Reviewer LLM says "low risk" but the rule engine says "high", the rule engine governs.

## 9.2 Risk Gate Architecture

```
Orchestrator: about to execute step[i]
  → RiskGate.evaluate(step[i].risk_level, step[i].agent_id, step[i].operation)
     ├── LOW:
     │   → Proceed immediately
     │   → Audit: RISK_GATE_PASSED (level=low)
     │
     ├── MEDIUM:
     │   → Proceed
     │   → Audit: RISK_GATE_PASSED (level=medium, enhanced_logging=true)
     │   → Reviewer check is mandatory (already in pipeline)
     │
     └── HIGH:
         → Pause execution
         → Create HumanApprovalRequest
         → Update task status = awaiting_approval
         → Audit: RISK_GATE_TRIGGERED (level=high, approval_id)
         → Await human decision (see Section 10)
```

## 9.3 Destructive Operations

No agent currently has write permission on enterprise data. This is an architectural default (FR-DATA-001).

If write operations are introduced in future phases:
- They must be explicitly added to the relevant agent's permission manifest.
- All write operations are classified as HIGH risk by the risk engine (rule 1 above).
- All write operations require human approval.
- All write operations require enhanced provenance recording.
- Write operations are recorded in a separate `write_audit_log` table.

This constraint is enforced architecturally (read-only PostgreSQL roles for all agent connections) not just by policy.

---

# SECTION 10 — HUMAN-IN-THE-LOOP ARCHITECTURE

## 10.1 Approval Queue Backend

The approval queue is a PostgreSQL-backed persistent store (table: `approval_requests`).

**Architecture**: No separate queue service is introduced. The `approval_requests` table serves as the queue. The Orchestrator polls this table for decisions on tasks it has paused.

**Approval request lifecycle**:
```
Orchestrator creates approval_request (status=pending, expires_at=NOW()+deadline)
  → Task status = awaiting_approval
  → Orchestrator pauses (LangGraph waits on conditional edge)

Approver polls GET /api/v1/approvals → sees pending requests
Approver submits POST /api/v1/approvals/{approval_id}/decision

Decision handler:
  → Validates: approver has approval role, approval_id exists, status=pending, not expired
  → Updates: status, decision, decided_by, decision_notes, decided_at
  → Signals Orchestrator (via Redis pub/sub or polling)

Orchestrator resumes:
  → decision=approve: resume step execution → continue workflow
  → decision=reject: fail task with reason=human_approval_rejected
```

## 10.2 Approval Request Fields

```python
ApprovalRequest:
  approval_id: UUID
  task_id: UUID
  step_index: int | None
  approval_type: "risk_gate" | "reviewer_escalation"
  requested_by: UUID           # user_id of task submitter
  risk_level: "high" | "medium"
  step_summary: str            # human-readable description of what needs approval
  evidence_ref: list[str]      # references to evidence records
  risk_factors: list[str]      # list of identified risk factors
  status: "pending" | "approved" | "rejected" | "expired" | "cancelled"
  decision: "approve" | "reject" | None
  decided_by: UUID | None      # user_id of approver
  decision_notes: str | None
  expires_at: datetime         # deadline for human decision
  created_at: datetime
  decided_at: datetime | None
```

## 10.3 Approval Notification (OPEN — AMB-001)

The mechanism for notifying human approvers of pending requests has NOT been resolved. The architecture defines the backend queue and API endpoint. The notification channel (polling vs. webhook vs. email vs. messaging system) remains an open decision.

The frontend approval UI is planned for Phase 8. For Phase 4 testing, approvers will use the `/api/v1/approvals` polling endpoint.

**See**: AMB-001 (Part 0.1), recorded explicitly as ARCH-OPEN-001 below.

## 10.4 Timeout Handling

```
Background scheduler (runs every 60 seconds):
  → SELECT * FROM approval_requests WHERE status='pending' AND expires_at < NOW()
  → For each expired approval:
      → UPDATE status='expired'
      → Signal Orchestrator via Redis pub/sub: { approval_id, event: 'expired' }
      → Orchestrator: task fails with reason=approval_timeout
      → Audit: APPROVAL_EXPIRED
  → NEVER auto-approve on timeout (architectural invariant)
```

## 10.5 Approval Audit Events

| Event | Trigger |
|---|---|
| APPROVAL_CREATED | HumanApprovalRequest created |
| APPROVAL_VIEWED | Approver reads approval detail (GET /approvals/{id}) |
| APPROVAL_DECIDED | Human submits approve or reject |
| APPROVAL_EXPIRED | Deadline elapsed without decision |
| APPROVAL_CANCELLED | Task failed before approval completed |
