# EAOC — Functional & Non-Functional Requirements Specification
# Part B: Sections 14-26, NFRs, Summary, Traceability, Checklist
# Phase 0 · Part 0.1 · Version 1.0
# Status: DRAFT — Awaiting Phase 0.1 approval

---

# SECTION 14 — FINANCE / HR / IT WORKFLOWS

## FR-FIN-001 · Invoice Lifecycle Query
- **ID**: FR-FIN-001
- **Name**: Invoice Lifecycle Query
- **Description**: Finance Agent must retrieve invoice records by ID, vendor, date range, or status and return full invoice data with provenance.
- **Actor/Component**: Finance Agent
- **Preconditions**: Finance schema populated; user has finance_user or admin role
- **Inputs**: { "entity_type": "invoice", "filters": { "invoice_id" | "vendor" | "date_range" | "status" } }
- **Expected Behavior**: Agent queries finance.invoices table using filters. Returns matching records with all fields plus provenance (source table, query timestamp, agent_id).
- **Outputs**: { "records": [ { invoice fields } ], "provenance": { "source", "agent_id", "timestamp" } }
- **Failure Conditions**: No matching invoices - returns empty list with metadata; DB unavailable - structured error
- **Security Considerations**: All finance queries recorded in audit log. PII fields (e.g. vendor contact) subject to role check.
- **Acceptance Criteria**: Query by invoice_id returns exact match. Query by date range returns all invoices in range. All results include provenance block.

## FR-FIN-002 · Budget Query
- **ID**: FR-FIN-002
- **Name**: Budget Query
- **Description**: Finance Agent retrieves budget allocations and actuals by department or project for a specified period.
- **Actor/Component**: Finance Agent
- **Preconditions**: Finance schema populated; finance_user or admin role
- **Inputs**: { "entity_type": "budget", "filters": { "department" | "project" | "period" } }
- **Expected Behavior**: Agent queries finance.budgets and finance.actuals. Returns allocation vs. actual spend with variance.
- **Outputs**: { "department", "period", "allocated", "actual", "variance", "provenance" }
- **Failure Conditions**: No budget data for period - returns empty with note; DB error - structured error
- **Security Considerations**: Budget data restricted to finance_user and admin.
- **Acceptance Criteria**: Query for Dept A Q3 budget returns allocated, actual, and variance. Variance is computed correctly.

## FR-HR-001 · Headcount Query
- **ID**: FR-HR-001
- **Name**: Headcount Query
- **Description**: HR Agent retrieves employee headcount aggregated by department, location, or employment type. Returns aggregate counts, not individual PII records by default.
- **Actor/Component**: HR Agent
- **Preconditions**: HR schema populated; hr_user or admin role
- **Inputs**: { "query_type": "headcount", "group_by": "department" | "location" | "employment_type" }
- **Expected Behavior**: Agent executes aggregate SQL query on hr.employees. Returns count per group. No individual employee records returned unless PII permission held.
- **Outputs**: { "groups": [ { "label", "count" } ], "total", "as_of_date" }
- **Failure Conditions**: HR DB unavailable; invalid group_by field
- **Security Considerations**: Individual employee records never returned to non-PII roles. PII fields always redacted by default.
- **Acceptance Criteria**: Headcount by department returns list of { department, count }. No individual names or IDs in result for non-PII roles.

## FR-HR-002 · HR Policy Lookup
- **ID**: FR-HR-002
- **Name**: HR Policy Lookup
- **Description**: HR Agent retrieves relevant HR policy documents or excerpts for a given policy topic, using the RAG Agent for semantic retrieval from the policy knowledge base.
- **Actor/Component**: HR Agent, Research/RAG Agent
- **Preconditions**: HR policy documents ingested into pgvector; hr_user or admin role
- **Inputs**: { "query_type": "policy", "topic": string }
- **Expected Behavior**: HR Agent delegates retrieval to RAG Agent with the policy topic query. RAG Agent returns relevant policy excerpts. HR Agent formats and returns result with citations.
- **Outputs**: { "policy_excerpts": [ { "title", "excerpt", "document_id" } ] }
- **Failure Conditions**: No relevant policy found - returns structured no_results; RAG Agent unavailable
- **Security Considerations**: Policy documents marked with access level; retrieval respects access level.
- **Acceptance Criteria**: Query "remote work policy" returns at least one relevant policy excerpt with document citation.

## FR-IT-001 · IT Ticket Query
- **ID**: FR-IT-001
- **Name**: IT Ticket Query
- **Description**: IT/Support Agent queries IT tickets by status, priority, assignee, or category and returns matching tickets with full detail.
- **Actor/Component**: IT/Support Agent
- **Preconditions**: IT ticket schema populated; it_user or admin role
- **Inputs**: { "operation": "query_tickets", "filters": { "status" | "priority" | "assignee" | "category" } }
- **Expected Behavior**: Agent queries it.tickets table using filters. Returns matching tickets with all non-sensitive fields.
- **Outputs**: { "tickets": [ { ticket fields } ], "count", "filters_applied" }
- **Failure Conditions**: IT DB unavailable; invalid filter field - structured error
- **Security Considerations**: IT ticket data limited to it_user and admin. Infra details (IP addresses, credentials) never returned to lower roles.
- **Acceptance Criteria**: Query priority=P1 status=open returns all matching tickets. Count matches length of returned list.

## FR-IT-002 · Resolution Recommendation
- **ID**: FR-IT-002
- **Name**: Resolution Recommendation
- **Description**: IT/Support Agent generates a resolution recommendation for a given IT ticket using RAG Agent to search the IT knowledge base.
- **Actor/Component**: IT/Support Agent, Research/RAG Agent
- **Preconditions**: IT knowledge base ingested into pgvector; ticket detail available
- **Inputs**: { "operation": "recommend_resolution", "ticket_id": string }
- **Expected Behavior**: Agent retrieves ticket detail, constructs query from ticket description, delegates to RAG Agent, formats recommendation with steps and knowledge base citations.
- **Outputs**: { "ticket_id", "recommendation": string, "steps": [...], "sources": [...] }
- **Failure Conditions**: Ticket not found; no relevant knowledge base articles found
- **Security Considerations**: Recommendations must not expose infrastructure passwords or secrets.
- **Acceptance Criteria**: Resolution recommendation for a password reset ticket returns step-by-step instructions with KB article citation.

---

# SECTION 15 — REVIEWER / VALIDATION

## FR-REV-001 · Output Completeness Check
- **ID**: FR-REV-001
- **Name**: Output Completeness Check
- **Description**: Reviewer/Validation Agent verifies that all required output fields specified in the ExecutionPlan are present and non-null in the assembled WorkflowContext output.
- **Actor/Component**: Reviewer/Validation Agent
- **Preconditions**: All agent steps complete; output assembled in WorkflowContext
- **Inputs**: Assembled output, required_fields list from ExecutionPlan
- **Expected Behavior**: Reviewer checks each required field. Missing or null fields recorded as findings. If any required field missing, ReviewResult.passed = false.
- **Outputs**: ReviewResult with completeness_check: { "passed": bool, "missing_fields": [...] }
- **Failure Conditions**: required_fields list not defined in plan - reviewer uses default schema for the task type
- **Security Considerations**: Reviewer operates on a read-only view of the output.
- **Acceptance Criteria**: Output missing a required field fails completeness check with that field named in findings. Complete output passes.

