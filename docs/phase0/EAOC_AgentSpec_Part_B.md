# EAOC Agent Specification — Part B (Agents 7-9)
# Phase 0 · Part 0.2 · Version 1.0

---

# AGENT 7 — FINANCE AGENT

## A. Agent ID
`AGT-FIN`

## B. Agent Name
Finance Agent

## C. Purpose
The Finance Agent handles all financial data operations within the EAOC system. It is the authoritative agent for querying invoices, budgets, and expenses from the finance schema. It delegates raw SQL execution to the SQL/Data Agent and applies finance-domain formatting and provenance wrapping to results. Access is strictly limited to users holding the finance_user or admin role.

## D. Responsibilities
- Receive finance queries from the Orchestrator (invoice retrieval, budget queries, expense analysis, financial report generation).
- Determine the appropriate finance sub-operation and delegate structured data retrieval to the SQL/Data Agent.
- Apply finance-domain formatting to results (currency formatting, period labelling, totals).
- Attach provenance metadata to every response.
- Enforce role-based access: only finance_user and admin roles may invoke this agent.
- Log all finance data access to the audit trail.
- Support budget vs. actuals comparison and variance computation.
- Delegate document-based invoice operations (e.g. PDF invoice extraction) to the Document Agent.

## E. Non-Responsibilities
- Must NOT query the finance database directly; all DB access delegated to SQL/Data Agent.
- Must NOT access HR, IT, or analytics schemas.
- Must NOT approve or reject invoices or payments (no write operations on financial records by default).
- Must NOT generate financial forecasts or predictions.
- Must NOT expose raw database records without provenance wrapping.
- Must NOT grant itself access beyond its permission manifest.

## F. Inputs
- `finance_query` (string): natural language finance question
- `entity_type` (string): invoice | budget | expense | report
- `filters` (dict): e.g. { invoice_id, vendor, date_range, department, period, status }
- `task_id`

## G. Outputs
- { "entity_type", "records": [...], "summary": string, "variance" (if budget query), "provenance": { source, agent_id, timestamp } }
- On failure: { "status": "error", "error_code": string, "detail": string }

## H. Required Context
- UserContext with verified finance_user or admin role
- Finance entity type and filters
- SQL/Data Agent availability
- Document Agent availability (for PDF invoice extraction)
- task_id

## I. Tools It May Use
- SQL/Data Agent (delegated finance schema queries)
- Document Agent (delegated invoice PDF extraction)
- LLM Inference Tool (for summary narrative generation only)
- Audit Logger

## J. Data Sources It May Access (via delegation)
- finance.invoices, finance.budgets, finance.expenses, finance.reports (read-only via SQL Agent)
- Enterprise document store for invoice PDFs (read-only via Document Agent)

## K. Permissions
- Invoke SQL/Data Agent with finance schema scope
- Invoke Document Agent for finance documents
- Invoke LLM for summary generation
- Append to audit log

## L. Prohibited Operations
- Cannot INSERT, UPDATE, DELETE, or APPROVE financial records
- Cannot access hr, it, or analytics schemas
- Cannot be invoked by users without finance_user or admin role
- Cannot call external financial APIs or services
- Cannot return raw records without provenance wrapper

## M. Risk Levels
- Read-only invoice/budget query: Medium (financial data is sensitive)
- Financial report generation involving multiple periods: Medium
- Any write to financial records (not currently permitted): High

## N. Human Approval Conditions
- None for read-only queries.
- Any write operation on financial data (if ever elevated) requires human approval.

## O. Validation Requirements
- Role check (finance_user or admin) performed by RBAC before agent invocation.
- All SQL delegations filtered to finance schema only.
- Results must include provenance block; missing provenance causes response rejection by Reviewer.

## P. Provenance Requirements
- agent_id: AGT-FIN, entity_type, filters_applied, sql_agent_ref, document_agent_ref (if used), timestamp

## Q. Audit Events
- FINANCE_QUERY_RECEIVED: task_id, entity_type, user_id
- FINANCE_SQL_DELEGATED: task_id, entity_type
- FINANCE_RESULT_RETURNED: task_id, record_count
- FINANCE_ACCESS_DENIED: task_id, user_id, reason

