"""
EAOC HR Agent Data Provider Interface & Implementations
Establishes a safe dependency-injected boundary for HR data retrieval and policy lookups,
preventing direct production database connections and locking SQL access strictly to the 'hr' schema.
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from eaoc.agents.hr.schemas import HrQueryType
from eaoc.schemas.domain import AgentExecutionContext, AgentID, TaskStatus


class BaseHrDataProvider(ABC):
    """
    Abstract boundary for HR structured data and policy retrieval.
    Concrete implementations fetch data from the SQL/Data Agent (scoped to the hr schema),
    the Research/RAG Agent (hr policy knowledge base), or local test fixtures.
    """

    @abstractmethod
    async def query_hr_data(
        self,
        query_type: HrQueryType,
        filters: Dict[str, Any],
        context: Optional[AgentExecutionContext] = None,
    ) -> List[Dict[str, Any]]:
        """
        Retrieves structured HR records matching query type and filter criteria.
        """
        pass

    @abstractmethod
    async def query_policy(
        self,
        topic: str,
        context: Optional[AgentExecutionContext] = None,
    ) -> Dict[str, Any]:
        """
        Retrieves HR policy text and citations for the requested topic.
        """
        pass

    @property
    def source_name(self) -> str:
        """Name of the data source for provenance tracking."""
        return "base_hr_provider"


class InMemoryHrDataProvider(BaseHrDataProvider):
    """
    In-memory HR data provider for unit testing and isolated evaluation.
    Holds records for employees, departments, org charts, and HR policies.
    """

    def __init__(
        self,
        employees: Optional[List[Dict[str, Any]]] = None,
        departments: Optional[List[Dict[str, Any]]] = None,
        org_chart: Optional[List[Dict[str, Any]]] = None,
        policies: Optional[Dict[str, Dict[str, Any]]] = None,
        source_name: str = "in_memory_provider",
    ):
        self._employees = employees or []
        self._departments = departments or []
        self._org_chart = org_chart or []
        self._policies = policies or {}
        self._source_name = source_name

    @property
    def source_name(self) -> str:
        return self._source_name

    async def query_hr_data(
        self,
        query_type: HrQueryType,
        filters: Dict[str, Any],
        context: Optional[AgentExecutionContext] = None,
    ) -> List[Dict[str, Any]]:
        """
        Filters in-memory dataset based on query_type and filters.
        """
        if query_type in (HrQueryType.HEADCOUNT, HrQueryType.EMPLOYEE_INFO):
            source_list = self._employees
        elif query_type == HrQueryType.ORG_CHART:
            source_list = self._org_chart if self._org_chart else self._employees
        else:
            source_list = []

        if not filters:
            return [dict(r) for r in source_list]

        filtered = []
        for rec in source_list:
            match = True
            for f_key, f_val in filters.items():
                if f_key in rec:
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

    async def query_policy(
        self,
        topic: str,
        context: Optional[AgentExecutionContext] = None,
    ) -> Dict[str, Any]:
        """
        Returns policy excerpt matching the topic, or empty if not found.
        """
        clean_topic = topic.strip().lower()
        for p_topic, p_data in self._policies.items():
            if p_topic.lower() in clean_topic or clean_topic in p_topic.lower():
                return dict(p_data)

        # Return default policy structure if not found
        return {
            "topic": topic,
            "policy_found": False,
            "content": f"No specific HR policy document found for topic '{topic}'.",
            "citation": "HR Policy Knowledge Base",
        }


class SqlHrDataProvider(BaseHrDataProvider):
    """
    HR data provider that delegates structured queries to the SQL/Data Agent.
    Strictly enforces schema allowlisting to the 'hr' schema only.
    Access to 'finance', 'it', 'analytics', or any other schema is strictly blocked.
    """

    def __init__(
        self,
        sql_agent: Any,
        source_name: str = "hr_sql_delegation",
    ):
        self._sql_agent = sql_agent
        self._source_name = source_name

    @property
    def source_name(self) -> str:
        return self._source_name

    async def query_hr_data(
        self,
        query_type: HrQueryType,
        filters: Dict[str, Any],
        context: Optional[AgentExecutionContext] = None,
    ) -> List[Dict[str, Any]]:
        """
        Dispatches a strictly bounded read-only query targeting only the 'hr' schema
        through the SQL/Data Agent.
        """
        # 1. Enforce that no non-hr schema can be requested in filters
        requested_schema = filters.get("schema") or filters.get("target_schema")
        if requested_schema is not None and str(requested_schema).lower() != "hr":
            raise PermissionError(
                f"HR data access is strictly restricted to schema 'hr'. "
                f"Access to schema '{requested_schema}' is prohibited."
            )

        # 2. Map query type strictly to canonical hr schema tables
        table_map = {
            HrQueryType.HEADCOUNT: "hr.employees",
            HrQueryType.EMPLOYEE_INFO: "hr.employees",
            HrQueryType.ORG_CHART: "hr.org_chart",
        }
        table = table_map.get(query_type)
        if not table:
            raise ValueError(f"Unsupported HR query type for SQL retrieval: '{query_type}'.")

        # 3. Formulate query strictly against hr schema table
        sql_query = f"SELECT * FROM {table}"
        where_clauses = []
        params = {}
        for k, v in filters.items():
            if k not in ("schema", "target_schema"):
                where_clauses.append(f"{k} = :{k}")
                params[k] = v
        if where_clauses:
            sql_query += " WHERE " + " AND ".join(where_clauses)

        # 4. Formulate delegated execution context with hardcoded permitted_schemas=['hr']
        sql_context = AgentExecutionContext(
            execution_id=getattr(context, "execution_id", "sql-del-001"),
            workflow_id=getattr(context, "workflow_id", "wf-del-001"),
            task_id=getattr(context, "task_id", "task-del-001"),
            agent_id=AgentID.SQL_DATA,
            request_id=getattr(context, "request_id", "req-del-001"),
            correlation_id=getattr(context, "correlation_id", "corr-del-001"),
            input_payload={
                "query_goal": f"Retrieve {query_type.value} records from hr schema",
                "sql_query": sql_query,
                "permitted_schemas": ["hr"],  # Strictly locked to hr schema
                "parameters": params,
                "row_limit": 500,
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
            raise RuntimeError(f"HR SQL delegation failed: {err_msg}")

        return result.output_payload.get("rows", [])

    async def query_policy(
        self,
        topic: str,
        context: Optional[AgentExecutionContext] = None,
    ) -> Dict[str, Any]:
        """SQL data provider does not support vector policy lookups directly."""
        return {
            "topic": topic,
            "policy_found": False,
            "content": f"Policy lookups not supported over relational SQL provider for topic '{topic}'.",
            "citation": "HR Knowledge Base",
        }
