# EAOC — Research Evaluation Design
# Part 0.5-D: Reproducibility, Statistical Analysis, Threats to Validity, Protocol, Traceability, and Final Review
# Phase 0 · Part 0.5 · Version 1.5 (AIC Metric Classification Correction)
# Status: DRAFT — Awaiting Part 0.5 approval
# Inputs: Master Spec v1.0, Part 0.1 FRS/NFRS, Part 0.2 Agent Spec, Part 0.3 Architecture, Part 0.4 Tech Spec

---

# SECTION 12 — REPRODUCIBILITY

To support scientific reproducibility across independent evaluation runs, all experimental assets, parameters, and environments are versioned and logged. Because local model inference and system runtime variations may introduce non-determinism, reproducibility measures are designed to minimize variance and enable rigorous comparison.

### 12.1 Versioning & Configuration Control

| Domain | Parameter | Version Specification / Control Mechanism |
|---|---|---|
| **Dataset** | Enterprise Benchmark Version | `eaoc-bench-v1.0.json` (SHA-256 hash pinned) |
| **Tasks** | Task Definitions & Ground Truth | `tasks_v1.0.json` ($N=150$ unique task definitions) |
| **LLM Weights** | Local Model Identifiers | `qwen2.5:7b-instruct-q4_k_m` (Digest: `sha256:d8a...`), `llama3.2:3b-instruct-q4_k_m` |
| **Embeddings** | Vector Model Identification | `nomic-embed-text:v1.5` (768 dimensions) |
| **Prompts** | System Prompts & Tool Schemas | `prompts_v1.0.yaml` version-controlled in repository |
| **Configuration** | System & Agent Config | `config_v1.0.env` containing environment variables |
| **Random Seed** | Sampling & Partitioning Seeds | Fixed sampling seed (`SEED = 42`) across test runs |
| **Software** | Backend Dependencies | Locked via `poetry.lock` / `requirements.txt` (Python 3.12, FastAPI 0.110, LangGraph 0.0.30) |
| **Containers** | Infrastructure Docker Images | `Dockerfile.api`, `Dockerfile.worker`, `pgvector/pgvector:pg16` |

### 12.2 Experiment Tracking & Artifact Storage
- **Experiment Identifiers**: Each evaluation run generates a unique `experiment_id` (e.g., `EXP-20261015-FULL-01`).
- **Run Tracking**: Individual task trials log to PostgreSQL with `run_id` (UUIDv4), mapping `experiment_id`, `system_condition`, `task_id`, `prompt_hash`, `seed`, and `timestamp`.
- **Log Archival**: Raw execution logs, intermediate node states, and event streams are exported as JSONL files to `artifacts/eval_logs/{experiment_id}/`.

---

# SECTION 13 — STATISTICAL ANALYSIS

The statistical analysis plan specifies candidate analytical tests to evaluate performance differences across system conditions. Final test selection depends on data distributional properties, paired vs. unpaired structures, and measurement scales evaluated during Phase 1 sandbox testing.

### 13.1 Descriptive & Inferential Statistics
- **Descriptive Metrics**: For continuous variables (Latency, Token Usage, Correctness Score), report Mean ($\mu$), Median ($M$), Standard Deviation ($\sigma$), and 95% Confidence Intervals ($95\%\text{ CI}$).
- **Categorical Metrics**: For binary metrics (Task Completion Rate, Permission Breach Rate), report proportion ($p$) and standard error ($SE$).

### 13.2 Candidate Statistical Tests
1. **Paired Metric Comparisons (EAOC vs. Baselines on identical tasks)**:
   - Continuous Data (Latency, Correctness): **Wilcoxon Signed-Rank Test** (non-parametric) or **Paired $t$-test** (if normally distributed).
   - Binary Paired Data (Completion Rate, Permission Breach): **McNemar’s Test** for paired nominal data.
2. **Multi-Condition Analysis across 4 Systems**:
   - Continuous Data: **Friedman Test** (non-parametric ANOVA alternative) or **Repeated Measures ANOVA** followed by post-hoc pairwise comparisons.