## FR-REV-002 · Factual Grounding Check
- **ID**: FR-REV-002
- **Name**: Factual Grounding Check
- **Description**: Reviewer/Validation Agent checks that all numerical claims and data references in the output are traceable to source data (SQL results, retrieved documents) present in the WorkflowContext.
- **Actor/Component**: Reviewer/Validation Agent
- **Preconditions**: Source data available in WorkflowContext alongside output
- **Inputs**: Output text/data, source data records from WorkflowContext
- **Expected Behavior**: Reviewer cross-references numerical values in output against source records. Any figure not traceable to a source is flagged as an ungrounded claim.
- **Outputs**: ReviewResult with grounding_check: { "passed": bool, "ungrounded_claims": [...] }
- **Failure Conditions**: Source data not available in context - reviewer flags as unverifiable, escalates
- **Security Considerations**: Reviewer must not call external APIs to verify claims.
- **Acceptance Criteria**: Output claiming "Revenue Q3: $500K" when source data shows $400K is flagged. Output matching source data passes.

## FR-REV-003 · Risk Assessment
- **ID**: FR-REV-003
- **Name**: Risk Assessment
- **Description**: Reviewer/Validation Agent assesses the risk level of the task output and recommends whether the output should be auto-approved, escalated to human review, or rejected.
- **Actor/Component**: Reviewer/Validation Agent
- **Preconditions**: Output reviewed for completeness and grounding; risk classification criteria loaded
- **Inputs**: ReviewResult so far, task_type, output content, risk_criteria
- **Expected Behavior**: Reviewer applies risk classification rules to produce a risk_level (low | medium | high). Maps risk_level to recommendation: low = approve, medium = escalate, high = reject or escalate.
- **Outputs**: ReviewResult with risk_assessment: { "risk_level": "low" | "medium" | "high", "risk_factors": [...], "recommendation": "approve" | "escalate" | "reject" }
- **Failure Conditions**: Risk criteria not loaded - default to high risk and escalate
- **Security Considerations**: Risk assessment criteria are policy-defined and immutable at runtime.
- **Acceptance Criteria**: Financial write operation classified as high risk. Read-only analytics result classified as low risk. Risk factors enumerated in output.

---

# SECTION 16 — EVIDENCE PROVENANCE

## FR-PROV-001 · Per-Step Evidence Recording
- **ID**: FR-PROV-001
- **Name**: Per-Step Evidence Recording
- **Description**: After every agent step, the system must record a provenance entry in the WorkflowContext capturing: agent_id, tool_used, data_source, query_or_action, result_reference, and timestamp.
- **Actor/Component**: Orchestrator, all agents
- **Preconditions**: Agent step completed (success or failure)
- **Inputs**: Agent step result, agent metadata
- **Expected Behavior**: After each step, Orchestrator appends a ProvenanceEntry to WorkflowContext.provenance_chain. Entry is immutable once written.
- **Outputs**: ProvenanceEntry: { "step_index", "agent_id", "tool_used", "data_source", "action_summary", "result_reference", "timestamp" }
- **Failure Conditions**: Provenance write fails - log error and retry; on persistent failure mark context as provenance_incomplete
- **Security Considerations**: Provenance entries are append-only. No agent may modify a previously written entry.
- **Acceptance Criteria**: Every completed workflow has a provenance_chain with one entry per step. Each entry references the actual data source used.

## FR-PROV-002 · Full Trace Reconstruction
- **ID**: FR-PROV-002
- **Name**: Full Trace Reconstruction
- **Description**: The system must expose an endpoint GET /api/v1/tasks/{task_id}/trace that returns the complete provenance chain for a completed task, enabling full trace reconstruction from final output back to source data.
- **Actor/Component**: FastAPI, Orchestrator, State Store
- **Preconditions**: Task completed; provenance chain persisted
- **Inputs**: task_id, auth token (admin or task-owner role)
- **Expected Behavior**: System retrieves full provenance chain from state store and returns it in chronological order. Each entry links output to the agent, tool, and data source that produced it.
- **Outputs**: { "task_id", "provenance_chain": [ ProvenanceEntry, ... ], "trace_complete": bool }
- **Failure Conditions**: Task not found - 404; unauthorized - 403; provenance_incomplete flag set - returns chain with warning
- **Security Considerations**: Trace endpoint accessible only to admin and task-owner roles.
- **Acceptance Criteria**: Completed 3-step task returns 3 provenance entries. Each entry uniquely identifies agent, tool, and source. trace_complete is true when all steps recorded successfully.

---

# SECTION 17 — RISK CLASSIFICATION

## FR-RISK-001 · Task-Level Risk Classification
- **ID**: FR-RISK-001
- **Name**: Task-Level Risk Classification
- **Description**: The Planner must assign a risk_level (low | medium | high) to each step in the ExecutionPlan based on: operation type, data domain sensitivity, write vs. read, and financial impact potential.
- **Actor/Component**: Planner, Security/Policy Agent
- **Preconditions**: ExecutionPlan being constructed; risk classification rules loaded
- **Inputs**: Step details (agent_id, operation, data_domain, operation_type)
- **Expected Behavior**: Planner applies risk rules to each step. Rules: read-only analytical tasks = low; cross-domain queries = medium; financial writes, HR PII access, policy changes = high. Risk level recorded in step metadata.
- **Outputs**: Each ExecutionPlan step annotated with risk_level and risk_factors
- **Failure Conditions**: Risk rules not loaded - default all steps to high risk
- **Security Considerations**: Risk rules are policy-defined and version-controlled. Not modifiable at runtime.
- **Acceptance Criteria**: SQL SELECT on analytics schema classified as low. Invoice write classified as high. Risk classification present on every plan step.

## FR-RISK-002 · Risk-Based Execution Gating
- **ID**: FR-RISK-002
- **Name**: Risk-Based Execution Gating
- **Description**: Before executing any step classified as high risk, the Orchestrator must pause execution and route the request to the Human Approval workflow (FR-HITL-001). Medium-risk steps may proceed with enhanced logging. Low-risk steps proceed automatically.
- **Actor/Component**: Orchestrator, Risk Gate
- **Preconditions**: ExecutionPlan step annotated with risk_level (FR-RISK-001)
- **Inputs**: Step metadata including risk_level
- **Expected Behavior**: Low risk - step executes automatically. Medium risk - step executes with full audit logging and Reviewer check. High risk - execution paused, task status set to awaiting_approval, human approval requested.
- **Outputs**: Step executed (low/medium) or approval request created (high)
- **Failure Conditions**: Risk gate unavailable - default to high risk gating (fail-safe)
- **Security Considerations**: Risk gate cannot be bypassed by any agent or user without admin override, which itself is logged.
- **Acceptance Criteria**: High-risk step triggers awaiting_approval status. Low-risk step executes without pause. Medium-risk step executes with full audit log entry.

---

# SECTION 18 — HUMAN APPROVAL

