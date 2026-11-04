"""The agent's SQL tools. Results go back to the model marked as untrusted data."""

from __future__ import annotations

import functools
import re
from typing import Callable

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from agent.harness.audit import RunContext
from agent.harness.budgets import RunBudget
from agent.harness.errors import BudgetExceeded, NovaError
from agent.harness.executor import SqlExecutor
from agent.harness.guardrails import SqlPolicy
from agent.harness.untrusted import wrap_untrusted
from agent.tools.profiler import PERSONAL, Profiler

MAX_ROWS_SHOWN = 50
TABLE_NAME = re.compile(r"^[A-Za-z0-9_$-]+\.[A-Za-z0-9_$-]+\.[A-Za-z0-9_$-]+$")


def format_failure(error: NovaError) -> str:
    """A failure the model can act on, tagged with its category."""
    return f"FAILED [{error.category.value}]: {error.message}"


def reporting_failures(function: Callable[..., str]) -> Callable[..., str]:
    """Hand tool failures back to the model as text, so it can recover. A spent budget
    is the exception: that has to stop the run."""

    @functools.wraps(function)
    def wrapped(*args, **kwargs) -> str:
        try:
            return function(*args, **kwargs)
        except BudgetExceeded:
            raise
        except NovaError as error:
            return format_failure(error)

    return wrapped


class NoInput(BaseModel):
    """For tools that take no arguments. Without an explicit schema a wrapped function's
    *args and **kwargs can be offered to the model as parameters."""


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


class IntrospectSchemaInput(BaseModel):
    catalog: str = Field(description="A connected source's Trino catalog")


class SampleTableInput(BaseModel):
    table: str = Field(description="catalog.schema.table")
    rows: int = Field(default=5, ge=1, le=20)


def make_schema_tools(executor: SqlExecutor, *, policy: SqlPolicy, context: RunContext,
                      budget: RunBudget) -> list[BaseTool]:
    profiler = Profiler(executor, context=context, budget=budget)

    @reporting_failures
    def introspect_schema(catalog: str) -> str:
        """List a source's tables, columns and types. Check the skill first: it usually
        has what you need, and explains it."""
        if catalog not in policy.allowed_catalogs:
            return f"FAILED: {catalog!r} is not a connected source; available: {sorted(policy.allowed_catalogs)}"
        return wrap_untrusted(profiler.read_shape(catalog))

    @reporting_failures
    def sample_table(table: str, rows: int = 5) -> str:
        """Show a few rows of one table, to see what its values look like. Columns that
        may hold personal data are left out."""
        if not TABLE_NAME.match(table):
            return "FAILED: name the table as catalog.schema.table"
        catalog, schema, name = table.split(".")
        if catalog not in policy.allowed_catalogs:
            return f"FAILED: {catalog!r} is not a connected source; available: {sorted(policy.allowed_catalogs)}"
        columns = profiler.read_shape(catalog).get(f"{schema}.{name}")
        if not columns:
            return f"FAILED: no table {table}"
        visible = [column for column in columns if not PERSONAL.search(column)]
        selected = ", ".join('"' + column.replace('"', '""') + '"' for column in visible)
        result = executor.run(f"SELECT {selected} FROM {table} LIMIT {rows}", policy=policy, context=context,
                              budget=budget)
        return f"columns: {result.columns}\n{wrap_untrusted(result.rows)}"

    return [
        StructuredTool.from_function(func=introspect_schema, name="introspect_schema",
                                     args_schema=IntrospectSchemaInput),
        StructuredTool.from_function(func=sample_table, name="sample_table", args_schema=SampleTableInput),
    ]
