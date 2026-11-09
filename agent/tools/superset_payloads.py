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
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, Field, model_validator

from agent.tools.superset_countries import COUNTRY_MAPS

UUID_NAMESPACE = uuid5(NAMESPACE_URL, "https://nova.local/superset")

GRID_COLUMNS = 12
DEFAULT_ROW_HEIGHT = 50
MAX_KPIS_PER_ROW = 4
MAX_CHARTS_PER_ROW = 2
KPI_TYPES = {"big_number_total", "big_number"}
KPI_ROW_HEIGHT = 20


def stable_uuid(kind: str, key: str) -> UUID:
    """The identity of a Superset object. The same key always gives the same UUID,
    which is what makes writes idempotent: a retry finds what the last attempt made.
    """
    return uuid5(UUID_NAMESPACE, f"{kind}:{key}")


class Viz(StrEnum):
    # Single numbers
    BIG_NUMBER = "big_number_total"
    BIG_NUMBER_TREND = "big_number"
    GAUGE = "gauge_chart"
    BULLET = "bullet"
    # Over time, or across categories
    LINE = "echarts_timeseries_line"
    SMOOTH_LINE = "echarts_timeseries_smooth"
    STEP_LINE = "echarts_timeseries_step"
    AREA = "echarts_area"
    BAR = "echarts_timeseries_bar"
    DOTS = "echarts_timeseries_scatter"
    COMBO = "mixed_timeseries"
    WATERFALL = "waterfall"
    # Parts of a whole
    PIE = "pie"
    TREEMAP = "treemap_v2"
    SUNBURST = "sunburst_v2"
    FUNNEL = "funnel"
    # Distributions and relationships
    HISTOGRAM = "histogram_v2"
    BOX_PLOT = "box_plot"
    SCATTER = "bubble_v2"
    HEATMAP = "heatmap_v2"
    CALENDAR = "cal_heatmap"
    RADAR = "radar"
    # Flows and structures
    SANKEY = "sankey_v2"
    NETWORK = "graph_chart"
    TREE = "tree_chart"
    # Geography
    WORLD_MAP = "world_map"
    COUNTRY_MAP = "country_map"
    # Tables and text
    TABLE = "table"
    PIVOT = "pivot_table_v2"
    WORD_CLOUD = "word_cloud"


class Stacking(StrEnum):
    NONE = "none"
    STACKED = "stacked"
    PERCENT = "percent"  # each bar or point fills the height, showing shares


class TimeGrain(StrEnum):
    DAY = "P1D"
    WEEK = "P1W"
    MONTH = "P1M"
    QUARTER = "P3M"
    YEAR = "P1Y"


TIME_SERIES = {Viz.LINE, Viz.SMOOTH_LINE, Viz.STEP_LINE, Viz.AREA, Viz.BIG_NUMBER_TREND}
SERIES_CHARTS = {Viz.LINE, Viz.SMOOTH_LINE, Viz.STEP_LINE, Viz.AREA, Viz.BAR, Viz.DOTS}
# What can have either time or a category along the bottom.
X_AXIS_CHARTS = {*TIME_SERIES, *SERIES_CHARTS, Viz.COMBO, Viz.WATERFALL, Viz.HEATMAP}
# These read rows, not a few groups, so a low limit would quietly draw the wrong shape.
RAW_ROW_CHARTS = {Viz.HISTOGRAM, Viz.BOX_PLOT}
RAW_ROW_LIMIT = 50000
DEFAULT_ROW_LIMIT = 1000


@dataclass(frozen=True)
class Shape:
    """How many metrics and dimensions a chart takes, and what they mean."""

    metrics: tuple[int, int] = (1, 99)
    dimensions: tuple[int, int] = (0, 99)
    note: str = ""


