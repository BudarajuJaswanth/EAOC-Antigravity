# EAOC Agent Specification — Part C (Agents 10-12)
# Phase 0 · Part 0.2 · Version 1.0

---

# AGENT 10 — DEVELOPER AGENT (Human-in-the-Loop Interactive)

## A. Agent ID
`AGT-DEV`

## B. Agent Name
Developer Agent

## C. Purpose
The Developer Agent is a Human-in-the-Loop (HITL) interactive agent for code-related assistance. It can inspect user-provided code or error context, identify likely problem areas, explain issues, propose corrected code, and — in a controlled sandboxed environment only — execute tests to verify proposed fixes. It returns evidence of any test execution. It never deploys production changes, never executes arbitrary commands, and never accesses unrestricted enterprise resources. All code execution requires explicit sandbox permission in the agent manifest and produces an evidence artifact.

## D. Responsibilities
- Receive a developer task from the Orchestrator with code/error context provided by the user.
- Analyze the provided code or error message to identify the issue.
- Explain the identified issue in clear natural language.
- Propose corrected or improved code with explanation.
- If sandbox execution is available and in the permission manifest: run specified tests in the isolated sandbox; capture stdout/stderr; return a test evidence record.
- Verify that proposed changes satisfy the user's stated goal based on test output.
- Return a structured result with explanation, proposed code, and (if applicable) test evidence.
- For code review operations: return structured review findings with severity levels.
- For documentation generation: return markdown documentation for provided code.

## E. Non-Responsibilities
- Must NOT execute code outside a defined, isolated sandbox environment.
- Must NOT deploy any changes to production systems.
- Must NOT modify source code repositories or CI/CD pipelines automatically.
- Must NOT access enterprise databases, document stores, or financial/HR/IT data.
- Must NOT execute destructive OS commands (rm, format, kill, shutdown, etc.).
- Must NOT install packages or modify the execution environment beyond the sandbox.
- Must NOT generate code containing hardcoded credentials, API keys, or secrets.
- Must NOT bypass any security control as part of a "fix".
- Must NOT autonomously approve its own test results as sufficient for production deployment.

## F. Inputs
- `dev_task` (string): natural language description of the developer task
- `operation` (string): explain | review | document | generate | debug_with_test
- `code_snippet` (optional string): the code to analyze/review/fix (user-provided)
- `error_context` (optional string): error message, stack trace, or test failure output
- `language` (optional string): programming language
- `test_command` (optional string): test command to run in sandbox (only for debug_with_test operation; requires sandbox permission in manifest)
- `task_id`

## G. Outputs
- explain: { "operation", "explanation": string, "identified_issues": [...], "provenance_ref" }
- review: { "operation", "findings": [{ "line", "severity", "description", "suggestion" }], "overall_assessment": string, "provenance_ref" }
- document: { "operation", "documentation": string, "language", "provenance_ref" }
- generate: { "operation", "generated_code": string, "explanation": string, "language", "ai_generated_label": true, "provenance_ref" }
- debug_with_test: { "operation", "proposed_fix": string, "explanation": string, "test_evidence": { "command_run", "stdout", "stderr", "exit_code", "sandbox_id", "executed_at" }, "fix_verified": bool, "provenance_ref" }
- On failure: { "status": "error", "error_code": string, "detail": string }

## H. Required Context
- User-provided code snippet or error context
- Operation type
- Language (if provided)
- Sandbox availability (from permission manifest)
- task_id and user_id (for HITL traceability)

## I. Tools It May Use
- LLM Inference Tool (all operations: explanation, generation, review, documentation)
- Sandboxed Code Execution Tool (ONLY for debug_with_test operation AND ONLY if sandbox_execution: true in agent manifest)
- Audit Logger

## J. Data Sources It May Access
- User-provided code/error context only (passed as input)
- No enterprise databases, document stores, or APIs

## K. Permissions
- Invoke LLM for all code operations
- Invoke Sandboxed Code Execution Tool (ONLY if sandbox_execution: true in manifest)
- Append to audit log

