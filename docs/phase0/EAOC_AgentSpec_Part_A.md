# EAOC — Agent Specification
# Part 0.2 — Agents 1 to 6 of 12
# Phase 0 · Part 0.2 · Version 1.0
# Status: DRAFT — Awaiting Part 0.2 approval
# Conforms to: EAOC Master Specification v1.0 and Part 0.1 FRS/NFRS

---

## Agent Specification Template

Each agent is defined by 25 fields (A–Y) as mandated by Part 0.2 scope:
A. Agent ID | B. Agent Name | C. Purpose | D. Responsibilities
E. Non-Responsibilities | F. Inputs | G. Outputs | H. Required Context
I. Tools | J. Data Sources | K. Permissions | L. Prohibited Operations
M. Risk Levels | N. Human Approval Conditions | O. Validation Requirements
P. Provenance Requirements | Q. Audit Events | R. Failure Conditions
S. Retry/Recovery | T. Escalation | U. Agent Interactions
V. Performance Considerations | W. Security Constraints
X. Test Scenarios | Y. Acceptance Criteria

---

# AGENT 1 — ORCHESTRATOR

## A. Agent ID
`AGT-ORCH`

## B. Agent Name
Orchestrator Agent

## C. Purpose
The Orchestrator is the master workflow controller and single internal entry point for all validated, authenticated tasks. It coordinates the full lifecycle of a task: from receiving a validated request, through plan execution, to final response assembly. It does not perform domain-specific reasoning; it delegates all domain work to specialized agents.

## D. Responsibilities
- Receive validated TaskRequest from the API layer with attached UserContext and task_id.
- Initialize and maintain WorkflowContext for the duration of the task.
- Dispatch task to the Planner and receive back a validated ExecutionPlan.
- Execute the ExecutionPlan step-by-step, respecting declared inter-step dependencies.
- Enforce RBAC checks before each agent dispatch (via RBAC module).
- Invoke the Risk Gate before executing any step annotated as medium or high risk.
- Pause execution and create a HumanApprovalRequest for any high-risk step.
- Resume or terminate workflow based on human approval decision.
- Collect outputs from each agent step and store them in WorkflowContext.
- Persist WorkflowContext to the state store (Redis + PostgreSQL) after each step.
- Detect step failures and classify them by failure class.
- Trigger retry for transient failures and contextual re-planning when appropriate.
- Forward completed step outputs to the Reviewer/Validation Agent.
- Assemble the final validated response.
- Update task status in the state store throughout the lifecycle.
- Record provenance entries after each step.

## E. Non-Responsibilities
- Must NOT perform any domain-specific reasoning (finance, HR, IT, analytics, etc.).
- Must NOT generate SQL, call vector stores, or parse documents directly.
- Must NOT make LLM calls for domain content. Its LLM usage (if any) is limited to internal control logic only.
- Must NOT modify enterprise data directly.
- Must NOT bypass RBAC checks on behalf of any user.
- Must NOT auto-approve high-risk operations; all high-risk approvals require a designated human approver.
- Must NOT expose internal WorkflowContext structure in external API responses.
- Must NOT make authorization decisions unilaterally; uses RBAC and Security/Policy Agent.

## F. Inputs
- Validated `TaskRequest`: { task_id, task_description, requesting_user_id, context (optional) }
- `UserContext`: { user_id, roles, permissions }
- `ExecutionPlan` from Planner: { steps: [{ step_index, agent_id, inputs, dependencies, risk_level }] }
- Agent step outputs from each specialized agent
- Human approval decisions from the Approval module
- State store reads for recovery after restart

## G. Outputs
- `WorkflowContext` (persisted): { task_id, user_context, status, steps_completed, step_results, provenance_chain, created_at, updated_at }
- `FinalResponse`: { task_id, status, result, provenance_summary, warnings, api_version }
- `HumanApprovalRequest` records (on high-risk steps)
- Task status updates (accepted → planning → running → awaiting_approval → completed | failed)
- Provenance entries per step

## H. Required Context
- UserContext (from auth middleware)
- Agent capability registry (to validate plan steps)
- RBAC role-to-agent permission mappings
- Risk classification rules
- Agent permission manifests (to enforce at dispatch time)
- Current ExecutionPlan
- WorkflowContext (loaded from state store on recovery)

## I. Tools It May Use
- State Store Tool (Redis read/write, PostgreSQL read/write for WorkflowContext)
- RBAC Check Tool (internal permission verification)
- Risk Gate Tool (classify and gate by risk level)
- Approval Queue Tool (create and monitor HumanApprovalRequests)
- Planner Invocation (internal LangGraph transition)
- Agent Dispatch (internal LangGraph node invocations)
- Audit Logger (append-only write to audit log)

## J. Data Sources It May Access
- `workflow_state` table (PostgreSQL) — read/write own task state
- `approval_requests` table (PostgreSQL) — read/write approval records
- `audit_log` table (PostgreSQL) — append-only write
- `agent_registry` (in-memory, loaded at startup)
- `rbac_roles` table or config (read-only)
- Redis task state cache (read/write)

## K. Permissions
- Read/write own task's WorkflowContext
- Dispatch to any agent that the requesting user's role permits
- Create HumanApprovalRequest records
- Append to audit log
- Read agent permission manifests
- Read RBAC mappings

## L. Prohibited Operations
- Cannot access finance, HR, IT, analytics, or document data schemas directly
- Cannot call the LLM for domain content generation
- Cannot modify any enterprise data (invoices, employees, tickets, budgets, etc.)
- Cannot auto-approve high-risk steps regardless of any system condition
- Cannot escalate user permissions beyond what the user's token grants
- Cannot modify RBAC definitions or agent permission manifests
- Cannot call external APIs or services not listed in its tool manifest

## M. Risk Levels
- Orchestrator itself operates at the system level; its actions are classified by the risk level of the steps it executes.
- Risk of re-planning: Low
- Risk of human approval creation: System action (not user-data-affecting)
- Risk of executing a high-risk agent step: Blocked pending human approval