SHAPES: dict[Viz, Shape] = {
    Viz.BIG_NUMBER: Shape((1, 1)),
    Viz.BIG_NUMBER_TREND: Shape((1, 1)),
    Viz.GAUGE: Shape((1, 1), (0, 1), "Set gauge_max, or the dial has no meaningful end."),
    Viz.BULLET: Shape((1, 1), (0, 0), "Set bullet_ranges, and bullet_target for the target line."),
    Viz.PIE: Shape((1, 1), (1, 1)),
    Viz.TREEMAP: Shape((1, 1), (1, 9), "Dimensions run from the largest grouping to the smallest."),
    Viz.SUNBURST: Shape((1, 1), (1, 9), "Dimensions run from the centre outwards."),
    Viz.FUNNEL: Shape((1, 1), (1, 1), "The dimension holds the stages."),
    Viz.WATERFALL: Shape((1, 1), (0, 2), "The metric is the change at each step."),
    Viz.HISTOGRAM: Shape((0, 0), (0, 1), "Set value_column. It bins rows, so it takes no metrics."),
    Viz.BOX_PLOT: Shape((1, 9), (1, 2), "Dimensions are the groups. Set distribute_across to what each dot is."),
    Viz.SCATTER: Shape((2, 3), (1, 2), "Metrics are x, y and optionally size. The first dimension is what each "
                                       "point is, and an optional second one colours them."),
    Viz.HEATMAP: Shape((1, 1), (1, 2), "It needs two axes: two dimensions, or one dimension with time on the other."),
    Viz.CALENDAR: Shape((1, 1), (0, 0), "Set time_column."),
    Viz.RADAR: Shape((3, 9), (0, 1), "Each metric is a spoke. An optional dimension draws one shape per value."),
    Viz.SANKEY: Shape((1, 1), (2, 2), "Dimensions are the source and the target."),
    Viz.NETWORK: Shape((1, 1), (2, 2), "Dimensions are the source and the target."),
    Viz.TREE: Shape((1, 1), (2, 3), "Dimensions are the id, the parent's id, and optionally a name."),
    Viz.WORD_CLOUD: Shape((1, 1), (1, 1)),
    Viz.WORLD_MAP: Shape((1, 1), (1, 1), "The dimension holds countries, in the form set by country_codes."),
    Viz.COUNTRY_MAP: Shape((1, 1), (1, 1), "The dimension holds ISO 3166-2 region codes, such as DE-BY."),
}
GRAIN_UNITS = {TimeGrain.DAY: "day", TimeGrain.WEEK: "week", TimeGrain.MONTH: "month", TimeGrain.QUARTER: "quarter",
               TimeGrain.YEAR: "year"}
# Superset's smart format labels January as "2026" between December and February.
GRAIN_FORMATS = {TimeGrain.DAY: "%b %d", TimeGrain.WEEK: "%b %d", TimeGrain.MONTH: "%b %Y",
                 TimeGrain.QUARTER: "%Y Q%q", TimeGrain.YEAR: "%Y"}
