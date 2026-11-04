"""Checks the harness runs itself, so a result doesn't rest on the model's word."""

from __future__ import annotations

import json
import logging
import re
import statistics
from datetime import datetime
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
HARD_CODED_DATE = re.compile(r"'(19|20)\d{2}-\d{2}-\d{2}")


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
    if HARD_CODED_DATE.search(guarded.sql):
        raise QueryFailure("this SQL contains a fixed date, which will go stale. Keep all history in the dataset: "
                           "charts apply the time window with `periods`.")
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


def chart_dataset_id(detail: dict[str, Any]) -> int | None:
    datasource = json.loads(detail.get("params") or "{}").get("datasource") or ""
    return int(datasource.split("__")[0]) if datasource.split("__")[0].isdigit() else None


def filters_missing_from(superset: SupersetClient, dashboard_id: int, details: dict[int, dict[str, Any]]) -> dict[int, list[str]]:
    """Which select filters can't reach which charts. Superset applies a dashboard filter by
    column name, and quietly drops it for a chart whose dataset lacks that column. The chart
    then keeps showing every tenant's data under a filter that says otherwise."""
    metadata = json.loads(superset.dashboard_detail(dashboard_id).get("json_metadata") or "{}")
    wanted = [(native["name"], native["targets"][0]["column"]["name"])
              for native in metadata.get("native_filter_configuration") or []
              if native.get("filterType") == "filter_select" and native.get("targets")]
    if not wanted:
        return {}
    columns: dict[int, set[str]] = {}
    missing: dict[int, list[str]] = {}
    for chart_id, detail in details.items():
        dataset_id = chart_dataset_id(detail)
        if dataset_id is None:
            continue
        if dataset_id not in columns:
            columns[dataset_id] = {c["column_name"] for c in superset.dataset_columns(dataset_id)}
        absent = [f"{name} ({column})" for name, column in wanted if column not in columns[dataset_id]]
        if absent:
            missing[chart_id] = absent
    return missing


def window_end_problem(window_end: str | None, grain: str | None) -> str | None:
    """A window has to end on a boundary of its time grain. Superset excludes the end, so a
    monthly chart ending on the 31st silently loses that day: the months look complete, and
    a verifier told the chart's own window will agree with it."""
    if not window_end or not grain:
        return None
    end = datetime.strptime(window_end[:10], "%Y-%m-%d").date()
    aligned = {"P1M": end.day == 1, "P3M": end.day == 1 and end.month in (1, 4, 7, 10),
               "P1Y": (end.month, end.day) == (1, 1), "P1W": end.weekday() == 0}.get(grain, True)
    if aligned:
        return None
    return (f"its time range ends on {end}, part-way through a period, and the end is excluded, so the last "
            "period is cut short. Use `periods` for a rolling window.")


def verify_dashboard(superset: SupersetClient, dashboard_id: int,
                     specs: dict[int, ChartSpec] | None = None) -> list[ChartCheck]:
    """Run every chart on a dashboard, bypassing Superset's cache, and check the charts
    agree. A chart with no time window beside charts with one shows a different period
    under the same title, and one a filter can't reach ignores it. Either way its numbers
    disagree with the others, and nothing on the page says why."""
    charts = superset.dashboard_charts(dashboard_id)
    details = {chart.id: superset.chart_detail(chart.id) for chart in charts}
    windows = {chart_id: chart_time_window(detail) for chart_id, detail in details.items()}
    # Compared within a dataset only. A count of today's fleet rightly has no window, even
    # beside revenue trends, while two charts on one dataset should cover one period.
    windowed_datasets = {chart_dataset_id(details[chart_id]) for chart_id, window in windows.items() if window}
    unreachable = filters_missing_from(superset, dashboard_id, details)

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
            grain = json.loads(details[chart.id].get("params") or "{}").get("time_grain_sqla")
            if data.row_count == 0:
                check.problem = "the chart returned no rows"
            elif window_end_problem(data.window_end, grain):
                check.problem = window_end_problem(data.window_end, grain)
            elif chart.id in unreachable:
                check.problem = (f"the filter {', '.join(unreachable[chart.id])} can't reach this chart, because its "
                                 "dataset has no such column. Add that column to the dataset, with the same name.")
            elif not windows[chart.id] and chart_dataset_id(details[chart.id]) in windowed_datasets:
                check.problem = ("this chart has no time range but others on its dataset do, so it covers a "
                                 "different period. Give it the same time_column, time_grain and periods.")
            elif specs and chart.id in specs:
                check.warning = last_point_warning(data, specs[chart.id])
        checks.append(check)
    return checks