3. **Effect Size Measures**:
   - Continuous Data: **Cohen’s $d$** (parametric) or **Cliff’s $\delta$** / **Wilcoxon $r$** (non-parametric effect size).
   - Categorical Data: **Odds Ratio ($OR$)** and Risk Difference.

### 13.3 Open Decision — Statistical Power Analysis
- **OPEN DECISION (EVAL-OPEN-01)**: Final sample size of repeated experimental trials per task definition depends on preliminary variance estimates ($\sigma^2$) obtained during Phase 1 sandbox tests, triggering formal Power Analysis to achieve 80% statistical power ($\beta = 0.20$) at $\alpha = 0.05$.

---

# SECTION 14 — ERROR ANALYSIS

When a task trial fails to achieve complete success, it is categorized using an 11-category Error Taxonomy ($ERR-01$ to $ERR-11$).

### 14.1 Error Taxonomy & Classification Framework

| Error Code | Error Category | Root Cause Definition | Primary Diagnostic Source |
|---|---|---|---|
| **ERR-01** | Planning Failure | Planner Agent produces incomplete, invalid, or logically broken plan | State Graph `plan` JSON |
| **ERR-02** | Routing Failure | Orchestrator routes task to incorrect or incapable specialized agent | Node Transition Log |
| **ERR-03** | Retrieval Failure | RAG Retriever fails to retrieve relevant evidence when benchmark ground truth specifies that relevant evidence exists | Retriever Tool Log |
| **ERR-04** | SQL Generation Failure | SQL Agent generates invalid SQL syntax or incorrect table schema joins | Database Driver Log |
| **ERR-05** | Tool Execution Failure | External tool throws runtime exception or times out | Tool Interceptor Log |
| **ERR-06** | Permission Denial | Unauthorized tool execution blocked by RBAC Interceptor | RBAC Interceptor Log |
| **ERR-07** | Model Generation Failure | Base LLM produces repetitive garbled text or fails token generation | Ollama Container Log |
| **ERR-08** | Output Validation Failure | Output fails Pydantic schema validation after retries | Validation Node Log |
| **ERR-09** | Human Approval Timeout/Reject| Human approver rejects task execution or approval window times out | Human Gate Node Log |
| **ERR-10** | Data Quality Failure | Input data in database/document is missing or corrupt | Execution Context Log |
| **ERR-11** | Recovery Loop Exhaustion | System exhausts max dynamic replanning loops without resolution | LangGraph Recursion Log |

---

# SECTION 15 — THREATS TO VALIDITY

We analyze potential threats to experimental validity and specify mitigation strategies.

### 15.1 Threats and Mitigation Strategies

| Validity Type | Threat Identification | Potential Impact | Mitigation Strategy in Evaluation Design |
|---|---|---|---|
| **Internal** | Prompt Sensitivity | System performance fluctuates due to minor prompt wording changes | Pinned system prompt templates (`prompts_v1.0.yaml`); multiple repeated trials per prompt |
| **Internal** | Host Runtime Variance | System background load alters latency metrics | Controlled execution environment; continuous resource tracking (`psutil`/Docker stats) |
| **External** | Synthetic Dataset Bias | Synthetic benchmark does not reflect full real-world enterprise complexity | Benchmark designed across 8 real enterprise domains matching FRS/NFRS requirements |
| **External** | Model Dependence | Results may not generalize beyond `qwen2.5:7b` model weights | Secondary-model validation is PLANNED; final model selection is governed by `EVAL-OPEN-03` |
| **Construct** | Metric Scoring Bias | LLM-as-a-judge automated scoring may favor verbose outputs | Hybrid evaluation pairing automated AST/schema validators with blind human expert panel review |
| **Statistical** | Sample Size Limitations | Insufficient trial count fails to detect true performance differences | Planned $N=150$ task set executed across repeated trials governed by Power Analysis (`EVAL-OPEN-01`) |
| **Construct** | Benchmark Data Leakage | Pre-trained LLM memorized standard public dataset tasks | Custom synthetic benchmark dataset generated specifically for EAOC evaluation |