## R. Failure Conditions
- RBAC denial (wrong role) → access_denied; audit entry
- SQL/Data Agent error → data_retrieval_failed
- No records found → empty result with metadata (not an error)
- LLM summary timeout → return records without summary (graceful degradation)

## S. Retry/Recovery Behavior
- SQL delegation failure: retry once.
- LLM summary timeout: retry once; on failure return records without narrative.

## T. Escalation Behavior
- Persistent data retrieval failure → error to Orchestrator; task fails or degrades.
- RBAC denial → immediate block; no human escalation.

## U. Agent Interactions
- ← Orchestrator
- → SQL/Data Agent (data retrieval)
- → Document Agent (PDF invoice extraction)
- → LLM (summary)

## V. Performance Considerations
- SQL delegation + results: target < 20 seconds
- Summary narrative: target < 15 seconds
- Total: target < 40 seconds

## W. Security Constraints
- Finance schema access enforced at SQL/Data Agent level.
- All finance access logged with user_id.
- Provenance block mandatory; Reviewer rejects responses lacking it.

## X. Test Scenarios
1. finance_user retrieves INV-001 → invoice returned with provenance.
2. hr_user requests invoice → RBAC denial + audit entry.
3. Budget vs. actuals for Q3 → variance computed correctly.
4. No invoices in date range → empty result with metadata.
5. SQL Agent failure → data_retrieval_failed error after one retry.

## Y. Acceptance Criteria
- All finance responses include provenance block.
- Non-finance roles always denied with audit entry.
- No financial records returned without role verification.
- Finance schema access never occurs without finance_user or admin role.

---

# AGENT 8 — HR AGENT

## A. Agent ID
`AGT-HR`

## B. Agent Name
HR Agent

## C. Purpose
The HR Agent handles employee and HR data operations: headcount queries, org structure lookups, HR policy retrieval, and employee information queries. It enforces PII minimization by default — individual PII fields are redacted unless the requesting user holds explicit PII permission. Access is restricted to hr_user and admin roles.

## D. Responsibilities
- Receive HR queries from the Orchestrator.
- Delegate aggregate/structured HR data retrieval to the SQL/Data Agent (hr schema only).
- Delegate HR policy lookups to the Research/RAG Agent.
- Apply PII minimization to all results: redact DOB, salary, home address, contact details for non-PII roles.
- Return structured results with pii_redacted flag.
- Log all HR data access to the audit trail.
- Support org structure and department hierarchy queries.

## E. Non-Responsibilities
- Must NOT handle payroll processing or payment operations.
- Must NOT access disciplinary or performance review records unless explicitly scoped.
- Must NOT query finance, IT, or analytics schemas.
- Must NOT return individual PII to users without explicit PII permission.
- Must NOT modify HR records.
- Must NOT generate employee performance evaluations.

## F. Inputs
- `hr_query` (string): natural language HR question
- `query_type` (string): headcount | org_chart | policy | employee_info
- `department` (optional string)
- `task_id`

## G. Outputs
- { "query_type", "result": { ... }, "pii_redacted": bool, "provenance_ref" }
- On failure: { "status": "error", "error_code": string, "detail": string }

## H. Required Context
- UserContext with verified hr_user or admin role
- PII permission flag from UserContext
- Query type and parameters
- task_id

## I. Tools It May Use
- SQL/Data Agent (delegated hr schema queries)
- Research/RAG Agent (HR policy lookups)
- LLM Inference Tool (limited: for query interpretation only)
- Audit Logger

## J. Data Sources It May Access (via delegation)
- hr.employees, hr.departments, hr.org_chart (read-only via SQL Agent)
- HR policy knowledge base in pgvector (read-only via RAG Agent)

## K. Permissions
- Invoke SQL/Data Agent with hr schema scope
- Invoke RAG Agent for hr_policy namespace
- Append to audit log

## L. Prohibited Operations
- Cannot access finance, it, or analytics schemas
- Cannot return PII fields to non-PII-authorized users
- Cannot modify HR records
- Cannot be invoked without hr_user or admin role
- Cannot access payroll or disciplinary records unless explicitly permitted in manifest

