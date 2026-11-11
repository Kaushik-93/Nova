"""Charts and dashboards, as Nova draws them: each chart is a semantic query plus how to show it.

A dashboard is stored as one `DashboardSpec`, and every chart's numbers come from Cube each time
it's shown, as the person looking, so a shared dashboard shows each person their own rows.

The rules that keep a chart honest are here rather than in a prompt, so no chart can get them
wrong: a chart takes the measures and dimensions its type needs, and a rolling window ends at the
last complete period, because a month three days old makes a trend appear to collapse. Windows are
resolved to dates when a chart is shown, which is what keeps "the last 12 months" right next month.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent.semantic.query import Granularity, Order, QueryFilter, SemanticQuery, TimeDimension

GRID_COLUMNS = 12
MAX_KPIS_PER_ROW = 4
MAX_CHARTS_PER_ROW = 3
DEFAULT_LIMIT = 1000
RAW_LIMIT = 50_000
EARLIEST = "1900-01-01"
FIXED_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
KEY = re.compile(r"^[a-z][a-z0-9_]{0,40}$")


class Viz(StrEnum):
    # Single numbers
    BIG_NUMBER = "big_number"
    BIG_NUMBER_TREND = "big_number_trend"  # the number, its change on the period before, and a sparkline
    GAUGE = "gauge"
    BULLET = "bullet"
    # Over time, or across categories
    LINE = "line"
    SMOOTH_LINE = "smooth_line"
    STEP_LINE = "step_line"
    AREA = "area"
    BAR = "bar"
    DOTS = "dots"
    COMBO = "combo"
    WATERFALL = "waterfall"
    # Parts of a whole
    PIE = "pie"
    TREEMAP = "treemap"
    SUNBURST = "sunburst"
    FUNNEL = "funnel"
    # Distributions and relationships
    HISTOGRAM = "histogram"
    BOX_PLOT = "box_plot"
    SCATTER = "scatter"
    HEATMAP = "heatmap"
    CALENDAR = "calendar"
    RADAR = "radar"
    # Flows and structures
    SANKEY = "sankey"
    NETWORK = "network"
    TREE = "tree"
    # Geography
    WORLD_MAP = "world_map"
    # Tables and text
    TABLE = "table"
    PIVOT = "pivot"
    WORD_CLOUD = "word_cloud"


class Stacking(StrEnum):
    NONE = "none"
    STACKED = "stacked"
    PERCENT = "percent"  # each bar or point fills the height, showing shares


KPIS = {Viz.BIG_NUMBER, Viz.BIG_NUMBER_TREND, Viz.GAUGE, Viz.BULLET}
TIME_SERIES = {Viz.LINE, Viz.SMOOTH_LINE, Viz.STEP_LINE, Viz.AREA, Viz.BIG_NUMBER_TREND}
SERIES_CHARTS = {Viz.LINE, Viz.SMOOTH_LINE, Viz.STEP_LINE, Viz.AREA, Viz.BAR, Viz.DOTS}
X_AXIS_CHARTS = {*TIME_SERIES, *SERIES_CHARTS, Viz.COMBO, Viz.WATERFALL, Viz.HEATMAP}
RAW_CHARTS = {Viz.HISTOGRAM, Viz.BOX_PLOT}  # these read many rows, so a low limit would quietly draw the wrong shape


@dataclass(frozen=True)
class Shape:
    """How many measures and dimensions a chart takes, and what they mean."""

    measures: tuple[int, int] = (1, 99)
    dimensions: tuple[int, int] = (0, 99)
    note: str = ""


SHAPES: dict[Viz, Shape] = {
    Viz.BIG_NUMBER: Shape((1, 1), (0, 0), "Set target to show progress towards it."),
    Viz.BIG_NUMBER_TREND: Shape((1, 1), (0, 0), "Shows the latest period, its change on the one before, and a sparkline."),
    Viz.GAUGE: Shape((1, 1), (0, 0), "Set gauge_max, or the dial has no meaningful end."),
    Viz.BULLET: Shape((1, 1), (0, 0), "Set bullet_ranges, and target for the target line."),
    Viz.PIE: Shape((1, 1), (1, 1)),
    Viz.TREEMAP: Shape((1, 1), (1, 4), "Dimensions run from the largest grouping to the smallest."),
    Viz.SUNBURST: Shape((1, 1), (1, 4), "Dimensions run from the centre outwards."),
    Viz.FUNNEL: Shape((1, 1), (1, 1), "The dimension holds the stages, in order."),
    Viz.WATERFALL: Shape((1, 1), (0, 1), "The measure is the change at each step."),
    Viz.HISTOGRAM: Shape((1, 1), (0, 0), "Set value_dimension to the number to bin; the measure counts rows at each "
                                         "value, usually a count."),
    Viz.BOX_PLOT: Shape((1, 1), (1, 1), "The dimension is the groups. Set distribute_across to what each dot is."),
    Viz.SCATTER: Shape((2, 3), (1, 2), "Measures are x, y and optionally size. The first dimension is what each point "
                                       "is, and an optional second one colours them."),
    Viz.HEATMAP: Shape((1, 1), (1, 2), "It needs two axes: two dimensions, or one dimension with time on the other."),
    Viz.CALENDAR: Shape((1, 1), (0, 0), "Set time_dimension; it shows one square a day."),
    Viz.RADAR: Shape((3, 9), (0, 1), "Each measure is a spoke. An optional dimension draws one shape per value."),
    Viz.SANKEY: Shape((1, 1), (2, 2), "Dimensions are the source and the target."),
    Viz.NETWORK: Shape((1, 1), (2, 2), "Dimensions are the source and the target."),
    Viz.TREE: Shape((1, 1), (2, 3), "Dimensions are the id, the parent's id, and optionally a name."),
    Viz.WORD_CLOUD: Shape((1, 1), (1, 1)),
    Viz.WORLD_MAP: Shape((1, 1), (1, 1), "The dimension holds countries, in the form set by country_codes."),
    Viz.TABLE: Shape((0, 20), (0, 20), "Measures and dimensions are its columns."),
    Viz.PIVOT: Shape((1, 9), (0, 4), "Dimensions are the rows and pivot_columns the columns."),
}


# --- Time windows ----------------------------------------------------------------------------------

def period_start(day: date, grain: Granularity) -> date:
    if grain == "day":
        return day
    if grain == "week":
        return day - timedelta(days=day.weekday())  # ISO weeks start on Monday
    if grain == "month":
        return day.replace(day=1)
    if grain == "quarter":
        return day.replace(month=(day.month - 1) // 3 * 3 + 1, day=1)
    return day.replace(month=1, day=1)


def shift(start: date, grain: Granularity, periods: int) -> date:
    """The start of the period `periods` away from the one starting at `start`."""
    if grain == "day":
        return start + timedelta(days=periods)
    if grain == "week":
        return start + timedelta(weeks=periods)
    months = {"month": 1, "quarter": 3, "year": 12}[grain] * periods
    index = start.year * 12 + start.month - 1 + months
    return date(index // 12, index % 12 + 1, 1)


def complete_window(today: date, grain: Granularity, periods: int | None) -> list[str]:
    """The last `periods` complete periods, or all history up to the last complete one."""
    current = period_start(today, grain)
    end = current - timedelta(days=1)
    start = shift(current, grain, -periods).isoformat() if periods else EARLIEST
    return [start, end.isoformat()]


def partial_window(today: date, grain: Granularity, periods: int) -> list[str]:
    """The last `periods` periods, counting the one still in progress."""
    current = period_start(today, grain)
    return [shift(current, grain, -(periods - 1)).isoformat(), today.isoformat()]


# --- Charts --------------------------------------------------------------------------------------

class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChartSpec(Strict):
    key: str = Field(description="Stable id within the dashboard, snake_case")
    title: str = Field(max_length=120)
    viz: Viz
    subtitle: str | None = Field(default=None, max_length=160, description="A short caption")
    measures: list[str] = Field(default_factory=list, description="cube.measure names")
    dimensions: list[str] = Field(default_factory=list, description="cube.dimension names")
    time_dimension: str | None = Field(default=None, description="A time dimension: what the window and trend are over")
    grain: Granularity | None = Field(default=None, description="The period to group time by")
    periods: int | None = Field(default=None, ge=1, le=1000, description="How many complete periods of grain to show, "
                                                                        "such as 12 months")
    include_partial_period: bool = Field(default=False, description="Keep the period still in progress")
    date_range: list[str] | None = Field(default=None, min_length=2, max_length=2,
                                         description="[from, to] dates, both included. Only with fixed_dates")
    fixed_dates: bool = Field(default=False, description="Set only if the user asked for specific dates")
    time_axis: bool | None = Field(default=None, description="Whether time runs along the bottom. With a dimension it "
                                                             "doesn't: the chart compares that dimension across the window")
    filters: list[QueryFilter] = Field(default_factory=list, description="Conditions this chart always has")
    segments: list[str] = Field(default_factory=list)
    limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=RAW_LIMIT)
    descending: bool = True
    horizontal: bool = Field(default=False, description="Bar: draw the bars sideways")
    stacking: Stacking = Field(default=Stacking.NONE, description="Bar, line and area with several series")
    donut: bool = Field(default=True, description="Pie: a ring. False draws a solid pie")
    line_measures: list[str] = Field(default_factory=list, description="Combo: drawn as lines; `measures` are the bars")
    pivot_columns: list[str] = Field(default_factory=list, description="Pivot: the dimensions across the top")
    totals: bool = Field(default=True, description="Pivot and table: show totals. Off for rates, which can't be added")
    distribute_across: list[str] = Field(default_factory=list, description="Box plot: what one dot is")
    value_dimension: str | None = Field(default=None, description="Histogram: the numeric dimension to bin")
    bins: int = Field(default=20, ge=2, le=100)
    gauge_min: float = 0
    gauge_max: float | None = None
    target: float | None = Field(default=None, description="Big number and bullet: the target")
    bullet_ranges: list[float] = Field(default_factory=list, description="Bullet: band boundaries, such as poor, fair, good")
    country_codes: Literal["cca2", "cca3", "name"] = Field(default="cca2", description="World map: how countries are named")
    tree_root: str | None = Field(default=None, description="Tree: the id of the top node")

    @model_validator(mode="after")
    def _matches_viz(self) -> "ChartSpec":
        if not KEY.match(self.key):
            raise ValueError("a chart key is snake_case, such as revenue_trend")
        shape = SHAPES.get(self.viz, Shape())
        for name, have, (low, high) in (("measure", len(self.measures), shape.measures),
                                        ("dimension", len(self.dimensions), shape.dimensions)):
            if not low <= have <= high:
                count = "one" if low == 1 else low
                wanted = f"exactly {count}" if low == high else f"{low} to {high}" if high < 99 else f"at least {count}"
                plural = "" if low == high == 1 else "s"
                raise ValueError(f"{self.viz.value} takes {wanted} {name}{plural}, not {have}. {shape.note}".strip())
        if self.viz in TIME_SERIES and not (self.time_dimension and self.grain):
            raise ValueError(f"{self.viz.value} needs time_dimension and grain")
        if self.viz in {Viz.BAR, Viz.DOTS, Viz.COMBO, Viz.WATERFALL} and not (self.dimensions or self.time_dimension):
            raise ValueError(f"{self.viz.value} needs a dimension or a time dimension for its x axis")
        if self.time_axis and not (self.time_dimension and self.grain):
            raise ValueError("time_axis needs time_dimension and grain")
        if self.time_axis is False and self.viz in X_AXIS_CHARTS and not self.dimensions:
            raise ValueError("without time along the bottom, this chart needs a dimension there instead")
        if self.viz is Viz.HEATMAP and len(self.dimensions) + self.time_on_x != 2:
            raise ValueError(SHAPES[Viz.HEATMAP].note)
        if self.viz is Viz.CALENDAR and not self.time_dimension:
            raise ValueError(SHAPES[Viz.CALENDAR].note)
        if self.viz is Viz.COMBO and not self.line_measures:
            raise ValueError("a combo chart needs line_measures as well as measures")
        if self.viz is Viz.HISTOGRAM and not self.value_dimension:
            raise ValueError(SHAPES[Viz.HISTOGRAM].note)
        if self.viz is Viz.BOX_PLOT and not self.distribute_across:
            raise ValueError(SHAPES[Viz.BOX_PLOT].note)
        if self.viz is Viz.PIVOT and not (self.dimensions or self.pivot_columns):
            raise ValueError("a pivot needs dimensions for its rows, pivot_columns for its columns, or both")
        if self.viz is Viz.TABLE and not (self.measures or self.dimensions):
            raise ValueError("a table needs at least one measure or dimension")
        if len(set(self.measures + self.line_measures)) != len(self.measures + self.line_measures):
            raise ValueError("a measure can only appear once on a chart")
        if self.viz in RAW_CHARTS and "limit" not in self.model_fields_set:
            self.limit = RAW_LIMIT
        if self.periods and not (self.time_dimension and self.grain):
            raise ValueError("periods needs time_dimension and grain")
        if self.date_range is not None:
            if not self.fixed_dates:
                raise ValueError("date_range holds fixed dates, which go stale. Use periods for a rolling window, "
                                 "and set fixed_dates only if the user asked for specific dates.")
            if not all(FIXED_DATE.match(day) for day in self.date_range) or self.date_range[0] > self.date_range[1]:
                raise ValueError("date_range is [from, to] as YYYY-MM-DD")
            if not self.time_dimension:
                raise ValueError("date_range needs time_dimension")
        return self

    @property
    def time_on_x(self) -> bool:
        """Whether time runs along the bottom, rather than only setting the window. Rentals by class
        over the last 12 months compares classes: a month per bar would be a different chart."""
        if self.viz in TIME_SERIES or self.viz is Viz.CALENDAR:
            return True
        if self.viz not in X_AXIS_CHARTS or not (self.time_dimension and self.grain):
            return False
        if self.time_axis is not None:
            return self.time_axis
        return len(self.dimensions) < (2 if self.viz is Viz.HEATMAP else 1)

    def window(self, today: date) -> list[str] | None:
        """The dates this chart covers, resolved today. Trends end at the last complete period unless
        told otherwise; this is applied here rather than left to the prompt."""
        if self.date_range:
            return self.date_range
        if not (self.time_dimension and self.grain):
            return None
        if self.include_partial_period:
            return partial_window(today, self.grain, self.periods) if self.periods else None
        if self.periods or self.time_on_x:
            return complete_window(today, self.grain, self.periods)
        return None

    def members(self) -> list[str]:
        return [*self.measures, *self.line_measures, *self.dimensions, *self.pivot_columns, *self.distribute_across,
                *([self.value_dimension] if self.value_dimension else []), *([self.time_dimension] if self.time_dimension else []),
                *(f.member for f in self.filters), *self.segments]

    def to_query(self, today: date, *, extra_filters: list[QueryFilter] = (), date_range: list[str] | None = None
                 ) -> SemanticQuery:
        """The semantic query for this chart, today, with a dashboard's filters if any. A dashboard's
        time filter replaces the chart's own window."""
        window = date_range if (date_range and self.time_dimension) else self.window(today)
        dimensions = [*self.dimensions, *self.pivot_columns, *self.distribute_across,
                      *([self.value_dimension] if self.value_dimension else [])]
        time_dimensions = []
        if self.time_dimension:
            grain = "day" if self.viz is Viz.CALENDAR else self.grain if self.time_on_x else None
            if grain or window:
                time_dimensions.append(TimeDimension(dimension=self.time_dimension, granularity=grain, date_range=window))
        measures = [*self.measures, *self.line_measures]
        if self.time_on_x and self.time_dimension:
            order = [Order(member=self.time_dimension, direction="asc")]
        elif measures and dimensions and self.viz not in {Viz.FUNNEL, Viz.HISTOGRAM}:
            order = [Order(member=measures[0], direction="desc" if self.descending else "asc")]
        else:
            order = []
        return SemanticQuery(measures=measures, dimensions=dimensions, time_dimensions=time_dimensions,
                             filters=[*self.filters, *extra_filters], segments=self.segments, order=order, limit=self.limit)


