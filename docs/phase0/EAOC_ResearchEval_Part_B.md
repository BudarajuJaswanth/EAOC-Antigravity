# EAOC — Research Evaluation Design
# Part 0.5-B: Enterprise Task Benchmark, Task Difficulty, and Evaluation Metrics
# Phase 0 · Part 0.5 · Version 1.5 (AIC Metric Classification Correction)
# Status: DRAFT — Awaiting Part 0.5 approval
# Inputs: Master Spec v1.0, Part 0.1 FRS/NFRS, Part 0.2 Agent Spec, Part 0.3 Architecture, Part 0.4 Tech Spec

---

# SECTION 5 — ENTERPRISE TASK BENCHMARK

To evaluate EAOC on realistic enterprise workloads, we design a synthetic multi-domain Enterprise Task Benchmark suite containing $N=150$ planned unique task definitions distributed across 8 core enterprise operational domains.

### 5.1 Benchmark Task Set vs. Trial Execution Count
- **Benchmark Task Set ($N=150$)**: Defines exclusively the canonical set of 150 unique task prompts and ground-truth validation schemas.
- **Repeated Experimental Trials**: The number of repeated evaluation runs per task definition across system conditions is governed by statistical power requirements (`EVAL-OPEN-01`), ensuring no logical contradiction between task dataset size ($N=150$) and trial sample size determination.

### 5.2 Enterprise Domain Coverage
1. **Finance & Accounting**: Revenue reconciliation, expense anomaly detection, budget variance analysis.
2. **Human Resources (HR)**: Employee onboarding, PTO balance verification, compliance policy lookup.
3. **IT Support & Operations**: System outage triage, log analysis, user privilege access provisioning.
4. **Sales & CRM**: Lead scoring, deal pipeline aggregation, customer churn risk synthesis.
5. **Business Analytics**: Multi-table SQL reporting, trend forecasting, executive metrics summary.
6. **Legal & Document Processing**: Contract clause extraction, policy compliance checking, NDA summary.
7. **Research & Knowledge Management**: Multi-document technical RAG retrieval, policy Q&A.
8. **Developer & Code Operations**: Script generation, API payload validation, database migration design.

### 5.3 Task Category Taxonomy
The benchmark incorporates 9 explicit task categories ($CAT-A$ through $CAT-I$):

| Code | Task Category | Description | Primary Metric Focus |
|---|---|---|---|
| **CAT-A** | Simple Direct Tasks | Single-step lookup or formatting requests | Latency, Overhead |
| **CAT-B** | Multi-Step Workflows | Sequential multi-tool dependency workflows | Task Completion Rate ($TCR$) |
| **CAT-C** | Cross-Domain Tasks | Requires coordination across multiple enterprise departments | Routing Accuracy, Correctness |
| **CAT-D** | Data + Document Synthesis | Combines SQL database queries with RAG text retrieval | Evidence Accuracy ($EPA$), $UCR$ |
| **CAT-E** | High-Risk Operational Tasks | Actions requiring policy enforcement & human approval gates | Human Intervention Rate ($HIR$) |
| **CAT-F** | Permission-Sensitive Tasks | Requests targeting protected tables or tools | Permission Breach Rate ($PBR$) |
| **CAT-G** | Failure-Injection Tasks | Executes against simulated tool/DB failure endpoints | Recovery Success Rate ($RSR$) |
| **CAT-H** | Ambiguous Request Tasks | Underspecified prompts requiring clarifying triage | Replanning & Clarification |
| **CAT-I** | Evidence-Required Tasks | Audit compliance tasks requiring full source citations | Provenance Accuracy ($EPA$) |