## M. Risk Levels
- Aggregate headcount query: Low
- Org chart query: Low
- Individual employee info (non-PII): Low-Medium
- Individual employee info (PII fields): High — requires PII permission

## N. Human Approval Conditions
- None for aggregate queries.
- Queries requesting PII fields without PII permission: blocked (not escalated).
- No routine human approval required.

## O. Validation Requirements
- Role check (hr_user or admin) before invocation.
- PII redaction applied before results leave the agent.
- pii_redacted flag must be accurate.
- All results include provenance block.

## P. Provenance Requirements
- agent_id: AGT-HR, query_type, department_filter, pii_redacted: bool, sql_agent_ref, rag_agent_ref (if used), timestamp

## Q. Audit Events
- HR_QUERY_RECEIVED: task_id, query_type, user_id
- HR_SQL_DELEGATED: task_id, query_type
- HR_POLICY_DELEGATED: task_id, topic
- HR_PII_REDACTED: task_id, fields_redacted
- HR_RESULT_RETURNED: task_id, record_count, pii_redacted
- HR_ACCESS_DENIED: task_id, user_id, reason

## R. Failure Conditions
- RBAC denial → access_denied; audit entry
- SQL/Data Agent error → data_retrieval_failed
- RAG Agent error → policy_retrieval_failed (graceful: return available data)
- PII access attempted without permission → pii_access_denied; audit entry

## S. Retry/Recovery Behavior
- SQL delegation failure: retry once.
- RAG delegation failure: retry once; on failure return available data without policy excerpt.

## T. Escalation Behavior
- Persistent data retrieval failure → error to Orchestrator.
- PII access denied → immediate block; no human escalation.

## U. Agent Interactions
- ← Orchestrator
- → SQL/Data Agent (structured HR data)
- → Research/RAG Agent (policy lookups)

## V. Performance Considerations
- SQL delegation: target < 15 seconds
- RAG policy lookup: target < 25 seconds
- Total: target < 40 seconds

## W. Security Constraints
- PII redaction applied in-agent before results stored in WorkflowContext.
- hr schema access enforced at SQL/Data Agent level.
- All HR access logged with user_id and pii_redacted flag.

## X. Test Scenarios
1. hr_user requests headcount by department → aggregate count returned, no individual records.
2. hr_user requests employee salary → salary field redacted; pii_redacted: true.
3. PII-authorized admin requests salary → salary returned; pii_redacted: false; audit entry.
4. viewer role requests HR data → RBAC denial + audit entry.
5. HR policy lookup for "remote work" → RAG Agent returns excerpts with citations.

## Y. Acceptance Criteria
- PII fields always redacted for non-PII roles; pii_redacted flag is always accurate.
- All HR access logged with user_id.
- Non-HR roles always denied with audit entry.
- Policy lookups always return source citations.

---

# AGENT 9 — IT / SUPPORT AGENT

## A. Agent ID
`AGT-IT`

## B. Agent Name
IT / Support Agent

## C. Purpose
The IT/Support Agent handles IT service management operations: querying open tickets, checking ticket status, generating resolution recommendations from the IT knowledge base, and performing IT policy lookups. It is restricted to it_user and admin roles and does not expose infrastructure-sensitive details to lower roles.

## D. Responsibilities
- Receive IT queries from the Orchestrator.
- Delegate IT ticket queries to the SQL/Data Agent (it schema).
- Delegate IT knowledge base and policy lookups to the Research/RAG Agent.
- Generate resolution recommendations by combining ticket details with KB article retrieval.
- Return structured ticket data and recommendations.
- Redact infrastructure-sensitive details (IP addresses, internal hostnames, credentials) from responses to non-admin roles.
- Log all IT data access to the audit trail.

## E. Non-Responsibilities
- Must NOT modify or close tickets directly (no write operations by default).
- Must NOT access finance or HR schemas.
- Must NOT expose infrastructure credentials or secrets in responses.
- Must NOT deploy infrastructure changes.
- Must NOT make autonomous decisions to resolve tickets.
- Must NOT call external IT management APIs unless explicitly in permission manifest.

## F. Inputs
- `it_query` (string): natural language IT question
- `operation` (string): query_tickets | ticket_status | recommend_resolution | policy_lookup
- `ticket_id` (optional string)
- `filters` (dict): { priority, status, assignee, category }
- `task_id`

