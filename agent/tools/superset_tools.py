"""The agent's tools for building in Superset. Every write is idempotent and audited, and
SQL can't become a dataset without passing the guardrails."""

from __future__ import annotations

import json
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from agent.harness.errors import PermissionFailure
from agent.harness.untrusted import wrap_untrusted
from agent.harness.validators import (chart_dataset_id, chart_time_window, check_dataset, filters_missing_from,
                                      last_point_warning, verify_dashboard)
from agent.runtime import Runtime
from agent.tools.renderer import render_dashboard, review_screenshot
from agent.tools.sql_tools import NoInput, reporting_failures
from agent.tools.superset_payloads import (CHART_KEY_PREFIX, KPI_ROW_HEIGHT, KPI_TYPES, ChartSlot, ChartSpec, SelectFilter,
                                           TimeFilter, fill_row, rows_from_positions, wrap_row)

MAX_ROWS_SHOWN = 15
DATASET_DATABASE = "Nova Trino"
# Far more than a dashboard needs. These stop a confused repair loop from filling Superset.
MAX_DATASETS = 6
MAX_CHARTS = 24


class CreateDatasetInput(BaseModel):
    key: str = Field(description="Short stable id, such as sales-items")
    sql: str = Field(description="One Trino SELECT with fully qualified tables, returning raw rows. Don't aggregate "
                                 "or LIMIT: charts do that.")
    grain: list[str] = Field(min_length=1, description="Output columns that identify one row. They're checked.")


class CreateChartInput(BaseModel):
    dataset_id: int
    spec: ChartSpec


class Slot(BaseModel):
    chart_id: int
    width: int = Field(ge=1, le=12, description="Columns out of 12; each row adds up to at most 12")
    height: int = Field(default=50, ge=8)


class SelectFilterInput(BaseModel):
    name: str
    dataset_id: int
    column: str


class CreateDashboardInput(BaseModel):
    title: str
    rows: list[list[Slot]] = Field(min_length=1)
    filters: list[SelectFilterInput] = Field(default_factory=list)
    time_filter: bool = Field(default=True, description="Offer a time range filter")


class UpdateDashboardInput(BaseModel):
    title: str | None = None
    rows: list[list[Slot]] = Field(min_length=1)


class ChartIdInput(BaseModel):
    chart_id: int


class FinishInput(BaseModel):
    summary: str = Field(description="What was built, for the user. A few sentences.")
    definitions: list[str] = Field(description="The definitions used, such as 'Revenue excludes cancelled orders'")


