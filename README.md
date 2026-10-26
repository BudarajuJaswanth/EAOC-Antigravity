# Enterprise AI Operations Center (EAOC)

## Project Purpose
The **Enterprise AI Operations Center (EAOC)** is a research-oriented, adaptive multi-agent AI platform designed for enterprise workflow automation and decision support. EAOC is built as a scientific research instrument to evaluate whether a structured, adaptive, permission-controlled multi-agent architecture improves reliability, security, traceability, and failure resilience for complex enterprise workflows compared with simpler single-agent baselines.

---

## Current Status & Roadmap

* **Phase 0 Status**: **APPROVED ✅ · GATE 0 PASSED · LOCKED 🔒**
* **Phase 1 Status**: **APPROVED ✅ · COMPLETED & LOCKED 🔒**
* **Phase 2 Status**: **IN PROGRESS**
  * **Part 2.16-C**: **ACCEPTED & LOCKED 🔒 (WITH LIMITATIONS)**
    * **Test Verification**: 356 unit/hermetic tests passing.
    * **Recorded Limitations**:
      1. Live PostgreSQL row-locking and multi-connection concurrency are not yet verified.
      2. Physical database idempotency-constraint races are not yet verified.
      3. Migration 0003 upgrade/downgrade has not been executed against a dedicated PostgreSQL test database.
      4. Production database migration and deployment are not authorized by this gate decision.
      5. Exactly-once external side effects are not guaranteed.

---

## Approved Architecture

### Request Flow Blueprint
```
User / REST API Intake
       │
       ▼
  Orchestrator Node
       │
       ▼
   Planner Node
       │
       ▼
Specialized Agent Nodes (12 Agents)
       │
       ▼
Security Interceptor / RBAC Gate
       │
       ▼
Enterprise Tools / DB / pgvector
       │
       ▼
Reviewer / Validation Node
       │
       ▼
  Risk Decision Engine
    ├── Low Risk  ──► Validated Final Output + Audit Log
    └── High Risk ──► Human Approval Gate (HITL) ──► Approved Output / Abort
```

### Approved 12 Logical Agents
EAOC defines exactly 12 logical agents (multiple agents may share the same underlying LLM instance):
1. **Research / RAG Agent** (`src/eaoc/agents/research_rag/`)
2. **SQL / Data Agent** (`src/eaoc/agents/sql_data/`)
3. **Document Processing Agent** (`src/eaoc/agents/document/`)
4. **Analytics Agent** (`src/eaoc/agents/analytics/`)
5. **Finance Agent** (`src/eaoc/agents/finance/`)
6. **HR Agent** (`src/eaoc/agents/hr/`)
7. **IT / Support Agent** (`src/eaoc/agents/it_support/`)
8. **Developer Agent** (`src/eaoc/agents/developer/`)
9. **Reviewer / Validation Agent** (`src/eaoc/agents/reviewer_validation/`)
10. **Security / Policy Agent** (`src/eaoc/agents/security_policy/`)
11. **Orchestrator Agent** (`src/eaoc/agents/orchestrator/`)
12. **Planner Agent** (`src/eaoc/agents/planner/`)

---

## Approved Technology Stack

| Layer | Technology | Specification / Role |
|---|---|---|
| **Runtime** | Python 3.12 | Core backend language |
| **API Framework** | FastAPI 0.110+ | REST API & SSE streaming endpoints |
| **Data Validation** | Pydantic v2 | Data schemas, tool parameters, settings |
| **Agent Orchestration** | LangGraph 0.0.30+ | Stateful multi-agent execution graph |
| **LLM Utilities** | LangChain Core 0.2+ | ChatOpenAI adapter & output parsers |
| **System Store** | PostgreSQL 16 | Relational data, task state, audit logs |
| **Vector Store** | pgvector 0.7+ | HNSW index vector store for RAG embeddings |
| **Cache & Pub/Sub** | Redis 7.2+ | Session cache, rate limiting, SSE pub/sub |
| **Local LLM Runtime** | Ollama / LM Studio | Zero-cloud local LLM serving runtime |
| **Containerization** | Docker Engine & Compose v2 | Infrastructure orchestration |

---

## Repository Package Layout

```
EAOC Antigravity/
├── .env.example                  # Environment configuration template (NO secrets)
├── .gitignore                    # Python git ignore rules
├── pyproject.toml                # Package metadata & dependencies declaration
├── requirements.txt              # Pinned core dependencies
├── README.md                     # Master project README
├── docs/                         # Specifications & documentation
│   ├── phase0/                   # Approved Phase 0 specifications
│   └── phase1/                   # Phase 1 documentation placeholders
├── infrastructure/               # Container & deployment placeholders
│   ├── docker/
│   │   ├── Dockerfile.api        # API service container placeholder
│   │   ├── Dockerfile.worker     # Worker service container placeholder
│   │   └── docker-compose.yml    # Compose orchestration placeholder
│   └── scripts/                  # Infra setup scripts placeholder
├── src/                          # Backend source code package
│   └── eaoc/
│       ├── api/                  # FastAPI routes, handlers, middlewares
│       ├── agents/               # 12 approved logical agent packages
│       ├── config/               # Pydantic settings & config loader
│       ├── db/                   # SQLAlchemy async engine & repositories
│       ├── evaluation/           # Benchmark harness & metric calculators
│       ├── knowledge/            # RAG loader & embedding generators
│       ├── orchestration/        # LangGraph state graph definitions
│       ├── security/             # RBAC & Tool Interceptor modules
│       ├── state/                # Workflow state models & checkpoints
│       └── tools/                # Enterprise tool definitions & registries
└── tests/                        # Test suite directory
    ├── unit/                     # Unit test placeholders
    └── integration/              # Integration test placeholders
```

---

## Development Discipline

Project development strictly follows the approved execution workflow:

$$\text{PHASE} \longrightarrow \text{PART} \longrightarrow \text{TASK} \longrightarrow \text{IMPLEMENT} \longrightarrow \text{TEST} \longrightarrow \text{FIX} \longrightarrow \text{VERIFY} \longrightarrow \text{GATE} \longrightarrow \text{NEXT PART}$$

Every part is built incrementally without proceeding to future parts until explicit validation and gate approval are granted.