# --- Dashboards ------------------------------------------------------------------------------------

class Slot(Strict):
    key: str = Field(description="A chart's key")
    width: int = Field(default=0, ge=0, le=GRID_COLUMNS, description="Columns of 12. 0 shares the row evenly")


class DashboardFilter(Strict):
    """A choice above the charts, such as tenant or region, applied to every chart it can reach."""

    name: str = Field(max_length=60)
    member: str = Field(description="The dimension it filters on, cube.member")
    applies_to: list[str] = Field(default_factory=list, description="Worked out by Nova: the charts it reaches")


class DashboardSpec(Strict):
    title: str = Field(max_length=120)
    charts: list[ChartSpec] = Field(default_factory=list)
    rows: list[list[Slot]] = Field(default_factory=list)
    filters: list[DashboardFilter] = Field(default_factory=list)
    time_filter: bool = Field(default=False, description="Let people change the time window of the charts that have one")

    def chart(self, key: str) -> ChartSpec | None:
        return next((chart for chart in self.charts if chart.key == key), None)

    @model_validator(mode="after")
    def _consistent(self) -> "DashboardSpec":
        keys = [chart.key for chart in self.charts]
        if len(set(keys)) != len(keys):
            raise ValueError("two charts share a key")
        placed = [slot.key for row in self.rows for slot in row]
        unknown = [key for key in placed if key not in keys]
        if unknown:
            raise ValueError(f"the layout names charts that don't exist: {unknown}")
        if len(set(placed)) != len(placed):
            raise ValueError("a chart appears twice in the layout")
        for row in self.rows:
            if not row:
                raise ValueError("a row is empty")
            if sum(slot.width for slot in row) > GRID_COLUMNS:
                raise ValueError(f"a row is wider than {GRID_COLUMNS} columns")
            kpis = [slot for slot in row if self.chart(slot.key).viz in KPIS]
            if kpis and len(kpis) != len(row):
                raise ValueError("keep single numbers in rows of their own, so they line up")
            if len(row) > (MAX_KPIS_PER_ROW if kpis else MAX_CHARTS_PER_ROW):
                raise ValueError(f"at most {MAX_KPIS_PER_ROW} numbers or {MAX_CHARTS_PER_ROW} charts fit in a row")
        names = [f.name for f in self.filters]
        if len(set(names)) != len(names):
            raise ValueError("two filters share a name")
        return self

    def unplaced(self) -> list[str]:
        placed = {slot.key for row in self.rows for slot in row}
        return [chart.key for chart in self.charts if chart.key not in placed]


def widths(row: list[Slot]) -> list[int]:
    """Columns for each slot: given widths are kept and the rest share what's left evenly."""
    fixed = sum(slot.width for slot in row)
    flexible = [slot for slot in row if not slot.width]
    if not flexible:
        return [slot.width for slot in row]
    share, extra = divmod(max(GRID_COLUMNS - fixed, len(flexible)), len(flexible))
    result, given = [], 0
    for slot in row:
        if slot.width:
            result.append(slot.width)
        else:
            result.append(share + (1 if given < extra else 0))
            given += 1
    return result
