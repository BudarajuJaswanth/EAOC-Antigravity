# EAOC — Technology Specification
# Part 0.4-C: Technologies I–L (Docker & Docker Compose, Ollama/LM Studio, Local Embeddings, Local LLMs)
# Phase 0 · Part 0.4 · Version 1.0
# Status: DRAFT — Awaiting Part 0.4 approval
# Inputs: Master Spec v1.0, Part 0.1 FRS/NFRS, Part 0.2 Agent Spec, Part 0.3 Architecture

---

# TECHNOLOGY I — DOCKER & DOCKER COMPOSE

## 1. Technology Name
Docker Engine & Docker Compose v2

## 2. Category
Containerization & Local Infrastructure Orchestration

## 3. Version Policy
- **Docker Engine**: Minimum `24.0.x`, Recommended `26.x+`
- **Docker Compose**: Minimum `v2.20.0` (using `docker compose` plugin syntax)
- **Base OS Images**: `python:3.12-slim-bookworm` for API/Worker, `postgres:16-bullseye` for Database, `redis:7.2-alpine` for Redis, `ollama/ollama:latest` for Ollama runtime.
- **Upgrade policy**: Image base tags pinned to minor versions (e.g., `python:3.12.2-slim`). Minor security patch upgrades allowed after CI verification.

## 4. Purpose in EAOC
Provides reproducible deployment environments across develop, test, and local production. Encapsulates Python app services (`api`, `worker`), database services (`postgres`), caching (`redis`), and local LLM runtime (`ollama`).

## 5. Architectural Components Using It
All runtime services: `eaoc-api`, `eaoc-worker`, `eaoc-postgres`, `eaoc-redis`, `eaoc-ollama`.

## 6. Interface / API Boundary
- Containers expose defined ports on host (`8000` API, `5432` Postgres, `6379` Redis, `11434` Ollama).
- Docker network `eaoc-internal` bridges container inter-service HTTP/TCP traffic.

## 7. Inputs
`docker-compose.yml`, Dockerfiles (`Dockerfile.api`, `Dockerfile.worker`), `.env` configuration file, mounted volumes (`postgres_data`, `redis_data`, `ollama_models`).

## 8. Outputs
Container logs (stdout/stderr stream), container states, persistent volumes, host network bindings.

## 9. Configuration Strategy
- Service definitions in `docker-compose.yml`.
- Environment variable substitution via root `.env` file.
- Resource constraints (`deploy.resources.limits`) set per service in compose file.

## 10. Failure / Fallback Strategy
- `restart: unless-stopped` restart policy for core services.
- Container healthchecks configured for Postgres, Redis, and Ollama before dependent app services start (`depends_on.condition: service_healthy`).
- Fallback: Host-native python virtual environment execution fallback available for development (`venv` + system PostgreSQL/Redis).

## 11. Security Model
- App containers run as non-root user (`appuser` UID 10001).
- Internal container network isolated; only API port 8000 exposed to host unless explicitly configured.
- Environment secrets passed via `.env` file (excluded from git).

## 12. Operational Requirements
- Host System Requirements: 16 GB RAM minimum (32 GB recommended for 7B LLM models), 4 CPU cores minimum, 50 GB free SSD disk space.
- GPU acceleration optional: NVIDIA Docker runtime (`nvidia-container-toolkit`) supported for hardware-accelerated Ollama.

## 13. Justification for Selection
Standardizes dependencies, guarantees exact Python runtime and C-extensions (asyncpg, pgvector), eliminates "works on my machine" discrepancies, and enables simple single-command evaluation (`docker compose up`).

## 14. Rejected Alternatives and Why
- **Kubernetes / Helm**: Excessive overhead and complexity for a single-node research and enterprise desktop system.
- **Bare-metal Python Installation**: High risk of system library mismatches (e.g. pgvector, C++ compilers, CUDA drivers).

## 15. Concrete Integration Code / Config Pattern