## FR-HITL-001 · Human Approval Request Creation
- **ID**: FR-HITL-001
- **Name**: Human Approval Request Creation
- **Description**: When a high-risk step is encountered, system creates a HumanApprovalRequest record with: task_id, step details, risk factors, recommended action, and approval deadline.
- **Actor/Component**: Orchestrator, Approval Queue
- **Preconditions**: High-risk step identified (FR-RISK-002); task status set to awaiting_approval
- **Inputs**: task_id, step_index, risk_assessment, recommended_action, deadline_seconds (configurable)
- **Expected Behavior**: System creates HumanApprovalRequest in PostgreSQL approval_requests table. Task execution is paused. Approval queue is updated.
- **Outputs**: HumanApprovalRequest record; task status = awaiting_approval
- **Failure Conditions**: DB write fails - retry 3x; on failure mark task as failed with reason approval_request_failed
- **Security Considerations**: Approval requests visible only to authorized approvers (admin role or designated approver role).
- **Acceptance Criteria**: High-risk step creates approval request within 1 second. Task status is awaiting_approval. Approval request contains risk factors and step summary.

## FR-HITL-002 · Human Approval API Endpoint
- **ID**: FR-HITL-002
- **Name**: Human Approval API Endpoint
- **Description**: EAOC must expose POST /api/v1/approvals/{approval_id}/decision for an authorized human approver to submit an approve or reject decision with optional notes.
- **Actor/Component**: FastAPI, Approval module
- **Preconditions**: HumanApprovalRequest exists; caller has approver role; deadline not expired
- **Inputs**: approval_id (path), { "decision": "approve" | "reject", "notes": string (optional) }
- **Expected Behavior**: System validates approver role, records decision with approver_id and timestamp, and signals Orchestrator to resume (approve) or terminate (reject) the workflow.
- **Outputs**: { "approval_id", "decision", "decided_by", "decided_at" }; Orchestrator signalled
- **Failure Conditions**: Expired deadline - 409 Conflict; unauthorized approver - 403; approval already decided - 409
- **Security Considerations**: Approver identity recorded immutably. Decision cannot be reversed once submitted.
- **Acceptance Criteria**: Approved decision resumes workflow and completes task. Rejected decision sets task to failed with rejection reason. Approver identity and timestamp recorded in audit log.

## FR-HITL-003 · Approval Deadline Handling
- **ID**: FR-HITL-003
- **Name**: Approval Deadline Handling
- **Description**: If a HumanApprovalRequest reaches its deadline without a decision, the system must automatically reject the high-risk step and set the task to failed with reason approval_timeout.
- **Actor/Component**: Orchestrator, Scheduler
- **Preconditions**: HumanApprovalRequest exists; deadline_seconds elapsed
- **Inputs**: approval_id, elapsed time
- **Expected Behavior**: Scheduler detects expired approval. Records auto-rejection with reason approval_timeout. Task status set to failed. Audit entry created.
- **Outputs**: Task status = failed; audit entry with reason = approval_timeout
- **Failure Conditions**: Scheduler fails to detect expiry - liveness check must detect and handle on next poll
- **Security Considerations**: Auto-rejection is a fail-safe. System must never auto-approve on timeout.
- **Acceptance Criteria**: Approval request not decided within deadline_seconds results in task failed with approval_timeout. No auto-approval on timeout under any circumstance.

---

# SECTION 19 — AUDIT LOGGING

## FR-AUDIT-001 · Immutable Audit Log
- **ID**: FR-AUDIT-001
- **Name**: Immutable Audit Log
- **Description**: Every significant system event must be written to an append-only audit log stored in PostgreSQL. Events include: authentication, task creation, agent invocations, tool calls, permission decisions, approval decisions, and errors.
- **Actor/Component**: Audit module (cross-cutting)
- **Preconditions**: Audit log table exists and is reachable
- **Inputs**: Event type, actor, target, outcome, timestamp, metadata
- **Expected Behavior**: On every auditable event, the audit module writes an AuditEntry to the audit_log table. Entries are never updated or deleted. Write uses an append-only DB role.
- **Outputs**: AuditEntry: { "id", "event_type", "actor_id", "target_id", "outcome", "timestamp", "metadata" }
- **Failure Conditions**: Audit write fails - log to secondary fallback (e.g. file); system continues operating but flags audit_degraded
- **Security Considerations**: Audit log table accessible only via read-only DB role for queries. Write only via append-only role. No agent may read or modify audit entries.
- **Acceptance Criteria**: Every auditable event produces an AuditEntry. Entries cannot be updated or deleted via any API. Audit table is queryable by admin role.

## FR-AUDIT-002 · Audit Query Endpoint
- **ID**: FR-AUDIT-002
- **Name**: Audit Query Endpoint
- **Description**: EAOC exposes GET /api/v1/audit with filter params (event_type, actor_id, task_id, date_range) for admin users to query the audit log.
- **Actor/Component**: FastAPI, Audit module
- **Preconditions**: Caller has admin role; audit log table populated
- **Inputs**: Query params: event_type (optional), actor_id (optional), task_id (optional), from_date (optional), to_date (optional), page, page_size
- **Expected Behavior**: Returns paginated list of AuditEntry records matching filters, ordered by timestamp descending.
- **Outputs**: { "entries": [...], "total", "page", "page_size" }
- **Failure Conditions**: Non-admin caller - 403; DB unavailable - 503
- **Security Considerations**: Only admin role may query audit log. All audit queries are themselves logged.
- **Acceptance Criteria**: Admin querying by task_id returns all audit entries for that task. Non-admin caller returns 403. Query itself appears in audit log.

---

# SECTION 20 — OBSERVABILITY

## FR-OBS-001 · Structured Logging
- **ID**: FR-OBS-001
- **Name**: Structured Logging
- **Description**: All EAOC components must emit structured JSON logs with consistent fields: timestamp, level, component, task_id (if applicable), event, and metadata.
- **Actor/Component**: All components
- **Preconditions**: Logging framework initialized at startup
- **Inputs**: Log event data
- **Expected Behavior**: Every log entry is valid JSON with required fields. Log level is configurable via environment variable. task_id is propagated through all log entries within a workflow.
- **Outputs**: JSON log entries to stdout (captured by Docker/log aggregator)
- **Failure Conditions**: Log write fails - silent failure (logging must not crash the application)
- **Security Considerations**: Logs must not include secrets, tokens, or PII. Sensitive fields masked before logging.
- **Acceptance Criteria**: All log entries are valid JSON. task_id present in all workflow-scoped entries. No secrets appear in logs. Log level configurable without code change.

## FR-OBS-002 · Health Check Endpoint
- **ID**: FR-OBS-002
- **Name**: Health Check Endpoint
- **Description**: EAOC must expose GET /api/v1/health returning system health status including: API service, database connectivity, Redis connectivity, and LLM availability.
- **Actor/Component**: FastAPI, Health module
- **Preconditions**: None (health check must work even when partially degraded)
- **Inputs**: HTTP GET /api/v1/health
- **Expected Behavior**: System checks each dependency. Returns overall status (healthy | degraded | unhealthy) with per-component status.
- **Outputs**: { "status": "healthy" | "degraded" | "unhealthy", "components": { "database", "redis", "llm" } }
- **Failure Conditions**: Any component unhealthy - overall status reflects worst state; health check must never throw unhandled exception
- **Security Considerations**: Health endpoint publicly accessible (no auth required) but must not expose internal IPs, versions, or config details.
- **Acceptance Criteria**: All components healthy - 200 healthy. DB unavailable - 200 with database: unhealthy and overall: degraded. LLM unavailable - 200 with llm: unhealthy.