## L. Prohibited Operations
- Cannot access any enterprise database (finance, hr, it, analytics, or otherwise)
- Cannot execute code outside the sandbox
- Cannot run OS-level destructive commands under any circumstance
- Cannot deploy to production, staging, or any shared environment
- Cannot modify CI/CD pipelines, repositories, or infrastructure
- Cannot include hardcoded secrets in generated code
- Cannot autonomously apply fixes to production systems
- Cannot execute test commands not explicitly provided by the user in the task input

## M. Risk Levels
- explain, review, document: Low
- generate: Low-Medium (generated code reviewed by user before use)
- debug_with_test (sandbox execution): Medium — full audit trail required
- Any production deployment (not permitted): High

## N. Human Approval Conditions
- debug_with_test operation: the user is the human-in-the-loop by providing the code, error context, and test command. The agent proposes the fix; the user must decide whether to apply it to production. The agent does NOT auto-apply.
- Sandbox execution itself does not require a separate human approval gate, but must be in the agent manifest.
- Any future capability to apply fixes to production would require explicit human approval; this is NOT currently in scope.

## O. Validation Requirements
- Generated code must be checked by LLM for presence of hardcoded credentials before returning (credential scan prompt).
- All generated code must include ai_generated_label: true in the response.
- Test evidence must include sandbox_id (isolated environment identifier), stdout, stderr, and exit_code.
- fix_verified = true only if test exit_code = 0 and test output contains no failure indicators.

## P. Provenance Requirements
- agent_id: AGT-DEV
- operation
- language
- code_snippet_hash (SHA-256 of input code; not the code itself for audit log)
- llm_model_used
- sandbox_id (if sandbox execution used)
- test_command (if used)
- fix_verified (if debug_with_test)
- timestamp

## Q. Audit Events
- DEV_TASK_RECEIVED: task_id, operation, user_id, language
- DEV_LLM_INVOKED: task_id, operation, input_length
- DEV_SANDBOX_EXECUTION_STARTED: task_id, sandbox_id, test_command
- DEV_SANDBOX_EXECUTION_COMPLETED: task_id, sandbox_id, exit_code, fix_verified
- DEV_CREDENTIAL_DETECTED_IN_OUTPUT: task_id (if scan detects hardcoded credential — output blocked)
- DEV_RESULT_RETURNED: task_id, operation, fix_verified (if applicable)

## R. Failure Conditions
- LLM timeout → error: llm_timeout
- Sandbox unavailable (sandbox_execution in manifest but sandbox offline) → error: sandbox_unavailable
- Credential detected in generated output → output blocked; error: credential_in_output; audit entry
- Input code exceeds size limit → error: input_too_large
- Unsupported operation → error: unsupported_operation

## S. Retry/Recovery Behavior
- LLM timeout: retry once.
- Sandbox unavailable: no retry; return error immediately (sandbox must be explicitly available).
- Credential detected: do not retry; return error and require user to request without the credential.

## T. Escalation Behavior
- Credential detected in output → block and log; Orchestrator marks step failed with reason credential_in_output.
- Sandbox unavailable → return structured error to Orchestrator.

## U. Agent Interactions
- ← Orchestrator (receives task; returns result)
- → LLM (all operations)
- → Sandboxed Execution Tool (debug_with_test only)
- User is the implicit human-in-the-loop through task input provision

## V. Performance Considerations
- explain/review/document: target < 20 seconds
- generate: target < 20 seconds
- debug_with_test (LLM + sandbox): target < 60 seconds (sandbox startup + test run variable)

## W. Security Constraints
- Sandbox must be a fully isolated container with no network access and no access to enterprise resources.
- Sandbox must be ephemeral: destroyed after test execution completes.
- Generated code scanned for hardcoded credentials before returning.
- All sandbox executions recorded with sandbox_id for forensic traceability.
- User-provided code is treated as untrusted input; it is passed to LLM only, never executed outside sandbox.

