# EAOC — Technology Specification
# Part 0.4-B: Technologies E–H (LangChain, PostgreSQL, pgvector, Redis)
# Phase 0 · Part 0.4 · Version 1.0

---

# TECHNOLOGY E — LANGCHAIN

## 1. Technology Name
LangChain (langchain-core + selected component packages)

## 2. Category
LLM Application Component Library

## 3. Version Policy
- `langchain-core >= 0.2.x` (shared types, base classes)
- `langchain-openai >= 0.1.x` (OpenAI-compatible LLM client — used for Ollama/LM Studio)
- `langchain-community >= 0.2.x` (only if specific community tools are needed; prefer direct implementations)
- **Constraint**: LangChain is a SUPPORTING component library, not the overall architecture. Do NOT introduce LangChain abstractions (Chains, Agents, AgentExecutor) that conflict with LangGraph's workflow control.
- **Upgrade policy**: Pin exact versions. LangChain evolves rapidly; upgrades require full agent test suite.

## 4. Purpose in EAOC
LangChain provides selected utility components:
- `ChatOpenAI` client (used to call Ollama / LM Studio via OpenAI-compatible API)
- Prompt template utilities (`ChatPromptTemplate`, `SystemMessagePromptTemplate`)
- Output parsers (`JsonOutputParser`, `PydanticOutputParser`) for structured LLM output
- Document loaders for ingestion pipeline (if used)