## FR-OBS-003 · Metrics Endpoint
- **ID**: FR-OBS-003
- **Name**: Metrics Endpoint
- **Description**: EAOC must expose a Prometheus-compatible metrics endpoint at GET /metrics with counters and histograms for: request count, task completion rate, agent invocation count, tool call count, approval count, and error rates.
- **Actor/Component**: FastAPI, Metrics module
- **Preconditions**: Metrics library initialized at startup
- **Inputs**: HTTP GET /metrics
- **Expected Behavior**: Returns Prometheus text format metrics. Counters increment on each event. Histograms track latency distributions per agent.
- **Outputs**: Prometheus text format metrics
- **Failure Conditions**: Metrics library failure - return 503 for /metrics endpoint; core application continues
- **Security Considerations**: Metrics endpoint should be accessible only on internal network or with IP allowlist in production.
- **Acceptance Criteria**: After 10 tasks, task_completed_total counter = 10. Agent latency histogram populated per agent type. Prometheus scrape succeeds without errors.

---

# SECTION 21 — WORKFLOW STATE / MEMORY

## FR-STATE-001 · Task State Persistence
- **ID**: FR-STATE-001
- **Name**: Task State Persistence
- **Description**: Task state (status, WorkflowContext, provenance chain) must be persisted to PostgreSQL so that it survives system restarts. Redis is used as a fast cache; PostgreSQL is the source of truth.
- **Actor/Component**: Orchestrator, State Store (Redis + PostgreSQL)
- **Preconditions**: PostgreSQL and Redis both available at task creation
- **Inputs**: WorkflowContext updates at each step
- **Expected Behavior**: On each state update: write to Redis (fast, TTL-based) AND write to PostgreSQL (durable). On system restart: Orchestrator reads state from PostgreSQL for any in-progress tasks and attempts recovery.
- **Outputs**: Consistent task state in both Redis and PostgreSQL
- **Failure Conditions**: Redis unavailable - write to PostgreSQL only, continue with degraded performance; PostgreSQL unavailable - task cannot be safely persisted, pause and alert
- **Security Considerations**: State data in Redis must have TTL set. Sensitive workflow data encrypted at rest in PostgreSQL.
- **Acceptance Criteria**: System restart mid-task recovers task state from PostgreSQL. Redis miss falls back to PostgreSQL transparently. Completed task state retrievable 24h after completion.

## FR-STATE-002 · Agent Context Window Management
- **ID**: FR-STATE-002
- **Name**: Agent Context Window Management
- **Description**: When an agent receives context from prior steps, the system must manage the context size to fit within the LLM context window. Truncation or summarization must be applied if context exceeds configured limits.
- **Actor/Component**: Orchestrator, all agents
- **Preconditions**: Prior step outputs available in WorkflowContext
- **Inputs**: WorkflowContext step results, configured context_window_limit (tokens)
- **Expected Behavior**: Before passing context to an agent, Orchestrator calculates estimated token count. If over limit: apply summarization of earlier steps (using LLM), pass summarized context + full most-recent step. Record that summarization occurred in provenance.
- **Outputs**: Context within token limit passed to agent; summarization event recorded if applied
- **Failure Conditions**: Summarization fails - pass truncated context with warning in provenance
- **Security Considerations**: Context summarization must not discard security-relevant provenance entries.
- **Acceptance Criteria**: Context exceeding limit triggers summarization. Agent receives context within token limit. Summarization event recorded in provenance chain.

---

# SECTION 22 — FAILURE DETECTION

## FR-FAIL-001 · Agent Step Failure Detection
- **ID**: FR-FAIL-001
- **Name**: Agent Step Failure Detection
- **Description**: The Orchestrator must detect and classify agent step failures into: timeout, error_response, schema_violation, permission_denied, and llm_failure.
- **Actor/Component**: Orchestrator
- **Preconditions**: Agent step has been dispatched
- **Inputs**: Agent step result or timeout signal
- **Expected Behavior**: Orchestrator monitors each agent step with a configurable timeout. On timeout: classify as timeout. On exception or error response: classify by error type. On schema violation in output: classify as schema_violation. Failure class recorded in WorkflowContext.
- **Outputs**: StepFailureEvent: { "step_index", "agent_id", "failure_class", "detail", "timestamp" }
- **Failure Conditions**: Failure detection itself crashes - Orchestrator recovery process handles; step marked as unknown_failure
- **Security Considerations**: Failure details must not expose internal stack traces or secrets in external API responses.
- **Acceptance Criteria**: Agent timeout classified correctly. LLM error classified as llm_failure. Schema violation classified as schema_violation. All failures recorded in WorkflowContext and audit log.

## FR-FAIL-002 · LLM Availability Detection
- **ID**: FR-FAIL-002
- **Name**: LLM Availability Detection
- **Description**: The system must detect when the local LLM is unavailable (not responding within timeout) and set a system-level LLM availability flag, preventing new tasks from entering the planning or execution phase.
- **Actor/Component**: Health module, Orchestrator
- **Preconditions**: Health check running; LLM endpoint configured
- **Inputs**: LLM health check response / timeout
- **Expected Behavior**: Health module pings LLM endpoint every N seconds. On 3 consecutive failures, sets llm_available=false. New task submissions return HTTP 503 with reason llm_unavailable. Existing in-progress tasks pause at next LLM call.
- **Outputs**: LLM availability flag; HTTP 503 for new tasks when unavailable
- **Failure Conditions**: Health check loop crashes - treat as LLM unavailable
- **Security Considerations**: LLM endpoint URL and credentials never exposed in error responses.
- **Acceptance Criteria**: After 3 LLM health check failures, new tasks receive 503. When LLM recovers, tasks resume accepting. Flag is visible in /api/v1/health response.

---

# SECTION 23 — RETRY / RECOVERY

## FR-RETR-001 · Automatic Step Retry
- **ID**: FR-RETR-001
- **Name**: Automatic Step Retry
- **Description**: On a transient agent step failure (timeout or llm_failure), the Orchestrator must automatically retry the step up to a configurable maximum (default: 2 retries) with exponential backoff before classifying as a permanent failure.
- **Actor/Component**: Orchestrator
- **Preconditions**: Step failure classified as transient (FR-FAIL-001)
- **Inputs**: StepFailureEvent with failure_class in [timeout, llm_failure], retry_count, max_retries
- **Expected Behavior**: Orchestrator waits backoff_seconds (initial 2s, doubled per retry) then re-dispatches step. Retry attempt number recorded in WorkflowContext. After max_retries, classifies as permanent failure.
- **Outputs**: Retried step result or permanent failure after max_retries
- **Failure Conditions**: All retries exhausted - permanent failure triggers re-planning (FR-ADAP-002) or task failure
- **Security Considerations**: Retry must use same permission context as original call. No permission escalation on retry.
- **Acceptance Criteria**: Transient failure triggers retry within 2 seconds. After 2 retries, permanent failure recorded. Retry count visible in WorkflowContext. Exponential backoff applied.

