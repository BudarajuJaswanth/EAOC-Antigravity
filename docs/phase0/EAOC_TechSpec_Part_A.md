# EAOC — Technology Specification
# Part 0.4-A: Technologies A–D (Python, FastAPI, Pydantic, LangGraph)
# Phase 0 · Part 0.4 · Version 1.0
# Status: DRAFT — Awaiting Part 0.4 approval
# Inputs: Master Spec v1.0, Part 0.1 FRS/NFRS, Part 0.2 Agent Spec, Part 0.3 Architecture

---

# TECHNOLOGY A — PYTHON RUNTIME

## 1. Technology Name
Python

## 2. Category
Programming Language / Runtime

## 3. Version Policy
- **Minimum**: Python 3.11
- **Recommended**: Python 3.12 (LTS, improved performance, better error messages)
- **Maximum tested**: Python 3.12.x (Python 3.13 not yet verified compatible with all dependencies)
- **Version pinned in**: `.python-version` (pyenv), `pyproject.toml` `requires-python`, and Docker base image tag
- **Upgrade policy**: Evaluate new minor Python releases in a separate branch. Do not upgrade without verifying all dependencies (especially LangGraph, SQLAlchemy, asyncpg).

## 4. Purpose in EAOC
Python is the sole backend programming language. All components — API layer, orchestration, agents, tools, data access, observability — are implemented in Python.

## 5. Architectural Components Using It
All EAOC backend components (FastAPI, LangGraph, agent nodes, tool implementations, audit logger, health checker, ingestion pipeline).

## 6. Interface / API Boundary
Python runtime is the internal execution environment. It has no external API boundary itself — external interfaces are provided by FastAPI (HTTP) and the Docker container.

## 7. Inputs
Source code, environment variables, configuration files.

## 8. Outputs
Running processes: `api` (FastAPI/Uvicorn) and `worker` (LangGraph Orchestrator background process).

## 9. Dependencies
- pip / uv (package manager)
- All Python packages defined in `pyproject.toml` / `requirements.txt`

## 10. Local Development Requirements
- Python 3.11 or 3.12 installed (pyenv recommended for version management)
- Virtual environment (venv or uv venv)
- All packages installed via `pip install -e .[dev]` or `uv sync`

## 11. Production / Deployment Considerations
- Base Docker image: `python:3.12-slim` (minimal attack surface)
- Non-root user in Docker container
- `PYTHONDONTWRITEBYTECODE=1`, `PYTHONUNBUFFERED=1` set in Dockerfile

## 12. Security Considerations
- No eval(), exec(), or dynamic code execution anywhere in the codebase (except Developer Agent sandbox — fully isolated)
- `subprocess` module not used in non-DEV-Agent code paths
- All external inputs validated via Pydantic before processing

## 13. Failure Modes
- Import error on startup → service fails to start (surfaces immediately in Docker logs)
- Memory exhaustion → Python process OOM-killed by Docker; Orchestrator recovery handles in-progress tasks

## 14. Testing Approach
- `pytest` as test runner
- `pytest-asyncio` for async test support
- All modules testable without Docker (dependency injection for DB/Redis/LLM)

## 15. Observability Requirements
- `structlog` for structured JSON logging
- `prometheus_client` for metrics exposure
- Python runtime version logged at startup

## 16. Alternatives Considered
None — Python is mandated by the Master Specification and is the standard language for AI/ML system development.

## 17. Reason for Selection
Ecosystem alignment: LangGraph, LangChain, SQLAlchemy, FastAPI, Pydantic, Hugging Face, Ollama client — all first-class Python libraries. Python 3.12 async support (asyncio) enables concurrent API serving and background task execution.

## 18. Replacement Conditions
Not applicable — Python is the mandated runtime.

---

# TECHNOLOGY B — FASTAPI

## 1. Technology Name
FastAPI

## 2. Category
Web Framework / REST API Server

## 3. Version Policy
- **Minimum**: FastAPI 0.110.0
- **Pinned in**: `pyproject.toml` and lock file
- **ASGI server**: Uvicorn 0.29+ with `uvicorn[standard]` (includes uvloop for performance)
- **Upgrade policy**: Patch upgrades allowed. Minor version upgrades require regression test of all endpoints.

## 4. Purpose in EAOC
FastAPI serves as the HTTP REST API layer for EAOC. It handles all client-facing request intake, response assembly, authentication middleware, RBAC middleware, and background task management.