NO_FILTER = "No filter"
COLOR_SCHEME = "novaColors"  # defined in superset_config.py
SEQUENTIAL_SCHEME = "novaBlues"  # also there: one hue, for how much of something
# A change is either way, so the pair is opposite and the total is neutral.
INCREASE_COLOR = {"r": 42, "g": 120, "b": 214, "a": 1}
DECREASE_COLOR = {"r": 227, "g": 73, "b": 72, "a": 1}
TOTAL_COLOR = {"r": 110, "g": 110, "b": 105, "a": 1}
TREND_COLOR = {"r": 42, "g": 120, "b": 214, "a": 1}
FUNNEL_KEY_VALUE_PERCENT = 5  # the frontend's EchartsFunnelLabelType, which is numeric
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
    metrics: list[Metric] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    time_column: str | None = None
    time_grain: TimeGrain | None = None
    periods: int | None = Field(default=None, ge=1, le=1000,
                                description="How many complete periods of time_grain to show, such as 12 months")
    time_range: str = Field(default=NO_FILTER, description="An explicit Superset range. Prefer `periods`.")
    fixed_dates: bool = Field(default=False, description="Set only if the user asked for specific dates")
    include_partial_period: bool = Field(default=False, description="Keep the period still in progress")
    row_limit: int = Field(default=1000, ge=1, le=50000)
    descending: bool = True
    y_axis_format: str = "SMART_NUMBER"
    time_axis: bool | None = Field(default=None, description="Whether time runs along the bottom. With a dimension "
                                                             "it doesn't: the chart compares that dimension across the "
                                                             "time window. Set true to show each period split by it")
    subtitle: str | None = Field(default=None, description="KPI: a short caption under the number")
    horizontal: bool = Field(default=False, description="Bar: draw the bars sideways")
    stacking: Stacking = Field(default=Stacking.NONE, description="Bar, line and area with several series")
    donut: bool = Field(default=True, description="Pie: a ring. False draws a solid pie")
    line_metrics: list[Metric] = Field(default_factory=list,
                                       description="Combo: drawn as lines. `metrics` are drawn as bars")
    secondary_axis: bool = Field(default=False, description="Combo: give the lines their own axis. Only if the "
                                                            "user asks: two scales on a chart mislead")
    line_format: str = Field(default="SMART_NUMBER", description="Combo: the number format of the second axis")
    pivot_columns: list[str] = Field(default_factory=list, description="Pivot: the dimensions across the top. "
                                                                       "`dimensions` are the rows")
    totals: bool = Field(default=True, description="Pivot: show totals. Turn off for rates, which can't be added")
    distribute_across: list[str] = Field(default_factory=list,
                                         description="Box plot: the columns that make one data point")
    value_column: str | None = Field(default=None, description="Histogram: the numeric column to bin")
    bins: int = Field(default=20, ge=2, le=100, description="Histogram: how many bins")
    gauge_min: float = Field(default=0, description="Gauge: where the dial starts")
    gauge_max: float | None = Field(default=None, description="Gauge: where the dial ends")
    bullet_target: float | None = Field(default=None, description="Bullet: the target line")
    bullet_ranges: list[float] = Field(default_factory=list,
                                       description="Bullet: the band boundaries, such as poor, fair and good")
    country_codes: Literal["cca2", "cca3", "name"] = Field(default="cca2", description="World map: how the dimension "
                                                           "names countries: cca2 is DE, cca3 is DEU, name is Germany")
    map_country: str | None = Field(default=None, description="Country map: the country in lowercase, with "
                                                              "underscores, such as germany, usa or uk")
    tree_root: str | None = Field(default=None, description="Tree: the id of the top node")

    @model_validator(mode="after")
    def _matches_viz(self) -> "ChartSpec":
        shape = SHAPES.get(self.viz, Shape())
        for name, have, (low, high) in (("metric", len(self.metrics), shape.metrics),
                                        ("dimension", len(self.dimensions), shape.dimensions)):
            if not low <= have <= high:
                count = "one" if low == 1 else low
                wanted = f"exactly {count}" if low == high else f"{low} to {high}" if high < 99 else f"at least {count}"
                plural = "" if low == high == 1 else "s"
                raise ValueError(f"{self.viz.value} takes {wanted} {name}{plural}, not {have}. {shape.note}".strip())
        if self.viz in TIME_SERIES and not (self.time_column and self.time_grain):
            raise ValueError(f"{self.viz.value} needs time_column and time_grain")
        if self.viz in {Viz.BAR, Viz.DOTS, Viz.COMBO, Viz.WATERFALL} and not (self.dimensions or self.time_column):
            raise ValueError(f"{self.viz.value} needs a dimension or a time column for its x axis")
        if self.time_axis and not self.time_column:
            raise ValueError("time_axis needs time_column")
        if self.time_axis is False and self.viz in X_AXIS_CHARTS and not self.dimensions:
            raise ValueError("without time along the bottom, this chart needs a dimension there instead")
        if self.viz is Viz.HEATMAP and len(self.dimensions) + self.time_on_x != 2:
            raise ValueError(SHAPES[Viz.HEATMAP].note)
        if self.viz is Viz.CALENDAR and not self.time_column:
            raise ValueError(SHAPES[Viz.CALENDAR].note)
        if self.viz is Viz.COMBO and not self.line_metrics:
            raise ValueError("a combo chart needs line_metrics as well as metrics")
        if self.viz is Viz.HISTOGRAM and not self.value_column:
            raise ValueError(SHAPES[Viz.HISTOGRAM].note)
        if self.viz is Viz.BOX_PLOT and not self.distribute_across:
            raise ValueError(SHAPES[Viz.BOX_PLOT].note)
        if self.viz is Viz.COUNTRY_MAP and self.map_country not in COUNTRY_MAPS:
            raise ValueError(f"{self.map_country!r} isn't a country map Superset has. Use a lowercase name with "
                             "underscores, such as germany, usa or uk")
        if self.viz is Viz.PIVOT and not (self.dimensions or self.pivot_columns):
            raise ValueError("a pivot needs dimensions for its rows, pivot_columns for its columns, or both")
        labels = [metric.label for metric in [*self.metrics, *self.line_metrics]]
        if len(set(labels)) != len(labels):
            raise ValueError("each metric on a chart needs its own label")
        if self.viz in RAW_ROW_CHARTS and "row_limit" not in self.model_fields_set:
            self.row_limit = RAW_ROW_LIMIT
        if self.periods and not (self.time_column and self.time_grain):
            raise ValueError("periods needs time_column and time_grain")
        if FIXED_DATE.search(self.time_range) and not self.fixed_dates:
            raise ValueError("time_range holds fixed dates, which go stale, and whose end is excluded, so an "
                             "inclusive end date loses its last day. Use `periods` for a rolling window. Set "
                             "`fixed_dates` only if the user asked for specific dates.")
        return self

    @property
    def time_on_x(self) -> bool:
        """Whether time runs along the bottom, rather than only setting the window. Rentals by
        class over the last 12 months compares classes: a month per bar would be a different chart."""
        if self.viz in TIME_SERIES:
            return True
        if self.viz not in X_AXIS_CHARTS or not self.time_column:
            return False
        if self.time_axis is not None:
            return self.time_axis
        return len(self.dimensions) < (2 if self.viz is Viz.HEATMAP else 1)

    @property
    def is_trend(self) -> bool:
        return self.time_on_x

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
    if spec.viz not in X_AXIS_CHARTS:
        return None, spec.dimensions
    if spec.time_on_x:
        return spec.time_column, spec.dimensions
    return spec.dimensions[0], spec.dimensions[1:]