## FR-RETR-002 · Graceful Degradation
- **ID**: FR-RETR-002
- **Name**: Graceful Degradation
- **Description**: If a non-critical optional agent step permanently fails (e.g. narrative generation in Analytics Agent), the system may complete the task with a partial result rather than failing the entire workflow, provided the core required output is available.
- **Actor/Component**: Orchestrator, Reviewer/Validation Agent
- **Preconditions**: Failed step is marked as optional in ExecutionPlan; core required steps completed successfully
- **Inputs**: StepFailureEvent for optional step; WorkflowContext with core results
- **Expected Behavior**: Orchestrator marks optional step as skipped_due_to_failure. Reviewer validates core output. If core output passes review, task completes with partial_result status. Caller informed of which optional steps were skipped.
- **Outputs**: Task completed with status = partial_result; skipped_steps listed in response
- **Failure Conditions**: Core required steps fail - graceful degradation does not apply; task fails
- **Security Considerations**: Partial results must be clearly marked as incomplete in the response.
- **Acceptance Criteria**: Optional narrative generation failure allows task to complete with metrics result. Response includes "partial_result": true and lists skipped steps.

---

# SECTION 24 — SECURE TOOL EXECUTION

## FR-TOOL-001 · Tool Registry
- **ID**: FR-TOOL-001
- **Name**: Tool Registry
- **Description**: EAOC must maintain a Tool Registry listing all available tools with their: tool_id, name, description, input schema, output schema, required permissions, and risk_level.
- **Actor/Component**: Tool execution layer
- **Preconditions**: Tool registry loaded at system startup
- **Inputs**: Tool configuration files
- **Expected Behavior**: Tool Registry loaded at startup. Any tool invocation resolves tool metadata from registry. Tools not in registry cannot be invoked.
- **Outputs**: Loaded ToolRegistry with all registered tools
- **Failure Conditions**: Registry file missing or malformed - system fails to start
- **Security Considerations**: Tool Registry is read-only at runtime. New tools require re-deployment.
- **Acceptance Criteria**: All tools load at startup. Tool invocation for unregistered tool_id returns ToolNotFound error. Registry queryable for capability discovery.

## FR-TOOL-002 · Input Sanitization Before Tool Execution
- **ID**: FR-TOOL-002
- **Name**: Input Sanitization Before Tool Execution
- **Description**: All inputs passed to tools must be sanitized and validated against the tool input schema before execution. Inputs failing schema validation are rejected without tool execution.
- **Actor/Component**: Tool execution layer
- **Preconditions**: Tool schema defined in Tool Registry (FR-TOOL-001)
- **Inputs**: Tool ID, raw input parameters from agent
- **Expected Behavior**: Tool execution layer validates input against tool input schema (Pydantic). On validation failure: tool not called, ToolInputValidationError returned to agent. On success: sanitized input passed to tool.
- **Outputs**: Tool execution result or ToolInputValidationError
- **Failure Conditions**: Tool input schema not found - treat as validation failure; tool not called
- **Security Considerations**: Prevents injection attacks via tool parameters. All validation errors logged.
- **Acceptance Criteria**: Invalid tool input returns ToolInputValidationError without executing tool. Valid input executes tool successfully. Validation errors logged with input details (sensitive values masked).

---

# SECTION 25 — FINAL RESPONSE GENERATION

## FR-RESP-001 · Response Assembly
- **ID**: FR-RESP-001
- **Name**: Response Assembly
- **Description**: After Reviewer approval, the Orchestrator assembles the final response from all agent step outputs and presents it in a structured, user-facing format.
- **Actor/Component**: Orchestrator
- **Preconditions**: All agent steps completed; Reviewer passed output (FR-AGT-REV-001); risk gate cleared
- **Inputs**: WorkflowContext with all step outputs, ReviewResult
- **Expected Behavior**: Orchestrator assembles FinalResponse: includes primary result, supporting data references, provenance summary, and any warnings (e.g. partial_result). Writes to state store. Updates task status to completed.
- **Outputs**: FinalResponse: { "task_id", "status": "completed", "result": { ... }, "provenance_summary": [...], "warnings": [...] }
- **Failure Conditions**: Assembly fails due to missing required output - task status set to failed with reason assembly_failure
- **Security Considerations**: Final response must not include internal workflow metadata (agent IDs, internal tool names) unless user has admin role.
- **Acceptance Criteria**: Completed task returns FinalResponse with result and provenance summary. Partial result includes warnings. Admin role response includes full provenance. Standard role response omits internal metadata.

## FR-RESP-002 · Response Schema Versioning
- **ID**: FR-RESP-002
- **Name**: Response Schema Versioning
- **Description**: All API responses must include an api_version field. Response schemas must be versioned. Breaking schema changes require a new API version path (e.g. /api/v2/).
- **Actor/Component**: FastAPI, all response models
- **Preconditions**: API version defined in application config
- **Inputs**: Any API response
- **Expected Behavior**: Every response includes "api_version": "1.0" (or current version). Schema changes that break existing consumers increment the version.
- **Outputs**: API response with api_version field
- **Failure Conditions**: Version config missing - return 500 with internal error
- **Security Considerations**: Version field must not expose internal build version or commit hash.
- **Acceptance Criteria**: Every API response includes api_version. Breaking schema change increments version. Old version path continues to work after new version launch.

---

# SECTION 26 — API ERROR HANDLING

## FR-ERR-001 · Structured Error Responses
- **ID**: FR-ERR-001
- **Name**: Structured Error Responses
- **Description**: All API error responses must use a consistent structured JSON format with: error_code, error_message, detail (optional), and request_id for traceability.
- **Actor/Component**: FastAPI error handlers
- **Preconditions**: FastAPI exception handlers registered at startup
- **Inputs**: Any unhandled exception or validation error
- **Expected Behavior**: Global exception handler catches all errors. Maps to structured ErrorResponse. HTTP status code appropriate to error type. Internal details (stack traces, DB errors) never exposed in error message.
- **Outputs**: ErrorResponse: { "error_code": string, "error_message": string, "detail": string (optional), "request_id": string }
- **Failure Conditions**: Exception handler itself fails - FastAPI default 500 returned; alert raised
- **Security Considerations**: Stack traces, internal server names, and DB error details must never appear in external error responses.
- **Acceptance Criteria**: All 4xx and 5xx responses use ErrorResponse schema. request_id present in all error responses (traceable to audit log). DB error returns generic "internal_error" not raw DB message.

## FR-ERR-002 · Request ID Propagation
- **ID**: FR-ERR-002
- **Name**: Request ID Propagation
- **Description**: Every API request must be assigned a unique request_id at the gateway. This request_id must be propagated through all logs, audit entries, and error responses for that request.
- **Actor/Component**: FastAPI middleware
- **Preconditions**: Request received at API gateway
- **Inputs**: HTTP request (may include X-Request-ID header from caller)
- **Expected Behavior**: Middleware assigns request_id (from X-Request-ID header if valid UUID, else generates new UUID). Attaches to request context. All log entries and audit entries for this request include request_id.
- **Outputs**: X-Request-ID response header; request_id in all log/audit entries
- **Failure Conditions**: UUID generation fails - use fallback random ID
- **Security Considerations**: request_id must not encode user identity.
- **Acceptance Criteria**: Every response includes X-Request-ID header. All log entries for a request share the same request_id. Caller-provided X-Request-ID used if valid UUID.