## 5. Architectural Components Using It
API Layer (Section 3.1 of Part 0.3 Architecture). All endpoints listed in Part 0.3 Section 3.1.

## 6. Interface / API Boundary
HTTP/REST on port 8000. All responses in JSON. OpenAPI schema auto-generated at `/docs` (disabled in production, or restricted to internal network).

## 7. Inputs
HTTP requests with Bearer JWT token (except `/health`, `/metrics`, `/auth/token`).

## 8. Outputs
JSON responses conforming to Pydantic response models. HTTP status codes per RFC 7231.

## 9. Dependencies
- `fastapi >= 0.110.0`
- `uvicorn[standard] >= 0.29.0`
- `python-jose[cryptography]` (JWT handling)
- `passlib[bcrypt]` (password hashing)
- `pydantic >= 2.0.0` (request/response validation)

## 10. Local Development Requirements
- Run with `uvicorn eaoc.main:app --reload --host 0.0.0.0 --port 8000`
- `.env` file for configuration
- `httpx` for API integration testing

## 11. Production / Deployment Considerations
- Multiple Uvicorn workers behind Gunicorn (or run directly in Docker with Uvicorn workers)
- OpenAPI docs disabled or restricted to internal network in production
- Rate limiting middleware applied at this layer
- No synchronous agent execution in the API request thread (async task dispatch to Redis queue)

## 12. Security Considerations
- All endpoints (except health, metrics, auth) require validated JWT
- CORS configured to allowed origins only
- Request size limits enforced (max body size)
- Global exception handler catches all exceptions; internal details never returned to client
- `X-Request-ID` header generated/propagated for every request

## 13. Failure Modes
- Uvicorn process crash → Docker restart policy restarts container; new requests served after restart
- Database unavailable → endpoints return 503 with structured error
- Redis unavailable → task submission fails with 503; health endpoint reflects degraded state