def _months_shown(spec: ChartSpec) -> int:
    if spec.periods is None:
        return 12
    return max(1, round(spec.periods / 30)) if spec.time_grain is TimeGrain.DAY else spec.periods


def build_form_data(spec: ChartSpec, dataset_id: int) -> dict[str, Any]:
    """The chart's `params`, read by the frontend to render it and build its query."""
    metrics = [_adhoc_metric(m) for m in spec.metrics]
    x_axis, groupby = _x_axis(spec)
    form: dict[str, Any] = {
        "datasource": f"{dataset_id}__table",
        "viz_type": spec.viz.value,
        "color_scheme": COLOR_SCHEME,
        "adhoc_filters": _adhoc_filters(spec),
        "row_limit": spec.row_limit,
    }
    if spec.time_grain:
        form["time_grain_sqla"] = spec.time_grain.value

    match spec.viz:
        case Viz.BIG_NUMBER:
            form |= {"metric": metrics[0], "y_axis_format": spec.y_axis_format, "header_font_size": 0.3,
                     "subheader_font_size": 0.15}
            if spec.subtitle:
                form |= {"subtitle": spec.subtitle, "subtitle_font_size": 0.15}
        case Viz.BIG_NUMBER_TREND:
            form |= {"metric": metrics[0], "x_axis": x_axis, "y_axis_format": spec.y_axis_format,
                     "show_trend_line": True, "start_y_axis_at_zero": True, "compare_lag": 1,
                     "compare_suffix": "vs previous", "color_picker": TREND_COLOR}
        case Viz.LINE | Viz.SMOOTH_LINE | Viz.STEP_LINE | Viz.AREA | Viz.BAR | Viz.DOTS:
            percent = spec.stacking is Stacking.PERCENT
            form |= {"metrics": metrics, "x_axis": x_axis, "groupby": groupby,
                     "y_axis_format": ".0%" if percent else spec.y_axis_format,
                     "x_axis_sort_asc": True, "show_legend": len(metrics) > 1 or bool(groupby), "rich_tooltip": True,
                     "tooltipTimeFormat": GRAIN_FORMATS.get(spec.time_grain, "smart_date"),
                     "x_axis_time_format": GRAIN_FORMATS.get(spec.time_grain, "smart_date")}
            if spec.stacking is not Stacking.NONE:
                form |= {"stack": "Stack"}
            if percent:
                form |= {"contributionMode": "row"}
            if spec.viz is Viz.BAR:
                form |= {"orientation": "horizontal" if spec.horizontal else "vertical"}
                if not spec.time_on_x:
                    # Rank bars by their value, not alphabetically by label.
                    form |= {"x_axis_sort": spec.metrics[0].label, "x_axis_sort_asc": not spec.descending}
        case Viz.COMBO:
            form |= {"x_axis": x_axis, "metrics": metrics, "groupby": groupby, "seriesType": "bar", "yAxisIndex": 0,
                     "metrics_b": [_adhoc_metric(m) for m in spec.line_metrics], "groupby_b": [], "seriesType_b": "line",
                     "yAxisIndex_b": 1 if spec.secondary_axis else 0, "adhoc_filters_b": _adhoc_filters(spec),
                     "row_limit_b": spec.row_limit, "y_axis_format": spec.y_axis_format,
                     "y_axis_format_secondary": spec.line_format, "show_legend": True, "rich_tooltip": True,
                     "tooltipTimeFormat": GRAIN_FORMATS.get(spec.time_grain, "smart_date"),
                     "x_axis_time_format": GRAIN_FORMATS.get(spec.time_grain, "smart_date")}
        case Viz.PIE:
            form |= {"metric": metrics[0], "groupby": groupby, "sort_by_metric": True, "show_legend": True,
                     "show_labels": True, "label_type": "key_percent", "number_format": spec.y_axis_format,
                     "donut": spec.donut, "innerRadius": 45, "outerRadius": 70}
        case Viz.TREEMAP:
            form |= {"metric": metrics[0], "groupby": groupby, "sort_by_metric": True, "show_labels": True,
                     "show_upper_labels": True, "label_type": "key_value", "number_format": spec.y_axis_format}
        case Viz.SUNBURST:
            form |= {"metric": metrics[0], "columns": groupby, "sort_by_metric": True, "show_labels": True,
                     "show_total": True, "label_type": "key", "number_format": spec.y_axis_format}
        case Viz.FUNNEL:
            form |= {"metric": metrics[0], "groupby": groupby, "sort_by_metric": True, "show_labels": True,
                     "show_tooltip_labels": True, "label_type": FUNNEL_KEY_VALUE_PERCENT,
                     "tooltip_label_type": FUNNEL_KEY_VALUE_PERCENT, "number_format": spec.y_axis_format,
                     "percent_calculation_type": "first_step", "show_legend": False}
        case Viz.GAUGE:
            form |= {"metric": metrics[0], "groupby": groupby, "sort_by_metric": True, "min_val": spec.gauge_min,
                     "number_format": spec.y_axis_format, "value_formatter": "{value}", "show_progress": True,
                     "show_pointer": True, "overlap": True, "start_angle": 225, "end_angle": -45, "font_size": 15,
                     "split_number": 5, "show_axis_tick": False, "show_split_line": False, "animation": False}
            if spec.gauge_max is not None:
                form |= {"max_val": spec.gauge_max}
        case Viz.BULLET:
            form |= {"metric": metrics[0], "ranges": ",".join(f"{value:g}" for value in spec.bullet_ranges),
                     "range_labels": "", "markers": "", "marker_labels": "",
                     "marker_lines": "" if spec.bullet_target is None else f"{spec.bullet_target:g}",
                     "marker_line_labels": "" if spec.bullet_target is None else "Target"}
        case Viz.WATERFALL:
            form |= {"x_axis": x_axis, "groupby": groupby, "metric": metrics[0], "show_legend": True,
                     "show_total": True, "y_axis_format": spec.y_axis_format,
                     "x_axis_time_format": GRAIN_FORMATS.get(spec.time_grain, "smart_date"),
                     "increase_color": INCREASE_COLOR, "decrease_color": DECREASE_COLOR, "total_color": TOTAL_COLOR,
                     "increase_label": "Increase", "decrease_label": "Decrease", "total_label": "Total"}
        case Viz.HISTOGRAM:
            form |= {"column": spec.value_column, "groupby": groupby, "bins": spec.bins, "normalize": False,
                     "cumulative": False, "x_axis_title": spec.value_column.replace("_", " ").capitalize(),
                     "y_axis_title": "Count",
                     "x_axis_format": spec.y_axis_format, "y_axis_format": "SMART_NUMBER",
                     "show_legend": bool(groupby)}
        case Viz.BOX_PLOT:
            form |= {"columns": spec.distribute_across, "groupby": groupby, "metrics": metrics, "whiskerOptions": "Tukey",
                     "number_format": spec.y_axis_format, "x_ticks_layout": "auto", "date_format": "smart_date"}
        case Viz.SCATTER:
            size = metrics[2] if len(metrics) > 2 else _adhoc_metric(Metric(label="Size", sql="MAX(1)"))
            form |= {"entity": groupby[0], "x": metrics[0], "y": metrics[1], "size": size, "max_bubble_size": "15",
                     "opacity": 0.6, "x_axis_label": spec.metrics[0].label, "y_axis_label": spec.metrics[1].label,
                     "x_axis_title_margin": 30, "y_axis_title_margin": 50, "xAxisFormat": "SMART_NUMBER",
                     "y_axis_format": spec.y_axis_format, "tooltipSizeFormat": "SMART_NUMBER",
                     "show_legend": len(groupby) > 1}
            if len(groupby) > 1:
                form |= {"series": groupby[1]}
        case Viz.HEATMAP:
            form |= {"x_axis": x_axis, "groupby": groupby[0], "metric": metrics[0], "normalize_across": "heatmap",
                     "linear_color_scheme": SEQUENTIAL_SCHEME, "legend_type": "continuous", "show_legend": True,
                     "show_percentage": False, "show_values": False, "border_width": 0, "sort_x_axis": "alpha_asc",
                     "sort_y_axis": "alpha_asc", "xscale_interval": -1, "yscale_interval": -1,
                     "left_margin": 90, "bottom_margin": "auto", "y_axis_format": spec.y_axis_format,
                     "x_axis_time_format": GRAIN_FORMATS.get(spec.time_grain, "smart_date")}
        case Viz.CALENDAR:
            form |= {"granularity_sqla": spec.time_column, "time_range": spec.effective_time_range, "metrics": metrics,
                     "domain_granularity": "month", "subdomain_granularity": "day", "steps": _months_shown(spec),
                     "linear_color_scheme": SEQUENTIAL_SCHEME, "cell_size": 14, "cell_padding": 2, "cell_radius": 2,
                     "y_axis_format": spec.y_axis_format, "x_axis_time_format": "smart_date", "show_legend": True,
                     "show_values": False, "show_metric_name": False}
        case Viz.RADAR:
            form |= {"groupby": groupby, "metrics": metrics, "number_format": spec.y_axis_format, "show_labels": False,
                     "label_type": "value", "is_circle": False, "show_legend": True, "column_config": {}}
        case Viz.SANKEY:
            form |= {"source": groupby[0], "target": groupby[1], "metric": metrics[0], "sort_by_metric": True}
        case Viz.NETWORK:
            # Superset's defaults spread nodes far past the edge of a dashboard tile.
            form |= {"source": groupby[0], "target": groupby[1], "metric": metrics[0], "layout": "force",
                     "selected_mode": "single", "show_legend": False, "roam": "scale", "repulsion": 300,
                     "gravity": 0.6, "edgeLength": 120}
        case Viz.TREE:
            # Sideways and closed past the first level: siblings' labels collide when spread across the top.
            form |= {"id": groupby[0], "parent": groupby[1], "metric": metrics[0], "layout": "orthogonal", "orient": "LR",
                     "symbol": "circle", "symbolSize": 7, "roam": True, "node_label_position": "left",
                     "child_label_position": "right", "emphasis": "descendant", "initialTreeDepth": 2}
            if len(groupby) > 2:
                form |= {"name": groupby[2]}
            if spec.tree_root:
                form |= {"root_node_id": spec.tree_root}
        case Viz.WORD_CLOUD:
            form |= {"series": groupby[0], "metric": metrics[0], "size_from": 12, "size_to": 60, "rotation": "flat",
                     "sort_by_metric": True}
        case Viz.WORLD_MAP:
            form |= {"entity": groupby[0], "country_fieldtype": spec.country_codes, "metric": metrics[0],
                     "sort_by_metric": True, "show_bubbles": False, "color_by": "metric",
                     "linear_color_scheme": SEQUENTIAL_SCHEME, "y_axis_format": spec.y_axis_format}
        case Viz.COUNTRY_MAP:
            form |= {"entity": groupby[0], "metric": metrics[0], "select_country": spec.map_country,
                     "number_format": spec.y_axis_format, "linear_color_scheme": SEQUENTIAL_SCHEME}
            del form["color_scheme"]  # it wins over the sequential scheme, giving each region a category colour
        case Viz.PIVOT:
            form |= {"groupbyRows": spec.dimensions, "groupbyColumns": spec.pivot_columns, "metrics": metrics,
                     "metricsLayout": "COLUMNS", "aggregateFunction": "Sum", "rowTotals": spec.totals,
                     "colTotals": spec.totals, "rowSubTotals": spec.totals and len(spec.dimensions) > 1,
                     "valueFormat": spec.y_axis_format, "date_format": "smart_date", "order_desc": spec.descending,
                     "transposePivot": False, "combineMetric": False, "rowOrder": "key_a_to_z", "colOrder": "key_a_to_z"}
            if spec.time_column in [*spec.dimensions, *spec.pivot_columns]:
                form |= {"temporal_columns_lookup": {spec.time_column: True}}
        case Viz.TABLE:
            form |= {"query_mode": "aggregate", "metrics": metrics, "groupby": groupby, "all_columns": [],
                     "percent_metrics": [], "order_desc": spec.descending, "server_pagination": False,
                     "show_cell_bars": False, "table_timestamp_format": "smart_date",
                     "column_config": {metric.label: {"d3NumberFormat": spec.y_axis_format} for metric in spec.metrics}}
    return form


