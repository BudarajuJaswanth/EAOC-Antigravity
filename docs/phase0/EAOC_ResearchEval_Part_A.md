# EAOC — Research Evaluation Design
# Part 0.5-A: Research Question, Hypotheses, Systems to Compare, and Experimental Controls
# Phase 0 · Part 0.5 · Version 1.3 (Final Micro-Correction)
# Status: DRAFT — Awaiting Part 0.5 approval
# Inputs: Master Spec v1.0, Part 0.1 FRS/NFRS, Part 0.2 Agent Spec, Part 0.3 Architecture, Part 0.4 Tech Spec

---

# SECTION 1 — RESEARCH QUESTION

The Enterprise AI Operations Center (EAOC) research evaluation is guided by the approved master research question:

> **"Can a structured, adaptive, permission-controlled multi-agent architecture improve reliability, security, traceability, and failure resilience for complex enterprise workflows compared with simpler single-agent approaches?"**

### 1.1 Scope and Sub-Questions
To systematically address the master research question, the evaluation breaks down the primary inquiry into five core sub-questions (SQs):

* **SQ1 (Reliability)**: Does multi-agent specialized decomposition with reviewer verification increase end-to-end task completion rate and correctness on complex multi-step enterprise workflows compared to baseline approaches?
* **SQ2 (Security)**: Does agent-level role-based tool access control (RBAC) and parameter validation reduce unauthorized execution attempts and security violations in enterprise automation compared to coarse-grained baseline permission models?
* **SQ3 (Traceability)**: Does dynamic execution state graphing and cryptographic audit logging improve evidence provenance accuracy for executed actions and claims compared to baseline outputs?
* **SQ4 (Failure Resilience)**: Does dynamic re-planning, reviewer-driven error routing, and human-in-the-loop fallback increase recovery success rates under controlled tool/agent/database failures compared to fixed baseline retry mechanisms?
* **SQ5 (Adaptive Orchestration Efficiency)**: Does adaptive workflow routing based on task complexity reduce unnecessary agent invocations and latency overhead compared to fixed static multi-agent pipelines?

---

# SECTION 2 — RESEARCH HYPOTHESIS

We formulate five primary testable research hypotheses ($H_1$ to $H_5$). Each hypothesis is defined with rigorous experimental parameters and neutral expected-direction indicators without assuming predetermined numerical outcomes.

## Hypothesis H1: Workflow Reliability & Task Correctness
* **Formal Statement**: For multi-step enterprise workflows (Level 2 and Level 3 complexity), EAOC achieves a higher Task Completion Rate ($TCR$) and Task Correctness Score ($TCS$), and a lower Unsupported Claim Rate ($UCR$), than single-agent and baseline multi-agent systems.
* **Independent Variable**: System Architecture Condition (Baseline A: Single Agent, Baseline B: Single Agent + RAG, Baseline C: Basic Multi-Agent, Proposed: EAOC).
* **Dependent Variable**: Task Completion Rate ($TCR$, %), Task Correctness Score ($TCS$, 0–1.0 scale), Unsupported Claim Rate ($UCR$, %).
* **Baseline / Control**: Baseline A (Single LLM Agent) and Baseline B (Single LLM + RAG).
* **Measurement Method**: Automated evaluation against deterministic ground-truth assertion suites combined with blind human expert panel review for synthesis tasks across $N=150$ task definitions with repeated trials.
* **Expected Direction**: $TCR_{EAOC} > TCR_{Baselines}$; $TCS_{EAOC} > TCS_{Baselines}$; $UCR_{EAOC} < UCR_{Baselines}$.
* **Experimental Conditions**: Identical LLM base model (`qwen2.5:7b` / `llama3.2:3b`), identical system temperature ($T=0.1$), identical enterprise database schema and vector corpus.
* **Threats to Validity**: Potential LLM-as-a-judge scoring bias, model non-determinism across runs. (Mitigated by hybrid automated AST/schema verification and fixed sampling seeds).