```yaml
# docker-compose.yml snippet
version: "3.8"

networks:
  eaoc-internal:
    driver: bridge

volumes:
  postgres_data:
  redis_data:
  ollama_models:

services:
  postgres:
    image: pgvector/pgvector:pg16
    container_name: eaoc-postgres
    environment:
      POSTGRES_DB: ${POSTGRES_DB:-eaoc_db}
      POSTGRES_USER: ${POSTGRES_USER:-eaoc_user}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-eaoc_pass}
    ports:
      - "5432:5432"
    volumes:
      - postgres_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER:-eaoc_user} -d ${POSTGRES_DB:-eaoc_db}"]
      interval: 5s
      timeout: 5s
      retries: 5
    networks:
      - eaoc-internal

  ollama:
    image: ollama/ollama:latest
    container_name: eaoc-ollama
    ports:
      - "11434:11434"
    volumes:
      - ollama_models:/root/.ollama
    healthcheck:
      test: ["CMD-SHELL", "curl -f http://localhost:11434/api/version || exit 1"]
      interval: 10s
      timeout: 5s
      retries: 3
    networks:
      - eaoc-internal

  api:
    build:
      context: .
      dockerfile: Dockerfile.api
    container_name: eaoc-api
    ports:
      - "8000:8000"
    environment:
      - DATABASE_URL=postgresql+asyncpg://eaoc_user:eaoc_pass@postgres:5432/eaoc_db
      - REDIS_URL=redis://redis:6379/0
      - OLLAMA_BASE_URL=http://ollama:11434
    depends_on:
      postgres:
        condition: service_healthy
      ollama:
        condition: service_healthy
    networks:
      - eaoc-internal
```

---

# TECHNOLOGY J — OLLAMA / LM STUDIO

## 1. Technology Name
Ollama (Default serving runtime) / LM Studio (Supported alternative)

## 2. Category
Local LLM Inference Engine & Model Server

## 3. Version Policy
- **Ollama**: Minimum `v0.1.30+`, Recommended `v0.3.x`
- **LM Studio**: Minimum `v0.2.20+` (OpenAI API local server mode)
- **Upgrade policy**: Tested periodically against new quantizations and API changes. Local model artifacts cached persistently in docker volume.

## 4. Purpose in EAOC
Serves local quantized open-weights LLMs and embedding models via standard HTTP/REST endpoints. Provides zero-cloud inference execution ensuring enterprise data privacy and offline capability.

## 5. Architectural Components Using It
- Planner Agent node
- Task Executor / Specialist Agent nodes
- Summarizer / Code Generator / SQL Generator nodes
- RAG Embedding Generator

## 6. Interface / API Boundary
- **Ollama Native API**: `POST /api/generate`, `POST /api/chat`, `POST /api/embeddings`
- **Ollama OpenAI-Compatible API**: `POST /v1/chat/completions`, `POST /v1/embeddings` (used by standard OpenAI SDK / LangChain ChatOpenAI).

## 7. Inputs
JSON payload with model name, system prompt, messages array, sampling parameters (`temperature`, `top_p`, `num_ctx`, `repeat_penalty`).

## 8. Outputs
Streaming SSE or standard JSON responses containing generated text tokens, token counts, evaluation metrics, or float array embedding vectors.

## 9. Configuration Strategy
- Base URL configured via `OLLAMA_BASE_URL` (default `http://localhost:11434` or `http://ollama:11434`).
- Timeout setting `LLM_REQUEST_TIMEOUT_SECONDS` (default 120s for local inference).
- Pre-loading models on container startup using entrypoint pull scripts.

## 10. Failure / Fallback Strategy
- Retry logic with exponential backoff for connection refusals.
- Fallback model routing: If primary model (e.g. `qwen2.5:7b`) fails or timeouts, retry with lightweight model (`llama3.2:3b`).
- Fallback server: Support secondary host URL (`OLLAMA_FALLBACK_URL` or local LM Studio port `http://localhost:1234/v1`).

## 11. Security Model
- Air-gapped deployment: Ollama operates without internet access after models are pulled.
- No sensitive data leaves local container environment.
- Bound exclusively to container network.