---

# NON-FUNCTIONAL REQUIREMENTS (NFRS)

---

## NFR-SEC-001 · Authentication Enforcement
- **ID**: NFR-SEC-001
- **Category**: Security
- **Description**: 100% of API endpoints (except /api/v1/health and /api/v1/auth/token) must require authenticated bearer tokens. No endpoint may be accessible without authentication in production deployment.
- **Acceptance Criteria**: Automated security scan finds zero unauthenticated non-exempt endpoints. Penetration test confirms authentication is enforced.

## NFR-SEC-002 · Secret Management
- **ID**: NFR-SEC-002
- **Category**: Security
- **Description**: No secrets (DB passwords, LLM API keys, JWT secrets, service tokens) may be hardcoded in source code or committed to version control. All secrets loaded from environment variables or a secret manager at runtime.
- **Acceptance Criteria**: Static code analysis finds zero hardcoded secrets. Secrets not present in Docker images. Rotating a secret requires only environment variable update, not code change.

## NFR-SEC-003 · Transport Security
- **ID**: NFR-SEC-003
- **Category**: Security
- **Description**: All external API traffic must be encrypted in transit using TLS 1.2 or higher in production. HTTP (non-TLS) is permitted only in local development environments.
- **Acceptance Criteria**: TLS scan of production endpoint confirms TLS 1.2+. HTTP requests in production are rejected or redirected.

## NFR-SEC-004 · Principle of Least Privilege
- **ID**: NFR-SEC-004
- **Category**: Security
- **Description**: Every agent, service, and database user must have only the minimum permissions required for its defined function. No shared superuser credentials.
- **Acceptance Criteria**: Each DB role holds only required grants. No agent DB user has superuser or schema-owner privileges. Permission audit confirms least privilege per component.

## NFR-REL-001 · Mean Time to Recovery
- **ID**: NFR-REL-001
- **Category**: Reliability
- **Description**: After a single-component failure (e.g. one agent service crash), the system must recover and resume processing new tasks within 60 seconds.
- **Acceptance Criteria**: Chaos test: kill one component; system recovers within 60 seconds measured from failure to first successful new task.

## NFR-REL-002 · Task State Durability
- **ID**: NFR-REL-002
- **Category**: Reliability
- **Description**: In-progress task state must be recoverable after an Orchestrator restart. No in-progress task may be silently lost due to a single-node restart.
- **Acceptance Criteria**: Kill Orchestrator mid-task; restart; task resumes from last completed step within 30 seconds.

## NFR-AVAIL-001 · API Availability Target
- **ID**: NFR-AVAIL-001
- **Category**: Availability
- **Description**: The EAOC API must target 99% availability during planned evaluation periods. Planned maintenance windows excluded.
- **Acceptance Criteria**: Uptime monitoring over 7-day evaluation period shows >= 99% availability.

## NFR-AVAIL-002 · Graceful Degradation on LLM Unavailability
- **ID**: NFR-AVAIL-002
- **Category**: Availability
- **Description**: When the local LLM is unavailable, the system must remain partially operational: health endpoint, audit query, and task status polling must continue to function. Only task submission and execution are suspended.
- **Acceptance Criteria**: LLM killed; /api/v1/health, /api/v1/audit, /api/v1/tasks/{id} all respond correctly. New task submission returns 503.

## NFR-PERF-001 · API Response Latency
- **ID**: NFR-PERF-001
- **Category**: Performance
- **Description**: Non-LLM API endpoints (auth, status polling, health, audit query) must return responses within 200ms at p95 under expected load (up to 50 concurrent requests).
- **Acceptance Criteria**: Load test at 50 concurrent users shows p95 latency <= 200ms for non-LLM endpoints.

## NFR-PERF-002 · Single-Agent Task Completion Time
- **ID**: NFR-PERF-002
- **Category**: Performance
- **Description**: A simple single-agent read-only task (e.g. SQL query on synthetic data) must complete end-to-end within 30 seconds under normal LLM load.
- **Acceptance Criteria**: 10 sequential single-agent SQL tasks all complete within 30 seconds each.

## NFR-PERF-003 · Vector Search Latency
- **ID**: NFR-PERF-003
- **Category**: Performance
- **Description**: Semantic retrieval from pgvector (top-5 results from a corpus of 10,000 chunks) must complete within 2 seconds.
- **Acceptance Criteria**: 50 sequential vector searches against 10K-chunk corpus all return within 2 seconds.

## NFR-SCALE-001 · Horizontal Scalability Design
- **ID**: NFR-SCALE-001
- **Category**: Scalability
- **Description**: The FastAPI API layer must be designed to support horizontal scaling (multiple instances behind a load balancer). No in-process state that would break multi-instance deployment.
- **Acceptance Criteria**: Running 2 API instances simultaneously shows no state-sharing issues. Tasks submitted to instance A are retrievable via instance B.

## NFR-SCALE-002 · Vector Store Growth Accommodation
- **ID**: NFR-SCALE-002
- **Category**: Scalability
- **Description**: The pgvector schema must accommodate up to 1 million document chunks without schema changes. Architecture must document the migration path to Qdrant if this limit is approached.
- **Acceptance Criteria**: pgvector table with 100K chunks functions correctly. Migration path to Qdrant documented in architecture docs.

## NFR-AUDIT-001 · Complete Auditability
- **ID**: NFR-AUDIT-001
- **Category**: Auditability
- **Description**: Every action affecting data, security, or workflow outcomes must produce an audit log entry. It must be possible to reconstruct the full history of any task solely from audit log entries.
- **Acceptance Criteria**: For a completed task, audit log entries cover: task creation, all agent invocations, all tool calls, all permission checks, approval decisions, and task completion.

## NFR-AUDIT-002 · Audit Log Retention
- **ID**: NFR-AUDIT-002
- **Category**: Auditability
- **Description**: Audit log entries must be retained for a minimum of 90 days in the PostgreSQL audit table, accessible via the audit query endpoint.
- **Acceptance Criteria**: Entries from 90 days prior are retrievable via /api/v1/audit. Entries are not automatically purged before 90 days.

## NFR-TRACE-001 · End-to-End Trace Completeness
- **ID**: NFR-TRACE-001
- **Category**: Traceability
- **Description**: For every completed task, it must be possible to trace each element of the final output back to the specific agent, tool, and data source that produced it.
- **Acceptance Criteria**: Provenance trace for a completed multi-agent task links every output element to a source. No output element is untraced.

## NFR-TRACE-002 · Distributed Trace Correlation
- **ID**: NFR-TRACE-002
- **Category**: Traceability
- **Description**: All log entries, audit entries, and metrics for a given task must share a common task_id and request_id, enabling correlation across all system components.
- **Acceptance Criteria**: Filtering logs by task_id returns all entries for that task across all components. No log entries for a task lack the task_id field.