## N. Human Approval Conditions
- Any ExecutionPlan step annotated with risk_level = high triggers a HumanApprovalRequest before execution.
- Re-planning after failure does not require approval unless the revised plan introduces new high-risk steps.
- Admin override of a blocked high-risk step must itself generate an audit entry.

## O. Validation Requirements
- ExecutionPlan received from Planner must be Pydantic-validated before any step is executed.
- Each agent step output must conform to the agent's declared output schema before being stored in WorkflowContext.
- Final response must pass Reviewer/Validation Agent check before being made available to the caller.

## P. Provenance Requirements
After every agent step, Orchestrator must append to WorkflowContext.provenance_chain:
- step_index, agent_id, tool_used (if known), data_source_accessed (if known), action_summary, result_reference, timestamp, status

## Q. Audit Events
- TASK_RECEIVED: task_id, user_id, timestamp
- PLAN_DISPATCHED: task_id, planner_invocation_id
- PLAN_RECEIVED: task_id, step_count, plan_type
- STEP_DISPATCHED: task_id, step_index, agent_id
- STEP_COMPLETED: task_id, step_index, agent_id, outcome
- STEP_FAILED: task_id, step_index, agent_id, failure_class
- RISK_GATE_TRIGGERED: task_id, step_index, risk_level
- APPROVAL_REQUESTED: task_id, approval_id, step_index
- APPROVAL_RECEIVED: task_id, approval_id, decision, decided_by
- REPLANNING_TRIGGERED: task_id, reason
- TASK_COMPLETED: task_id, final_status
- TASK_FAILED: task_id, reason

## R. Failure Conditions
- Planner unavailable → task status = failed, reason = planner_unavailable
- Agent step fails after max retries → classified permanent failure; triggers re-planning or task failure
- State store write fails → retry 3x; on persistent failure mark task degraded
- Human approval times out → task status = failed, reason = approval_timeout
- WorkflowContext lost (both Redis and PostgreSQL unavailable) → task status = failed, reason = state_store_failure

## S. Retry/Recovery Behavior
- Transient agent step failures (timeout, llm_failure): retry up to 2 times with exponential backoff (initial 2s)
- Permanent failures: trigger contextual re-planning (FR-ADAP-002) up to configurable max_replan_attempts (default 2)
- On system restart: recover in-progress tasks from PostgreSQL WorkflowContext; resume from last completed step
- State store write retry: 3 attempts before marking degraded

## T. Escalation Behavior
- Exceeded re-plan limit → task fails with full trace; caller receives failure response with reason
- Reviewer rejects output → Orchestrator escalates: returns to Planner for re-execution of failing steps or fails task
- Human approval rejection → task terminates with reason = human_approval_rejected

## U. Agent Interactions
- → Planner (dispatches task for decomposition)
- → All 10 specialized agents (dispatches execution steps)
- → Reviewer/Validation Agent (submits assembled output for review)
- → Security/Policy Agent (policy checks at key points)
- ← RBAC Module (receives authorization decisions)
- ← Risk Gate (receives risk classification decisions)
- ← Approval Queue (receives human approval decisions)

## V. Performance Considerations
- Orchestrator overhead per step (excluding agent execution time): target < 100ms
- State store write per step: target < 50ms
- Recovery from restart to first step dispatch: target < 30 seconds
- Provenance entry write: target < 20ms

## W. Security Constraints
- All Orchestrator actions authenticated with service token (FR-AUTH-003)
- Cannot forward sensitive data between agents unless both are authorized for that data class
- WorkflowContext in Redis must have TTL set; must not persist indefinitely
- WorkflowContext in PostgreSQL must be encrypted at rest
- Orchestrator logs must not contain raw secrets, tokens, or PII

## X. Test Scenarios
1. Single-agent task completes successfully end-to-end with correct status transitions.
2. Multi-agent task (3 steps) executes in correct order with inter-step context passing.
3. Step 2 failure triggers retry, then re-planning, then task failure with full trace.
4. High-risk step triggers approval request; approval resumes task; rejection fails task.
5. System restart mid-task: state recovered from PostgreSQL; task resumes from step 2.
6. State store write failure: task continues with degraded flag; audit entry created.
7. Unauthorized agent dispatch (user role mismatch): RBAC denial logged; task rejected.

## Y. Acceptance Criteria
- Every task transitions through the correct status sequence.
- Every completed task has a provenance_chain with one entry per step.
- No domain data is accessed directly by the Orchestrator.
- High-risk steps never execute without a recorded human approval decision.
- Restarted Orchestrator resumes in-progress tasks within 30 seconds.
- All audit events are present and correctly timestamped for every completed task.

---

# AGENT 2 — PLANNER

## A. Agent ID
`AGT-PLAN`

## B. Agent Name
Planner Agent

## C. Purpose
The Planner is responsible for task decomposition. It analyzes a task description and produces a structured, validated ExecutionPlan that the Orchestrator uses to dispatch work to specialized agents. The Planner decides: which agents are needed, in what order, what inputs each step requires, what each step's risk level is, and whether the task is single-agent or multi-agent. The Planner does not execute any steps itself.

## D. Responsibilities
- Receive WorkflowContext from the Orchestrator.
- Analyze the task_description to determine what the user is asking for.
- Determine whether the task requires a single specialized agent or multiple agents.
- Select the appropriate sequence of specialized agents from the agent capability registry.
- Specify input requirements, expected outputs, and inter-step data dependencies for each step.
- Annotate each step with its risk_level using the risk classification rules.
- Validate the resulting ExecutionPlan (no unknown agents, no circular dependencies, all inputs resolvable).
- Record planning rationale in WorkflowContext for traceability.
- Support contextual re-planning when called by the Orchestrator after a step failure.