## X. Test Scenarios
1. explain operation on a Python function with a bug → explanation identifies the bug and describes it.
2. review operation on a Python snippet → structured findings with severity levels returned.
3. generate SQL query for a business requirement → valid SQL returned with ai_generated_label: true and no hardcoded credentials.
4. debug_with_test with a failing unit test → proposed fix + sandbox execution + test_evidence with exit_code=0; fix_verified: true.
5. debug_with_test with sandbox offline → sandbox_unavailable error; no code executed.
6. Generated code containing a hardcoded password → credential_in_output error; output blocked; audit entry.
7. developer role required; viewer role attempt → RBAC denial + audit entry.

## Y. Acceptance Criteria
- No code is ever executed outside the sandbox.
- All generated code includes ai_generated_label: true.
- Hardcoded credential detection blocks output in 100% of test cases.
- debug_with_test always returns test_evidence including sandbox_id, stdout, stderr, exit_code.
- fix_verified is only true when test exit_code = 0.
- All sandbox executions appear in audit log with sandbox_id.

---

# AGENT 11 — REVIEWER / VALIDATION AGENT

## A. Agent ID
`AGT-REV`

## B. Agent Name
Reviewer / Validation Agent

## C. Purpose
The Reviewer/Validation Agent is the independent quality and compliance gate for all EAOC workflow outputs. It verifies output completeness, factual grounding, internal consistency, policy compliance, and schema conformance before the Orchestrator assembles the final response. It may recommend approval, escalation to human review, or rejection. It does not perform domain work; it validates the work of other agents. It cannot grant permissions that no other component has granted, and it cannot auto-approve outputs from agents that required human approval.

## D. Responsibilities
- Receive the assembled WorkflowContext output from the Orchestrator after all agent steps complete.
- Verify completeness: all required output fields present and non-null.
- Verify factual grounding: all numerical claims and data references traceable to source data in WorkflowContext provenance.
- Verify internal consistency: no contradictions between outputs from different agent steps.
- Verify schema conformance: output structure matches the expected response schema.
- Verify policy compliance: output does not contain policy violations (e.g. PII in a non-PII-authorized context, finance data returned to non-finance role).
- Assess risk of the assembled output and produce a risk_level and recommendation.
- Return a ReviewResult with: passed flag, findings list, risk_assessment, and recommendation.
- If passed: signal Orchestrator to proceed to final response assembly.
- If escalate: signal Orchestrator to create a human review request.
- If reject: signal Orchestrator to fail the task or return to re-execution.

## E. Non-Responsibilities
- Must NOT modify the output content it is reviewing (annotation only).
- Must NOT grant permissions not held by the requesting user or workflow.
- Must NOT auto-approve outputs from steps that required human approval.
- Must NOT perform domain reasoning (no SQL generation, RAG retrieval, document parsing, etc.).
- Must NOT access enterprise data sources directly.
- Must NOT make irreversible decisions; all rejections and escalations go through Orchestrator.
- Must NOT block outputs based on subjective quality alone; findings must be specific and rule-based.

## F. Inputs
- All step outputs from WorkflowContext (assembled output)
- Original task_description
- ExecutionPlan (to know required output fields and validation criteria)
- Provenance chain from WorkflowContext
- Validation criteria and policy rules (loaded from config)
- risk_classification_rules

## G. Outputs
- ReviewResult:
  {
    "review_id": string,
    "task_id": string,
    "passed": bool,
    "findings": [
      { "finding_id", "type": "completeness" | "grounding" | "consistency" | "schema" | "policy" | "risk",
        "severity": "error" | "warning", "description": string, "field_reference": string (optional) }
    ],
    "risk_assessment": { "risk_level": "low" | "medium" | "high", "risk_factors": [...] },
    "recommendation": "approve" | "escalate" | "reject",
    "reviewed_at": timestamp
  }

## H. Required Context
- Complete WorkflowContext with all step outputs
- Provenance chain
- Original task_description
- ExecutionPlan required_fields
- Validation and policy rules
- task_id

## I. Tools It May Use
- LLM Inference Tool (for consistency and grounding checks — LLM assists but is not sole authority)
- Schema Validation Tool (Pydantic, for output schema conformance)
- Policy Rule Engine (rule-based checks for PII, role constraints, data sensitivity)
- Risk Classification Tool (rule-based risk assessment)
- Audit Logger