def _axis_column(column: str, spec: ChartSpec) -> dict[str, Any]:
    return {
        "timeGrain": spec.time_grain.value if spec.time_grain else None,
        "columnType": "BASE_AXIS",
        "sqlExpression": column,
        "label": column,
        "expressionType": "SQL",
    }


def _query_columns(spec: ChartSpec) -> list[Any]:
    """The columns a chart's query selects, as each plugin's buildQuery works them out."""
    match spec.viz:
        case Viz.HISTOGRAM:
            return [*spec.dimensions, spec.value_column]
        case Viz.BOX_PLOT:
            return [*spec.distribute_across, *spec.dimensions]
        case Viz.CALENDAR:
            return [_axis_column(spec.time_column, spec)]
        case Viz.BULLET:
            return []
        case Viz.PIVOT:
            names = dict.fromkeys([*spec.pivot_columns, *spec.dimensions])
            return [_axis_column(name, spec) if name == spec.time_column else name for name in names]
    x_axis, groupby = _x_axis(spec)
    if x_axis is None:
        return list(groupby)
    return [_axis_column(x_axis, spec) if x_axis == spec.time_column else x_axis, *groupby]


def _query_order(spec: ChartSpec, metrics: list[dict[str, Any]]) -> list[list[Any]] | None:
    """The sort each plugin asks for. None leaves it to the database."""
    by_columns = [[column, True] for column in spec.dimensions]
    match spec.viz:
        case Viz.PIE | Viz.TABLE | Viz.PIVOT:
            return [[metrics[0], not spec.descending]]
        case Viz.BAR if not spec.time_on_x:
            return [[metrics[0], not spec.descending]]
        case Viz.FUNNEL | Viz.GAUGE | Viz.SUNBURST | Viz.RADAR:
            return [[metrics[0], False]]
        case Viz.TREEMAP | Viz.SANKEY | Viz.WORD_CLOUD:
            return [[metrics[0], False], *by_columns]
        case Viz.NETWORK:
            return by_columns
        case Viz.TREE:
            return [by_columns[1], by_columns[0], *by_columns[2:]]
        case Viz.WATERFALL | Viz.HEATMAP:
            x_axis, groupby = _x_axis(spec)
            return [[column, True] for column in (x_axis, *groupby)]
    return None