## NFR-MAINT-001 · Configuration Externalization
- **ID**: NFR-MAINT-001
- **Category**: Maintainability
- **Description**: All configurable parameters (timeouts, retry limits, row limits, risk thresholds, LLM endpoint, DB connection strings) must be externalized to environment variables or a config file. No configuration is hardcoded.
- **Acceptance Criteria**: Changing LLM endpoint requires only environment variable update and service restart. No code change required.

## NFR-MAINT-002 · Code Test Coverage
- **ID**: NFR-MAINT-002
- **Category**: Maintainability
- **Description**: Backend code must maintain a minimum of 70% test coverage (measured by line coverage) across unit and integration tests.
- **Acceptance Criteria**: pytest-cov report shows >= 70% line coverage for all non-test modules.

## NFR-REPRO-001 · Deterministic Synthetic Data
- **ID**: NFR-REPRO-001
- **Category**: Reproducibility
- **Description**: Synthetic enterprise data generation must use a fixed random seed so that the same dataset can be regenerated identically for research reproducibility.
- **Acceptance Criteria**: Running data generation script twice with same seed produces identical datasets. Seed documented in research notes.

## NFR-REPRO-002 · Experiment Configuration Logging
- **ID**: NFR-REPRO-002
- **Category**: Reproducibility
- **Description**: Every evaluation run must log the full configuration used: model name, model version, temperature, system prompts, dataset version, and EAOC version. This enables reproduction of evaluation results.
- **Acceptance Criteria**: Evaluation run log contains all specified configuration fields. A second run with identical config produces comparable results.

## NFR-PRIV-001 · PII Minimization
- **ID**: NFR-PRIV-001
- **Category**: Privacy
- **Description**: PII fields (names, DOB, salary, home address, contact details) must never appear in API responses to users without explicit PII permission. Agents must apply redaction before returning results.
- **Acceptance Criteria**: API response to non-PII role contains no PII fields. Redaction is verified by automated test using synthetic PII data.

## NFR-PRIV-002 · Synthetic Data Only
- **ID**: NFR-PRIV-002
- **Category**: Privacy
- **Description**: No real personal data, real financial data, or real enterprise data may be loaded into the EAOC development or evaluation environment. All data must be synthetic.
- **Acceptance Criteria**: Data generation script reviewed and confirmed to use only synthetic generators. No real data files present in repository or Docker volumes.

## NFR-OBS-001 · Log Aggregation Readiness
- **ID**: NFR-OBS-001
- **Category**: Observability
- **Description**: All logs must be written to stdout in JSON format, enabling collection by any standard log aggregator (e.g. ELK, Loki, CloudWatch) without custom parsing.
- **Acceptance Criteria**: Log entries are valid JSON parseable by standard tools. No multi-line non-JSON log output.

## NFR-FAULT-001 · Fail-Closed Security Posture
- **ID**: NFR-FAULT-001
- **Category**: Fault Tolerance
- **Description**: Any failure in the authentication, RBAC, or permission enforcement subsystems must result in denial of the operation, never automatic permission grant.
- **Acceptance Criteria**: Injecting a fault into the RBAC module causes all permission checks to deny. No operation is auto-approved on security subsystem failure.

## NFR-FAULT-002 · Circuit Breaker for External Dependencies
- **ID**: NFR-FAULT-002
- **Category**: Fault Tolerance
- **Description**: The system must implement circuit breaker patterns for calls to: the LLM endpoint, PostgreSQL, and Redis. After a configurable threshold of consecutive failures, the circuit opens and fast-fails subsequent calls until the dependency recovers.
- **Acceptance Criteria**: After 5 consecutive LLM timeouts, circuit opens and new LLM calls immediately return llm_circuit_open error without waiting for timeout. Circuit closes after successful health check.

---

# REQUIREMENTS SUMMARY

| Category | Count |
|---|---|
| Request Intake (FR-REQ) | 4 |
| Authentication (FR-AUTH) | 3 |
| RBAC (FR-RBAC) | 3 |
| Orchestrator (FR-ORCH) | 3 |
| Planner (FR-PLAN) | 3 |
| Adaptive Orchestration (FR-ADAP) | 2 |
| Agent Responsibilities (FR-AGT) | 12 |
| Agent-to-Tool Permissions (FR-PERM) | 2 |
| Enterprise Data Access (FR-DATA) | 2 |
| RAG / Retrieval (FR-RAG) | 2 |
| SQL / Data Operations (FR-SQL) | 2 |
| Document Processing (FR-DOC) | 2 |
| Analytics (FR-ANA) | 2 |
| Finance/HR/IT Workflows (FR-FIN/HR/IT) | 6 |
| Reviewer/Validation (FR-REV) | 3 |
| Evidence Provenance (FR-PROV) | 2 |
| Risk Classification (FR-RISK) | 2 |
| Human Approval (FR-HITL) | 3 |
| Audit Logging (FR-AUDIT) | 2 |
| Observability (FR-OBS) | 3 |
| Workflow State/Memory (FR-STATE) | 2 |
| Failure Detection (FR-FAIL) | 2 |
| Retry/Recovery (FR-RETR) | 2 |
| Secure Tool Execution (FR-TOOL) | 2 |
| Final Response Generation (FR-RESP) | 2 |
| API Error Handling (FR-ERR) | 2 |
| **Total Functional Requirements** | **71** |

| NFR Category | Count |
|---|---|
| Security | 4 |
| Reliability | 2 |
| Availability | 2 |
| Performance | 3 |
| Scalability | 2 |
| Auditability | 2 |
| Traceability | 2 |
| Maintainability | 2 |
| Reproducibility | 2 |
| Privacy | 2 |
| Observability | 1 |
| Fault Tolerance | 2 |
| **Total Non-Functional Requirements** | **26** |

| **Grand Total** | **97** |

---

# AMBIGUITIES REQUIRING HUMAN APPROVAL

## AMB-001 · Human Approval Delivery Mechanism [MEDIUM — Pre-Phase 4]
FR-HITL-001 and FR-HITL-002 define the approval API endpoint, but the delivery mechanism for notifying human approvers is not specified. Options: (a) polling the /api/v1/approvals endpoint, (b) webhook callback, (c) email notification, (d) Slack/Teams integration. The current spec assumes option (a) polling via API. This must be confirmed before Phase 4.

## AMB-002 · Risk Classification Rules Schema [MEDIUM — Pre-Phase 4]
FR-RISK-001 defines risk classification as rules-based but does not specify the rules schema format. Options: (a) hard-coded Python rules, (b) YAML/JSON policy file, (c) database-driven policy, (d) LLM-assessed risk with rules as guidance. Must be confirmed before Phase 4 implementation.

## AMB-003 · Agent Isolation Model [LOW — Pre-Phase 3]
FR-AGT-ORCH-001 through FR-AGT-SEC-001 describe 12 logical agents. Their physical isolation model (LangGraph nodes in-process vs. separate FastAPI sub-services vs. isolated Python processes) is not specified. This affects the implementation of FR-RBAC-003 (agent-level permission scoping) and FR-PERM-001 (permission manifests). Must be confirmed before Phase 3.