## G. Outputs
- query_tickets: { "tickets": [...], "count", "filters_applied", "provenance_ref" }
- ticket_status: { "ticket_id", "status", "priority", "assignee", "created_at", "provenance_ref" }
- recommend_resolution: { "ticket_id", "recommendation": string, "steps": [...], "sources": [...], "provenance_ref" }
- policy_lookup: { "policy_excerpts": [...], "provenance_ref" }
- On failure: { "status": "error", "error_code": string, "detail": string }

## H. Required Context
- UserContext with verified it_user or admin role
- Operation type and parameters
- ticket_id (for status/recommendation operations)
- task_id

## I. Tools It May Use
- SQL/Data Agent (delegated it schema queries)
- Research/RAG Agent (IT KB and policy lookups)
- LLM Inference Tool (recommendation formatting)
- Audit Logger

## J. Data Sources It May Access (via delegation)
- it.tickets, it.ticket_categories, it.sla_definitions (read-only via SQL Agent)
- IT knowledge base in pgvector (read-only via RAG Agent)

## K. Permissions
- Invoke SQL/Data Agent with it schema scope
- Invoke RAG Agent for it_kb namespace
- Append to audit log

## L. Prohibited Operations
- Cannot write to ticket records (no ticket creation, update, or closure by default)
- Cannot access finance or hr schemas
- Cannot expose infrastructure credentials or secrets
- Cannot deploy or modify infrastructure
- Cannot be invoked without it_user or admin role

## M. Risk Levels
- Ticket query: Low
- Ticket status check: Low
- Resolution recommendation: Low-Medium
- Policy lookup: Low

## N. Human Approval Conditions
- None for standard read-only IT operations.

## O. Validation Requirements
- Role check (it_user or admin) before invocation.
- Infrastructure-sensitive fields (IP addresses, passwords) redacted for non-admin roles.
- Resolution recommendations must cite KB sources.

## P. Provenance Requirements
- agent_id: AGT-IT, operation, ticket_id (if used), sql_agent_ref, rag_agent_ref (if used), timestamp

## Q. Audit Events
- IT_QUERY_RECEIVED: task_id, operation, user_id
- IT_SQL_DELEGATED: task_id, operation
- IT_KB_DELEGATED: task_id, query
- IT_RESULT_RETURNED: task_id, record_count
- IT_ACCESS_DENIED: task_id, user_id, reason
- IT_SENSITIVE_FIELD_REDACTED: task_id, field_name

## R. Failure Conditions
- RBAC denial → access_denied; audit entry
- SQL/Data Agent error → data_retrieval_failed
- RAG Agent error → kb_retrieval_failed (graceful: return ticket data without recommendation)
- Ticket not found → ticket_not_found structured response

## S. Retry/Recovery Behavior
- SQL delegation failure: retry once.
- RAG delegation failure: retry once; on failure return ticket data without KB recommendation.

## T. Escalation Behavior
- Persistent failure → error to Orchestrator.

## U. Agent Interactions
- ← Orchestrator
- → SQL/Data Agent (ticket data)
- → Research/RAG Agent (KB lookups)
- → LLM (recommendation formatting)

## V. Performance Considerations
- SQL delegation: target < 15 seconds
- RAG KB lookup: target < 25 seconds
- Total: target < 40 seconds

## W. Security Constraints
- Infrastructure-sensitive fields redacted before results leave the agent.
- it schema access enforced at SQL/Data Agent level.
- All IT access logged with user_id.

## X. Test Scenarios
1. Query all open P1 tickets → list returned with IDs and statuses.
2. Resolution recommendation for password reset ticket → steps + KB citations returned.
3. viewer role requests IT data → RBAC denial + audit entry.
4. Ticket not found → ticket_not_found structured response.
5. RAG Agent unavailable → ticket data returned without recommendation; partial_result flag.

## Y. Acceptance Criteria
- All IT responses include provenance ref.
- Non-IT roles always denied with audit entry.
- Infrastructure-sensitive fields never exposed to non-admin roles.
- Resolution recommendations always include KB source citations.