def build_query_context(spec: ChartSpec, dataset_id: int) -> dict[str, Any]:
    """The saved query behind `GET /api/v1/chart/{id}/data/`, used to verify charts.

    Mirrors the plugins' buildQuery so that verifying a chart runs the same SQL it
    renders with. Post-processing such as pivoting is left out: it changes the
    shape of the result, not whether the query works.
    """
    metrics = [_adhoc_metric(m) for m in spec.metrics]
    if spec.viz is Viz.SCATTER and len(metrics) == 2:
        metrics.append(_adhoc_metric(Metric(label="Size", sql="MAX(1)")))
    query: dict[str, Any] = {
        "filters": [],
        "extras": {"having": "", "where": ""},
        "applied_time_extras": {},
        "columns": _query_columns(spec),
        "annotation_layers": [],
        "row_limit": spec.row_limit,
        "series_limit": 0,
        "order_desc": spec.descending,
        "url_params": {},
        "custom_params": {},
        "custom_form_data": {},
    }
    if metrics:  # a histogram reads raw rows, which is what no metrics at all means
        query["metrics"] = metrics
    if spec.time_column:
        query["filters"] = [{"col": spec.time_column, "op": "TEMPORAL_RANGE", "val": spec.effective_time_range}]
    if spec.time_grain:
        query["extras"]["time_grain_sqla"] = spec.time_grain.value
    order = _query_order(spec, metrics)
    if order:
        query["orderby"] = order
    # A combo draws two queries, one for its bars and one for its lines.
    queries = [query, {**query, "metrics": [_adhoc_metric(m) for m in spec.line_metrics]}] if spec.viz is Viz.COMBO else [query]

    return {
        "datasource": {"id": dataset_id, "type": "table"},
        "force": False,
        "queries": queries,
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
    """A place on a sheet, holding a chart or a block of text."""

    chart_id: int | None = None
    text: str | None = Field(default=None, description="Markdown, shown as written on the sheet")
    title: str = ""
    width: int = Field(ge=1, le=GRID_COLUMNS)
    height: int = Field(default=DEFAULT_ROW_HEIGHT, ge=8)

    @model_validator(mode="after")
    def _holds_one_thing(self) -> "ChartSlot":
        if (self.chart_id is None) == (self.text is None):
            raise ValueError("a slot holds either a chart_id or text")
        return self

    def element_id(self, row: int, column: int) -> str:
        return f"CHART-{self.chart_id}" if self.chart_id is not None else f"MARKDOWN-{row}-{column}"


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
            "children": [slot.element_id(index, column) for column, slot in enumerate(row)],
            "parents": ["ROOT_ID", "GRID_ID"],
            "meta": {"background": "BACKGROUND_TRANSPARENT"},
        }
        for column, slot in enumerate(row):
            key = slot.element_id(index, column)
            positions[key] = {"id": key, "children": [], "parents": ["ROOT_ID", "GRID_ID", row_id]}
            if slot.text is not None:
                positions[key] |= {"type": "MARKDOWN", "meta": {"code": slot.text, "width": slot.width, "height": slot.height}}
            else:
                positions[key] |= {"type": "CHART", "meta": {"chartId": slot.chart_id, "width": slot.width,
                                                             "height": slot.height, "sliceName": slot.title}}
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
        "color_scheme": COLOR_SCHEME,
        # Filters in a row above the charts, so that they scope everything below them and
        # the charts keep the full width, which matters most inside an embed.
        "filter_bar_orientation": "HORIZONTAL",
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
            size = dict(width=meta.get("width", 4), height=meta.get("height", DEFAULT_ROW_HEIGHT))
            if meta.get("chartId"):
                slots.append(ChartSlot(chart_id=meta["chartId"], title=meta.get("sliceName", ""), **size))
            elif meta.get("code"):
                slots.append(ChartSlot(text=meta["code"], **size))
        if slots:
            rows.append(slots)
    return rows