## 12. Operational Requirements
- VRAM / RAM requirements:
  - 3B parameter model (Q4_K_M): ~2.5 GB RAM/VRAM.
  - 7B parameter model (Q4_K_M): ~5.5 GB RAM/VRAM.
- CPU threads: Utilizes available CPU threads if GPU is absent (AVX2/AVX-512 instructions recommended).

## 13. Justification for Selection
Ollama is light-weight, single-binary, natively containerizable, supports model pull/management scripts, and offers OpenAI API compatibility out of the box.

## 14. Rejected Alternatives and Why
- **vLLM / TGI**: High memory requirements, complex setup, optimized for enterprise multi-GPU clusters rather than single-node local desktop deployment.
- **LocalAI**: Slower token generation speeds compared to Ollama's llama.cpp backend core.

## 15. Concrete Integration Code / Config Pattern

```python
import httpx
from langchain_openai import ChatOpenAI

def get_local_llm_client(model_name: str = "llama3.2:3b", base_url: str = "http://localhost:11434/v1") -> ChatOpenAI:
    return ChatOpenAI(
        model=model_name,
        base_url=base_url,
        api_key="ollama",
        temperature=0.1,
        timeout=120.0,
        max_retries=2
    )

async def check_ollama_health(base_url: str = "http://localhost:11434") -> bool:
    async with httpx.AsyncClient() as client:
        try:
            res = await client.get(f"{base_url}/api/tags", timeout=5.0)
            return res.status_code == 200
        except Exception:
            return False
```

---

# TECHNOLOGY K — LOCAL EMBEDDING MODELS

## 1. Technology Name
Local Embedding Models (`nomic-embed-text` default, `bge-small-en-v1.5` alternative)

## 2. Category
Dense Vector Embedding Models

## 3. Version Policy
- **nomic-embed-text**: Version `v1.5` (768 dimensions, context length 8192 tokens)
- **bge-small-en-v1.5**: Version `v1.5` (384 dimensions, context length 512 tokens)
- **Selection Rule**: Dimension length fixed per PostgreSQL database instance. Changing embedding model requires vector index re-indexing.

## 4. Purpose in EAOC
Transforms ingested document chunks, agent memory items, and enterprise schema descriptions into dense numerical vector embeddings for similarity search in pgvector.

## 5. Architectural Components Using It
- Data Processor / Ingestion Pipeline
- RAG Retriever Agent
- Semantic Cache Manager

## 6. Interface / API Boundary
Ollama Embeddings API endpoint `POST /api/embeddings` or `/v1/embeddings`.

## 7. Inputs
Text strings or batch of text chunks (`List[str]`).

## 8. Outputs
Vector of floating-point numbers (`List[float]` of length 768 or 384).

## 9. Configuration Strategy
- Model name specified in `EMBEDDING_MODEL_NAME` (default `nomic-embed-text`).
- Vector dimension size specified in `EMBEDDING_DIMENSION` (default `768`).
- Direct batch size configured in `EMBEDDING_BATCH_SIZE` (default 32).

## 10. Failure / Fallback Strategy
- Truncation handling for text inputs exceeding max context window.
- Graceful error logging and document retry queue insertion if embedding service fails.

## 11. Security Model
All text processing and embedding generation occurs strictly in-memory within local runtime. Zero data egress.

## 12. Operational Requirements
- Low CPU/VRAM footprint (~500 MB RAM).
- Fast generation speed (>100 chunks/sec on CPU).

## 13. Justification for Selection
`nomic-embed-text` provides superior retrieval accuracy on technical enterprise benchmarks, long context length (8k), and small parameter footprint (137M parameters).

## 14. Rejected Alternatives and Why
- **OpenAI text-embedding-3-small**: Cloud dependency; violates privacy constraint.
- **HuggingFace SentenceTransformers in Python**: Adds heavy PyTorch dependency to backend container; using Ollama unifies model management under single service.

## 15. Concrete Integration Code / Config Pattern