## J. Data Sources It May Access
- WorkflowContext (read-only, passed by Orchestrator — no direct DB access)
- Provenance chain (read-only, passed by Orchestrator)
- Validation rules config (read-only, loaded at startup)
- Policy rules config (read-only, loaded at startup)

## K. Permissions
- Read WorkflowContext (read-only view)
- Read provenance chain (read-only)
- Invoke LLM for grounding and consistency checks
- Invoke Policy Rule Engine
- Append to audit log

## L. Prohibited Operations
- Cannot modify any output content
- Cannot grant permissions to outputs (no permission elevation)
- Cannot auto-approve outputs that required human approval at any prior step
- Cannot access enterprise databases, vector stores, or document stores directly
- Cannot make irreversible decisions independently; all decisions go through Orchestrator
- Cannot override Security/Policy Agent decisions

## M. Risk Levels
- Reviewer operation itself: Low (read-only validation)
- Outputs it processes may be of any risk level; risk is passed through, not generated by the Reviewer

## N. Human Approval Conditions
- If Reviewer recommendation = escalate: Orchestrator creates a HumanApprovalRequest for the assembled output.
- Reviewer LLM failure: Orchestrator escalates to human review (fail-escalate, not fail-approve).

## O. Validation Requirements
- All validation checks must produce specific, machine-readable findings (not vague LLM text).
- LLM is used to assist grounding and consistency checks but findings must be rule-anchored.
- Risk assessment must apply the same risk classification rules used by the Planner.
- Reviewer must not pass an output with error-severity findings.

## P. Provenance Requirements
- agent_id: AGT-REV
- review_id
- task_id
- findings_count (by severity)
- recommendation
- risk_level
- reviewed_at
- llm_used_for_checks: bool

## Q. Audit Events
- REVIEW_STARTED: task_id, review_id
- REVIEW_COMPLETENESS_FAILED: task_id, missing_fields
- REVIEW_GROUNDING_FAILED: task_id, ungrounded_claims_count
- REVIEW_POLICY_VIOLATION: task_id, violation_type
- REVIEW_SCHEMA_VIOLATION: task_id, schema_errors
- REVIEW_PASSED: task_id, risk_level
- REVIEW_ESCALATED: task_id, reason
- REVIEW_REJECTED: task_id, reason
- REVIEW_LLM_FAILED: task_id (triggers escalation)

## R. Failure Conditions
- LLM failure during grounding/consistency check → escalate to human review (fail-escalate, NOT fail-approve)
- Validation rules not loaded → reject all outputs with reason: rules_unavailable
- Missing provenance chain → finding: provenance_incomplete; recommendation: escalate

## S. Retry/Recovery Behavior
- LLM failure during review: retry once; on failure escalate (not approve).
- Schema validation failure is deterministic; no retry needed.

## T. Escalation Behavior
- recommendation = escalate → Orchestrator creates HumanApprovalRequest for human reviewer.
- recommendation = reject → Orchestrator re-runs failing steps or fails task.
- LLM failure → automatic escalation to human review.

## U. Agent Interactions
- ← Orchestrator (receives assembled output; returns ReviewResult)
- → LLM (grounding and consistency assistance)
- → Policy Rule Engine (policy compliance)
- → Risk Classification Tool (risk assessment)

## V. Performance Considerations
- Schema validation: target < 500ms
- Policy rule check: target < 500ms
- LLM grounding check: target < 20 seconds
- Total review: target < 30 seconds

## W. Security Constraints
- Reviewer has read-only access to WorkflowContext; cannot write to it.
- LLM prompts for review must include only the data present in WorkflowContext; no external data fetching.
- Reviewer findings must not expose internal system details in the final response.
- Reviewer cannot be invoked by specialized agents; only by the Orchestrator.

## X. Test Scenarios
1. Complete, grounded output → ReviewResult: passed=true, recommendation=approve.
2. Output missing required field → finding: completeness error; passed=false; recommendation=reject.
3. Narrative claims $500K revenue when source data shows $400K → grounding finding; passed=false.
4. Finance data returned in output for non-finance role → policy violation finding; passed=false; recommendation=reject.
5. Reviewer LLM failure → escalate to human review; no auto-approve.
6. Output with warning-level finding only → passed=true; recommendation=approve with warnings noted.
7. High-risk output → risk_level=high; recommendation=escalate.