## Hypothesis H2: Enterprise Security & Access Control Enforcement
* **Formal Statement**: Under controlled security test scenarios (unauthorized access attempts, parameter tampering, prompt injection), EAOC achieves lower Security Violation Rates ($SVR$) and Permission Breach Rates ($PBR$) than baseline systems equipped with coarse-grained permissions.
* **Independent Variable**: Access Control Architecture (Coarse-Grained Shared Permissions vs. EAOC Agent-Level RBAC & Security Interceptor).
* **Dependent Variable**: Security Violation Rate ($SVR$, %), Permission Breach Rate ($PBR$, %), Blocked Attempt Rate ($BUAR$, %).
* **Baseline / Control**: Baseline A, Baseline B, Baseline C (all utilizing coarse-grained or shared application permissions).
* **Measurement Method**: Automated security test harness executing controlled attack test cases targeting unauthorized SQL mutations, forbidden tool calls, and prompt injection vectors.
* **Expected Direction**: $SVR_{EAOC} < SVR_{Baselines}$; $PBR_{EAOC} < PBR_{Baselines}$; $BUAR_{EAOC} > BUAR_{Baselines}$.
* **Experimental Conditions**: Identical database schema and API tool suite across all test conditions.
* **Threats to Validity**: Test case coverage limitations for novel prompt injection vectors. (Mitigated by standard OWASP Top 10 for LLMs test harness).

## Hypothesis H3: Evidence Traceability & Provenance Accuracy
* **Formal Statement**: EAOC produces a higher Evidence & Provenance Accuracy ($EPA$) score (empirically measured factual claim support and valid source citation) compared to single-agent and basic multi-agent baselines.
* **Independent Variable**: Provenance Tracking Architecture (Implicit Prompt Context vs. Dynamic Execution Graph + Cryptographic Audit Logger).
* **Dependent Variable**: Evidence & Provenance Accuracy ($EPA$, %), Citation Existence Rate ($\text{CER}$), Citation Correctness Rate ($\text{CCR}$), Source Support Rate ($\text{SSR}$).
* **Baseline / Control**: Baseline B (Single LLM + RAG without verification) and Baseline C (Basic Multi-Agent without strict reviewer verification).
* **Measurement Method**: Automated extraction of inline citations and claims mapped to raw database/document IDs, verified via deterministic binary matching against actual execution state graphs.
* **Expected Direction**: $EPA_{EAOC} > EPA_{Baselines}$.
* **Experimental Conditions**: Tasks requiring structured data extraction and policy document retrieval ($CAT-D$, $CAT-I$).
* **Threats to Validity**: Ambiguity in multi-source text summaries. (Mitigated by explicit UUID tagging of document chunks).

## Hypothesis H4: Failure Resilience & Fault Recovery
* **Formal Statement**: When tool failures, database timeouts, or agent generation errors occur during workflow execution, EAOC achieves a higher Recovery Success Rate ($RSR$) than baseline systems due to dynamic graph replanning and reviewer feedback loops.
* **Independent Variable**: Resilience Mechanism (Fixed Single-Retry / Basic Catch-and-Continue vs. EAOC Dynamic Re-planning & Reviewer Feedback Loops).
* **Dependent Variable**: Recovery Success Rate ($RSR$, %), Unhandled Failure Rate ($UFR$, %).
* **Baseline / Control**: Baselines equipped with standard fixed single-retry and basic exception catch mechanisms.
* **Measurement Method**: Controlled injection of runtime failures (tool API HTTP 500, database lock timeout, malformed JSON response, reviewer rejection).
* **Expected Direction**: $RSR_{EAOC} > RSR_{Baselines}$.
* **Experimental Conditions**: Deterministic fault injection at specified workflow steps.
* **Threats to Validity**: Recursion loops during replanning. (Mitigated by max state graph execution depth limits).

## Hypothesis H5: Adaptive Orchestration Efficiency
* **Formal Statement**: For simple single-domain tasks (Level 1), EAOC's adaptive routing mechanism bypasses unnecessary agent node invocations, resulting in lower latency and reduced agent calls compared to fixed multi-agent pipelines, while retaining full multi-agent capability for complex tasks (Level 3).
* **Independent Variable**: Orchestration Routing Engine (Fixed Multi-Agent Pipeline vs. EAOC Adaptive Router).
* **Dependent Variable**: End-to-End Workflow Latency ($WFL$, seconds), Agent Invocation Count ($AIC$), Unnecessary Agent Invocation Rate ($UAIR$, %).
* **Baseline / Control**: Baseline C (Fixed multi-agent pipeline invoking a standard multi-node sequence unconditionally).
* **Measurement Method**: System timer logs measuring elapsed time from request intake to final output across benchmark tasks spanning Level 1 to Level 3.
* **Expected Direction**: On Level 1 tasks: $WFL_{EAOC} < WFL_{BaselineC}$ and $AIC_{EAOC} < AIC_{BaselineC}$. On Level 3 tasks: $WFL_{EAOC} \approx WFL_{BaselineC}$ with superior task correctness ($TCS$).
* **Experimental Conditions**: Identical controlled-environment resource allocation and model parameters.
* **Threats to Validity**: Router classification overhead. (Mitigated by lightweight single-prompt triage classifier).