**What LangChain is NOT used for in EAOC:**
- Agent execution (LangGraph handles all agent orchestration)
- Chain execution (direct LangGraph node function calls replace Chains)
- Memory management (LangGraph WorkflowState replaces LangChain Memory)
- Tool execution (EAOC's own Tool Interceptor handles tool execution)

## 5. Architectural Components Using It
- LLM Inference Tool (ChatOpenAI client for Ollama/LM Studio endpoint)
- Planner node (prompt templates, JSON output parser)
- All agent nodes that call LLM (prompt templates, structured output parsers)
- Ingestion Pipeline (document loaders — optional)

## 6. Interface / API Boundary
LangChain components are used internally within Python functions. The only external network call is `ChatOpenAI` → Ollama/LM Studio REST API.

## 7. Inputs
Prompt templates filled with task-specific context. LLM configuration (model name, temperature, max_tokens).

## 8. Outputs
Parsed, structured Python objects (via `PydanticOutputParser`) or raw text (for summarization).

## 9. Dependencies
- `langchain-core >= 0.2.x`
- `langchain-openai >= 0.1.x`
- `langchain-community >= 0.2.x` (minimal, only needed components)

## 10. Local Development Requirements
No additional infrastructure. LLM calls go to local Ollama/LM Studio endpoint (configured via `OPENAI_BASE_URL` environment variable).

## 11. Production / Deployment Considerations
All LangChain network calls go to the local LLM runtime container. No external network access required unless a cloud LLM API is optionally configured.

## 12. Security Considerations
- `ChatOpenAI` configured with `base_url = OLLAMA_BASE_URL` (not OpenAI's servers) for local operation.
- API key set to a dummy value (`"ollama"`) for local mode; real key from environment variable for cloud mode.
- LLM prompts sanitized before construction (prompt injection prevention).
- LLM responses validated by Pydantic parsers before use in business logic.

## 13. Failure Modes
- LLM connection refused → `langchain.exceptions.LangChainException` → caught by tool wrapper → classified as `llm_failure`
- Structured output parsing failure → `OutputParserException` → classified as `schema_violation`
- Context window exceeded → LLM returns error → retry with reduced context (FR-STATE-002)

## 14. Testing Approach
- Mock `ChatOpenAI` client in unit tests (inject mock LLM responses).
- Integration tests call real Ollama endpoint (requires LLM runtime to be running).

## 15. Observability Requirements
- LLM call duration logged: `llm_call_start`, `llm_call_complete` with token counts if available.
- Prometheus histogram: `eaoc_llm_inference_duration_seconds`.

## 16. Alternatives Considered
| Alternative | Reason |
|---|---|
| Direct `httpx` calls to Ollama API | More control, less abstraction — considered valid; LangChain adds structured parsing and prompt management that saves implementation effort |
| LlamaIndex | Primarily a RAG framework; EAOC implements its own RAG logic; unnecessary duplication |

## 17. Reason for Selection
LangChain's `ChatOpenAI` client provides OpenAI-compatible interface that works with both Ollama and LM Studio with a single code path. Its prompt template and output parser utilities reduce boilerplate without constraining the architecture.

## 18. Replacement Conditions
If LangChain introduces breaking changes that cannot be absorbed without significant refactoring, the `ChatOpenAI` client can be replaced by a direct `httpx` implementation of the OpenAI REST API. Prompt templates can be replaced by `str.format()` or Jinja2 templates. The replacement would be localized to the LLM Inference Tool.

---

# TECHNOLOGY F — POSTGRESQL

## 1. Technology Name
PostgreSQL

## 2. Category
Relational Database Management System

## 3. Version Policy
- **Version**: PostgreSQL 15 or 16 (LTS)
- **Docker image**: `pgvector/pgvector:pg16` (includes pgvector extension pre-installed)
- **Upgrade policy**: Major version upgrades (e.g. 16 → 17) require explicit migration planning and testing. Pin image tag.

## 4. Purpose in EAOC
PostgreSQL is the primary persistent relational datastore for EAOC. It stores:

| Data Category | Tables |
|---|---|
| Enterprise synthetic data | finance.*, hr.*, it.*, analytics.* |
| Workflow state | public.workflow_state, public.langgraph_checkpoints |
| Permission records | public.users, public.rbac_roles, public.rbac_role_permissions |
| Approval records | public.approval_requests |
| Audit records | public.audit_log |
| Provenance | public.provenance_chain |
| Document metadata | public.documents |
| Vector chunks | public.document_chunks (pgvector) |
| LangGraph checkpoints | public.langgraph_checkpoints |
| Evaluation records | public.evaluation_runs (Phase 7) |

## 5. Architectural Components Using It
- Orchestrator (workflow_state R/W, LangGraph checkpoints)
- SQL/Data Agent (enterprise schema reads)
- RAG Agent (document_chunks search via pgvector)
- Document Agent (document metadata reads)
- Audit Logger (audit_log append-only writes)
- Provenance Store (provenance_chain writes)
- Auth Module (users R/W)
- RBAC Module (rbac_roles reads)
- Approval Queue (approval_requests R/W)

## 6. Interface / API Boundary
PostgreSQL is accessed via `asyncpg` (native async PostgreSQL driver) through SQLAlchemy async engine. No direct SQL from outside the EAOC system. All SQL generation for enterprise data is mediated by the SQL/Data Agent's validation pipeline.

## 7. Inputs
SQL queries from SQLAlchemy ORM or Core expressions. Raw validated SQL from SQL/Data Agent (read-only role).

## 8. Outputs
Result sets (rows, columns). Status codes for DML operations. Error codes for constraint violations.

## 9. Dependencies
- `asyncpg >= 0.29.0` (async driver)
- `sqlalchemy[asyncio] >= 2.0.0` (ORM/Core)
- `alembic >= 1.13.0` (schema migrations)
- `psycopg[binary] >= 3.1.0` (required by LangGraph checkpoint backend)

## 10. Local Development Requirements
- PostgreSQL running in Docker Compose (`pgvector/pgvector:pg16`)
- `alembic upgrade head` to apply schema migrations
- `psql` or pgAdmin for inspection during development

## 11. Production / Deployment Considerations
- Named Docker volume for data persistence (`postgres_data`)
- `pg_hba.conf` configured to allow only EAOC service accounts (MD5 or SCRAM-SHA-256 auth)
- Connection pool: SQLAlchemy `create_async_engine` with `pool_size=10, max_overflow=20`
- Read-only roles for all agent data access (separate role per schema)
- Superuser credentials ONLY for migrations (never used at runtime by agents)
- `statement_timeout` set per connection type (e.g. 15s for agent queries, 30s for analytics)

## 12. Security Considerations
- Separate PostgreSQL roles per schema (eaoc_read_analytics, eaoc_read_finance, etc.)
- Audit log role: INSERT-only (`eaoc_audit_append`)
- State role: SELECT/INSERT/UPDATE on workflow tables only (`eaoc_state_rw`)
- Superuser never used at runtime
- All credentials loaded from environment variables
- `SSL mode = require` for non-local environments
- `pg_stat_statements` enabled for query performance monitoring

## 13. Failure Modes
- Connection refused → circuit breaker opens (after 3 failures) → fast-fail subsequent calls → HTTP 503
- Query timeout (`statement_timeout`) → exception classified as `database_unavailable` → retry once
- Constraint violation (duplicate key, FK) → exception → structured error response (no raw PG error exposed to client)
- Disk full → PG stops accepting writes → audit writes fail → `audit_degraded` flag set; alert raised

## 14. Testing Approach
- Integration tests use a dedicated test PostgreSQL instance (Docker Compose `test` profile or `pytest-docker`)
- `pytest-postgresql` or test fixtures with `alembic downgrade base` + `alembic upgrade head` for clean state
- Each test function that modifies state runs in a rolled-back transaction (no permanent side effects)

## 15. Observability Requirements
- Connection pool utilization logged and exported as Prometheus gauge
- Slow query log (via PG `log_min_duration_statement = 1000ms`)
- Prometheus gauge: `eaoc_db_available` (0|1)
- SQLAlchemy `event.listen` hooks for query execution time logging

## 16. Alternatives Considered
| Alternative | Reason Not Selected |
|---|---|
| MySQL / MariaDB | No pgvector extension; inferior JSONB support; not mandated |
| MongoDB | No relational integrity for audit/provenance; no pgvector |
| SQLite | Not suitable for concurrent multi-process access; no pgvector |

## 17. Reason for Selection
PostgreSQL with pgvector unifies relational data, workflow state, audit, and vector storage in a single managed database. This reduces operational complexity and aligns with the Master Specification mandate.

## 18. Replacement Conditions
Not applicable — PostgreSQL is mandated by the Master Specification. pgvector → Qdrant migration (for vector search only) is documented in Part 0.3 Section 6.6 but the trigger is OPEN (ARCH-OPEN-004).

---

# TECHNOLOGY G — PGVECTOR

## 1. Technology Name
pgvector (PostgreSQL extension)

## 2. Category
Vector Similarity Search Extension

## 3. Version Policy
- **pgvector version**: 0.7.0+ (as included in `pgvector/pgvector:pg16` Docker image)
- **Included in**: PostgreSQL Docker image — no separate installation needed
- **Upgrade policy**: pgvector upgrades follow PostgreSQL image updates; pin image tag.

## 4. Purpose in EAOC
pgvector provides vector embedding storage and cosine similarity search within PostgreSQL. It is used exclusively by the RAG Agent for semantic retrieval from the enterprise knowledge base.

**Responsibilities**:
- Store document chunk embeddings (dense vectors) alongside chunk metadata
- Execute approximate nearest-neighbor (ANN) search via `ivfflat` or `hnsw` index
- Apply server-side filters: `namespace`, `access_level` — enforced in SQL WHERE clause (not post-retrieval)

## 5. Architectural Components Using It
- RAG Agent (via pgvector Search Tool)
- Ingestion Pipeline (writes embeddings during document ingestion)

## 6. Interface / API Boundary
pgvector is accessed via standard PostgreSQL SQL. The `<=>` operator (cosine distance) is used in SELECT queries. SQLAlchemy with the `pgvector` Python package handles ORM-level integration.

```sql
-- Example retrieval query
SELECT chunk_id, text, source_title,
       1 - (embedding <=> $1::vector) AS score
FROM document_chunks
WHERE namespace = $2
  AND access_level <= $3
ORDER BY embedding <=> $1::vector
LIMIT $4;
```

## 7. Inputs
Query vector (from Embedding Tool). Namespace filter. Access-level filter. Top-K limit.

## 8. Outputs
Ranked list of `(chunk_id, document_id, text, source_title, score)` — top-K results.

## 9. Dependencies
- `pgvector >= 0.3.0` (Python client package for SQLAlchemy integration)
- `pgvector/pgvector:pg16` Docker image (PostgreSQL with extension pre-installed)

## 10. Local Development Requirements
- Docker Compose starts `postgres` container with pgvector extension pre-loaded.
- `CREATE EXTENSION IF NOT EXISTS vector;` in migration script.
- Embedding dimension must match the embedding model output dimension.

## 11. Production / Deployment Considerations
- `ivfflat` index (faster build, slightly lower recall) for initial deployment: `CREATE INDEX ON document_chunks USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);`
- `hnsw` index (higher recall, slower build): preferred for production if corpus > 50K chunks
- Index must be rebuilt after bulk ingestion (`REINDEX`)
- Vector dimension is determined by the embedding model (ARCH-OPEN-002 — model selection is OPEN)
- Dimension placeholder: 1536 (matches text-embedding-ada-002, Nomic-embed, and many local models)

## 12. Security Considerations
- pgvector tables follow the same PostgreSQL role-based access as relational tables
- `document_chunks.access_level` filter applied in SQL WHERE clause — not in application code — ensuring server-side enforcement
- No vector data exposed directly via API; only processed RAG answers with citations

## 13. Failure Modes
- Index not built → full sequential scan (functionally correct but very slow); alert if scan time > threshold
- Embedding dimension mismatch → PostgreSQL error → `vector_store_unavailable` failure class
- Extension not loaded → all queries fail → startup health check catches this

## 14. Testing Approach
- Integration tests: insert synthetic document chunks, execute retrieval queries, verify top-K results and access-level filtering.
- Test that restricted-access chunks are never returned to queries with lower access-level filter.

## 15. Observability Requirements
- Retrieval query latency logged and exported: `eaoc_pgvector_search_duration_seconds`
- Index scan vs. sequential scan detected and logged (via `EXPLAIN` output sampling)

## 16. Alternatives Considered
| Alternative | Reason |
|---|---|
| Qdrant | Separate service; higher operational complexity; better at scale — migration path documented, trigger is OPEN |
| Weaviate | Complex schema management; separate service; over-engineered for current corpus size |
| Chroma | In-memory/file-based; not suitable for production durability |
| FAISS | In-memory; no persistence; no multi-user access control |

## 17. Reason for Selection
pgvector integrates directly with the existing PostgreSQL instance — no additional service to deploy or manage. For a synthetic corpus of 5K-20K chunks, pgvector with `ivfflat` index meets the NFR-PERF-003 target (< 2 seconds for top-5 search). Eliminates need for a separate vector database service during research evaluation.

## 18. Replacement Conditions
**OPEN — ARCH-OPEN-004**: Migration trigger not yet decided. Architecture provides a backend-agnostic `SearchService` interface for future Qdrant migration.

---

# TECHNOLOGY H — REDIS

## 1. Technology Name
Redis

## 2. Category
In-Memory Data Structure Store / Cache / Message Broker

## 3. Version Policy
- **Version**: Redis 7.x (LTS)
- **Docker image**: `redis:7-alpine` (minimal image)
- **Python client**: `redis[hiredis] >= 5.0.0` (hiredis for performance)
- **Upgrade policy**: Minor version upgrades allowed after dependency check. Major version upgrades require explicit testing.

## 4. Purpose in EAOC
Redis serves multiple supporting roles in EAOC:

| Role | Detail |
|---|---|
| Task queue | `RPUSH task:queue task_id` / `BLPOP task:queue` for worker pickup |
| WorkflowContext cache | Fast read for task status polling (avoids PostgreSQL hit on every poll) |
| LangGraph checkpoint cache | Fast checkpoint read on resume |
| Token revocation store | SET with TTL for logged-out JWT JTIs |
| Brute-force lockout | Counter with TTL for failed auth attempts |
| Circuit breaker state | Failure count per dependency |
| LLM availability flag | `SET health:llm_available "true"/"false"` |
| Approval signal | Pub/Sub channel for Orchestrator wake-up on approval decision |

## 5. Architectural Components Using It
- API Layer (task enqueue, status cache read)
- Orchestrator (task dequeue, state cache R/W, approval signal subscribe)
- Auth Module (token revocation, brute-force lockout)
- Health Checker (LLM/DB availability flags)
- Circuit Breaker (failure counters)
- Approval API (approval decision pub/sub publish)

## 6. Interface / API Boundary
Redis is accessed via the `redis-py` async client (`aioredis` API in `redis[hiredis] >= 5.0`). No direct external access to Redis — all access mediated by EAOC Python code.

## 7. Inputs
Key-value operations: SET, GET, EXPIRE, RPUSH, BLPOP, PUBLISH, SUBSCRIBE, INCR.

## 8. Outputs
Cached values, queue items, pub/sub messages, counter values.

## 9. Dependencies
- `redis[hiredis] >= 5.0.0`

## 10. Local Development Requirements
- Redis running in Docker Compose (`redis:7-alpine`)
- No persistent data volume needed (Redis state is transient by design)
- `redis-cli` for inspection during development

## 11. Production / Deployment Considerations
- `appendonly no` (performance mode; Redis restarts recover from PostgreSQL as source of truth)
- `maxmemory-policy allkeys-lru` (evict LRU keys if memory limit reached)
- Docker named volume NOT required (Redis data is transient)
- `requirepass` Redis password set (loaded from environment variable)
- Redis Sentinel for HA in production (outside current research scope)

## 12. Security Considerations
- Redis password required (`requirepass` set)
- Redis not exposed on host network port (internal Docker network only)
- No sensitive enterprise data stored in Redis (only task IDs, status strings, JWT JTIs)
- WorkflowContext cached in Redis must NOT contain raw PII or secrets in unencrypted form
- TTL set on ALL keys — no permanent Redis records

## 13. Failure Modes
- Redis unavailable → task queue stops (no new tasks processed) → API returns 503 for new task submissions
- WorkflowContext cache miss → fall back to PostgreSQL read (performance degraded, functionality maintained)
- Token revocation store miss → token may appear valid temporarily (short window; acceptable for research phase)
- Circuit breaker state lost on Redis restart → all circuits reset to CLOSED (conservative; acceptable)

## 14. Testing Approach
- `fakeredis` (Python library) used in unit and integration tests as an in-memory Redis substitute (no Redis server needed for unit tests)
- Integration tests use real Redis (Docker Compose test profile)

## 15. Observability Requirements
- Redis connection pool status logged
- `eaoc_redis_available` Prometheus gauge (0|1)
- Cache hit/miss ratio logged for WorkflowContext cache

## 16. Alternatives Considered
| Alternative | Reason Not Selected |
|---|---|
| RabbitMQ / Kafka | Heavier message brokers; more operational complexity; Redis List is sufficient for task queue at research scale |
| Memcached | No pub/sub; no sorted sets; no TTL-per-key control; less versatile |
| In-memory Python dict | No cross-process sharing; lost on process restart |

## 17. Reason for Selection
Redis provides a unified solution for multiple supporting concerns (queue, cache, pub/sub, rate limiting) with minimal operational overhead. For the research evaluation scale (tens of concurrent tasks), Redis 7 with a single instance is more than sufficient.

## 18. Replacement Conditions
- If EAOC scales to hundreds of concurrent tasks: Redis Cluster or migrate task queue to a dedicated message broker.
- If pub/sub reliability becomes critical: replace approval signal delivery with PostgreSQL NOTIFY/LISTEN.
- Neither condition is expected during the research evaluation phase.