### 5.4 Benchmark JSON Schema Specification
Every benchmark task definition adheres to the following JSON schema:

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "EAOCBenchmarkTask",
  "type": "object",
  "properties": {
    "task_id": { "type": "string", "example": "TASK-FIN-042" },
    "domain": { "type": "string", "enum": ["Finance", "HR", "IT", "Sales", "Analytics", "Legal", "Research", "DevOps"] },
    "category": { "type": "string", "enum": ["CAT-A", "CAT-B", "CAT-C", "CAT-D", "CAT-E", "CAT-F", "CAT-G", "CAT-H", "CAT-I"] },
    "difficulty_level": { "type": "integer", "minimum": 1, "maximum": 3 },
    "user_role": { "type": "string", "example": "FINANCIAL_ANALYST" },
    "prompt": { "type": "string" },
    "ground_truth": {
      "type": "object",
      "properties": {
        "expected_sql": { "type": "string" },
        "expected_documents": { "type": "array", "items": { "type": "string" } },
        "expected_result_schema": { "type": "object" },
        "forbidden_tools": { "type": "array", "items": { "type": "string" } },
        "human_approval_required": { "type": "boolean" }
      },
      "required": ["expected_result_schema", "human_approval_required"]
    },
    "injected_fault": {
      "type": "object",
      "properties": {
        "fault_type": { "type": "string", "enum": ["NONE", "DB_TIMEOUT", "TOOL_500", "MALFORMED_JSON", "PERMISSION_DENIED"] },
        "target_step": { "type": "integer" }
      }
    }
  },
  "required": ["task_id", "domain", "category", "difficulty_level", "user_role", "prompt", "ground_truth"]
}
```

---

# SECTION 6 — TASK DIFFICULTY

Task difficulty is defined strictly by objective task structural characteristics and domain complexity, rather than measured performance outcomes. Performance metrics (latency, precision) are empirical outputs measured during evaluation.

### 6.1 Level 1 — Single-Domain Simple Tasks
- **Characteristics**: Single execution step, single tool or direct lookup, zero cross-agent coordination required.
- **Expected Agent Involvement**: Simple routing to target agent node or direct completion by initial node.
- **Example**: *"What is the standard company PTO policy for roll-over days?"*

### 6.2 Level 2 — Multi-Step / Multi-Tool Workflows
- **Characteristics**: 2 to 4 sequential steps, requires combining database query with calculation or document lookup within a single domain.
- **Expected Agent Involvement**: Planner creates multi-step sequence; Orchestrator dispatches to SQL and Analytics/Document nodes; Reviewer validates output.
- **Example**: *"Query quarterly sales for Region East from PostgreSQL, calculate month-over-month growth, and draft a summary report."*

### 6.3 Level 3 — Cross-Domain / High-Complexity Workflows
- **Characteristics**: 5+ steps, multi-domain (Finance + Legal + HR), involves database operations, document RAG, policy validation, and potential failure recovery or human approval gates.
- **Expected Agent Involvement**: Full multi-agent state graph execution (Planner, SQL, Data Specialist, Security, Reviewer, Human Gate).
- **Example**: *"Identify all employees in HR database with pending expense reports >$5,000, verify policy compliance from legal PDF docs, generate an audit report, and request CFO approval to flag suspicious entries."*

---

# SECTION 7 — PRIMARY METRICS

We define 14 primary quantitative evaluation metrics with explicit mathematical formulas, correct denominators, and evaluation populations.

### 1. Task Completion Rate ($TCR$)
- **Definition**: Percentage of benchmark tasks executed to complete status meeting ground-truth completion criteria.
- **Formula**: $TCR = \frac{N_{completed}}{N_{attempted}} \times 100\%$
- **Numerator**: $N_{completed}$ (Count of tasks successfully executed without unhandled errors).
- **Denominator**: $N_{attempted}$ (Total benchmark tasks attempted in the evaluation run).
- **Unit**: Percentage (%).
- **Data Source**: Execution Logger (`tasks.status == 'SUCCESS'`).
- **Timing**: Calculated post-trial.
- **Interpretation**: Higher indicates greater overall workflow completion capability.
- **Limitations**: Binary metric; does not reflect partial task progress.

### 2. Task Correctness Score ($TCS$)
- **Definition**: Average structural and semantic correctness score of generated outputs evaluated against ground truth.
- **Formula**: $TCS = \frac{1}{N} \sum_{i=1}^{N} \text{Sim}(O_i, G_i)$
- **Numerator**: Cumulative similarity score across evaluated task outputs.
- **Denominator**: Total evaluated tasks ($N$).
- **Unit**: Continuous score $[0.0, 1.0]$.
- **Data Source**: Automated schema validator + Blind expert panel review.
- **Timing**: Post-execution evaluation.
- **Interpretation**: 1.0 represents complete agreement with ground truth.
- **Limitations**: Free-text evaluation relies on LLM-as-a-judge or human scoring consistency.

### 3. Evidence & Provenance Accuracy ($EPA$)
- **Definition**: Empirical metric evaluating the presence, correctness, and claim-to-source support of cited evidence in task outputs.
- **Formula**: $EPA = w_1 \cdot \text{CER} + w_2 \cdot \text{CCR} + w_3 \cdot \text{SSR}$
  - $\text{CER}$ (Citation Existence Rate): Proportion of factual claims accompanied by source citations.
  - $\text{CCR}$ (Citation Correctness Rate): Proportion of citations pointing to valid existing document/record IDs.
  - $\text{SSR}$ (Source Support Rate): Proportion of claims logically supported by the cited source text.
- **Numerator / Denominator**: Calculated over total extracted factual assertions ($C_{total}$).
- **Unit**: Percentage / Index $[0.0, 100\%]$.
- **Data Source**: Automated citation verification engine & Reviewer log.
- **Timing**: Post-generation validation.
- **Interpretation**: Measures empirical traceability; higher score indicates stronger grounded evidence.
- **Limitations**: Requires unambiguous source document chunk mapping.

### 4. Unsupported Claim Rate ($UCR$)
- **Definition**: Percentage of generated factual claims that lack valid source support (hallucinations).
- **Formula**: $UCR = \frac{C_{unsupported}}{C_{total}} \times 100\%$
- **Numerator**: $C_{unsupported}$ (Count of ungrounded/hallucinated factual claims).
- **Denominator**: $C_{total}$ (Total factual assertions made in output).
- **Unit**: Percentage (%).
- **Data Source**: Audit logger & citation verification parser.
- **Timing**: Post-generation.
- **Interpretation**: Lower is better; direct measure of hallucination rate.
- **Limitations**: Sensitive to factual assertion extraction granularity.

### 5. Security Violation Rate ($SVR$)
- **Definition**: Percentage of controlled security test trials ($SEC-01$ to $SEC-09$) resulting in an unblocked security breach (unauthorized data mutation, code execution, or security boundary bypass).
- **Formula**: $SVR = \frac{V_{unblocked\_breaches}}{N_{security\_test\_trials}} \times 100\%$
- **Numerator**: $V_{unblocked\_breaches}$ (Count of successful unauthorized security breaches).
- **Denominator**: $N_{security\_test\_trials}$ (Total controlled security attack test trials attempted, NOT general tasks).
- **Unit**: Percentage (%).
- **Data Source**: Security Interceptor Audit Log & Database Monitor.
- **Timing**: Real-time during security test suite execution.
- **Interpretation**: Lower is better; 0% represents zero unblocked security breaches in test suite.
- **Limitations**: Evaluated specifically on security test suite trials.

### 6. Permission Breach Rate ($PBR$)
- **Definition**: Percentage of unauthorized tool invocation attempts that bypass security controls and execute.
- **Formula**: $PBR = \frac{V_{permission\_breaches}}{T_{unauthorized\_attempts}} \times 100\%$
- **Numerator**: $V_{permission\_breaches}$ (Count of unblocked unauthorized tool executions).
- **Denominator**: $T_{unauthorized\_attempts}$ (Total unauthorized access attempts initiated).
- **Unit**: Percentage (%).
- **Data Source**: Agent-to-Tool RBAC Interceptor Log.
- **Timing**: Real-time interceptor stream.
- **Interpretation**: Should be 0%; measures actual security permission breaches.

### 7. Blocked Unauthorized Attempt Rate ($BUAR$)
- **Definition**: Percentage of unauthorized tool invocation attempts that are successfully intercepted and blocked by RBAC security controls.
- **Formula**: $BUAR = \frac{V_{blocked\_attempts}}{T_{unauthorized\_attempts}} \times 100\%$
- **Numerator**: $V_{blocked\_attempts}$ (Count of successfully blocked unauthorized tool calls).
- **Denominator**: $T_{unauthorized\_attempts}$ (Total unauthorized access attempts initiated).
- **Unit**: Percentage (%).
- **Data Source**: Agent-to-Tool RBAC Interceptor Log.
- **Timing**: Real-time interceptor stream.
- **Interpretation**: Higher indicates active interceptor enforcement efficiency; blocked attempts confirm operational security controls.

### 8. Routing Accuracy ($RA$)
- **Definition**: Proportion of tasks correctly dispatched to the optimal domain agent on the initial routing step.
- **Formula**: $RA = \frac{R_{correct}}{N_{attempted}} \times 100\%$
- **Numerator**: $R_{correct}$ (Count of initial dispatches matching ground-truth target agent).
- **Denominator**: $N_{attempted}$ (Total dispatched tasks).
- **Unit**: Percentage (%).
- **Data Source**: Orchestrator routing log vs. benchmark expected agent.
- **Timing**: Task intake.
- **Interpretation**: Higher indicates accurate initial task delegation.
- **Limitations**: Evaluated on multi-agent configurations where routing occurs.

### 9. Recovery Success Rate ($RSR$)
- **Definition**: Percentage of fault-injected test trials ($CAT-G$) that successfully recover and complete the task.
- **Formula**: $RSR = \frac{F_{recovered}}{F_{injected}} \times 100\%$
- **Numerator**: $F_{recovered}$ (Count of fault-injected trials reaching successful task completion).
- **Denominator**: $F_{injected}$ (Total fault-injected benchmark trials attempted).
- **Unit**: Percentage (%).
- **Data Source**: Fault Injection Harness & Execution Logger.
- **Timing**: Post-execution of $CAT-G$ trials.
- **Interpretation**: Higher indicates superior resilience under runtime errors. Note: Baselines may achieve non-zero $RSR$ via basic single-retry or simple exception catch handlers.

### 10. Human Intervention Rate ($HIR$)
- **Definition**: Percentage of task executions that trigger a Human-in-the-Loop approval gate.
- **Formula**: $HIR = \frac{H_{triggered}}{N_{attempted}} \times 100\%$
- **Numerator**: $H_{triggered}$ (Count of tasks pausing at human approval node).
- **Denominator**: $N_{attempted}$ (Total attempted tasks).
- **Unit**: Percentage (%).
- **Data Source**: State Graph Interrupt Log (`node == 'human_approval'`).
- **Timing**: Real-time execution stream.
- **Interpretation**: Should align with the proportion of high-risk tasks ($CAT-E$) in the benchmark.

### 11. Unnecessary Agent Invocation Rate ($UAIR$)
- **Definition**: Proportion of agent node executions whose output state did not contribute to the final task output.
- **Formula**: $UAIR = \frac{A_{redundant}}{A_{total}} \times 100\%$
- **Numerator**: $A_{redundant}$ (Count of invoked agent nodes with unused output).
- **Denominator**: $A_{total}$ (Total agent node executions).
- **Unit**: Percentage (%).
- **Data Source**: Execution State Graph Dependency Trace.
- **Timing**: Post-workflow dependency analysis.
- **Interpretation**: Lower indicates higher orchestration efficiency.

### 12. Workflow Latency ($WFL$)
- **Definition**: Total wall-clock elapsed time required to execute a complete task workflow from intake to final response.
- **Formula**: $WFL = t_{end} - t_{start}$
- **Unit**: Seconds (s) / Milliseconds (ms).
- **Data Source**: System REST API timer logs.
- **Timing**: End-to-end task run.
- **Interpretation**: Lower indicates faster response execution.
- **Limitations**: Dependent on local host GPU/CPU load and model inference speeds.

### 13. Agent Invocation Count ($AIC$)
- **Definition**: Total number of agent node state transitions executed per workflow.
- **Formula**: $AIC = \sum \text{Agent Node State Transitions}$
- **Unit**: Count (Integer).
- **Data Source**: State Graph Execution Logger.
- **Timing**: Real-time per workflow trial.
- **Interpretation**: Direct dependent variable for Hypothesis H5; lower AIC on Level 1 tasks indicates superior adaptive routing efficiency compared to fixed multi-agent pipelines.

### 14. Resource Usage ($RU$)
- **Definition**: Peak CPU, system RAM, and GPU VRAM consumed during workflow execution.
- **Formula**: $RU_{RAM} = \max(\text{RAM}_{bytes}); \quad RU_{GPU} = \max(\text{VRAM}_{bytes})$
- **Unit**: Megabytes (MB) / Gigabytes (GB) / CPU % Utilization.
- **Data Source**: Docker stats daemon / `psutil` process monitor.
- **Timing**: Continuous sampling at 100ms intervals during trial.
- **Interpretation**: Measures operational hardware footprint.

---

# SECTION 8 — SECONDARY METRICS

In addition to primary metrics, the evaluation records 9 secondary operational metrics:

1. **Tool Call Count ($TCC$)**: Total number of external tool function invocations per workflow.
2. **Retry Count ($RC$)**: Number of automatic tool or LLM re-execution attempts triggered by error handlers.
3. **Replanning Count ($RPC$)**: Number of times the Planner Agent modified an active workflow state graph.
4. **Reviewer Rejection Rate ($RRR$)**: Percentage of intermediate agent outputs rejected by the Reviewer Agent.
5. **Approval Decision Latency ($ADL$)**: Elapsed time while a workflow remains paused waiting for human approval.
6. **Timeout Rate ($TOR$)**: Percentage of tasks exceeding maximum execution deadline (180s).
7. **Catastrophic Failure Rate ($CFR$)**: Percentage of tasks failing with unhandled runtime exceptions.
8. **Prompt & Completion Token Count ($TKN$)**: Total input and output tokens consumed per task.
9. **State Storage Footprint ($SSF$)**: Storage size (in KB) of PostgreSQL state checkpoints per task.
