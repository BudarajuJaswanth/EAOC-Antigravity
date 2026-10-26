"""
EAOC Finance Agent Data Provider Interface & Implementations
Establishes a safe dependency-injected boundary for financial data retrieval,
preventing direct production database connections, arbitrary external calls,
and strictly confining SQL delegation to the 'finance' schema.
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from eaoc.agents.finance.schemas import FinanceEntityType
from eaoc.schemas.domain import AgentExecutionContext, AgentID, TaskStatus


class BaseFinanceDataProvider(ABC):
    """
    Abstract boundary for financial data operations.
    Concrete implementations fetch data from the SQL/Data Agent (scoped to the finance schema),
    document extractions, or local test fixtures.
    """

    @abstractmethod
    async def query_finance_data(
        self,
        entity_type: FinanceEntityType,
        filters: Dict[str, Any],
        context: Optional[AgentExecutionContext] = None,
    ) -> List[Dict[str, Any]]:
        """
        Retrieves financial records matching the given entity_type and filter criteria.
        """
        pass

    @property
    def source_name(self) -> str:
        """Name of the data source for provenance tracking."""
        return "base_finance_provider"


class InMemoryFinanceDataProvider(BaseFinanceDataProvider):
    """
    In-memory financial data provider for unit testing and isolated evaluation.
    Holds records for invoices, budgets, expenses, and financial reports.
    """

    def __init__(
        self,
        data: Optional[Dict[str, List[Dict[str, Any]]]] = None,
        source_name: str = "in_memory_provider",
    ):
        self._data: Dict[str, List[Dict[str, Any]]] = data or {
            "invoice": [],
            "budget": [],
            "expense": [],
            "report": [],
        }
        self._source_name = source_name

    @property
    def source_name(self) -> str:
        return self._source_name

    async def query_finance_data(
        self,
        entity_type: FinanceEntityType,
        filters: Dict[str, Any],
        context: Optional[AgentExecutionContext] = None,
    ) -> List[Dict[str, Any]]:
        """
        Filters in-memory dataset by entity_type and matching key-value pairs in filters.
        """
        key = entity_type.value
        records = self._data.get(key, [])

        if not filters:
            return [dict(r) for r in records]

        filtered = []
        for rec in records:
            match = True
            for f_key, f_val in filters.items():
                if f_key in rec:
                    # Case-insensitive string matching if both are strings
                    rec_val = rec[f_key]
                    if isinstance(f_val, str) and isinstance(rec_val, str):
                        if rec_val.strip().lower() != f_val.strip().lower():
                            match = False
                            break
                    elif rec_val != f_val:
                        match = False
                        break
            if match:
                filtered.append(dict(rec))

        return filtered


class SqlFinanceDataProvider(BaseFinanceDataProvider):
    """
    Finance data provider that delegates structured queries to the SQL/Data Agent.
    Strictly enforces schema allowlisting to the 'finance' schema only.
    Access to 'hr', 'it', 'analytics', or any other schema is strictly blocked.
    """

    def __init__(
        self,
        sql_agent: Any,
        source_name: str = "finance_sql_delegation",
    ):
        self._sql_agent = sql_agent
        self._source_name = source_name

    @property
    def source_name(self) -> str:
        return self._source_name

    async def query_finance_data(
        self,
        entity_type: FinanceEntityType,
        filters: Dict[str, Any],
        context: Optional[AgentExecutionContext] = None,
    ) -> List[Dict[str, Any]]:
        """
        Constructs and dispatches a strictly bounded read-only query targeting
        only the 'finance' schema through the SQL/Data Agent.
        """
        # 1. Enforce that no non-finance schema can be requested in filters
        requested_schema = filters.get("schema") or filters.get("target_schema")
        if requested_schema is not None and str(requested_schema).lower() != "finance":
            raise PermissionError(
                f"Finance data access is strictly restricted to schema 'finance'. "
                f"Access to schema '{requested_schema}' is prohibited."
            )

        # 2. Map entity type strictly to canonical finance schema tables
        table_map = {
            FinanceEntityType.INVOICE: "finance.invoices",
            FinanceEntityType.BUDGET: "finance.budgets",
            FinanceEntityType.EXPENSE: "finance.expenses",
            FinanceEntityType.REPORT: "finance.reports",
        }
        table = table_map.get(entity_type)
        if not table:
            raise ValueError(f"Unsupported finance entity type '{entity_type}'.")

        # 3. Formulate query strictly against finance schema table
        sql_query = f"SELECT * FROM {table}"
        where_clauses = []
        params = {}
        for k, v in filters.items():
            if k not in ("schema", "target_schema"):
                where_clauses.append(f"{k} = :{k}")
                params[k] = v
        if where_clauses:
            sql_query += " WHERE " + " AND ".join(where_clauses)

        # 4. Formulate delegated execution context with hardcoded permitted_schemas=['finance']
        sql_context = AgentExecutionContext(
            execution_id=getattr(context, "execution_id", "sql-del-001"),
            workflow_id=getattr(context, "workflow_id", "wf-del-001"),
            task_id=getattr(context, "task_id", "task-del-001"),
            agent_id=AgentID.SQL_DATA,
            request_id=getattr(context, "request_id", "req-del-001"),
            correlation_id=getattr(context, "correlation_id", "corr-del-001"),
            input_payload={
                "query_goal": f"Retrieve {entity_type.value} records from finance schema",
                "sql_query": sql_query,
                "permitted_schemas": ["finance"],  # Strictly locked to finance schema
                "parameters": params,
                "row_limit": 100,
            },
            metadata=getattr(context, "metadata", {}),
        )

        result = await self._sql_agent.execute(sql_context)
        if result.status != TaskStatus.COMPLETED:
            err_msg = (
                result.error.details.get("error", result.error.message)
                if result.error
                else "SQL delegation failed"
            )
            raise RuntimeError(f"Finance SQL delegation failed: {err_msg}")

        return result.output_payload.get("rows", [])
