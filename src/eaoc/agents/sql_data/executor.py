"""
EAOC SQL/Data Agent - Execution Interface & Dependency Boundary
Defines abstract BaseSqlExecutor and SqlQueryResult data contracts to decouple
structured SQL execution from live databases, enabling deterministic testing,
row-limiting, and parameter binding.
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field
from eaoc.observability import Timer


class SqlQueryResult(BaseModel):
    """
    Structured outcome of a SQL query execution.
    """
    model_config = ConfigDict(frozen=True)

    columns: List[str] = Field(default_factory=list, description="Column names returned by the query")
    rows: List[Dict[str, Any]] = Field(default_factory=list, description="Row records mapping column names to values")
    row_count: int = Field(default=0, ge=0, description="Number of rows returned (capped at row_limit)")
    truncated: bool = Field(default=False, description="Whether result set was truncated by row_limit")
    execution_time_ms: float = Field(default=0.0, ge=0.0, description="Execution duration in milliseconds")


class BaseSqlExecutor(ABC):
    """
    Dependency boundary / abstraction for SQL query execution.
    Decouples SqlDataAgent from concrete database engines.
    """

    @abstractmethod
    async def execute_query(
        self,
        sql: str,
        parameters: Optional[Dict[str, Any]] = None,
        row_limit: int = 100,
    ) -> SqlQueryResult:
        """
        Executes a validated read-only SQL query with parameter binding and row limiting.
        """
        pass


class InMemorySqlExecutor(BaseSqlExecutor):
    """
    In-memory SQL executor stub for deterministic testing and local verification.
    Stores registered tables as in-memory datasets and performs basic column selection and filtering.
    """

    def __init__(self, tables: Optional[Dict[str, List[Dict[str, Any]]]] = None):
        self._tables: Dict[str, List[Dict[str, Any]]] = (
            {k.lower(): v for k, v in tables.items()} if tables else {}
        )

    def set_table(self, table_name: str, rows: List[Dict[str, Any]]) -> None:
        self._tables[table_name.lower()] = rows

    async def execute_query(
        self,
        sql: str,
        parameters: Optional[Dict[str, Any]] = None,
        row_limit: int = 100,
    ) -> SqlQueryResult:
        timer = Timer().start()

        # Find matching table from FROM clause
        sql_lower = sql.lower()
        matched_table = None
        for table in self._tables:
            if f"from {table}" in sql_lower:
                matched_table = table
                break

        if matched_table and matched_table in self._tables:
            all_rows = self._tables[matched_table]
            if parameters:
                filtered_rows = []
                for row in all_rows:
                    matches = True
                    for k, v in parameters.items():
                        if k in row and row[k] != v:
                            matches = False
                            break
                    if matches:
                        filtered_rows.append(row)
                target_rows = filtered_rows
            else:
                target_rows = all_rows

            truncated = len(target_rows) > row_limit
            selected_rows = target_rows[:row_limit]
            columns = list(selected_rows[0].keys()) if selected_rows else []
            elapsed = timer.stop()
            return SqlQueryResult(
                columns=columns,
                rows=selected_rows,
                row_count=len(selected_rows),
                truncated=truncated,
                execution_time_ms=elapsed,
            )

        elapsed = timer.stop()
        return SqlQueryResult(
            columns=[],
            rows=[],
            row_count=0,
            truncated=False,
            execution_time_ms=elapsed,
        )