def make_builder_tools(runtime: Runtime) -> dict[str, BaseTool]:
    superset, state = runtime.superset, runtime.state

    def database_id() -> int:
        return superset.database_id(DATASET_DATABASE)

    def dataset_prefix() -> str:
        return f"{state.dashboard_key}__"

    def require_own_dataset(dataset_id: int) -> None:
        """The agent reaches Superset as an admin, so ids from the model are checked. Without
        this a user could point a chart at someone else's dataset and read it."""
        if not superset.get_dataset(dataset_id).table_name.startswith(dataset_prefix()):
            raise PermissionFailure(f"dataset {dataset_id} doesn't belong to this dashboard")

    def require_own_chart(chart_id: int) -> None:
        dataset_id = chart_dataset_id(superset.chart_detail(chart_id))
        if dataset_id is None or not superset.get_dataset(dataset_id).table_name.startswith(dataset_prefix()):
            raise PermissionFailure(f"chart {chart_id} doesn't belong to this dashboard")

    def existing_datasets() -> dict[str, int]:
        return {d.table_name.removeprefix(dataset_prefix()): d.id for d in superset.datasets_named(dataset_prefix())}

    def existing_charts() -> dict[int, tuple[str, str, int]]:
        """Every chart built for this dashboard, as {id: (key, title, dataset_id)}."""
        found = {}
        for dataset_id in existing_datasets().values():
            for chart in superset.charts_on_dataset(dataset_id):
                description = superset.chart_detail(chart.id).get("description") or ""
                found[chart.id] = (description.removeprefix(CHART_KEY_PREFIX), chart.slice_name, dataset_id)
        return found

    def remove_orphans() -> list[str]:
        """Delete what this conversation built that the dashboard doesn't use. Only
        objects carrying this dashboard's prefix are ever touched."""
        on_dashboard = {chart.id for chart in superset.dashboard_charts(state.dashboard.id)}
        removed = []
        for dataset_key, dataset_id in existing_datasets().items():
            charts = superset.charts_on_dataset(dataset_id)
            for chart in charts:
                if chart.id not in on_dashboard:
                    superset.delete_chart(chart.id)
                    removed.append(f"chart {chart.slice_name}")
            if not any(chart.id in on_dashboard for chart in charts):
                superset.delete_dataset(dataset_id)
                removed.append(f"dataset {dataset_key}")
        return removed

    def to_slots(rows: list[list[Slot]]) -> list[list[ChartSlot]]:
        titles = {c.id: c.slice_name for c in superset.dashboard_charts(state.dashboard.id)} if state.dashboard else {}
        slots = []
        for row in rows:
            charts = [superset.get_chart(slot.chart_id) for slot in row]
            for indexes in wrap_row([chart.viz_type for chart in charts]):
                chosen = [row[index] for index in indexes]
                widths = fill_row([slot.width for slot in chosen])
                # A row of headline numbers stays compact, whatever height was asked for.
                only_kpis = all(charts[index].viz_type in KPI_TYPES for index in indexes)
                slots.append([ChartSlot(chart_id=slot.chart_id, width=width,
                                        height=KPI_ROW_HEIGHT if only_kpis else slot.height,
                                        title=titles.get(slot.chart_id) or charts[index].slice_name)
                              for index, slot, width in zip(indexes, chosen, widths)])
        return slots

    @reporting_failures
    def superset_create_dataset(key: str, sql: str, grain: list[str]) -> str:
        """Create or update a virtual dataset. The same key updates the same dataset. Its
        SQL is validated, run, and checked for joins that multiply rows, so a failure here
        is a real problem to fix."""
        existing = existing_datasets()
        if key not in existing and len(existing) >= MAX_DATASETS:
            return f"FAILED: this dashboard already has {len(existing)} datasets. Update one of: {sorted(existing)}"
        checked = check_dataset(sql, grain=grain, executor=runtime.executor, policy=runtime.policy,
                                context=runtime.context, budget=runtime.budget, tenant_columns=runtime.tenant_columns)
        state.catalogs.update(table.split(".")[0] for table in checked.tables)
        name = f"{state.dashboard_key}__{key}"
        dataset = superset.ensure_dataset(key=f"{state.dashboard_key}/{key}", name=name, sql=checked.sql,
                                          database_id=database_id())
        state.datasets[key] = dataset.id
        return f"dataset_id={dataset.id}, {checked.rows:,} rows, columns: {checked.columns}"

    @reporting_failures
    def superset_create_chart(dataset_id: int, spec: ChartSpec) -> str:
        """Create or update a chart. Use a stable `key`: the same key updates the same
        chart. Trends stop at the last complete period automatically."""
        require_own_dataset(dataset_id)
        existing = existing_charts()
        if spec.key not in {key for key, _, _ in existing.values()} and len(existing) >= MAX_CHARTS:
            return f"FAILED: this dashboard already has {len(existing)} charts. Reuse their keys."
        chart = superset.ensure_chart(spec, dataset_id=dataset_id, dashboard_key=state.dashboard_key)
        state.charts[chart.id] = spec
        data = superset.chart_data(chart.id, force=True)
        if data.row_count == 0:
            return f"chart_id={chart.id} was created but returned no rows. Check its filters and time range."
        runtime.emit("chart_ready", chart_id=chart.id, key=spec.key, title=spec.title, viz=spec.viz.value)
        return f"chart_id={chart.id}, {data.row_count} rows"

    def build_filters(filters: list[SelectFilterInput], time_filter: bool) -> list[SelectFilter | TimeFilter]:
        chosen: list[SelectFilter | TimeFilter] = [TimeFilter()] if time_filter else []
        return chosen + [SelectFilter(**item.model_dump()) for item in filters]

    @reporting_failures
    def superset_create_dashboard(title: str, rows: list[list[Slot]], filters: list[SelectFilterInput] | None = None,
                                  time_filter: bool = True) -> str:
        """Create the dashboard with its layout and filters. Create the charts first."""
        for row in rows:
            for slot in row:
                require_own_chart(slot.chart_id)
        for item in filters or []:
            require_own_dataset(item.dataset_id)
        state.dashboard = superset.ensure_dashboard(slug=state.dashboard_key, title=title, rows=to_slots(rows),
                                                    filters=build_filters(filters or [], time_filter))
        # Shown straight away, marked as still being checked, rather than after verification.
        state.embed_uuid = superset.enable_embedding(state.dashboard.id, [runtime.settings.nova_ui_origin])
        runtime.save_dashboard()
        runtime.emit("dashboard_ready", slug=state.dashboard_key, title=title)
        return f"dashboard_id={state.dashboard.id}, {sum(len(row) for row in rows)} charts"

    @reporting_failures
    def superset_build_status() -> str:
        """Show everything built so far: datasets and charts with their keys and ids. Check
        this before creating anything, and reuse existing keys to update what's there."""
        datasets = existing_datasets()
        charts = existing_charts()
        on_dashboard = ({chart.id for chart in superset.dashboard_charts(state.dashboard.id)}
                        if state.dashboard else set())
        return json.dumps({
            "dashboard_id": state.dashboard.id if state.dashboard else None,
            "datasets": [{"key": key, "dataset_id": dataset_id,
                          "columns": [c["column_name"] for c in superset.dataset_columns(dataset_id)]}
                         for key, dataset_id in datasets.items()],
            "charts": [{"key": key, "chart_id": chart_id, "title": title, "dataset_id": dataset_id,
                        "on_dashboard": chart_id in on_dashboard}
                       for chart_id, (key, title, dataset_id) in charts.items()],
        })

    @reporting_failures
    def superset_get_dashboard() -> str:
        """Read the current dashboard: its title, layout and charts."""
        if state.dashboard is None:
            return "No dashboard has been built in this conversation yet."
        detail = superset.dashboard_detail(state.dashboard.id)
        rows = rows_from_positions(json.loads(detail.get("position_json") or "{}"))
        layout = [[slot.model_dump() for slot in row] for row in rows]
        return json.dumps({"dashboard_id": state.dashboard.id, "title": detail["dashboard_title"], "rows": layout})

    @reporting_failures
    def superset_update_dashboard(rows: list[list[Slot]], title: str | None = None) -> str:
        """Replace the layout of the existing dashboard, keeping its filters. Pass the
        complete layout: charts left out are removed from the dashboard."""
        if state.dashboard is None:
            return "FAILED: there's no dashboard yet; use superset_create_dashboard."
        for row in rows:
            for slot in row:
                require_own_chart(slot.chart_id)
        state.dashboard = superset.ensure_dashboard(slug=state.dashboard_key, rows=to_slots(rows),
                                                    title=title or state.dashboard.dashboard_title)
        runtime.emit("dashboard_updated", slug=state.dashboard_key)
        return f"dashboard_id={state.dashboard.id}, {sum(len(row) for row in rows)} charts"

    @reporting_failures
    def superset_inspect_dashboard() -> str:
        """Show what chart data can't: each dashboard filter and which charts it reaches, the
        time window each chart uses, and each dataset's SQL. Use this rather than reporting
        that something couldn't be confirmed."""
        if state.dashboard is None:
            return "No dashboard has been built in this conversation yet."
        metadata = json.loads(superset.dashboard_detail(state.dashboard.id).get("json_metadata") or "{}")
        charts = superset.dashboard_charts(state.dashboard.id)
        details = {chart.id: superset.chart_detail(chart.id) for chart in charts}
        titles = {chart.id: chart.slice_name for chart in charts}
        unreachable = filters_missing_from(superset, state.dashboard.id, details)
        filters = []
        for native in metadata.get("native_filter_configuration") or []:
            column = ((native.get("targets") or [{}])[0].get("column") or {}).get("name")
            label = f"{native['name']} ({column})"
            filters.append({"name": native["name"], "type": native.get("filterType"), "column": column,
                            "missing_from": [titles[chart_id] for chart_id, absent in unreachable.items()
                                             if label in absent]})
        return json.dumps({
            "filters": filters,
            "charts": [{"chart_id": chart_id, "title": titles[chart_id], "dataset_id": chart_dataset_id(detail),
                        "time_window": chart_time_window(detail)} for chart_id, detail in details.items()],
            "datasets": [{"key": key, "dataset_id": dataset_id, "sql": superset.get_dataset(dataset_id).sql}
                         for key, dataset_id in existing_datasets().items()],
        })

    @reporting_failures
    def superset_get_chart_data(chart_id: int) -> str:
        """Run a chart's saved query and show the result, with the exact time window it
        covers: start included, end excluded."""
        require_own_chart(chart_id)
        data = superset.chart_data(chart_id, force=True)
        spec = state.charts.get(chart_id)
        warning = last_point_warning(data, spec) if spec else None
        rows = [list(row.values()) for row in data.rows[:MAX_ROWS_SHOWN]]
        window = (f"\ntime window: {data.window_start or 'open'} to {data.window_end or 'open'}"
                  if data.window_start or data.window_end else "")
        note = f"\nwarning: {warning}" if warning else ""
        return f"{data.row_count} rows, columns: {data.columns}{window}{note}\n{wrap_untrusted(rows)}"

    @reporting_failures
    def render_dashboard_screenshot() -> str:
        """Open the dashboard in a real browser and have it reviewed. Use this once the
        charts' data looks right: it's the only check that proves they draw."""
        if state.dashboard is None:
            return "FAILED: there's no dashboard yet."
        settings = runtime.settings
        expected = len(superset.dashboard_charts(state.dashboard.id))
        rendered = render_dashboard(settings.superset_url, settings.superset_admin_user,
                                    settings.superset_admin_password, state.dashboard_key, expected_charts=expected)
        review = review_screenshot(runtime.model, rendered.screenshot)
        return f"{rendered.charts} of {expected} charts drew. Errors on the page: {rendered.errors or 'none'}.\nReview:\n{review}"

    @reporting_failures
    def superset_guest_token() -> str:
        """Prepare the dashboard for embedding. The token itself goes to the user's
        browser from the server, and never passes through you."""
        if state.dashboard is None:
            return "FAILED: there's no dashboard yet."
        state.embed_uuid = superset.enable_embedding(state.dashboard.id, [runtime.settings.nova_ui_origin])
        return "Embedding is ready."

    @reporting_failures
    def finish_dashboard(summary: str, definitions: list[str]) -> str:
        """Finish. Every chart is run when you call this, and you're only done if they all
        return data. Otherwise you get the failures back to fix."""
        if state.dashboard is None:
            return "FAILED: there's no dashboard yet."
        state.finish_attempts += 1
        checks = verify_dashboard(superset, state.dashboard.id, state.charts)
        if not checks:
            return "FAILED: the dashboard has no charts."
        problems = [check for check in checks if check.problem]
        state.warnings = [f"{check.title}: {check.warning}" for check in checks if check.warning]
        if problems and state.finish_attempts <= runtime.settings.nova_chart_repair_rounds:
            lines = "\n".join(f"- {check.title} (chart_id={check.chart_id}): {check.problem}" for check in problems)
            return f"Not finished. These charts failed:\n{lines}\n\nFix them and call finish_dashboard again."
        state.warnings += [f"{check.title} failed: {check.problem}" for check in problems]
        remove_orphans()
        state.finished, state.summary, state.definitions = True, summary, definitions
        return "Finished."

    tools = [
        StructuredTool.from_function(func=superset_create_dataset, name="superset_create_dataset",
                                     args_schema=CreateDatasetInput),
        StructuredTool.from_function(func=superset_create_chart, name="superset_create_chart",
                                     args_schema=CreateChartInput),
        StructuredTool.from_function(func=superset_create_dashboard, name="superset_create_dashboard",
                                     args_schema=CreateDashboardInput),
        StructuredTool.from_function(func=superset_build_status, name="superset_build_status", args_schema=NoInput),
        StructuredTool.from_function(func=superset_get_dashboard, name="superset_get_dashboard", args_schema=NoInput),
        StructuredTool.from_function(func=superset_update_dashboard, name="superset_update_dashboard",
                                     args_schema=UpdateDashboardInput),
        StructuredTool.from_function(func=superset_inspect_dashboard, name="superset_inspect_dashboard",
                                     args_schema=NoInput),
        StructuredTool.from_function(func=superset_get_chart_data, name="superset_get_chart_data",
                                     args_schema=ChartIdInput),
        StructuredTool.from_function(func=render_dashboard_screenshot, name="render_dashboard_screenshot", args_schema=NoInput),
        StructuredTool.from_function(func=superset_guest_token, name="superset_guest_token", args_schema=NoInput),
        StructuredTool.from_function(func=finish_dashboard, name="finish_dashboard", args_schema=FinishInput),
    ]
    return {tool.name: tool for tool in tools}
