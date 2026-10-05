# EAOC — Technology Specification
# Part 0.4-D: Technologies M–N, Open Decisions, Deprecation Strategy, and Tech Matrix
# Phase 0 · Part 0.4 · Version 1.0
# Status: DRAFT — Awaiting Part 0.4 approval
# Inputs: Master Spec v1.0, Part 0.1 FRS/NFRS, Part 0.2 Agent Spec, Part 0.3 Architecture

---

# TECHNOLOGY M — PYPDF / PYTHON-DOCX / PANDAS

## 1. Technology Name
PyPDF (`pypdf`), python-docx (`python-docx`), and Pandas (`pandas`)

## 2. Category
Data Processing, Document Parsing & Extraction Libraries

## 3. Version Policy
- `pypdf >= 4.0.0`
- `python-docx >= 1.1.0`
- `pandas >= 2.2.0`
- **Upgrade policy**: Standard semantic version upgrades verified by unit test suite for document parsing correctness.

## 4. Purpose in EAOC
- `pypdf`: Extracts raw text, metadata, and page layouts from uploaded PDF enterprise documents.
- `python-docx`: Extracts text, tables, and document structure from Microsoft Word (.docx) files.
- `pandas`: Processes tabular datasets (CSV, Excel), performs quick data profiling, aggregation, and format conversion prior to database ingestion or LLM context insertion.

## 5. Architectural Components Using It
- Document Processing Agent
- Data Ingestion Pipeline / RAG Loader
- Analytics Agent (tabular summarization & preview generation)

## 6. Interface / API Boundary
Internal Python library calls invoked asynchronously within background worker tasks.

## 7. Inputs
File streams (`bytes`), local filepath pointers (`str`), or file-like objects (e.g. `UploadFile` from FastAPI).

## 8. Outputs
Extracted text blocks (`str`), structured metadata dicts (`dict`), pandas DataFrames (`DataFrame`), or JSON/CSV string exports.

## 9. Configuration Strategy
- Text chunking parameters configured in `config/rag_config.yaml`:
  `chunk_size`: 1000 characters
  `chunk_overlap`: 200 characters
- Maximum file size upload limit: 50 MB (configured in FastAPI request validator).

## 10. Failure / Fallback Strategy
- Password-protected or encrypted PDF: Log error and signal user via API for password prompt.
- Corrupted docx/pdf: Fall back to raw text string extraction (`pypdf.errors.PdfReadError` catch block).
- Memory limit safety: Process large CSV/Excel files using `pandas.read_csv(chunksize=5000)` to avoid OOM memory spikes.

## 11. Security Model
- Files parsed in memory or temporary directory (`/tmp/eaoc_uploads`).
- Upload files sanitized to prevent path traversal attacks (`securesystemslib.util` or `pathlib.Path.name` stripping).
- Files deleted immediately after chunking and vector storage unless explicitly archived.

## 12. Operational Requirements
Minimal CPU and RAM footprint (~50-200 MB per active parsing task).

## 13. Justification for Selection
- `pypdf`: Pure Python library, zero C++ external binary dependency (unlike `pdf2image` or `poppler`), thread-safe, fast.
- `python-docx`: Industry standard for DOCX parsing in Python.
- `pandas`: De-facto standard for tabular data manipulation in Python ecosystem.

## 14. Rejected Alternatives and Why
- **Unstructured.io**: High dependency weight, complex native dependencies, slow execution for basic enterprise doc types.
- **PyMuPDF (fitz)**: Dual GPL/Commercial licensing constraints; `pypdf` BSD-licensed.

## 15. Concrete Integration Code / Config Pattern

```python
from io import BytesIO
import pypdf
import pandas as pd
from typing import List, Dict, Any

def extract_pdf_chunks(pdf_bytes: bytes, chunk_size: int = 1000, overlap: int = 200) -> List[Dict[str, Any]]:
    reader = pypdf.PdfReader(BytesIO(pdf_bytes))
    extracted_chunks = []
    
    full_text = ""
    for page_num, page in enumerate(reader.pages):
        text = page.extract_text() or ""
        full_text += f"
--- Page {page_num + 1} ---
" + text
        
    start = 0
    chunk_id = 0
    while start < len(full_text):
        end = start + chunk_size
        chunk_text = full_text[start:end]
        extracted_chunks.append({
            "chunk_id": chunk_id,
            "text": chunk_text,
            "char_length": len(chunk_text)
        })
        start += (chunk_size - overlap)
        chunk_id += 1
        
    return extracted_chunks

def summarize_csv_bytes(csv_bytes: bytes) -> Dict[str, Any]:
    df = pd.read_csv(BytesIO(csv_bytes))
    return {
        "row_count": len(df),
        "column_count": len(df.columns),
        "columns": list(df.columns),
        "head_json": df.head(5).to_dict(orient="records")
    }
```