## Y. Acceptance Criteria
- Reviewer never auto-approves on LLM failure.
- Every error-severity finding results in passed=false.
- Policy violations always result in reject or escalate, never approve.
- All review events appear in audit log.
- Provenance entry includes finding counts and recommendation.

---

# AGENT 12 — SECURITY / POLICY AGENT

## A. Agent ID
`AGT-SEC`

## B. Agent Name
Security / Policy Agent

## C. Purpose
The Security/Policy Agent is the runtime policy enforcement and guardrail component of EAOC. It evaluates agent actions, tool invocations, and workflow outputs against defined enterprise policies and security rules. It is invoked at key enforcement points: pre-execution (can this agent call this tool?), post-execution (does this output comply with policy?), and during risk assessment. It is NOT an unrestricted administrator. It can block actions and record violations; it cannot grant permissions that do not exist in the policy definitions.

## D. Responsibilities
- Load and maintain enterprise policy definitions at system startup.
- Enforce agent permission manifests: verify that any agent-tool invocation is within the agent's manifest before execution.
- Perform input guardrail checks: detect prompt injection patterns, sensitive data in inputs, and policy-violating request patterns.
- Perform output guardrail checks: detect PII in unauthorized contexts, sensitive financial data in unauthorized responses, and policy-violating content.
- Classify risk level of actions and outputs using rule-based risk classification.
- Block policy-violating actions and record PolicyViolation events in the audit log.
- Support the Reviewer/Validation Agent's policy compliance checks.
- Evaluate tool call parameters for policy compliance before execution.

## E. Non-Responsibilities
- Must NOT grant permissions not defined in policy configurations.
- Must NOT become an administrator that overrides RBAC.
- Must NOT perform domain work (no SQL, no RAG, no document processing).
- Must NOT modify policy definitions at runtime (policies are immutable at runtime).
- Must NOT make final authorization decisions for RBAC (RBAC module handles user-level authorization; this agent handles agent-level and content-level policy).
- Must NOT autonomously approve high-risk actions; it assesses risk and blocks or flags, not approves.
- Must NOT be invokable or modifiable by other specialized agents.

## F. Inputs
- Policy check requests from:
  - Tool execution layer: { agent_id, tool_id, parameters }
  - Orchestrator: { action_type, agent_id, data_class, operation }
  - Reviewer/Validation Agent: { output_content, context, policy_scope }
- Policy definitions (loaded at startup from config)
- Agent permission manifests (loaded at startup)

## G. Outputs
- PolicyCheckResult:
  {
    "check_id": string,
    "allowed": bool,
    "violations": [{ "policy_id", "policy_name", "violation_type", "description" }],
    "policy_references": [string],
    "risk_level": "low" | "medium" | "high",
    "checked_at": timestamp
  }

## H. Required Context
- Agent permission manifests (loaded at startup, read-only)
- Policy definitions (loaded at startup, read-only)
- Risk classification rules (loaded at startup, read-only)
- Action details being evaluated

## I. Tools It May Use
- Policy Rule Engine (internal, rule-based evaluation against loaded policy definitions)
- Agent Manifest Lookup (in-memory, read-only)
- Risk Classification Tool (rule-based)
- Audit Logger
- Input Sanitization Tool (prompt injection pattern detection)

## J. Data Sources It May Access
- Policy definitions config (read-only, in-memory after startup load)
- Agent permission manifests (read-only, in-memory after startup load)
- Risk classification rules (read-only, in-memory after startup load)
- NO enterprise databases, document stores, or vector stores

## K. Permissions
- Read agent permission manifests (in-memory)
- Read policy definitions (in-memory)
- Append to audit log
- Block tool invocations (by returning allowed=false to the tool execution layer)

