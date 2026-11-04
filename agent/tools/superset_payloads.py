"""Pure builders for Superset's chart and dashboard payloads.

Superset stores these as JSON strings it barely validates, so a bad payload is
saved happily and only fails when rendered. The shapes here follow the 6.1.0
frontend (the plugins' buildQuery.ts and control panels), and everything is built
from the typed specs below, so the agent never writes this JSON by hand.
"""

from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, Field, model_validator

UUID_NAMESPACE = uuid5(NAMESPACE_URL, "https://nova.local/superset")

GRID_COLUMNS = 12
DEFAULT_ROW_HEIGHT = 50
MAX_KPIS_PER_ROW = 4
MAX_CHARTS_PER_ROW = 2
KPI_TYPES = {"big_number_total", "big_number"}


def stable_uuid(kind: str, key: str) -> UUID:
    """The identity of a Superset object. The same key always gives the same UUID,
    which is what makes writes idempotent: a retry finds what the last attempt made.
    """
    return uuid5(UUID_NAMESPACE, f"{kind}:{key}")


class Viz(StrEnum):
    BIG_NUMBER = "big_number_total"
    BIG_NUMBER_TREND = "big_number"
    LINE = "echarts_timeseries_line"
    BAR = "echarts_timeseries_bar"
    AREA = "echarts_area"
    PIE = "pie"
    TABLE = "table"


class TimeGrain(StrEnum):
    DAY = "P1D"
    WEEK = "P1W"
    MONTH = "P1M"
    QUARTER = "P3M"
    YEAR = "P1Y"


TIME_SERIES = {Viz.LINE, Viz.AREA, Viz.BIG_NUMBER_TREND}
SINGLE_METRIC = {Viz.BIG_NUMBER, Viz.BIG_NUMBER_TREND, Viz.PIE}
GRAIN_UNITS = {TimeGrain.DAY: "day", TimeGrain.WEEK: "week", TimeGrain.MONTH: "month", TimeGrain.QUARTER: "quarter",
               TimeGrain.YEAR: "year"}
# Superset's smart format labels January as "2026" between December and February.
GRAIN_FORMATS = {TimeGrain.DAY: "%b %d", TimeGrain.WEEK: "%b %d", TimeGrain.MONTH: "%b %Y",
                 TimeGrain.QUARTER: "%Y Q%q", TimeGrain.YEAR: "%Y"}
NO_FILTER = "No filter"
FIXED_DATE = re.compile(r"\b(19|20)\d{2}-\d{2}")
# Superset has nowhere else to keep a chart's stable key, and a follow-up request needs it
# to update the chart rather than make another one.
CHART_KEY_PREFIX = "Nova chart key: "


def complete_period_range(grain: TimeGrain, periods: int | None = None) -> str:
    """A Superset time range ending at the start of the current period, so the period
    still in progress is left out. A trend that includes a month three days old
    appears to collapse, and a KPI compared with it reports a huge fall that never
    happened."""
    unit = GRAIN_UNITS[grain]
    end = f"DATETRUNC(DATETIME('today'), {unit})"
    if periods is None:
        return f"DATEADD({end}, -100, year) : {end}"
    return f"DATEADD({end}, -{periods}, {unit}) : {end}"


def partial_period_range(grain: TimeGrain, periods: int) -> str:
    """The last `periods` periods, counting the one still in progress."""
    unit = GRAIN_UNITS[grain]
    current = f"DATETRUNC(DATETIME('today'), {unit})"
    return f"DATEADD({current}, -{periods - 1}, {unit}) : DATEADD({current}, 1, {unit})"


class Metric(BaseModel):
    """An aggregate over the dataset's columns, e.g. SUM(revenue)."""

    label: str
    sql: str