## E. Non-Responsibilities
- Must NOT execute any agent step itself.
- Must NOT call enterprise tools, databases, or vector stores.
- Must NOT make authorization decisions (RBAC is the Orchestrator's responsibility).
- Must NOT create HumanApprovalRequests.
- Must NOT access user data, financial records, HR data, or any domain data.
- Must NOT assign agents the user does not have RBAC permission to invoke.
- Must NOT modify the agent capability registry.
- Must NOT produce plans that reference agents outside the approved 12-agent roster.

## F. Inputs
- WorkflowContext: { task_id, user_context, task_description }
- Agent capability registry (in-memory, loaded at startup): maps agent_id → capabilities, input/output schemas
- Risk classification rules (loaded from config)
- RBAC permitted agents for the requesting user's roles
- For re-planning: partial WorkflowContext with completed steps and failure details

## G. Outputs
- Validated ExecutionPlan:
  {
    plan_id, task_id, plan_type (single_agent | multi_agent),
    steps: [
      { step_index, agent_id, description, inputs, expected_output_schema,
        dependencies: [step_indices], risk_level, optional: bool }
    ],
    planning_rationale: string,
    created_at
  }
- PlanValidationError (if plan cannot be produced after 1 re-attempt)

## H. Required Context
- task_description (from WorkflowContext)
- UserContext (to restrict to RBAC-permitted agents)
- Agent capability registry
- Risk classification rules
- For re-planning: failed step details and remaining step list

## I. Tools It May Use
- LLM Inference Tool (for plan generation using structured planning prompt)
- Agent Registry Lookup Tool (read-only, in-memory)
- Risk Classification Tool (rule-based, applies risk rules to steps)
- Plan Validation Tool (internal, checks for unknown agents and circular dependencies)
- Audit Logger (append provenance entries for planning decisions)

## J. Data Sources It May Access
- Agent capability registry (in-memory, read-only)
- Risk classification rules (config file, read-only)
- RBAC permitted-agents list for the user (read-only, passed by Orchestrator)
- WorkflowContext (read-only during planning)

## K. Permissions
- Read WorkflowContext (task_description, user_context)
- Read agent capability registry
- Read risk classification rules
- Invoke LLM for plan generation
- Write planning rationale to WorkflowContext (via Orchestrator)
- Append audit entries (planning decisions)

## L. Prohibited Operations
- Cannot execute any agent step
- Cannot call external databases, APIs, or vector stores
- Cannot access finance, HR, IT, or analytics data
- Cannot assign RBAC-unauthorized agents to a plan
- Cannot reference agents outside the approved 12-agent roster
- Cannot modify risk classification rules at runtime
- Cannot produce plans that cause an agent to receive data from a domain it is not authorized to access

## M. Risk Levels
- Planning itself is a low-risk system operation.
- Re-planning after failure is low risk.
- Planner annotates each plan step with its risk_level; does not set its own risk level.

## N. Human Approval Conditions
- None. The Planner does not trigger human approval. It annotates high-risk steps; the Orchestrator's Risk Gate triggers approval.

## O. Validation Requirements
- All plans must be Pydantic-validated against ExecutionPlan schema before being returned to Orchestrator.
- All referenced agent_ids must exist in the agent capability registry.
- No circular dependencies between steps.
- All step inputs must be resolvable from task context or prior step outputs.
- Plan must not include agents the user is not authorized to invoke.

## P. Provenance Requirements
- Planning decision must be recorded in WorkflowContext:
  { planner_invocation_id, plan_type, agent_sequence, planning_rationale, risk_annotations, timestamp }
- Re-planning events must record: replanning_reason, failed_step_index, revised_steps

## Q. Audit Events
- PLAN_GENERATION_STARTED: task_id, timestamp
- PLAN_GENERATED: task_id, plan_type, step_count, agent_sequence
- PLAN_VALIDATION_FAILED: task_id, reason
- PLAN_VALIDATED: task_id, plan_id
- REPLANNING_STARTED: task_id, reason, failed_step_index
- REPLANNING_COMPLETED: task_id, revised_step_count

## R. Failure Conditions
- LLM fails to produce a structurally valid plan after 2 attempts → return PlanValidationError to Orchestrator
- Plan references unknown agent → validation failure
- Circular dependency detected → validation failure
- LLM timeout → classify as llm_failure; propagate to Orchestrator
- All RBAC-permitted agents are insufficient for the task → return no_agent_available error

## S. Retry/Recovery Behavior
- On LLM failure during plan generation: retry once with a simplified prompt before returning error.
- On plan validation failure: attempt one re-generation with validation errors injected into prompt.
- After 2 total failures: return PlanValidationError to Orchestrator.

## T. Escalation Behavior
- PlanValidationError after 2 attempts → Orchestrator sets task status = failed, reason = planning_failed.
- Ambiguous task: Planner logs uncertainty in planning_rationale and defaults to safest/minimal plan.

## U. Agent Interactions
- ← Orchestrator (receives WorkflowContext; returns ExecutionPlan)
- → LLM (for plan generation)
- Reads: Agent Registry, Risk Classification Rules (no agent dispatch)

## V. Performance Considerations
- Plan generation (LLM call + validation): target < 15 seconds for typical tasks
- Re-planning: target < 15 seconds
- Agent registry lookup: < 10ms (in-memory)
- Risk annotation (rule-based): < 10ms per step

## W. Security Constraints
- Planner prompt must sanitize task_description to mitigate prompt injection before LLM call.
- Planner LLM output must be schema-validated; raw LLM text is never used as an ExecutionPlan without parsing.
- Planner must not include user-supplied raw text in plan step inputs without sanitization.
- Service-to-service token required for Orchestrator → Planner communication.

## X. Test Scenarios
1. Simple data retrieval task → single-agent plan with SQL/Data Agent.
2. "Summarize Q3 sales and check HR headcount" → multi-agent plan: SQL/Data Agent + HR Agent + Analytics Agent.
3. Plan referencing non-existent agent_id → validation failure, replanning attempted.
4. Circular dependency (step 2 depends on step 3 which depends on step 2) → validation failure.
5. LLM timeout during planning → retry once; on second failure, PlanValidationError returned.
6. Task requiring Finance Agent for a user without finance_user role → Finance Agent excluded from plan.
7. Re-planning after step 2 failure: revised plan covers remaining steps with updated inputs.

## Y. Acceptance Criteria
- All plans are Pydantic-validated before returning to Orchestrator.
- Single-agent tasks produce 1-step plans. Multi-domain tasks produce multi-step plans.
- Unknown agent references always rejected.
- RBAC-unauthorized agents never appear in plans.
- Planning rationale always recorded in WorkflowContext.
- Re-planning produces a revised plan that does not repeat the already-completed steps.

---

# AGENT 3 — RESEARCH / RAG AGENT

## A. Agent ID
`AGT-RAG`

## B. Agent Name
Research / RAG Agent

## C. Purpose
The Research/RAG Agent provides grounded, citation-backed answers to natural language queries by performing semantic retrieval from the enterprise knowledge base (pgvector) and synthesizing a response using the LLM. It is the primary agent for policy lookups, knowledge base queries, and unstructured information retrieval. It always returns source citations with its answers.

## D. Responsibilities
- Receive a retrieval query from the Orchestrator (directly or via HR Agent, IT Agent).
- Generate a query embedding using the configured embedding model.
- Perform vector similarity search against pgvector, filtered by permitted namespaces and access level.
- Retrieve top-K most relevant document chunks.
- Synthesize a grounded answer using the LLM, with retrieved chunks as context.
- Return the answer along with source citations: document_id, chunk_id, similarity score, excerpt.
- Enforce access-level filtering on retrieved chunks (server-side).
- Handle no-results cases gracefully with a structured no_results response.
- Record provenance: which documents were retrieved and used in the synthesis.

## E. Non-Responsibilities
- Must NOT access structured relational data (SQL databases, finance tables, HR tables).
- Must NOT parse or extract fields from binary documents directly (Document Agent handles this).
- Must NOT modify any document in the knowledge base.
- Must NOT ingest new documents (Ingestion Pipeline handles this).
- Must NOT generate answers without citing retrieved sources.
- Must NOT return results from namespaces the requesting workflow is not authorized to access.
- Must NOT call external internet APIs or web search services.

## F. Inputs
- `query` (string): the natural language retrieval query
- `top_k` (int, default 5, max configurable): number of chunks to retrieve
- `namespace` or `collection` (string): knowledge base collection to search within
- `access_level_filter` (optional, string): maximum access level permitted for retrieval
- `task_id` (for provenance and logging)

## G. Outputs
- On success:
  {
    "answer": string,
    "sources": [{ "document_id", "chunk_id", "score", "excerpt", "source_title" }],
    "retrieval_count": int,
    "model_used": string,
    "provenance_ref": string
  }
- On no results:
  { "answer": null, "sources": [], "status": "no_results", "query": string }
- On failure:
  { "status": "error", "error_code": string, "detail": string }

## H. Required Context
- Query text
- Permitted namespace(s) for the calling workflow
- Access level ceiling for the requesting user role
- Top-K parameter
- task_id for provenance

## I. Tools It May Use
- Embedding Tool: generates vector embeddings from query text (uses configured local embedding model)
- pgvector Search Tool: performs cosine similarity search with access-level filter
- LLM Inference Tool: synthesizes answer from retrieved chunks + query (RAG prompt)
- Audit Logger: records retrieval event

## J. Data Sources It May Access
- pgvector document chunk table (read-only, filtered by namespace and access_level)
- Document metadata table (read-only, to resolve document titles and sources)
- Agent permission manifest (read-only, to determine permitted namespaces)

## K. Permissions
- Read pgvector chunk table (SELECT only, filtered to permitted namespaces)
- Read document metadata table (SELECT only)
- Invoke embedding model
- Invoke LLM for synthesis
- Append to audit log

## L. Prohibited Operations
- Cannot modify, delete, or ingest documents
- Cannot access relational enterprise data (finance, HR, IT tables)
- Cannot search namespaces outside its permission manifest
- Cannot return chunks with access_level above the requestor's authorized ceiling
- Cannot call external internet search APIs
- Cannot modify pgvector index or schema

## M. Risk Levels
- Information retrieval from non-sensitive namespaces: Low
- Retrieval from sensitive namespaces (HR policies with PII, financial policies): Medium
- Risk level determined per namespace by the permission manifest

## N. Human Approval Conditions
- None under normal operation. Retrieval from a namespace flagged as "restricted" may be elevated to Medium risk, which requires enhanced audit logging but not human approval by default.
- If Security/Policy Agent classifies the query as matching a prohibited topic, the query is blocked (not escalated to human).

## O. Validation Requirements
- Query must be non-empty string, max 2000 characters.
- Namespace must be in the agent's permitted namespaces list.
- Retrieved chunks must pass access-level filter before being included in context.
- LLM output (answer) must be validated to confirm it references only retrieved source documents.
- Reviewer/Validation Agent checks that answer does not contain claims unsupported by retrieved chunks.

## P. Provenance Requirements
Provenance entry must include:
- agent_id: AGT-RAG
- query_text (sanitized)
- namespace searched
- top_k requested and retrieved
- document_ids and chunk_ids retrieved
- similarity scores
- LLM model used for synthesis
- timestamp

## Q. Audit Events
- RAG_QUERY_RECEIVED: task_id, namespace, query_length
- RAG_EMBEDDING_GENERATED: task_id, embedding_model
- RAG_RETRIEVAL_COMPLETED: task_id, chunks_retrieved, top_score
- RAG_SYNTHESIS_COMPLETED: task_id, answer_length, source_count
- RAG_NO_RESULTS: task_id, namespace, query
- RAG_ACCESS_DENIED: task_id, namespace, reason

## R. Failure Conditions
- Embedding model unavailable → error: embedding_unavailable
- pgvector DB unavailable → error: vector_store_unavailable
- No chunks in store for namespace → status: no_results (not an error)
- LLM timeout during synthesis → error: llm_timeout
- Access level filter violation detected → error: access_denied, audit entry created
- Query exceeds max length → error: query_too_long

## S. Retry/Recovery Behavior
- Embedding failure: retry once after 2s.
- pgvector query failure: retry once.
- LLM synthesis timeout: retry once with reduced context (fewer chunks). If second attempt fails, return partial response with retrieved sources and no synthesized answer.

## T. Escalation Behavior
- Persistent embedding or vector store failure after retry: return structured error to Orchestrator; Orchestrator handles re-planning or task failure.
- Access violation detected: block query; audit entry; no escalation to human (policy enforcement, not approval).

## U. Agent Interactions
- ← Orchestrator (receives query; returns answer with sources)
- ← HR Agent (delegates HR policy lookups)
- ← IT/Support Agent (delegates IT knowledge base lookups)
- → LLM (synthesis)
- → Embedding Tool
- → pgvector

## V. Performance Considerations
- Embedding generation: target < 500ms
- pgvector top-5 search (10K chunks): target < 2 seconds (NFR-PERF-003)
- LLM synthesis: target < 20 seconds
- Total RAG response time: target < 25 seconds

## W. Security Constraints
- Access-level filter applied in the SQL WHERE clause of pgvector query; not post-retrieval filtering.
- Namespace validation against permission manifest before query executes.
- Query text must be sanitized before embedding and before LLM prompt construction.
- Answer must not include verbatim content from restricted chunks.
- Service token required for all Orchestrator → RAG Agent calls.

## X. Test Scenarios
1. Query in permitted namespace returns top-5 chunks with answer and source citations.
2. Query in unpermitted namespace returns access_denied error and audit entry.
3. Query with no matching chunks returns structured no_results response.
4. Embedding model unavailable: retry once; on second failure, structured error returned.
5. LLM synthesis timeout: retry with reduced context; return sources without synthesized answer on second failure.
6. Answer references a document not in the retrieved chunks: Reviewer/Validation Agent flags it.
7. Restricted chunk (access_level too high for user) filtered from results; not included in answer.

## Y. Acceptance Criteria
- Every answer includes at least one source citation.
- Restricted chunks never returned to unauthorized callers.
- no_results response is structured and does not raise an exception.
- All retrieval events appear in audit log.
- Provenance entry accurately reflects which document chunks contributed to the answer.

---

# AGENT 4 — SQL / DATA AGENT

## A. Agent ID
`AGT-SQL`

## B. Agent Name
SQL / Data Agent

## C. Purpose
The SQL/Data Agent provides structured data access to the PostgreSQL database by translating natural language query goals into validated SQL SELECT statements and returning structured results. It is the primary agent for any task requiring tabular data retrieval from relational enterprise data. It operates read-only by default and has access only to the schemas listed in its permission manifest.

## D. Responsibilities
- Receive a natural language query_goal from the Orchestrator.
- Construct an LLM prompt with relevant schema context (DDL or schema summary for permitted schemas).
- Generate a SQL SELECT statement using the LLM.
- Validate the generated SQL: syntax check, forbidden-keyword check, schema scope check.
- Execute the validated SQL against PostgreSQL using a read-only database connection.
- Return structured results: column names, rows, row count, execution time.
- Enforce row result limits to prevent DoS-scale result sets.
- Return structured error responses on DB or generation failures.
- Record provenance: SQL generated, executed, schemas accessed, row count, timestamp.

## E. Non-Responsibilities
- Must NOT generate or execute INSERT, UPDATE, DELETE, DROP, TRUNCATE, ALTER, GRANT, or REVOKE statements.
- Must NOT access schemas not listed in its permission manifest.
- Must NOT access vector stores, document stores, or external APIs.
- Must NOT interpret or summarize the results (Analytics Agent handles interpretation).
- Must NOT make decisions based on query results.
- Must NOT expose raw DB error messages to external API callers.
- Must NOT cache query results across tasks (no cross-task data leakage).

## F. Inputs
- `query_goal` (string): natural language description of the data needed
- `permitted_schemas` (list of strings): schemas this workflow is authorized to access
- `schema_context` (string): DDL or schema summaries for permitted schemas
- `read_only_flag` (bool, default true)
- `row_limit` (int, default 1000)
- `task_id` (for provenance)

## G. Outputs
- On success:
  {
    "sql_generated": string,
    "sql_executed": string,
    "columns": [string],
    "rows": [[values]],
    "row_count": int,
    "truncated": bool,
    "execution_time_ms": int,
    "schemas_accessed": [string],
    "provenance_ref": string
  }
- On failure:
  { "status": "error", "error_code": string, "detail": string }

## H. Required Context
- Natural language query goal
- Permitted schema list
- Schema DDL or summary for permitted schemas
- read_only_flag
- task_id

## I. Tools It May Use
- LLM Inference Tool (SQL generation prompt)
- SQL Validation Tool (syntax check, forbidden keyword check, schema scope check)
- PostgreSQL Read-Only Execution Tool (executes validated SELECT on read-only connection)
- Audit Logger

## J. Data Sources It May Access
- PostgreSQL schemas listed in permission manifest (read-only by default):
  - analytics schema
  - finance schema (only if Finance Agent delegates, or if SQL Agent manifest includes it)
  - hr schema (only if HR Agent delegates, or if manifest includes it)
  - it schema (only if IT Agent delegates, or if manifest includes it)
  NOTE: schema access per instance depends on the calling workflow context and permission manifest.

## K. Permissions
- SELECT on permitted schemas only
- Read schema metadata (information_schema) for permitted schemas
- Invoke LLM for SQL generation
- Append to audit log

## L. Prohibited Operations
- Cannot execute: INSERT, UPDATE, DELETE, DROP, TRUNCATE, ALTER, GRANT, REVOKE, CREATE
- Cannot access schemas not in permission manifest
- Cannot access pgvector/vector tables
- Cannot call external APIs or services
- Cannot expose raw PostgreSQL error messages externally
- Cannot cache or share results across different tasks
- Cannot modify PostgreSQL roles or schema definitions

## M. Risk Levels
- Read-only query on non-sensitive schemas (analytics): Low
- Read-only query involving financial data: Medium
- Read-only query involving HR personal data: Medium
- Any write operation (if ever permitted by elevated manifest): High — requires human approval

## N. Human Approval Conditions
- None for default read-only queries.
- If a write-permitted variant of the SQL Agent is ever instantiated (requires explicit manifest elevation and admin authorization), all write operations require human approval.

## O. Validation Requirements
- Generated SQL must be syntactically valid (parsed before execution).
- SQL must not contain any forbidden keywords (DROP, DELETE, INSERT, UPDATE, TRUNCATE, GRANT, REVOKE, ALTER).
- All table references in SQL must resolve to permitted schemas.
- Row result must not exceed row_limit; truncated if needed with warning flag.
- SQL execution time must be within configured timeout (default 10 seconds).

## P. Provenance Requirements
- agent_id: AGT-SQL
- query_goal (sanitized)
- sql_generated
- sql_executed (may differ if sanitization applied)
- schemas_accessed
- row_count
- truncated flag
- execution_time_ms
- timestamp

## Q. Audit Events
- SQL_QUERY_RECEIVED: task_id, query_goal_length, permitted_schemas
- SQL_GENERATED: task_id, sql_length, schemas_referenced
- SQL_VALIDATION_PASSED: task_id
- SQL_VALIDATION_FAILED: task_id, reason, forbidden_keyword (if detected)
- SQL_EXECUTED: task_id, row_count, execution_time_ms, truncated
- SQL_EXECUTION_FAILED: task_id, error_code
- SQL_SCHEMA_VIOLATION: task_id, unauthorized_schema

## R. Failure Conditions
- LLM generates invalid SQL after 2 attempts → error: sql_generation_failed
- Forbidden keyword detected → error: forbidden_sql_operation; audit entry
- Unauthorized schema referenced → error: schema_access_denied; audit entry
- PostgreSQL unavailable → error: database_unavailable
- Query exceeds execution timeout → error: query_timeout
- Row limit exceeded → truncated result with warning (not an error)

## S. Retry/Recovery Behavior
- SQL generation failure: inject validation error message into prompt; retry once.
- DB connection failure: retry once after 2s.
- Query timeout: return timeout error (no retry; high-cost queries not retried automatically).

## T. Escalation Behavior
- Persistent SQL generation failure after retry → return error to Orchestrator; Orchestrator handles task failure.
- Schema violation → immediate block and audit; no retry.

## U. Agent Interactions
- ← Orchestrator (receives query_goal; returns structured results)
- ← Analytics Agent (may receive delegation for raw data retrieval)
- ← Finance Agent (may receive delegation for financial data queries)
- ← HR Agent (may receive delegation for HR aggregate queries)

## V. Performance Considerations
- SQL generation (LLM): target < 15 seconds
- SQL validation: target < 100ms
- Query execution on synthetic dataset: target < 5 seconds (NFR-PERF-002)
- Total response: target < 20 seconds

## W. Security Constraints
- Database connection is a read-only PostgreSQL role by default (SELECT only, no schema ownership).
- Generated SQL is validated before execution; raw LLM output is never executed directly.
- Row limit enforced at the application layer before returning results.
- Schema context provided to LLM must be pre-filtered to permitted schemas only; full DDL of unauthorized schemas is never included in prompts.
- No cross-task result caching.

## X. Test Scenarios
1. "Total sales by department for Q3 2024" → valid SELECT, correct results returned.
2. SQL generation produces INSERT → validation fails, error returned, audit entry created.
3. SQL references unauthorized schema → schema_access_denied error, audit entry.
4. Query returns 2000 rows → first 1000 returned with truncated:true.
5. PostgreSQL unavailable → structured error returned after one retry.
6. LLM generates syntactically invalid SQL twice → sql_generation_failed error.
7. Query execution exceeds 10 second timeout → query_timeout error.

## Y. Acceptance Criteria
- All generated SQL passes validation before execution.
- No forbidden SQL keywords ever executed.
- No unauthorized schema ever accessed.
- Row limits always enforced.
- All SQL events appear in audit log.
- Provenance accurately records SQL generated and schemas accessed.

---

# AGENT 5 — DOCUMENT AGENT

## A. Agent ID
`AGT-DOC`

## B. Agent Name
Document Agent

## C. Purpose
The Document Agent parses, extracts structured fields from, and summarizes enterprise documents stored in supported formats (PDF, DOCX, TXT). It is the primary agent for any task involving unstructured or semi-structured document content. It handles document parsing and field extraction; it does not perform semantic similarity search (that is the RAG Agent's responsibility).

## D. Responsibilities
- Retrieve documents from the enterprise document store by document_id or accept document content directly.
- Parse documents in supported formats: PDF (pdfplumber), DOCX (python-docx), TXT.
- Extract plain text content with page/section metadata.
- Perform structured field extraction: apply LLM with a defined extraction schema to extract named fields (e.g. invoice_number, total_amount, date, vendor_name).
- Produce document summaries using LLM with a summarization prompt.
- Return full text on request.
- Check document access permissions before processing.
- Record provenance: document_id, operation, fields extracted, timestamp.

## E. Non-Responsibilities
- Must NOT perform semantic similarity search (RAG Agent handles this).
- Must NOT ingest documents into pgvector (Ingestion Pipeline handles this).
- Must NOT modify or delete documents.
- Must NOT access relational database tables (SQL/Data Agent handles this).
- Must NOT access documents outside its permission manifest.
- Must NOT return raw document content to callers who do not have read permission.
- Must NOT extract fields outside the defined extraction schema for the operation.

## F. Inputs
- `document_id` (string) OR `document_content` (bytes)
- `format` (string): pdf | docx | txt
- `operation` (string): summarize | extract_fields | full_text
- `extraction_schema` (optional, Pydantic model or field list, required for extract_fields operation)
- `task_id` (for provenance)

## G. Outputs
- On summarize:
  { "document_id", "operation": "summarize", "summary": string, "page_count": int, "provenance_ref" }
- On extract_fields:
  { "document_id", "operation": "extract_fields", "extracted_fields": { field: value }, "missing_fields": [string], "provenance_ref" }
- On full_text:
  { "document_id", "operation": "full_text", "text_content": string, "page_count": int, "metadata": { ... }, "provenance_ref" }
- On failure:
  { "status": "error", "error_code": string, "detail": string }

## H. Required Context
- Document identifier or content
- Operation type
- Extraction schema (if extract_fields)
- Calling workflow's access permission for the document
- task_id

## I. Tools It May Use
- Document Store Retrieval Tool (fetches document bytes by document_id)
- PDF Parser Tool (pdfplumber)
- DOCX Parser Tool (python-docx)
- TXT Parser Tool (plain text read)
- LLM Inference Tool (summarization and field extraction prompts)
- Extraction Schema Validation Tool (Pydantic, validates extracted output against schema)
- Audit Logger

## J. Data Sources It May Access
- Enterprise document store (read-only): PDFs, DOCX, TXT files
- Document metadata table (read-only): document_id, title, access_level, owner

## K. Permissions
- Read documents from document store (access_level must be within user's authorized ceiling)
- Read document metadata table
- Invoke LLM for summarization and extraction
- Append to audit log

## L. Prohibited Operations
- Cannot write, modify, or delete documents
- Cannot ingest documents into pgvector
- Cannot access relational database tables
- Cannot access documents with access_level above the authorized ceiling
- Cannot extract fields not defined in the extraction_schema
- Cannot call external APIs or services
- Cannot return document content to callers without verified read permission

## M. Risk Levels
- Summarizing non-sensitive documents: Low
- Extracting fields from financial documents: Medium
- Extracting fields from HR documents with PII: Medium-High
- Accessing restricted documents: High — requires elevated role check

## N. Human Approval Conditions
- Accessing a document tagged access_level: restricted requires that the calling workflow be verified by the Security/Policy Agent before Document Agent proceeds. If not authorized, the request is blocked (not escalated to human approval).
- No routine human approval required for standard document operations.

## O. Validation Requirements
- Document must exist and access_level must be within authorized ceiling before any processing.
- Parsed text must be non-empty; empty parse result treated as parsing error.
- Extracted fields output must be validated against extraction_schema (Pydantic).
- Missing required fields must be listed explicitly; extraction is not silently partial.

## P. Provenance Requirements
- agent_id: AGT-DOC
- document_id
- operation performed
- format
- page_count (if applicable)
- fields_extracted (names only, not values, for audit efficiency)
- missing_fields
- LLM model used (for extraction/summarization)
- timestamp

## Q. Audit Events
- DOC_ACCESS_REQUESTED: task_id, document_id, operation, access_level
- DOC_ACCESS_GRANTED: task_id, document_id
- DOC_ACCESS_DENIED: task_id, document_id, reason
- DOC_PARSED: task_id, document_id, format, page_count
- DOC_EXTRACTION_COMPLETED: task_id, document_id, fields_extracted_count, missing_fields_count
- DOC_SUMMARIZATION_COMPLETED: task_id, document_id, summary_length
- DOC_PARSE_FAILED: task_id, document_id, error

## R. Failure Conditions
- Document not found → error: document_not_found
- Access denied → error: document_access_denied; audit entry
- Unsupported format → error: unsupported_format; returns supported_formats list
- Parse failure (corrupted file) → error: parse_failure; detail included
- LLM extraction returns no fields → partial result with all fields in missing_fields
- LLM timeout → error: llm_timeout

## S. Retry/Recovery Behavior
- LLM extraction/summarization timeout: retry once.
- Parse failure: no retry (document is corrupted; retry will not help).
- Document retrieval failure: retry once after 2s.

## T. Escalation Behavior
- Persistent LLM failure after retry → return error to Orchestrator; Orchestrator handles task failure or degraded completion.
- Access denied → block immediately; no escalation to human (policy enforcement).

## U. Agent Interactions
- ← Orchestrator (receives operation request; returns document result)
- ← Finance Agent (may delegate invoice document extraction)
- → LLM (summarization, extraction)

## V. Performance Considerations
- Document retrieval: target < 1 second
- PDF parsing (10 pages): target < 3 seconds
- LLM extraction (standard invoice): target < 15 seconds
- LLM summarization (10 pages): target < 20 seconds

## W. Security Constraints
- Access-level check performed before any document bytes are loaded into memory.
- Document content must not be logged (only metadata in audit entries).
- Extraction schema must not include fields outside the agent's permitted data scope.
- LLM prompts must not include content from documents the current user is not authorized to view.
- No cross-task document content caching.

## X. Test Scenarios
1. PDF invoice → extract_fields → returns invoice_number, total_amount, date, vendor_name.
2. DOCX policy document → summarize → returns summary with page_count.
3. Document not found → document_not_found error.
4. Unsupported format (XLSX) → unsupported_format error with supported_formats list.
5. Restricted document without authorization → document_access_denied error and audit entry.
6. LLM extraction misses one field → partial result with missing_fields list.
7. Corrupted PDF → parse_failure error.

## Y. Acceptance Criteria
- Extraction always returns missing_fields list, never silently partial.
- Access denied always audited.
- Document content never appears in logs.
- Provenance entry always includes document_id, operation, and fields_extracted_count.
- Unsupported formats return structured error with supported format list.

---

# AGENT 6 — ANALYTICS AGENT

## A. Agent ID
`AGT-ANA`

## B. Agent Name
Analytics Agent

## C. Purpose
The Analytics Agent performs quantitative analysis on structured enterprise data: computing aggregations, KPIs, trends, and period-over-period comparisons, and generating human-readable narrative interpretations grounded in the computed metrics. It delegates raw data retrieval to the SQL/Data Agent and does not query the database directly.

## D. Responsibilities
- Receive an analysis_goal and data specification from the Orchestrator.
- Determine which data is needed and delegate data retrieval to the SQL/Data Agent.
- Apply quantitative computations to the retrieved dataset: sums, averages, percentages, growth rates, KPIs.
- Handle edge cases in computation: null values, empty datasets, zero denominators.
- Generate a human-readable narrative summary of computed metrics using the LLM.
- Return structured metrics alongside the narrative.
- Enforce PII aggregation constraint: individual records not exposed; only aggregated results returned.
- Record provenance: data_source, metric_definitions, computation_summary, narrative.

## E. Non-Responsibilities
- Must NOT query databases directly (delegates to SQL/Data Agent).
- Must NOT access vector stores or document stores.
- Must NOT access finance-specific records (budget approvals, invoice approval workflows) — Finance Agent handles this.
- Must NOT expose individual employee or customer records even if available in the dataset.
- Must NOT generate metrics using data outside the scope of the analysis goal.
- Must NOT call external APIs or services.
- Must NOT perform SQL generation itself.

## F. Inputs
- `analysis_goal` (string): natural language description of the analysis objective
- `data_source` (string): table or view reference
- `time_range` (optional dict): { from_date, to_date }
- `group_by` (optional list): fields to group by
- `metric_definitions` (optional dict): custom KPI definitions
- `task_id`

## G. Outputs
- On success:
  {
    "metrics": { "metric_name": { "value", "unit", "period", "group_label" } },
    "computation_notes": string,
    "narrative": string,
    "data_reference": string,
    "pii_aggregated": bool,
    "provenance_ref": string
  }
- On failure:
  { "status": "error", "error_code": string, "detail": string }

## H. Required Context
- Analysis goal
- Data source specification
- Time range and grouping parameters
- SQL/Data Agent availability
- Metric definitions (if custom)
- task_id

## I. Tools It May Use
- SQL/Data Agent (delegated data retrieval — not a direct DB connection)
- LLM Inference Tool (narrative generation)
- Computation Library (Python numeric computation: statistics, aggregation)
- Audit Logger

## J. Data Sources It May Access
- Indirectly: only data returned by the SQL/Data Agent for permitted schemas
- Does NOT hold a direct database connection
- Does NOT access pgvector or document stores

## K. Permissions
- Invoke SQL/Data Agent for data retrieval
- Invoke LLM for narrative generation
- Read metric_definitions config (read-only)
- Append to audit log

## L. Prohibited Operations
- Cannot query databases directly
- Cannot expose individual-level records (PII aggregation constraint)
- Cannot use data not provided by the SQL/Data Agent in this workflow
- Cannot access finance approval workflows or HR PII fields without explicit elevated permission
- Cannot call external APIs

## M. Risk Levels
- Aggregate analytics on non-sensitive data: Low
- Analytics involving financial summaries: Medium
- Analytics involving HR headcount data: Low (aggregated)
- Analytics involving individual PII: High — requires PII permission and elevated audit logging

## N. Human Approval Conditions
- None for standard aggregate analytics.
- If analysis requires individual-level PII data, requesting user must hold PII permission (verified by RBAC); if not held, request blocked.

## O. Validation Requirements
- Dataset returned by SQL/Data Agent must be non-empty (or return empty_dataset error gracefully).
- Computed metrics must reference only fields present in the dataset.
- Narrative must not contain numerical values not present in the metrics dict (verified by Reviewer).
- PII aggregation constraint must be verified: result must not contain individual names, IDs, or row-level records.

## P. Provenance Requirements
- agent_id: AGT-ANA
- analysis_goal
- data_source_reference (table/view name)
- sql_agent_query_ref (provenance ref from SQL/Data Agent output)
- metric_names_computed
- pii_aggregated: bool
- narrative_length
- LLM model used for narrative
- timestamp

## Q. Audit Events
- ANALYTICS_STARTED: task_id, analysis_goal_length, data_source
- SQL_DELEGATION_SENT: task_id, query_goal
- SQL_DELEGATION_RECEIVED: task_id, row_count
- METRICS_COMPUTED: task_id, metric_count
- NARRATIVE_GENERATED: task_id, narrative_length
- PII_AGGREGATION_APPLIED: task_id, fields_aggregated
- ANALYTICS_FAILED: task_id, error_code

## R. Failure Conditions
- SQL/Data Agent returns error → error: data_retrieval_failed
- Empty dataset → error: empty_dataset (not a general failure; returned as structured result)
- Computation error (e.g. unexpected data type) → error: computation_error
- LLM narrative generation timeout → partial result returned with metrics but no narrative
- PII violation detected → request blocked; error: pii_access_denied

## S. Retry/Recovery Behavior
- SQL/Data Agent delegation failure: retry once (Orchestrator handles SQL agent retry separately).
- LLM narrative timeout: retry once; on failure return metrics without narrative (graceful degradation).

## T. Escalation Behavior
- Persistent data retrieval failure → return error to Orchestrator; Orchestrator handles task failure.
- PII access denied → block immediately; no human escalation (policy enforcement).

## U. Agent Interactions
- ← Orchestrator (receives analysis goal; returns metrics and narrative)
- → SQL/Data Agent (delegates data retrieval)
- → LLM (narrative generation)

## V. Performance Considerations
- SQL delegation + retrieval: target < 20 seconds
- Metric computation (Python, in-memory): target < 1 second for standard datasets
- LLM narrative generation: target < 15 seconds
- Total analytics response: target < 40 seconds

## W. Security Constraints
- PII aggregation constraint enforced at code level, not reliant on LLM judgment.
- Narrative prompt instructs LLM to use only provided metrics; Reviewer validates.
- No individual-level data retained after computation; only aggregated results stored in WorkflowContext.
- Service token required for all calls to SQL/Data Agent.

## X. Test Scenarios
1. "Q3 revenue by product" → valid SQL delegation → metrics computed → narrative generated.
2. Empty dataset → empty_dataset response with structured metadata.
3. Dataset with null values → metrics computed with nulls handled (null values noted in computation_notes).
4. Zero denominator in percentage calculation → null value with note; no exception raised.
5. LLM narrative timeout → metrics returned; narrative = null; partial_result flag set.
6. Narrative references a value not in metrics dict → Reviewer flags it.
7. Analysis requiring individual employee salaries without PII permission → pii_access_denied error.

## Y. Acceptance Criteria
- Metrics always reference only fields in the dataset.
- PII aggregation constraint verified by automated test.
- Empty dataset returns structured response, not an exception.
- Narrative failures result in graceful degradation with metrics returned.
- All analytics events appear in audit log.
- Provenance includes data_source_reference and sql_agent_query_ref.