## L. Prohibited Operations
- Cannot grant permissions not in policy definitions
- Cannot modify policy definitions or agent manifests at runtime
- Cannot be invoked by specialized agents (only by Orchestrator and tool execution layer)
- Cannot access any enterprise database or document store
- Cannot override RBAC decisions
- Cannot autonomously approve any action regardless of risk level
- Cannot be bypassed by any agent or system component

## M. Risk Levels
- Policy check operation itself: Low (read-only evaluation)
- The actions it evaluates may be of any risk level

## N. Human Approval Conditions
- None. Security/Policy Agent blocks or allows; it does not request human approval.
- High-risk actions are flagged to the Orchestrator which routes to the human approval workflow.

## O. Validation Requirements
- All policy check requests must include agent_id; requests without agent_id are rejected.
- Policy definitions must be fully loaded at startup; partial load causes system startup failure.
- Every PolicyCheckResult must include check_id for audit traceability.

## P. Provenance Requirements
- agent_id: AGT-SEC
- check_id
- action_evaluated
- agent_id_evaluated
- tool_id_evaluated (if tool check)
- allowed: bool
- violation_count
- policy_references_applied
- checked_at

## Q. Audit Events
- POLICY_CHECK_REQUESTED: check_id, agent_id, action_type
- POLICY_CHECK_PASSED: check_id, agent_id, risk_level
- POLICY_VIOLATION_DETECTED: check_id, agent_id, tool_id, violation_type, policy_id
- POLICY_ACTION_BLOCKED: check_id, agent_id, tool_id, reason
- MANIFEST_MISSING: agent_id (logged at startup if manifest not found)
- INPUT_INJECTION_DETECTED: task_id, pattern_detected

## R. Failure Conditions
- Policy definitions not loaded at startup → all policy-governed actions blocked; system startup alert raised
- Policy Rule Engine failure during check → fail-closed: return allowed=false; audit entry
- Agent manifest not found for agent_id → allow=false; audit entry
- Self-unavailability → tool execution layer fails closed (no tool executes without a successful policy check)

## S. Retry/Recovery Behavior
- Policy check is stateless and fast; no retry needed.
- On failure: fail-closed immediately; no retry.

## T. Escalation Behavior
- Policy violation detected → block action; record violation; Orchestrator informed via allowed=false response.
- Persistent policy engine failure → Orchestrator fails the task; system health alert raised.

## U. Agent Interactions
- ← Tool Execution Layer (tool permission checks before every tool call)
- ← Orchestrator (pre-execution and post-execution policy checks)
- ← Reviewer/Validation Agent (output policy compliance checks)
- → Audit Logger (all violations and checks)
- Does NOT interact with any specialized domain agent

## V. Performance Considerations
- Policy check (rule-based, in-memory): target < 10ms
- Manifest lookup: target < 5ms
- This agent must be fast as it is in the critical path for every tool invocation

## W. Security Constraints
- Policy definitions and manifests are immutable at runtime; changes require system re-deployment.
- Security/Policy Agent itself must be isolated — no other agent can invoke it directly or modify its state.
- Its audit log entries are written using the append-only audit log role; cannot be modified.
- Cannot be disabled by any runtime configuration change without full system re-deployment.

## X. Test Scenarios
1. HR Agent attempts to call a Finance tool → manifest check fails; allowed=false; audit entry.
2. SQL Agent queries finance schema within its manifest → allowed=true.
3. Prompt injection pattern detected in task input → injection flagged; task blocked or sanitized; audit entry.
4. Policy definitions not loaded at startup → system fails to start; alert raised.
5. Policy Rule Engine fails mid-check → allowed=false returned immediately (fail-closed); audit entry.
6. Output contains PII in non-PII-authorized context → policy violation detected; output blocked.
7. Agent manifest missing for AGT-DEV → all AGT-DEV tool calls blocked; audit entry.

## Y. Acceptance Criteria
- 100% of tool calls pass through a policy check before execution.
- Every policy violation produces an audit entry with policy_id.
- Fail-closed behavior verified: policy engine failure always results in allowed=false.
- No action is allowed without a successful policy check result.
- Policy definitions immutable at runtime verified by attempt to modify and observe no effect.
