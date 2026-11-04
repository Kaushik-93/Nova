"""Pure builders for Superset's chart and dashboard payloads.

Superset stores these as JSON strings it barely validates, so a bad payload is
saved happily and only fails when rendered. The shapes here follow the 6.1.0
frontend (the plugins' buildQuery.ts and control panels), and everything is built
from the typed specs below, so the agent never writes this JSON by hand.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, Field, model_validator

UUID_NAMESPACE = uuid5(NAMESPACE_URL, "https://nova.local/superset")

GRID_COLUMNS = 12
DEFAULT_ROW_HEIGHT = 50


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
    time_range: str = "No filter"
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
        return self


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
        "comparator": spec.time_range,
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
                     "tooltipTimeFormat": "smart_date", "x_axis_time_format": "smart_date"}
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
        query["filters"] = [{"col": spec.time_column, "op": "TEMPORAL_RANGE", "val": spec.time_range}]
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
    default: str = "No filter"


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
        return base | {
            "filterType": "filter_time",
            "targets": [{}],
            "controlValues": {"enableEmptyFilter": False},
            "defaultDataMask": {
                "extraFormData": {"time_range": native.default},
                "filterState": {"value": native.default},
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


def build_json_metadata(positions: dict[str, Any], filters: list[SelectFilter | TimeFilter]) -> dict[str, Any]:
    # `positions` is deliberate: Superset rebuilds the dashboard's chart list from
    # it on update, which is what removes a chart dropped from the layout.
    return {
        "positions": positions,
        "native_filter_configuration": [build_native_filter(f) for f in filters],
        "cross_filters_enabled": True,
        "color_scheme": "supersetColors",
        "filter_bar_orientation": "VERTICAL",
        "refresh_frequency": 0,
        "timed_refresh_immune_slices": [],
        "expanded_slices": {},
        "stagger_refresh": False,
    }