---

# SECTION 3 — SYSTEMS TO COMPARE

To establish a fair and rigorous scientific benchmark, EAOC is evaluated against three baseline architectures representing standard enterprise LLM deployment patterns. Baselines are configured with realistic, fair permission and recovery models rather than dangerous or strawman setups.

| Feature / Capability | BASELINE A: Single LLM Agent | BASELINE B: Single LLM + RAG | BASELINE C: Basic Multi-Agent | PROPOSED SYSTEM: EAOC |
|---|---|---|---|---|
| **Architecture** | Monolithic single LLM prompt loop | Monolithic LLM with RAG pipeline | Fixed linear pipeline of 3-4 specialized agents | Adaptive 12-agent graph with Reviewer & Router |
| **Capabilities** | Direct text generation & basic function calling | Text generation, document search & retrieval | Sequential domain task splitting | Dynamic planning, SQL, RAG, Analytics, Audit, Security |
| **Tool Set** | Shared application tool set | Vector search + Shared application tools | Coarse-grained tool assignment per agent | Granular Agent-to-Tool RBAC via Security Interceptor |
| **Knowledge Access** | In-context prompt memory | Vector DB retrieval + Prompt memory | Shared prompt context across agents | Isolated agent state + pgvector + Redis cache |
| **Orchestration** | Single ReAct loop (Think-Act-Observe) | Single ReAct loop with retriever tool | Fixed sequential chain (Agent 1 $\rightarrow$ 2 $\rightarrow$ 3) | Adaptive graph routing (LangGraph) with recursion |
| **Validation** | Single-prompt self-correction | Single-prompt self-correction | Final node format check | Dedicated Reviewer Agent + Output Validation Node |
| **Permissions** | Shared application role permissions | Shared application role permissions | Coarse-grained role permissions per agent | Strict Role-Based Access Control (RBAC) per node |
| **Provenance** | Standard text completion | Text snippets in context window | Intermediate node logs | Cryptographic hash audit logger + complete graph state |
| **Recovery** | Fixed single retry on tool error | Fixed single retry on tool/retrieval error | Static fallback / simple exception catch | Dynamic re-planning loop + parameter correction |
| **Human Approval** | None | None | Manual flag on unhandled exception | Risk-based Human-in-the-Loop approval gate |
| **Excluded Features** | Multi-agent, RAG, RBAC, Reviewer | Multi-agent, RBAC, Reviewer | Adaptive routing, fine RBAC, Reviewer | None (Full architecture under evaluation) |

---

# SECTION 4 — FAIRNESS AND CONTROLLED COMPARISON

To isolate architectural effects from confounding variables, the experimental protocol enforces strict parameter parity across all four conditions.

### 4.1 Fixed Experimental Variables
1. **Base Large Language Model**:
   - Primary LLM: `qwen2.5:7b-instruct-q4_k_m` serving all reasoning nodes across all conditions.
   - Fast Triage LLM (where applicable): `llama3.2:3b-instruct-q4_k_m`.
   - **Rule**: Baseline systems execute the exact same underlying local model weights via Ollama.

2. **Inference Parameters**:
   - Temperature: Fixed at $T = 0.1$ across all systems to minimize random sampling variance.
   - Top-P: Fixed at $P = 0.9$.
   - Max Generation Tokens: Fixed at 2,048 tokens per turn.

3. **Enterprise Dataset & Corpus**:
   - Relational Database: PostgreSQL 16 containing identical synthetic enterprise tables (Employees, Orders, Financials, IT Tickets).
   - Vector Store: pgvector with identical embedding model (`nomic-embed-text`, 768 dimensions) and chunking configuration.

4. **Controlled Infrastructure Specification**:
   - Controlled execution environment maintaining pinned resource bounds and isolated container limits.
   - **Note**: Final evaluation host hardware assignment remains an open decision (`EVAL-OPEN-04`) pending formal lab resource allocation.

5. **Evaluation Tasks & Prompts**:
   - Identical set of $N=150$ enterprise benchmark task definitions delivered via REST API intake.

### 4.2 Documented Architectural Intentional Differences
The evaluation explicitly permits differences that are fundamental to the architectures under test:
- **Number of LLM Calls**: EAOC and Baseline C make multiple LLM calls per workflow; Baselines A & B make fewer. (Token usage and latency are tracked to evaluate efficiency trade-offs).
- **Execution Overhead**: EAOC includes Pydantic validation, RBAC checking, and state logging overhead not present in simplified baselines.