---

# TECHNOLOGY N — SQLALCHEMY & ASYNCPG

## 1. Technology Name
SQLAlchemy v2 (ORM & Core) with `asyncpg` (Async PostgreSQL DBAPI driver)

## 2. Category
Object-Relational Mapping (ORM) & Database Connectivity

## 3. Version Policy
- `sqlalchemy >= 2.0.25` (using asyncio API extensions exclusively)
- `asyncpg >= 0.29.0`
- **Upgrade policy**: Standard minor patch updates. Ensure dialect compatibility with PostgreSQL 16 + pgvector.

## 4. Purpose in EAOC
Provides high-performance, asynchronous database access to PostgreSQL. Handles connection pooling, transactional unit-of-work patterns, model mapping (Users, Tasks, Executions, Audit Logs, Vector Store), and schema migration generation via Alembic.

## 5. Architectural Components Using It
- Persistence Layer (Data Access Repositories)
- Audit Logger
- Task & Execution Repositories
- Memory Manager
- RBAC and User Management services

## 6. Interface / API Boundary
Python `AsyncSession` provided via FastAPI dependency injection or worker context manager.

## 7. Inputs
SQLAlchemy ORM models, asyncio async database queries (`select()`, `insert()`, `update()`), text SQL queries.

## 8. Outputs
Async generator streams, mapped model instances, scalar query results, database execution metadata.

## 9. Configuration Strategy
- Connection string configured via `DATABASE_URL` env var (`postgresql+asyncpg://user:pass@host:5432/dbname`).
- Connection Pool parameters:
  `pool_size`: 20 connections
  `max_overflow`: 10 connections
  `pool_recycle`: 1800 seconds
  `pool_pre_ping`: True (validates connection prior to query release)

## 10. Failure / Fallback Strategy
- Automatic reconnect: `pool_pre_ping=True` detects stale or dropped connections and reconnects gracefully.
- Transaction rollback: Managed context managers automatically invoke `await session.rollback()` on exception.

## 11. Security Model
- Parameterized SQL generation prevents SQL injection across all ORM queries.
- Read-only database sessions used by SQL Agent for user-directed queries to prevent accidental modifications.

## 12. Operational Requirements
Low overhead (~20 MB memory footprint). Connection pool configured to prevent exhausting PostgreSQL `max_connections` (100 limit).

## 13. Justification for Selection
- SQLAlchemy 2.0 provides first-class `async/await` syntax.
- `asyncpg` is the fastest PostgreSQL driver available for Python (up to 3-5x faster than `psycopg2`).

## 14. Rejected Alternatives and Why
- **Tortoise ORM / Peewee**: Less mature ecosystem, limited support for pgvector extensions compared to SQLAlchemy 2.0 + pgvector-python.
- **psycopg3 async**: Slower performance benchmarks compared to `asyncpg`.

## 15. Concrete Integration Code / Config Pattern

```python
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import String, DateTime, Text, func
import datetime

# Async Database Engine Setup
DATABASE_URL = "postgresql+asyncpg://eaoc_user:eaoc_pass@localhost:5432/eaoc_db"

engine = create_async_engine(
    DATABASE_URL,
    pool_size=20,
    max_overflow=10,
    pool_pre_ping=True,
    echo=False
)

AsyncSessionFactory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

class Base(DeclarativeBase):
    pass

class TaskRecord(Base):
    __tablename__ = "tasks"
    
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    status: Mapped[str] = mapped_column(String(32), default="PENDING")
    prompt: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

async def get_db_session() -> AsyncSession:
    async with AsyncSessionFactory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
```

---

# SECTION O — OPEN TECHNOLOGY DECISIONS

The following technology choices are explicitly identified as **OPEN** pending empirical benchmark data from Phase 1 sandbox tests:

| Decision ID | Area | Options Under Evaluation | Trade-off Analysis | Trigger for Resolution |
|---|---|---|---|---|
| **TECH-OPEN-01** | Vector Distance Metric | Cosine Distance vs Inner Product (`ip`) | Cosine is standard for normalized embeddings; Inner Product is 15-20% faster in pgvector. | Benchmark vector query speed and recall precision on 10,000 document chunks in Phase 1. |
| **TECH-OPEN-02** | LLM Streaming Strategy | Server-Sent Events (SSE) vs WebSockets | SSE is simpler for HTTP API integration; WebSockets allow bi-directional streaming during long agent execution. | User interface requirement evaluation in Phase 2. |
| **TECH-OPEN-03** | Local LLM Serving Engine | Ollama Native API vs LM Studio Local Server | Ollama has superior docker support; LM Studio provides richer local GPU memory profiling GUI. | Developer workflow evaluation in Phase 1 setup. |
| **TECH-OPEN-04** | Document Processing Fallback | `pypdf` vs `pdfplumber` | `pypdf` is lightweight and fast; `pdfplumber` has higher accuracy for complex multi-column table extraction. | Test set evaluation on 50 sample enterprise financial PDFs during Phase 1. |

---

# SECTION P — DEPRECATION AND MIGRATION STRATEGY

To ensure long-term architectural maintainability and prevent framework lock-in, EAOC implements the following abstraction layer rules:

1. **LLM Abstraction Boundary (`LLMAdapter`)**:
   - Agent code NEVER calls LLM APIs directly.
   - All calls pass through `LLMAdapter` wrapper interface.
   - **Migration path**: If replacing Ollama with vLLM or cloud API (e.g., Azure OpenAI), only `LLMAdapter` implementation is updated; zero agent node code changes required.

2. **Vector Store Abstraction Boundary (`VectorRepository`)**:
   - RAG Retriever interacts solely with `VectorRepository` interface.
   - **Migration path**: If migrating from pgvector to Qdrant or Milvus, implement `QdrantVectorRepository` conforming to `VectorRepository` abstract base class.

3. **Cache & Pub/Sub Abstraction (`CacheService`)**:
   - Code accesses Redis via `CacheService` class.
   - **Migration path**: Allows switching to in-memory Python cache or Valkey without refactoring agent logic.

---

# SECTION Q — FULL TECHNOLOGY MATRIX

| ID | Technology | Category | Version Policy | Role in EAOC | Core Dependencies | Alternative Evaluated |
|---|---|---|---|---|---|---|
| T01 | **Python** | Programming Language | 3.12.x | Core Backend Runtime | None | Node.js, Go |
| T02 | **FastAPI** | Web Framework | 0.110.x+ | REST API & SSE Endpoint Provider | Pydantic, Starlette | Flask, Django REST |
| T03 | **Pydantic** | Validation Engine | 2.6.x+ | Data Models, Config & Tool Schemas | Python standard library | Marshmallow, dataclasses |
| T04 | **LangGraph** | Orchestration Engine | 0.0.30+ | Multi-Agent Stateful State Graph | Pydantic, LangChain Core | AutoGen, CrewAI, LangChain AgentExecutor |
| T05 | **LangChain** | LLM Utilities | Core 0.2.x | ChatOpenAI Client & Output Parsers | Pydantic | LlamaIndex, Haystack |
| T06 | **PostgreSQL** | Database | 16.x | System Store, Memory & Audit Logs | Docker / OS Kernel | MySQL, SQLite |
| T07 | **pgvector** | Vector Search Engine | 0.7.x | RAG Document Embeddings Store | PostgreSQL 16 | Qdrant, ChromaDB, Pinecone |
| T08 | **Redis** | Caching & Rate Limiting | 7.2.x | Session Cache, SSE PubSub & Rate Limit | Docker / OS Kernel | Memcached, Dragonfly |
| T09 | **Docker Compose**| Containerization | v2.20.0+ | Infrastructure Orchestration | Docker Engine | Kubernetes, Bare-metal venv |
| T10 | **Ollama** | Local LLM Server | v0.3.x | Zero-cloud Local Inference Engine | Docker / C++ llama.cpp | LM Studio, vLLM, LocalAI |
| T11 | **nomic-embed-text**| Local Vector Embeddings | v1.5 (768d) | Text Chunk Dense Vectorization | Ollama Engine | bge-small-en, OpenAI embeddings |
| T12 | **qwen2.5 / llama3.2**| Local LLM Weights | Q4_K_M Quant | Agent Reasoning & Code/SQL Gen | Ollama Engine | Mistral-7B, Llama-3-70B |
| T13 | **PyPDF / Pandas** | Document Processing | pypdf 4.0+ | PDF / CSV Ingestion & Structuring | Python standard library | Unstructured.io, PyMuPDF |
| T14 | **SQLAlchemy** | Async ORM & DB Driver | 2.0.25+ / asyncpg | Async PostgreSQL Data Access | asyncpg, PostgreSQL | Tortoise ORM, psycopg3 |