## AMB-004 · pgvector to Qdrant Migration Trigger [LOW — Pre-Phase 2]
FR-RAG-001 and NFR-SCALE-002 reference the optional migration to Qdrant but do not specify when this trigger applies. Must be confirmed before Phase 2 completes.

## AMB-005 · Local LLM Model Selection [LOW — Pre-Phase 1]
FR-PLAN-001, FR-AGT-RAG-001, and all agent FRs reference the LLM but do not name a specific model. The choice of local model (e.g. Llama 3.1, Mistral, Qwen 2.5, Phi-3) affects context window sizes for FR-STATE-002, performance targets in NFR-PERF-002, and embedding model for FR-RAG-001. Should be confirmed before Phase 1 ends.

## AMB-006 · Evaluation Metrics and Baseline Architecture [LOW — Pre-Phase 7]
NFR-REPRO-001 and NFR-REPRO-002 define reproducibility requirements for evaluation, but the specific metrics (accuracy, latency, error rate, human approval rate, provenance completeness) and the baseline system architectures (single-agent, basic multi-agent) are not yet defined. Must be confirmed before Phase 7.

---

# TRACEABILITY: REQUIREMENTS TO ARCHITECTURE

| Architecture Component | Implementing Requirements |
|---|---|
| User/API Request | FR-REQ-001, FR-REQ-002, FR-REQ-003, FR-REQ-004 |
| Authentication | FR-AUTH-001, FR-AUTH-002, FR-AUTH-003, NFR-SEC-001 |
| RBAC | FR-RBAC-001, FR-RBAC-002, FR-RBAC-003, NFR-SEC-004 |
| Orchestrator | FR-ORCH-001, FR-ORCH-002, FR-ORCH-003, FR-AGT-ORCH-001 |
| Planner | FR-PLAN-001, FR-PLAN-002, FR-PLAN-003, FR-AGT-PLAN-001 |
| Adaptive Orchestration | FR-ADAP-001, FR-ADAP-002, FR-PLAN-003 |
| Research/RAG Agent | FR-AGT-RAG-001, FR-RAG-001, FR-RAG-002 |
| SQL/Data Agent | FR-AGT-SQL-001, FR-SQL-001, FR-SQL-002, FR-DATA-001, FR-DATA-002 |
| Document Agent | FR-AGT-DOC-001, FR-DOC-001, FR-DOC-002 |
| Analytics Agent | FR-AGT-ANA-001, FR-ANA-001, FR-ANA-002 |
| Finance Agent | FR-AGT-FIN-001, FR-FIN-001, FR-FIN-002 |
| HR Agent | FR-AGT-HR-001, FR-HR-001, FR-HR-002 |
| IT/Support Agent | FR-AGT-IT-001, FR-IT-001, FR-IT-002 |
| Developer Agent | FR-AGT-DEV-001 |
| Reviewer/Validation Agent | FR-AGT-REV-001, FR-REV-001, FR-REV-002, FR-REV-003 |
| Security/Policy Agent | FR-AGT-SEC-001, FR-PERM-001, FR-PERM-002, FR-RISK-001, NFR-FAULT-001 |
| Enterprise Tools/Data | FR-DATA-001, FR-DATA-002, FR-TOOL-001, FR-TOOL-002, NFR-SEC-004 |
| Reviewer/Validation (cross-cutting) | FR-REV-001, FR-REV-002, FR-REV-003 |
| Risk Decision | FR-RISK-001, FR-RISK-002 |
| Human Approval | FR-HITL-001, FR-HITL-002, FR-HITL-003 |
| Validated Final Output | FR-RESP-001, FR-RESP-002 |
| Evidence Provenance (cross-cutting) | FR-PROV-001, FR-PROV-002, NFR-TRACE-001, NFR-TRACE-002 |
| Audit Trail (cross-cutting) | FR-AUDIT-001, FR-AUDIT-002, NFR-AUDIT-001, NFR-AUDIT-002 |
| Observability (cross-cutting) | FR-OBS-001, FR-OBS-002, FR-OBS-003, NFR-OBS-001 |
| Workflow Memory/State | FR-STATE-001, FR-STATE-002, NFR-REL-002 |
| Failure Detection | FR-FAIL-001, FR-FAIL-002 |
| Retry/Recovery | FR-RETR-001, FR-RETR-002, NFR-REL-001 |
| API Error Handling | FR-ERR-001, FR-ERR-002 |

---

# PHASE 0.1 COMPLETION CHECKLIST

- [x] FR-REQ: All 4 request intake requirements documented with full template fields
- [x] FR-AUTH: All 3 authentication requirements documented
- [x] FR-RBAC: All 3 RBAC requirements documented
- [x] FR-ORCH: All 3 orchestrator requirements documented
- [x] FR-PLAN: All 3 planner requirements documented
- [x] FR-ADAP: Both adaptive orchestration requirements documented
- [x] FR-AGT: All 12 approved logical agents have responsibility FRs
- [x] FR-PERM: Agent-to-tool permission requirements documented
- [x] FR-DATA: Enterprise data access requirements documented
- [x] FR-RAG: RAG/retrieval requirements documented
- [x] FR-SQL: SQL/data operation requirements documented
- [x] FR-DOC: Document processing requirements documented
- [x] FR-ANA: Analytics requirements documented
- [x] FR-FIN/HR/IT: Finance, HR, and IT workflow requirements documented
- [x] FR-REV: Reviewer/validation requirements documented
- [x] FR-PROV: Evidence provenance requirements documented
- [x] FR-RISK: Risk classification requirements documented
- [x] FR-HITL: Human approval requirements documented
- [x] FR-AUDIT: Audit logging requirements documented
- [x] FR-OBS: Observability requirements documented
- [x] FR-STATE: Workflow state/memory requirements documented
- [x] FR-FAIL: Failure detection requirements documented
- [x] FR-RETR: Retry/recovery requirements documented
- [x] FR-TOOL: Secure tool execution requirements documented
- [x] FR-RESP: Final response generation requirements documented
- [x] FR-ERR: API error handling requirements documented
- [x] NFR-SEC: Security NFRs documented (4)
- [x] NFR-REL: Reliability NFRs documented (2)
- [x] NFR-AVAIL: Availability NFRs documented (2)
- [x] NFR-PERF: Performance NFRs documented (3)
- [x] NFR-SCALE: Scalability NFRs documented (2)
- [x] NFR-AUDIT: Auditability NFRs documented (2)
- [x] NFR-TRACE: Traceability NFRs documented (2)
- [x] NFR-MAINT: Maintainability NFRs documented (2)
- [x] NFR-REPRO: Reproducibility NFRs documented (2)
- [x] NFR-PRIV: Privacy NFRs documented (2)
- [x] NFR-OBS: Observability NFR documented (1)
- [x] NFR-FAULT: Fault tolerance NFRs documented (2)
- [x] Requirements summary table produced (71 FRs + 26 NFRs = 97 total)
- [x] Ambiguities requiring human approval listed (6 items)
- [x] Traceability matrix linking requirements to architecture components produced
- [ ] USER APPROVAL OF THIS DOCUMENT PENDING
- [ ] Phase 0.2 scope to be defined after approval

---
Part 0.1 Complete. Awaiting user approval before proceeding to Part 0.2.
