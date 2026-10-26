"""
EAOC SQL/Data Agent Implementation
Provides secure, read-only structured data access by validating and executing
parameterized SQL queries against an abstract database executor. Strictly prevents
write operations, multi-statement injection, and unauthorized schema access.
"""

import re
from typing import Any, Dict, List, Optional, Set, Tuple

from eaoc.agents.base import BaseAgent
from eaoc.agents.sql_data.executor import BaseSqlExecutor
from eaoc.agents.sql_data.schemas import SqlDataInput, SqlDataOutput
from eaoc.models.base import ModelProviderWrapper
from eaoc.observability import get_logger
from eaoc.schemas.domain import AgentExecutionContext, AgentID

logger = get_logger("eaoc.agents.sql_data")

# Prohibited keywords indicating write operations, DDL, administrative commands, or data modification
FORBIDDEN_KEYWORDS: Set[str] = {
    "INSERT",
    "UPDATE",
    "DELETE",
    "DROP",
    "ALTER",
    "TRUNCATE",
    "CREATE",
    "GRANT",
    "REVOKE",
    "EXEC",
    "EXECUTE",
    "CALL",
    "REPLACE",
    "MERGE",
    "COPY",
    "INTO",  # Disallows SELECT ... INTO (table creation)
    "VACUUM",
    "LOCK",
}

SQL_GENERATION_PROMPT = (
    "You are the Enterprise AI Operations Center (EAOC) SQL/Data Agent.\n"
    "Your responsibility is to generate a single, read-only SQL SELECT statement for PostgreSQL\n"
    "to satisfy the user's query goal. You must strictly adhere to the permitted schemas.\n"
    "Rules:\n"
    "1. Generate ONLY a single SELECT or WITH ... SELECT statement.\n"
    "2. Never generate INSERT, UPDATE, DELETE, DROP, ALTER, TRUNCATE, or CREATE statements.\n"
    "3. Use only the permitted schemas: {permitted_schemas}.\n"
    "4. Return only raw SQL text without markdown code blocks."
)