class ChartSpec(BaseModel):
    key: str = Field(description="Stable id within the dashboard")
    title: str
    viz: Viz
    metrics: list[Metric] = Field(min_length=1)
    dimensions: list[str] = Field(default_factory=list)
    time_column: str | None = None
    time_grain: TimeGrain | None = None
    periods: int | None = Field(default=None, ge=1, le=240,
                                description="How many complete periods of time_grain to show, such as 12 months")
    time_range: str = Field(default=NO_FILTER, description="An explicit Superset range. Prefer `periods`.")
    fixed_dates: bool = Field(default=False, description="Set only if the user asked for specific dates")
    include_partial_period: bool = Field(default=False, description="Keep the period still in progress")
    row_limit: int = Field(default=1000, ge=1, le=50000)
    descending: bool = True
    y_axis_format: str = "SMART_NUMBER"

    @model_validator(mode="after")
    def _matches_viz(self) -> "ChartSpec":
        if self.viz in SINGLE_METRIC and len(self.metrics) != 1:
            raise ValueError(f"{self.viz.value} takes exactly one metric")
        if self.viz in TIME_SERIES and not (self.time_column and self.time_grain):
            raise ValueError(f"{self.viz.value} needs time_column and time_grain")
        if self.viz is Viz.PIE and len(self.dimensions) != 1:
            raise ValueError("pie takes exactly one dimension")
        if self.viz is Viz.BAR and not (self.dimensions or self.time_column):
            raise ValueError("bar needs a dimension or a time column for its x axis")
        if self.periods and not (self.time_column and self.time_grain):
            raise ValueError("periods needs time_column and time_grain")
        if FIXED_DATE.search(self.time_range) and not self.fixed_dates:
            raise ValueError("time_range holds fixed dates, which go stale, and whose end is excluded, so an "
                             "inclusive end date loses its last day. Use `periods` for a rolling window. Set "
                             "`fixed_dates` only if the user asked for specific dates.")
        return self

    @property
    def is_trend(self) -> bool:
        return self.viz in TIME_SERIES or (self.viz is Viz.BAR and self.time_column is not None)

    @property
    def effective_time_range(self) -> str:
        """Trends end at the last complete period unless told otherwise. This is applied
        here rather than left to the prompt, so that no chart can get it wrong."""
        if self.time_range != NO_FILTER or not (self.time_grain and self.time_column):
            return self.time_range
        if self.include_partial_period:
            return partial_period_range(self.time_grain, self.periods) if self.periods else NO_FILTER
        if self.periods or self.is_trend:
            return complete_period_range(self.time_grain, self.periods)
        return self.time_range


def _adhoc_metric(metric: Metric) -> dict[str, Any]:
    option = hashlib.sha1(f"{metric.label}:{metric.sql}".encode()).hexdigest()[:12]
    return {
        "expressionType": "SQL",
        "sqlExpression": metric.sql,
        "label": metric.label,
        "hasCustomLabel": True,
        "optionName": f"metric_{option}",
    }


def _adhoc_filters(spec: ChartSpec) -> list[dict[str, Any]]:
    # Charts always carry a temporal filter on their time column, even when it is
    # "No filter". That's what a dashboard's time-range filter attaches to.
    if not spec.time_column:
        return []
    return [{
        "clause": "WHERE",
        "subject": spec.time_column,
        "operator": "TEMPORAL_RANGE",
        "comparator": spec.effective_time_range,
        "expressionType": "SIMPLE",
    }]


def _x_axis(spec: ChartSpec) -> tuple[str | None, list[str]]:
    """Return the x axis column and the remaining group-by columns."""
    if spec.time_column and spec.viz in {*TIME_SERIES, Viz.BAR}:
        return spec.time_column, spec.dimensions
    if spec.viz is Viz.BAR:
        return spec.dimensions[0], spec.dimensions[1:]
    return None, spec.dimensions


def build_form_data(spec: ChartSpec, dataset_id: int) -> dict[str, Any]:
    """The chart's `params`, read by the frontend to render it and build its query."""
    metrics = [_adhoc_metric(m) for m in spec.metrics]
    x_axis, groupby = _x_axis(spec)
    form: dict[str, Any] = {
        "datasource": f"{dataset_id}__table",
        "viz_type": spec.viz.value,
        "adhoc_filters": _adhoc_filters(spec),
        "row_limit": spec.row_limit,
    }
    if spec.time_grain:
        form["time_grain_sqla"] = spec.time_grain.value

    match spec.viz:
        case Viz.BIG_NUMBER:
            form |= {"metric": metrics[0], "y_axis_format": spec.y_axis_format, "header_font_size": 0.4,
                     "subheader_font_size": 0.15}
        case Viz.BIG_NUMBER_TREND:
            form |= {"metric": metrics[0], "x_axis": x_axis, "y_axis_format": spec.y_axis_format,
                     "show_trend_line": True, "start_y_axis_at_zero": True, "compare_lag": 1,
                     "compare_suffix": "vs previous"}
        case Viz.LINE | Viz.AREA | Viz.BAR:
            form |= {"metrics": metrics, "x_axis": x_axis, "groupby": groupby, "y_axis_format": spec.y_axis_format,
                     "x_axis_sort_asc": True, "show_legend": len(metrics) > 1 or bool(groupby), "rich_tooltip": True,
                     "tooltipTimeFormat": GRAIN_FORMATS.get(spec.time_grain, "smart_date"),
                     "x_axis_time_format": GRAIN_FORMATS.get(spec.time_grain, "smart_date")}
            if spec.viz is Viz.BAR:
                form |= {"orientation": "vertical"}
                if not spec.time_column:
                    # Rank bars by their value, not alphabetically by label.
                    form |= {"x_axis_sort": spec.metrics[0].label, "x_axis_sort_asc": not spec.descending}
        case Viz.PIE:
            form |= {"metric": metrics[0], "groupby": groupby, "sort_by_metric": True, "show_legend": True,
                     "show_labels": True, "label_type": "key_percent", "number_format": spec.y_axis_format,
                     "donut": True, "innerRadius": 45, "outerRadius": 70}
        case Viz.TABLE:
            form |= {"query_mode": "aggregate", "metrics": metrics, "groupby": groupby, "all_columns": [],
                     "percent_metrics": [], "order_desc": spec.descending, "server_pagination": False,
                     "show_cell_bars": True, "table_timestamp_format": "smart_date"}
    return form