---

# SECTION 16 — EXPERIMENT EXECUTION PROTOCOL

The future experimental evaluation follows a 12-step execution protocol:

```
[1. Dataset Preparation] ──► [2. Task Selection] ──► [3. Environment Reset] ──► [4. System Configuration]
                                                                                          │
[8. Metric Calculation] ◄── [7. Evidence Collection] ◄── [6. Log Archival] ◄── [5. Task Execution]
         │
         ▼
[9. Statistical Analysis] ──► [10. Error Analysis] ──► [11. Ablation Analysis] ──► [12. Final Report]
```

1. **Dataset Preparation**: Seed PostgreSQL database and pgvector vector store with synthetic benchmark dataset `eaoc-bench-v1.0`.
2. **Task Selection**: Load task manifest (`tasks_v1.0.json`, $N=150$).
3. **Environment Reset**: Flush Redis cache, reset PostgreSQL execution tables, restart Docker container services between runs.
4. **System Configuration**: Initialize target condition (Baseline A, B, C, or EAOC) with pinned sampling seed (`SEED=42`).
5. **Task Execution**: Batch-dispatch task prompts through FastAPI REST intake API.
6. **Log Archival**: Capture raw SSE events, tool interceptor logs, and state checkpoints.
7. **Evidence Collection**: Extract inline source citations and execution traces.
8. **Metric Calculation**: Execute metric calculator script to compute primary and secondary metrics.
9. **Statistical Analysis**: Run candidate significance tests and calculate effect sizes.
10. **Error Analysis**: Categorize failed trials into $ERR-01$–$ERR-11$ error taxonomy.
11. **Ablation Analysis**: Run ablation conditions ($ABL-A$ to $ABL-G$) and compute delta metrics.
12. **Final Report Generation**: Synthesize findings into structured evaluation figures and tables.

---

# SECTION 17 — RESULTS FORMAT

Future experimental findings will be presented using standardized empty result table templates. (Values remain blank until experiment execution).

### 17.1 Table Template: Baseline vs. EAOC Performance Comparison

| Metric | Baseline A (Single Agent) | Baseline B (Single + RAG) | Baseline C (Basic Multi-Agent) | Proposed EAOC | Stat Sig ($p$-value) | Effect Size |
|---|---|---|---|---|---|---|
| **Task Completion Rate ($TCR$, %)** | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] |
| **Task Correctness Score ($TCS$, 0-1)**| [ ] | [ ] | [ ] | [ ] | [ ] | [ ] |
| **Evidence Accuracy ($EPA$, %)** | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] |
| **Unsupported Claim Rate ($UCR$, %)**| [ ] | [ ] | [ ] | [ ] | [ ] | [ ] |
| **Security Violation Rate ($SVR$, %)**| [ ] | [ ] | [ ] | [ ] | [ ] | [ ] |
| **Permission Breach Rate ($PBR$, %)** | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] |
| **Blocked Attempt Rate ($BUAR$, %)**  | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] |
| **Routing Accuracy ($RA$, %)** | N/A | N/A | [ ] | [ ] | [ ] | [ ] |
| **Recovery Success Rate ($RSR$, %)**| [ ] | [ ] | [ ] | [ ] | [ ] | [ ] |
| **Human Intervention Rate ($HIR$, %)**| N/A | N/A | [ ] | [ ] | N/A | N/A |
| **Unnecessary Agent Invocation Rate ($UAIR$, %)**| N/A | N/A | [ ] | [ ] | [ ] | [ ] |
| **Workflow Latency ($WFL$, sec)** | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] |
| **Agent Invocation Count ($AIC$, count)**| N/A | N/A | [ ] | [ ] | [ ] | [ ] |
| **Resource Usage ($RU$, Peak MB)** | [ ] | [ ] | [ ] | [ ] | [ ] | [ ] |

---

# SECTION 18 — RESEARCH REPRODUCIBILITY PACKAGE