class SqlDataAgent(BaseAgent[SqlDataInput, SqlDataOutput]):
    """
    SQL / Data specialized agent.
    Enforces read-only structured data querying, strict query validation,
    parameter binding, row limiting, and schema allowlisting.
    """

    agent_id: AgentID = AgentID.SQL_DATA
    input_schema = SqlDataInput
    output_schema = SqlDataOutput

    def __init__(
        self,
        executor: Optional[BaseSqlExecutor] = None,
        model_provider: Optional[ModelProviderWrapper] = None,
        default_timeout_seconds: Optional[float] = 10.0,
        **kwargs,
    ):
        super().__init__(
            agent_id=AgentID.SQL_DATA,
            input_schema=SqlDataInput,
            output_schema=SqlDataOutput,
            default_timeout_seconds=default_timeout_seconds,
            **kwargs,
        )
        self.executor = executor
        self.model_provider = model_provider

    async def _execute(
        self, validated_input: SqlDataInput, context: AgentExecutionContext
    ) -> SqlDataOutput:
        """
        Validates the SQL query against read-only security policies, schema boundaries,
        and executes through the injected BaseSqlExecutor.
        """
        if self.executor is None:
            raise RuntimeError(
                f"SQL Executor is not configured for agent '{self.agent_id.value}'."
            )

        # 1. Determine SQL to execute (supplied or model-generated)
        if validated_input.sql_query and validated_input.sql_query.strip():
            raw_sql = validated_input.sql_query.strip()
        elif self.model_provider is not None and self.model_provider.chat_model is not None:
            raw_sql = await self._generate_sql_from_goal(
                query_goal=validated_input.query_goal,
                permitted_schemas=validated_input.permitted_schemas,
            )
        else:
            raise ValueError(
                "No SQL query supplied and no model provider configured to formulate SQL from query_goal."
            )

        # 2. Strict SQL Validation & Security Authorization
        schemas_accessed = self._validate_sql_security(
            sql=raw_sql,
            permitted_schemas=validated_input.permitted_schemas,
        )

        logger.info(
            f"Executing read-only SQL query for goal: '{validated_input.query_goal[:50]}...'",
            extra={
                "event_name": "sql_execution_started",
                "execution_id": context.execution_id,
                "row_limit": validated_input.row_limit,
                "schemas_accessed": schemas_accessed,
            },
        )

        # 3. Execute query with parameter binding and row limit
        query_result = await self.executor.execute_query(
            sql=raw_sql,
            parameters=validated_input.parameters,
            row_limit=validated_input.row_limit,
        )

        status = "completed" if query_result.row_count > 0 else "empty_result"

        logger.info(
            f"SQL query execution finished with status '{status}' ({query_result.row_count} rows).",
            extra={
                "event_name": "sql_execution_completed",
                "execution_id": context.execution_id,
                "row_count": query_result.row_count,
                "truncated": query_result.truncated,
                "duration_ms": query_result.execution_time_ms,
            },
        )

        return SqlDataOutput(
            status=status,
            query_goal=validated_input.query_goal,
            sql_executed=raw_sql,
            columns=query_result.columns,
            rows=query_result.rows,
            row_count=query_result.row_count,
            truncated=query_result.truncated,
            execution_time_ms=query_result.execution_time_ms,
            schemas_accessed=schemas_accessed,
            provenance_ref=f"prov-{context.execution_id}",
        )

    def _validate_sql_security(
        self, sql: str, permitted_schemas: List[str]
    ) -> List[str]:
        """
        Validates SQL against read-only constraints, multi-statement injection,
        forbidden write/DDL keywords, and schema allowlists.
        Returns list of schemas identified and verified.
        """
        clean_sql = sql.strip()
        if not clean_sql:
            raise ValueError("SQL query cannot be empty.")

        # Strip comments (-- and /* */) for structural analysis
        uncommented = re.sub(r"--[^\n]*", " ", clean_sql)
        uncommented = re.sub(r"/\*.*?\*/", " ", uncommented, flags=re.DOTALL)
        uncommented = uncommented.strip()

        # Check multi-statement injection (semicolons outside string literals)
        self._check_no_multiple_statements(uncommented)

        # Check read-only initial statement keyword (SELECT or WITH)
        first_token = re.split(r"\s+", uncommented)[0].upper()
        if first_token not in ("SELECT", "WITH"):
            raise ValueError(
                f"Query must begin with SELECT or WITH. Forbidden statement type: '{first_token}'."
            )

        # Extract words outside string literals and check forbidden keywords
        code_without_literals = re.sub(r"'[^']*'", "''", uncommented)
        code_without_literals = re.sub(r'"[^"]*"', '""', code_without_literals)

        tokens = re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", code_without_literals)
        upper_tokens = {t.upper() for t in tokens}

        forbidden_found = upper_tokens.intersection(FORBIDDEN_KEYWORDS)
        if forbidden_found:
            raise ValueError(
                f"Forbidden SQL keyword(s) detected: {sorted(list(forbidden_found))}. "
                "SQL/Data Agent strictly permits read-only SELECT operations."
            )

        # Verify schema allowlist
        schemas_accessed = self._verify_permitted_schemas(
            code_without_literals=code_without_literals,
            permitted_schemas=permitted_schemas,
        )

        return schemas_accessed

    @staticmethod
    def _check_no_multiple_statements(sql: str) -> None:
        """
        Rejects multiple SQL statements separated by semicolons.
        Allows a single optional trailing semicolon.
        """
        in_quote = False
        quote_char = ""
        clean_chars = []

        for char in sql:
            if char in ("'", '"') and not in_quote:
                in_quote = True
                quote_char = char
            elif char == quote_char and in_quote:
                in_quote = False
            elif not in_quote:
                clean_chars.append(char)

        clean_str = "".join(clean_chars).strip()
        if clean_str.endswith(";"):
            clean_str = clean_str[:-1].strip()

        if ";" in clean_str:
            raise ValueError("Multiple SQL statements are strictly prohibited.")

    @staticmethod
    def _verify_permitted_schemas(
        code_without_literals: str, permitted_schemas: List[str]
    ) -> List[str]:
        """
        Checks dot-notation references (schema.table) to ensure only permitted schemas are accessed.
        """
        permitted_set = {s.lower() for s in permitted_schemas}
        # Find identifiers of the pattern <identifier>.<identifier>
        schema_matches = re.findall(
            r"\b([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\b",
            code_without_literals,
        )

        accessed_schemas: Set[str] = set()
        for schema_prefix, _ in schema_matches:
            schema_lower = schema_prefix.lower()
            if schema_lower not in permitted_set:
                raise PermissionError(
                    f"Access to schema '{schema_prefix}' is not permitted. "
                    f"Permitted schemas: {sorted(list(permitted_set))}."
                )
            accessed_schemas.add(schema_lower)

        return sorted(list(accessed_schemas)) if accessed_schemas else ["public"]

    async def _generate_sql_from_goal(
        self, query_goal: str, permitted_schemas: List[str]
    ) -> str:
        """Invokes chat model to formulate SQL query from goal when direct SQL is omitted."""
        prompt = SQL_GENERATION_PROMPT.format(
            permitted_schemas=", ".join(permitted_schemas)
        )
        user_message = f"Query Goal: {query_goal}"
        full_prompt = f"{prompt}\n\n{user_message}\n\nSQL:"

        chat_model = self.model_provider.chat_model
        if hasattr(chat_model, "ainvoke"):
            resp = await chat_model.ainvoke(full_prompt)
        elif hasattr(chat_model, "invoke"):
            resp = chat_model.invoke(full_prompt)
        else:
            resp = str(chat_model)

        raw_sql = resp.content if hasattr(resp, "content") else str(resp)
        # Strip potential markdown formatting if returned by model
        raw_sql = re.sub(r"^```(?:sql)?\s*", "", raw_sql.strip(), flags=re.IGNORECASE)
        raw_sql = re.sub(r"\s*```$", "", raw_sql.strip())
        return raw_sql.strip()