def build_query_context(spec: ChartSpec, dataset_id: int) -> dict[str, Any]:
    """The saved query behind `GET /api/v1/chart/{id}/data/`, used to verify charts.

    Mirrors the plugins' buildQuery so that verifying a chart runs the same SQL it
    renders with. Post-processing such as pivoting is left out: it changes the
    shape of the result, not whether the query works.
    """
    x_axis, groupby = _x_axis(spec)
    columns: list[Any] = []
    if x_axis == spec.time_column and x_axis is not None:
        columns.append({
            "timeGrain": spec.time_grain.value if spec.time_grain else None,
            "columnType": "BASE_AXIS",
            "sqlExpression": x_axis,
            "label": x_axis,
            "expressionType": "SQL",
        })
    elif x_axis is not None:
        columns.append(x_axis)
    columns.extend(groupby)

    metrics = [_adhoc_metric(m) for m in spec.metrics]
    query: dict[str, Any] = {
        "filters": [],
        "extras": {"having": "", "where": ""},
        "applied_time_extras": {},
        "columns": columns,
        "metrics": metrics,
        "annotation_layers": [],
        "row_limit": spec.row_limit,
        "series_limit": 0,
        "order_desc": spec.descending,
        "url_params": {},
        "custom_params": {},
        "custom_form_data": {},
    }
    if spec.time_column:
        query["filters"] = [{"col": spec.time_column, "op": "TEMPORAL_RANGE", "val": spec.effective_time_range}]
    if spec.time_grain:
        query["extras"]["time_grain_sqla"] = spec.time_grain.value
    if spec.viz in {Viz.PIE, Viz.TABLE} or (spec.viz is Viz.BAR and not spec.time_column):
        query["orderby"] = [[metrics[0], not spec.descending]]

    return {
        "datasource": {"id": dataset_id, "type": "table"},
        "force": False,
        "queries": [query],
        "form_data": build_form_data(spec, dataset_id),
        "result_format": "json",
        "result_type": "full",
    }


def build_chart_payload(spec: ChartSpec, dataset_id: int, dashboard_key: str) -> dict[str, Any]:
    return {
        "slice_name": spec.title,
        "description": f"{CHART_KEY_PREFIX}{spec.key}",
        "viz_type": spec.viz.value,
        "datasource_id": dataset_id,
        "datasource_type": "table",
        "params": json.dumps(build_form_data(spec, dataset_id)),
        "query_context": json.dumps(build_query_context(spec, dataset_id)),
        "query_context_generation": True,
        "uuid": str(stable_uuid("chart", f"{dashboard_key}/{spec.key}")),
    }


# --- Dashboards --------------------------------------------------------------

class ChartSlot(BaseModel):
    chart_id: int
    title: str
    width: int = Field(ge=1, le=GRID_COLUMNS)
    height: int = Field(default=DEFAULT_ROW_HEIGHT, ge=8)


class SelectFilter(BaseModel):
    name: str
    dataset_id: int
    column: str


class TimeFilter(BaseModel):
    name: str = "Time range"
    default: str | None = Field(default=None, description="None leaves each chart's own range in place")