```python
import httpx
from typing import List

async def generate_local_embedding(text: str, model: str = "nomic-embed-text", ollama_url: str = "http://localhost:11434") -> List[float]:
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{ollama_url}/api/embeddings",
            json={"model": model, "prompt": text},
            timeout=30.0
        )
        response.raise_for_status()
        data = response.json()
        return data["embedding"]
```

---

# TECHNOLOGY L — LOCAL LLM MODELS

## 1. Technology Name
Local Quantized Open LLM Models (`llama3.2:3b`, `qwen2.5:7b`, `mistral:7b`)

## 2. Category
Generative Large Language Model Weights

## 3. Version Policy
- **llama3.2:3b**: Quantization `Q4_K_M` (Default fast model for lightweight triage and formatting)
- **qwen2.5:7b**: Quantization `Q4_K_M` (Default reasoning & SQL generation model)
- **mistral:7b**: Quantization `Q4_K_M` (Alternative instruction-following model)
- **Model Registry**: Managed via `ollama pull <model_name>`.

## 4. Purpose in EAOC
Provides core intelligence for all 12 logical agents (planning, tool selection, SQL generation, document synthesis, workflow routing).

## 5. Architectural Components Using It
All 12 EAOC Agents (Orchestrator, Planner, Data Specialist, SQL Agent, Document Agent, Analytics Agent, Security Agent, Communication Agent, Task Execution Agent, Memory Agent, Audit Agent, Monitoring Agent).

## 6. Interface / API Boundary
Invoked via LangChain / Ollama API adapters using standardized prompt structures.

## 7. Inputs
System instruction prompt, structured context/documents, user input message, function/tool schema JSON.

## 8. Outputs
Generated natural language text, formatted JSON responses, tool call invocations.

## 9. Configuration Strategy
- Model assignments defined in `config/agent_models.yaml`:
```yaml
orchestrator: llama3.2:3b
planner: qwen2.5:7b
sql_agent: qwen2.5:7b
task_executor: llama3.2:3b
default: llama3.2:3b
```

## 10. Failure / Fallback Strategy
- If a 7B model exhausts host RAM/GPU VRAM, dynamic fallback routes task to 3B model.
- Temperature and sampling fallback: If JSON parsing fails twice, system re-prompts with `temperature=0.0` and explicit JSON schema constraint.

## 11. Security Model
Open-weights execution prevents telemetry leaking or external model context poisoning.

## 12. Operational Requirements
- Disk storage: ~2.0 GB per 3B model file, ~4.7 GB per 7B model file.
- RAM/VRAM: 4 GB to 12 GB runtime memory footprint.

## 13. Justification for Selection
- `llama3.2:3b`: Ultra-fast latency (<50ms first token), low resource usage.
- `qwen2.5:7b`: State-of-the-art 7B reasoning, superior coding and SQL query generation accuracy.
- Quantization `Q4_K_M`: Optimal trade-off between model accuracy retention (99% of FP16 benchmark) and memory consumption.

## 14. Rejected Alternatives and Why
- **Llama-3-70B / Falcon-180B**: Unsuitable for single-node developer/enterprise workstation deployment.
- **Unquantized FP16 Weights**: Requires multi-GPU enterprise server infrastructure.

## 15. Concrete Integration Code / Config Pattern

```python
from pydantic import BaseModel, Field
from langchain_openai import ChatOpenAI

class AgentTaskPlan(BaseModel):
    task_id: str = Field(description="Unique task identifier")
    steps: list[str] = Field(description="Sequential execution steps")
    estimated_risk: str = Field(description="LOW, MEDIUM, or HIGH")

def execute_structured_planner(prompt_text: str) -> AgentTaskPlan:
    llm = ChatOpenAI(
        model="qwen2.5:7b",
        base_url="http://localhost:11434/v1",
        api_key="ollama",
        temperature=0.0
    )
    structured_llm = llm.with_structured_output(AgentTaskPlan)
    return structured_llm.invoke(prompt_text)
```