## 14. Testing Approach
- `pytest` + `httpx.AsyncClient` for API endpoint tests
- `TestClient` (FastAPI's built-in) for synchronous endpoint tests
- Mock Auth Middleware for unit tests (inject pre-built UserContext)

## 15. Observability Requirements
- Structured log per request (method, path, status, latency, request_id, user_id)
- Prometheus histogram: `eaoc_api_request_duration_seconds{endpoint, method, status_code}`
- Health endpoint reflects component availability

## 16. Alternatives Considered
| Alternative | Reason Not Selected |
|---|---|
| Django REST Framework | Too heavyweight; includes ORM not needed (SQLAlchemy used directly) |
| Flask | No native async support; no automatic OpenAPI generation |
| Starlette (raw) | FastAPI adds Pydantic integration and route decoration on top of Starlette — no reason to use raw Starlette |

## 17. Reason for Selection
FastAPI provides: automatic Pydantic-based request/response validation, native async support, automatic OpenAPI documentation, excellent middleware support, and is the leading Python async web framework for API development.

## 18. Replacement Conditions
No planned replacement conditions. If EAOC eventually requires a non-Python API gateway (e.g. for performance-critical edge cases), the FastAPI layer could be fronted by a reverse proxy (Nginx), not replaced.

---

# TECHNOLOGY C — PYDANTIC

## 1. Technology Name
Pydantic

## 2. Category
Data Validation / Schema Definition Library

## 3. Version Policy
- **Version**: Pydantic v2 (>= 2.6.0)
- **Pinned in**: `pyproject.toml` and lock file
- **Constraint**: Pydantic v1 must NOT be used. LangChain and LangGraph both support Pydantic v2. All models must be written as `BaseModel` with `model_config = ConfigDict(...)` (v2 style).
- **Upgrade policy**: Patch upgrades automatic. Minor upgrades require schema compatibility verification.

## 4. Purpose in EAOC
Pydantic is the universal schema definition and runtime validation layer for EAOC. Every data structure that crosses a component boundary — task requests, agent inputs, agent outputs, tool inputs, tool outputs, workflow state, API responses — is defined as a Pydantic model.

## 5. Architectural Components Using It
- API Layer (request/response schemas)
- Orchestrator (WorkflowState validation)
- Planner (ExecutionPlan schema)
- All 12 Agent nodes (AgentTaskInput, AgentResult)
- Tool Layer (tool input/output schemas)
- Audit Logger (AuditEntry schema)
- Provenance Store (ProvenanceEntry schema)
- Approval Queue (ApprovalRequest schema)

## 6. Interface / API Boundary
Pydantic models are internal Python objects. FastAPI uses them to auto-generate OpenAPI specs and to validate HTTP request bodies and response shapes. LangGraph uses them as state type annotations.

## 7. Inputs
Raw Python dicts, JSON strings, or keyword arguments → validated into typed model instances.

## 8. Outputs
Typed Python model instances with validated, coerced data. JSON serialization via `.model_dump_json()`.

## 9. Dependencies
- `pydantic >= 2.6.0`
- `pydantic-settings` (for environment variable configuration models)

## 10. Local Development Requirements
Standard pip install. No additional local infrastructure.

## 11. Production / Deployment Considerations
Pydantic v2 (compiled with Rust-based `pydantic-core`) is significantly faster than v1. No production configuration changes needed beyond pinning the version.

## 12. Security Considerations
- Pydantic validation is the first defence against malformed inputs reaching business logic.
- `model_config = ConfigDict(extra='forbid')` used on security-sensitive models to reject unexpected fields.
- Sensitive field values (passwords, tokens) annotated with `SecretStr` to prevent accidental logging.

## 13. Failure Modes
- `ValidationError` raised on invalid input → caught by FastAPI global handler → structured `ErrorResponse` returned (400 or 422)
- `ValidationError` in agent output → classified as `schema_violation` failure class → triggers retry or re-plan

## 14. Testing Approach
- Each Pydantic model has a dedicated test module verifying: valid input acceptance, invalid input rejection, edge cases, extra field rejection (where configured).

## 15. Observability Requirements
- `ValidationError` events logged with field names (not values for sensitive models) and error messages.

## 16. Alternatives Considered
| Alternative | Reason Not Selected |
|---|---|
| `dataclasses` | No built-in validation; no JSON serialization integration |
| `attrs` | Less ecosystem integration with FastAPI/LangGraph/SQLAlchemy |
| `marshmallow` | More verbose; not natively integrated with FastAPI |

## 17. Reason for Selection
Pydantic v2 is the standard schema/validation library for FastAPI and LangGraph. It provides compile-time type safety (mypy compatibility), runtime validation, automatic JSON serialization, and settings management — all essential for EAOC's strict schema contract approach.

## 18. Replacement Conditions
Not applicable — Pydantic v2 is a foundational dependency of FastAPI and LangGraph.

---

# TECHNOLOGY D — LANGGRAPH

## 1. Technology Name
LangGraph

## 2. Category
Workflow Orchestration / State Machine Framework

## 3. Version Policy
- **Minimum**: LangGraph 0.2.x (or latest stable at Phase 1 start)
- **Pinned**: exact version pinned in lock file
- **Upgrade policy**: LangGraph is evolving rapidly; upgrades require full workflow integration test before adoption. Pin to a specific minor version for the duration of each phase.

## 4. Purpose in EAOC
LangGraph is the orchestration backbone of EAOC. It implements the Orchestrator as a stateful directed graph (StatefulGraph) where each node represents a workflow step (plan, execute step, risk gate, retry, review, approve, fail). LangGraph manages: workflow state persistence, conditional routing between steps, retry loops, human approval interruption/resumption, and crash recovery via checkpointing.

## 5. Architectural Components Using It
- Orchestrator (entire workflow graph)
- Planner (LangGraph node)
- All 12 agent nodes (LangGraph nodes within the Orchestrator graph)
- Reviewer/Validation Agent (LangGraph node)
- Risk Gate (LangGraph conditional edge logic)
- Human Approval interrupt/resume (LangGraph `interrupt` mechanism)

## 6. Interface / API Boundary
LangGraph is an internal Python framework. External callers interact only with FastAPI. The Orchestrator worker process runs the LangGraph graph. No external LangGraph API is exposed.

## 7. Inputs
`WorkflowState` (typed Pydantic model / TypedDict). Each graph invocation receives the current state and produces an updated state.

## 8. Outputs
Updated `WorkflowState` after each node execution. Final state contains `FinalResponse` or `ErrorDetail`. LangGraph checkpoints written to PostgreSQL after each node.

## 9. Dependencies
- `langgraph >= 0.2.x`
- `langgraph-checkpoint-postgres` (PostgreSQL checkpoint backend)
- `langchain-core >= 0.2.x` (shared types)
- `psycopg` (PostgreSQL async driver, required by checkpoint backend)

## 10. Local Development Requirements
- LangGraph Studio (optional): visual debugger for LangGraph graphs; useful during Phase 1 development.
- PostgreSQL must be running for checkpoint persistence (Docker Compose handles this).

## 11. Production / Deployment Considerations
- LangGraph runs in the `worker` Docker container (separate from `api` container).
- PostgreSQL checkpoint backend provides durability and crash recovery.
- LangGraph graph is compiled once at startup (`graph.compile(checkpointer=postgres_checkpointer)`).
- Thread configuration: each task runs as a separate LangGraph thread (isolated `thread_id = task_id`).

## 12. Security Considerations
- `WorkflowState` contains `UserContext` (roles, permissions) — must never be logged in full.
- Sensitive fields in state masked before any logging.
- LangGraph checkpoint data in PostgreSQL encrypted at rest (disk-level encryption; application-level encryption of sensitive JSONB fields is a future hardening step).
- LangGraph does not make any network calls itself; network calls happen inside agent node functions.

## 13. Failure Modes

| Failure | Handling |
|---|---|
| Node throws unhandled exception | LangGraph catches; Orchestrator classifies failure; retry or re-plan |
| Checkpoint write fails (PG unavailable) | LangGraph raises; Orchestrator marks task degraded; Redis cache only |
| Graph definition error (invalid edge) | Startup failure — caught at compile time |
| Human approval interrupt | LangGraph `interrupt()` suspends graph; resumes on `Command(resume=...)` |

## 14. Testing Approach
- Each LangGraph node tested as a standalone function (input state → output state).
- Full graph integration tests: inject mock states and verify routing at conditional edges.
- `langgraph.testing` utilities (if available in version) or custom test harness.
- Human approval interrupt/resume tested with mock approval events.

## 15. Observability Requirements
- Structured log entry at every node entry and exit (node_name, task_id, state_summary, duration_ms).
- LangGraph checkpoint events logged (checkpoint_written, checkpoint_loaded).
- Prometheus counter: `eaoc_langgraph_node_executions_total{node_name, outcome}`.

## 16. Alternatives Considered
| Alternative | Reason Not Selected |
|---|---|
| Celery | Task queue only; no workflow state machine; no conditional routing; no human approval interrupt |
| Prefect / Airflow | Workflow schedulers designed for data pipelines; not designed for LLM agent orchestration |
| Custom state machine | Would replicate LangGraph functionality; LangGraph is purpose-built for this use case |
| Autogen | Microsoft framework; different paradigm (agent conversation, not structured workflow); not mandated |

## 17. Reason for Selection
LangGraph is specifically designed for stateful, multi-step LLM workflows with conditional routing, retries, and human-in-the-loop interrupts. It is mandated by the Master Specification. Its checkpointing mechanism directly implements FR-STATE-001 (task state persistence) and NFR-REL-002 (task state durability after restart).

## 18. Replacement Conditions
Not applicable — LangGraph is mandated by the Master Specification v1.0. Any replacement would require an explicit amendment to the Master Specification by the project authority.

## LangGraph: Detailed Feature Mapping

| EAOC Requirement | LangGraph Mechanism |
|---|---|
| Workflow state persistence | `PostgresSaver` checkpoint backend |
| Agent nodes | LangGraph `StateGraph` nodes |
| Conditional routing | `add_conditional_edges()` |
| Retry loop | Cycle edge back to step node |
| Human approval interrupt | `interrupt()` + `Command(resume=...)` |
| Crash recovery | Checkpoint reload on Orchestrator restart |
| Context passing | Typed `WorkflowState` passed to every node |
| Task isolation | Separate LangGraph thread per task_id |
| Re-planning | Conditional edge to Planner node |
| Graceful failure | Conditional edge to `fail_task` node |

## LangGraph: Human Approval Interrupt/Resume

```python
# In the risk_gate node:
if step.risk_level == "high":
    approval_id = create_approval_request(state, step)
    # LangGraph interrupt suspends the graph at this point
    # State is checkpointed to PostgreSQL
    decision = interrupt({
        "approval_id": approval_id,
        "step_summary": step.description,
        "risk_factors": step.risk_factors
    })
    # Graph resumes here when Command(resume=decision) is sent
    if decision["decision"] == "reject":
        return {**state, "status": "failed", "error": "human_approval_rejected"}
    # else: proceed to agent dispatch
```

The Approval API endpoint signals resumption via:
```python
graph.invoke(
    Command(resume={"decision": "approve", "decided_by": approver_id}),
    config={"configurable": {"thread_id": task_id}}
)
```
