# EAOC Supabase PostgreSQL Setup & Migration Guide

## 1. Database Overview
The **Enterprise AI Operations Center (EAOC)** uses **Supabase PostgreSQL** as its exclusive primary database platform. 

* **ORM & Driver**: SQLAlchemy 2.0+ with `asyncpg` (`postgresql+asyncpg://...`)
* **Vector Store**: `pgvector` extension for future dense vector embeddings
* **Migration Manager**: Alembic (`alembic.ini`)

---

## 2. Connection Methods & Environment Setup

To configure Supabase PostgreSQL, set `DATABASE_URL` in your local `.env` file (copied from `.env.example`). 

> ⚠️ **SECURITY WARNING**: Never commit `.env` or hard-code database passwords in source code, Dockerfiles, or git repositories.

### Option A: Direct Connection (Default)
Best for direct asynchronous connections when using Supabase IPv4/IPv6 direct access:
```env
DATABASE_URL=postgresql+asyncpg://postgres:[YOUR-PASSWORD]@db.[YOUR-SUPABASE-REF].supabase.co:5432/postgres
```

### Option B: Supabase Connection Pooler (Recommended for High Concurrency)
Best when running under serverless or containerized environments using Supabase Transaction/Session Pooler:
```env
DATABASE_URL=postgresql+asyncpg://postgres.[YOUR-SUPABASE-REF]:[YOUR-PASSWORD]@aws-0-[REGION].pooler.supabase.com:6543/postgres
```

---

## 3. pgvector Extension Enablement

EAOC requires the `pgvector` extension for dense vector storage in future Phase 1/2 parts.

### Automatic Activation
Alembic migration `0001_initial_foundation` executes `CREATE EXTENSION IF NOT EXISTS vector;`.

### Manual Activation (If Privileges Require Dashboard Approval)
If your Supabase database user role lacks superuser extension privileges:
1. Log in to the [Supabase Dashboard](https://supabase.com/dashboard).
2. Select your EAOC Project.
3. Navigate to **Database** $\rightarrow$ **Extensions**.
4. Search for `vector` (`pgvector`).
5. Toggle the extension to **ON**.

---

## 4. Running Database Migrations

From the repository root with virtual environment activated:

```powershell
# Verify migration status
.\.venv\Scripts\python -m alembic current

# Upgrade to latest foundation revision
.\.venv\Scripts\python -m alembic upgrade head
```