def build_positions(title: str, rows: list[list[ChartSlot]]) -> dict[str, Any]:
    """The dashboard layout tree. Ids are derived from chart ids so that rebuilding
    an unchanged dashboard produces an identical layout.
    """
    positions: dict[str, Any] = {
        "DASHBOARD_VERSION_KEY": "v2",
        "ROOT_ID": {"id": "ROOT_ID", "type": "ROOT", "children": ["GRID_ID"]},
        "HEADER_ID": {"id": "HEADER_ID", "type": "HEADER", "meta": {"text": title}},
        "GRID_ID": {"id": "GRID_ID", "type": "GRID", "children": [], "parents": ["ROOT_ID"]},
    }
    for index, row in enumerate(rows):
        width = sum(slot.width for slot in row)
        if width > GRID_COLUMNS:
            raise ValueError(f"row {index} is {width} columns wide; the grid has {GRID_COLUMNS}")
        row_id = f"ROW-{index}"
        positions["GRID_ID"]["children"].append(row_id)
        positions[row_id] = {
            "id": row_id,
            "type": "ROW",
            "children": [f"CHART-{slot.chart_id}" for slot in row],
            "parents": ["ROOT_ID", "GRID_ID"],
            "meta": {"background": "BACKGROUND_TRANSPARENT"},
        }
        for slot in row:
            chart_key = f"CHART-{slot.chart_id}"
            positions[chart_key] = {
                "id": chart_key,
                "type": "CHART",
                "children": [],
                "parents": ["ROOT_ID", "GRID_ID", row_id],
                "meta": {"chartId": slot.chart_id, "width": slot.width, "height": slot.height,
                         "sliceName": slot.title},
            }
    return positions


def _filter_id(name: str) -> str:
    return f"NATIVE_FILTER-{hashlib.sha1(name.encode()).hexdigest()[:10]}"


def build_native_filter(native: SelectFilter | TimeFilter) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": _filter_id(native.name),
        "name": native.name,
        "type": "NATIVE_FILTER",
        "description": "",
        "cascadeParentIds": [],
        "scope": {"rootPath": ["ROOT_ID"], "excluded": []},
    }
    if isinstance(native, TimeFilter):
        # With no default the mask stays empty. Setting "No filter" here would replace
        # every chart's range, and bring back the partial period.
        chosen = native.default
        return base | {
            "filterType": "filter_time",
            "targets": [{}],
            "controlValues": {"enableEmptyFilter": False},
            "defaultDataMask": {
                "extraFormData": {"time_range": chosen} if chosen else {},
                "filterState": {"value": chosen} if chosen else {},
                "ownState": {},
            },
        }
    return base | {
        "filterType": "filter_select",
        "targets": [{"datasetId": native.dataset_id, "column": {"name": native.column}}],
        "controlValues": {"enableEmptyFilter": False, "multiSelect": True, "searchAllOptions": False,
                          "inverseSelection": False, "defaultToFirstItem": False},
        "defaultDataMask": {"extraFormData": {}, "filterState": {}, "ownState": {}},
    }


def build_json_metadata(positions: dict[str, Any], filters: list[dict[str, Any]]) -> dict[str, Any]:
    """`filters` are built native filters. `positions` is deliberate: Superset rebuilds
    the dashboard's chart list from it on update, which is what removes a chart dropped
    from the layout."""
    return {
        "positions": positions,
        "native_filter_configuration": filters,
        "cross_filters_enabled": True,
        "color_scheme": "supersetColors",
        "filter_bar_orientation": "VERTICAL",
        "refresh_frequency": 0,
        "timed_refresh_immune_slices": [],
        "expanded_slices": {},
        "stagger_refresh": False,
    }


def fill_row(widths: list[int]) -> list[int]:
    """Stretch a row to the full grid. Charts keep their proportions, and the largest
    take any leftover columns, so a row never ends in a gap."""
    total = sum(widths)
    scaled = [max(1, width * GRID_COLUMNS // total) for width in widths]
    order = sorted(range(len(widths)), key=lambda i: widths[i], reverse=True)
    for index in order * GRID_COLUMNS:
        if sum(scaled) >= GRID_COLUMNS:
            break
        scaled[index] += 1
    return scaled


def wrap_row(viz_types: list[str | None]) -> list[list[int]]:
    """Split a row that's too crowded, returning positions in the original row. Dashboards
    are often embedded beside other things, where four trends in a row leave each too
    narrow for its title."""
    limit = MAX_KPIS_PER_ROW if all(viz in KPI_TYPES for viz in viz_types) else MAX_CHARTS_PER_ROW
    positions = list(range(len(viz_types)))
    return [positions[start:start + limit] for start in range(0, len(positions), limit)]


def rows_from_positions(positions: dict[str, Any]) -> list[list[ChartSlot]]:
    """Read a layout back, so that a follow-up request can change it rather than start
    over."""
    rows: list[list[ChartSlot]] = []
    for row_id in positions.get("GRID_ID", {}).get("children", []):
        slots = []
        for child_id in positions.get(row_id, {}).get("children", []):
            meta = positions.get(child_id, {}).get("meta", {})
            if meta.get("chartId"):
                slots.append(ChartSlot(chart_id=meta["chartId"], title=meta.get("sliceName", ""),
                                       width=meta.get("width", 4), height=meta.get("height", DEFAULT_ROW_HEIGHT)))
        if slots:
            rows.append(slots)
    return rows