All research artifacts will be packaged into a public open-science reproducibility bundle:
- `/benchmark/`: Synthetic enterprise task dataset & JSON schemas.
- `/prompts/`: Version-controlled system prompts and tool descriptions (`prompts_v1.0.yaml`).
- `/config/`: Docker compose files and environment configuration manifests.
- `/scripts/`: Automated experiment execution harness, metric calculators, and statistical analysis scripts.
- `/logs/`: Anonymized execution trace logs and state checkpoint dumps.
- `/docs/`: Research evaluation specification and methodology documentation.

---

# SECTION 19 — TRACEABILITY MATRIX

### 19.1 Mapping: Research Question $\rightarrow$ Hypothesis $\rightarrow$ Architecture $\rightarrow$ Experiment $\rightarrow$ Metric

| Research Question | Hypothesis | Architecture Feature | Evaluation Experiment | Primary Metric | Target Evidence |
|---|---|---|---|---|---|
| **SQ1 (Reliability)** | $H_1$ | Reviewer Agent & Specialized Nodes | Baseline Comparison (Sect 3) | Task Completion ($TCR$), Correctness ($TCS$) | Execution log status & ground-truth match |
| **SQ2 (Security)** | $H_2$ | Agent-Level RBAC & Interceptor | Security Attack Suite (Sect 9) | Security Violation ($SVR$), Permission Breach ($PBR$), Blocked Attempts ($BUAR$) | Interceptor security log counts |
| **SQ3 (Traceability)**| $H_3$ | Execution Graph & Audit Logger | Evidence Benchmark ($CAT-I$) | Evidence Accuracy ($EPA$), $UCR$ | Verified citation match in audit database |
| **SQ4 (Resilience)** | $H_4$ | LangGraph Re-planning Loop | Failure Injection Suite (Sect 10) | Recovery Success Rate ($RSR$) | Fault recovery state transition trace |
| **SQ5 (Efficiency)** | $H_5$ | Adaptive Router Node | Latency & Token Analysis | Latency ($WFL$), Agent Invocation Count ($AIC$), Invocation Rate ($UAIR$) | REST API wall-clock timer & token logs |

---

# SECTION 20 — OPEN RESEARCH DECISIONS

The following decisions remain explicitly **OPEN** until experimental setup in Phase 1:

1. **EVAL-OPEN-01 (Power Analysis Sample Size)**: Final determination of exact repeated trial sample size following Phase 1 sandbox variance estimation.
2. **EVAL-OPEN-02 (Human Judge Panel Selection)**: Selection criteria and inter-annotator agreement metrics (Fleiss' $\kappa$) for blind human expert evaluation panel.
3. **EVAL-OPEN-03 (Secondary LLM Benchmark Models)**: Final selection of secondary local LLM model family for model-independence validation.
4. **EVAL-OPEN-04 (Hardware Evaluation Host)**: Final hardware evaluation host specification pending lab system allocation.

---

# SECTION 21 — PART 0.5 FINAL METHODOLOGY REVIEW

### 21.1 Methodological & Metric Classification Corrections Applied
1. **AIC Reclassified as Primary Metric #13 (14 Total Primary Metrics)**: Reclassified Agent Invocation Count ($AIC$) as a Primary evaluation metric because it is a direct dependent variable for Hypothesis H5 ($H_5 \rightarrow WFL, AIC, UAIR$). Part B Section 7 now formally specifies **14 primary quantitative evaluation metrics** ($TCR, TCS, EPA, UCR, SVR, PBR, BUAR, RA, RSR, HIR, UAIR, WFL, AIC, RU$).
2. **Secondary Metrics Updated (9 Total Secondary Metrics)**: Removed $AIC$ from Part B Section 8 secondary metric list, leaving **9 secondary operational metrics** ($TCC, RC, RPC, RRR, ADL, TOR, CFR, TKN, SSF$).
3. **Primary Metric Count Standardized (14 Individual Metrics)**: $PBR$ and $BUAR$ remain separate primary metrics #6 and #7.
4. **Security Test Suite Count**: Specified 45 synthetic adversarial test cases (5 per dimension across SEC-01 to SEC-09) matching the 9 security dimensions ($9 \times 5 = 45$).
5. **Retrieval Error Taxonomy (ERR-03)**: Aligned Part D ERR-03 with FLR-04: *"RAG Retriever fails to retrieve relevant evidence when benchmark ground truth specifies that relevant evidence exists"*.
6. **H5 Metric Name Consistency (AIC)**: Standardized on Agent Invocation Count ($AIC$) across all documents ($AIC_{EAOC} < AIC_{BaselineC}$).
7. **H4 Hypothesis Neutrality ($RSR_{EAOC} > RSR_{Baselines}$)**: Removed predetermined $>0\%$ constraint on baseline recovery in H4.
8. **FLR-03 Database Timeout Recovery**: Replaced automatic `LIMIT` query mutation with semantically equivalent optimizations.
9. **FLR-04 Retrieval Failure Definition**: Defined retrieval failure strictly as failing to retrieve relevant evidence when ground-truth specifies evidence exists.
10. **FLR-05 Model Resilience Framing**: Default failure recovery uses the same configured model/runtime.
11. **ABL-D Audit Integrity Alignment**: Cryptographic audit logging remains the removed component in ABL-D, with Audit Integrity / Tampering Detection as the primary outcome metric.
12. **ABL-G Sandboxed Safety Boundary**: Explicitly specified that ABL-G is evaluated ONLY in a sandboxed simulation environment.
13. **SEC-01 Security Conceptualization**: Separated SQL parameter validation, AST validation, database permission checking, and execution authorization.
14. **Secondary Model Status**: Stated that secondary-model validation is PLANNED and governed by `EVAL-OPEN-03`.
15. **Hardware Host Framing**: Replaced specific host hardware claims with neutral controlled-environment wording (`EVAL-OPEN-04`).
16. **Architectural Fidelity Wording**: Replaced "100% architectural fidelity" with "remain aligned with the approved architecture and technology specification."
17. **Phase Completion Status**: Replaced complete verification claims with "Part 0.5 Research Evaluation Design is methodologically complete for Phase 0, with explicitly documented EVAL-OPEN decisions deferred to Phase 1."

### 21.2 Remaining Open Decisions (Deferred to Phase 1)
- **EVAL-OPEN-01**: Trial sample size / Power analysis determination.
- **EVAL-OPEN-02**: Human judge panel selection & agreement metric (Fleiss' $\kappa$).
- **EVAL-OPEN-03**: Secondary local LLM model family verification.
- **EVAL-OPEN-04**: Final hardware host machine allocation.

### 21.3 Remaining Methodological Risks & Mitigations
- **Local LLM Non-Determinism**: Mitigated by fixed sampling seeds (`SEED=42`), temperature $T=0.1$, and repeated trial runs.
- **Prompt Sensitivity**: Mitigated by pinned prompt templates (`prompts_v1.0.yaml`) across all system conditions.
- **LLM-as-a-Judge Bias**: Mitigated by pairing automated schema/AST validators with blind human expert review.

### 21.4 Cross-File Consistency Status
- **Part 0.5-A, B, C, and D**: Fully verified across research sub-questions, neutral hypotheses, fair baselines, task difficulty taxonomy, 14 primary metric formulas ($TCR, TCS, EPA, UCR, SVR, PBR, BUAR, RA, RSR, HIR, UAIR, WFL, AIC, RU$), 9 secondary metrics, ablation definitions (ABL-A to ABL-G), and open decisions (`EVAL-OPEN-01` to `04`).
- **Alignment with Parts 0.1–0.4**: All 12 approved logical agents, security interceptors, PostgreSQL/pgvector stores, and FastAPI/LangGraph technologies remain aligned with the approved architecture and technology specification.

### 21.5 Part 0.5 Approval Readiness
**Part 0.5 Research Evaluation Design is methodologically complete for Phase 0, with explicitly documented EVAL-OPEN decisions deferred to Phase 1.**

---
**PHASE 0 · PART 0.5 AIC CLASSIFICATION CORRECTION IS COMPLETE.**
**STOPPING AS INSTRUCTED. DO NOT APPROVE GATE 0. DO NOT START PHASE 1.**
