"""Checks the harness runs itself, so a result doesn't rest on the model's word."""

from __future__ import annotations

import json
import logging
import statistics
from typing import Any

from pydantic import BaseModel

from agent.harness.audit import RunContext
from agent.harness.budgets import RunBudget
from agent.harness.errors import ErrorCategory, NovaError, QueryFailure
from agent.harness.executor import SqlExecutor
from agent.harness.guardrails import GuardedQuery, SqlPolicy, guard
from agent.tools.superset_client import ChartData, SupersetClient
from agent.tools.superset_payloads import NO_FILTER, ChartSpec

logger = logging.getLogger(__name__)

SUSPICIOUS_DROP = 0.5


class DatasetCheck(BaseModel):
    sql: str
    rows: int
    columns: list[str]


class ChartCheck(BaseModel):
    chart_id: int
    title: str
    row_count: int = 0
    problem: str | None = None
    warning: str | None = None


def check_dataset(sql: str, *, grain: list[str], executor: SqlExecutor, policy: SqlPolicy, context: RunContext,
                  budget: RunBudget) -> DatasetCheck:
    """Validate SQL before it becomes a dataset, and measure what could go wrong.

    A join that multiplies rows still returns plausible-looking numbers, so `grain` must
    name the columns that identify one row, and they must really be unique. Raises
    QueryFailure with a reason the model can act on.
    """
    guarded: GuardedQuery = guard(sql, policy, add_limit=False)
    preview = executor.run(f"SELECT * FROM ({guarded.sql}) AS d LIMIT 5", policy=policy, context=context, budget=budget)
    if preview.row_count == 0:
        raise QueryFailure("this query returns no rows; check its filters")

    missing = [column for column in grain if column not in preview.columns]
    if missing:
        raise QueryFailure(f"grain columns {missing} aren't in the query's output: {preview.columns}")
    columns = ", ".join(f'"{column}"' for column in grain)
    total, unique = executor.run(
        f'SELECT COUNT(*), COUNT(DISTINCT ROW({columns})) FROM ({guarded.sql}) AS d',
        policy=policy, context=context, budget=budget).rows[0]
    if total != unique:
        raise QueryFailure(f"the join multiplies rows: {total:,} rows but only {unique:,} distinct values of {grain}. "
                           "Sums over this dataset would be too high. Fix the join or declare the real grain.")
    return DatasetCheck(sql=guarded.sql, rows=total, columns=preview.columns)


def last_point_warning(data: ChartData, spec: ChartSpec) -> str | None:
    """A sharp fall at the end of a trend is usually a partial period, or a real problem
    worth a person's attention. It's a warning, because it can be real."""
    if not spec.is_trend or data.row_count < 4 or spec.dimensions:
        return None
    label = spec.metrics[0].label
    rows = sorted(data.rows, key=lambda row: row.get(spec.time_column) or 0)  # Superset may return newest first
    values = [row.get(label) for row in rows]
    if not all(isinstance(value, (int, float)) for value in values):
        return None
    usual = statistics.median(values[-4:-1])
    if usual > 0 and values[-1] < usual * SUSPICIOUS_DROP:
        return f"the last point ({values[-1]:,.0f}) is far below the ones before it ({usual:,.0f}): is it a partial period?"
    return None


def chart_time_window(detail: dict[str, Any]) -> str | None:
    """The time range a saved chart applies, or None if it has none."""
    params = json.loads(detail.get("params") or "{}")
    for adhoc in params.get("adhoc_filters") or []:
        if adhoc.get("operator") == "TEMPORAL_RANGE" and adhoc.get("comparator") not in (None, NO_FILTER):
            return adhoc["comparator"]
    return None


def verify_dashboard(superset: SupersetClient, dashboard_id: int,
                     specs: dict[int, ChartSpec] | None = None) -> list[ChartCheck]:
    """Run every chart on a dashboard, bypassing Superset's cache, and compare their time
    windows. A chart with no window beside charts with one shows a different period under
    the same title, so its numbers disagree with the others and nothing says why."""
    charts = superset.dashboard_charts(dashboard_id)
    windows = {chart.id: chart_time_window(superset.chart_detail(chart.id)) for chart in charts}
    windowed = any(windows.values())

    checks = []
    for chart in charts:
        check = ChartCheck(chart_id=chart.id, title=chart.slice_name)
        try:
            data = superset.chart_data(chart.id, force=True)
        except NovaError as error:
            if error.category is ErrorCategory.BUDGET:
                raise
            check.problem = error.message
        else:
            check.row_count = data.row_count
            if data.row_count == 0:
                check.problem = "the chart returned no rows"
            elif windowed and not windows[chart.id]:
                check.problem = ("this chart has no time range but others on the dashboard do, so it covers a "
                                 "different period. Give it the same time_column, time_grain and periods.")
            elif specs and chart.id in specs:
                check.warning = last_point_warning(data, specs[chart.id])
        checks.append(check)
    return checks
