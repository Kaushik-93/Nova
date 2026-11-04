"""The agent's SQL tools. Results go back to the model marked as untrusted data."""

from __future__ import annotations

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from agent.harness.audit import RunContext
from agent.harness.budgets import RunBudget
from agent.harness.errors import BudgetExceeded, NovaError
from agent.harness.executor import SqlExecutor
from agent.harness.guardrails import SqlPolicy
from agent.harness.untrusted import wrap_untrusted

MAX_ROWS_SHOWN = 50


def format_failure(error: NovaError) -> str:
    """A failure the model can act on, tagged with its category."""
    return f"FAILED [{error.category.value}]: {error.message}"


class RunReadonlySqlInput(BaseModel):
    sql: str = Field(description="One Trino SELECT. Every table must be written as catalog.schema.table.")


def make_run_readonly_sql(executor: SqlExecutor, *, policy: SqlPolicy, context: RunContext,
                          budget: RunBudget) -> BaseTool:
    def run_readonly_sql(sql: str) -> str:
        """Run one read-only Trino SELECT and return its first rows.

        Use this to test a definition before relying on it. Writes, system tables and
        table functions are refused, and a LIMIT is added automatically. An expensive
        query is refused with a reason: narrow it and try again.
        """
        try:
            result = executor.run(sql, policy=policy, context=context, budget=budget)
        except BudgetExceeded:
            raise
        except NovaError as error:
            return format_failure(error)
        shown = result.rows[:MAX_ROWS_SHOWN]
        note = f"{result.row_count} rows" + (", truncated by the limit" if result.truncated else "")
        if len(shown) < result.row_count:
            note += f"; showing the first {len(shown)}"
        return f"{note}\ncolumns: {result.columns}\n{wrap_untrusted(shown)}"

    return StructuredTool.from_function(func=run_readonly_sql, name="run_readonly_sql",
                                        args_schema=RunReadonlySqlInput)
