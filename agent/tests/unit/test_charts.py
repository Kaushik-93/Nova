"""Charts as semantic queries: the windows, the shapes, and what a dashboard may hold."""

from datetime import date

import pytest
from pydantic import ValidationError

from agent.semantic.charts import (ChartSpec, DashboardFilter, DashboardSpec, Slot, Viz, complete_window, partial_window,
                                   period_start, widths)
from agent.semantic.query import QueryFilter

TODAY = date(2026, 11, 9)  # a Monday, in the middle of a month and a quarter
REVENUE, ORDERS = "shop__orders.revenue", "shop__orders.count"
WHEN, STATUS, REGION = "shop__orders.ordered_at", "shop__orders.status", "shop__customers.region"


def chart(**overrides) -> ChartSpec:
    values = dict(key="c", title="t", viz=Viz.LINE, measures=[REVENUE], time_dimension=WHEN, grain="month")
    return ChartSpec(**(values | overrides))


@pytest.mark.parametrize("grain, periods, expected", [
    ("month", 12, ["2025-11-01", "2026-10-31"]),
    ("month", 1, ["2026-10-01", "2026-10-31"]),
    ("quarter", 4, ["2025-10-01", "2026-09-30"]),
    ("year", 2, ["2024-01-01", "2025-12-31"]),
    ("week", 2, ["2026-10-26", "2026-11-08"]),
    ("day", 7, ["2026-11-02", "2026-11-08"]),
])
def test_a_rolling_window_ends_at_the_last_complete_period(grain, periods, expected):
    assert complete_window(TODAY, grain, periods) == expected


def test_a_window_crosses_the_year_correctly():
    assert complete_window(date(2026, 1, 15), "month", 3) == ["2025-10-01", "2025-12-31"]
    assert complete_window(date(2026, 3, 1), "month", 1) == ["2026-02-01", "2026-02-28"]


def test_the_period_in_progress_is_only_kept_when_asked():
    assert partial_window(TODAY, "month", 3) == ["2026-09-01", "2026-11-09"]
    assert chart(include_partial_period=True, periods=3).window(TODAY) == ["2026-09-01", "2026-11-09"]


def test_a_trend_without_periods_shows_all_complete_history():
    assert chart().window(TODAY) == ["1900-01-01", "2026-10-31"]


def test_a_snapshot_without_a_window_has_none():
    assert chart(viz=Viz.BIG_NUMBER, grain=None).window(TODAY) is None


def test_weeks_start_on_monday():
    assert period_start(date(2026, 11, 15), "week") == date(2026, 11, 9)


def test_a_trend_groups_by_time_and_orders_by_it():
    query = chart(periods=12).to_query(TODAY)
    assert query.time_dimensions[0].granularity == "month"
    assert query.time_dimensions[0].date_range == ["2025-11-01", "2026-10-31"]
    assert [(o.member, o.direction) for o in query.order] == [(WHEN, "asc")]


def test_a_category_chart_over_a_window_compares_categories_not_months():
    bars = chart(viz=Viz.BAR, dimensions=[STATUS], periods=12)
    query = bars.to_query(TODAY)
    assert not bars.time_on_x
    assert query.time_dimensions[0].granularity is None and query.time_dimensions[0].date_range[1] == "2026-10-31"
    assert [(o.member, o.direction) for o in query.order] == [(REVENUE, "desc")]


def test_a_dashboard_filter_and_time_filter_reach_the_query():
    extra = QueryFilter(member=REGION, operator="equals", values=["EU"])
    query = chart(periods=12).to_query(TODAY, extra_filters=[extra], date_range=["2026-01-01", "2026-06-30"])
    assert query.filters == [extra]
    assert query.time_dimensions[0].date_range == ["2026-01-01", "2026-06-30"]


@pytest.mark.parametrize("overrides, message", [
    (dict(viz=Viz.PIE, dimensions=[]), "takes exactly one dimension"),
    (dict(viz=Viz.BIG_NUMBER, measures=[REVENUE, ORDERS], grain=None), "exactly one measure"),
    (dict(time_dimension=None), "needs time_dimension and grain"),
    (dict(viz=Viz.COMBO), "line_measures"),
    (dict(viz=Viz.HISTOGRAM, grain=None, time_dimension=None, measures=[ORDERS]), "value_dimension"),
    (dict(date_range=["2026-01-01", "2026-03-31"]), "fixed dates"),
    (dict(key="Not A Key"), "snake_case"),
    (dict(measures=[REVENUE], line_measures=[REVENUE], viz=Viz.COMBO), "only appear once"),
], ids=["pie", "kpi", "trend", "combo", "histogram", "fixed dates", "key", "repeated"])
def test_a_chart_that_cant_be_right_is_refused(overrides, message):
    with pytest.raises(ValidationError, match=message):
        chart(**overrides)


def test_fixed_dates_are_allowed_when_asked_for():
    fixed = chart(date_range=["2026-01-01", "2026-03-31"], fixed_dates=True)
    assert fixed.window(TODAY) == ["2026-01-01", "2026-03-31"]


def test_raw_charts_read_enough_rows():
    assert chart(viz=Viz.HISTOGRAM, measures=[ORDERS], value_dimension="shop__orders.items", grain=None,
                 time_dimension=None).limit == 50_000


def dashboard(**overrides) -> DashboardSpec:
    kpi = chart(key="kpi", viz=Viz.BIG_NUMBER, grain=None)
    trend = chart(key="trend", periods=12)
    values = dict(title="Sales", charts=[kpi, trend], rows=[[Slot(key="kpi")], [Slot(key="trend")]])
    return DashboardSpec(**(values | overrides))


def test_a_good_dashboard_passes():
    assert dashboard().unplaced() == []


@pytest.mark.parametrize("rows, message", [
    ([[Slot(key="ghost")]], "don't exist"),
    ([[Slot(key="kpi"), Slot(key="kpi")]], "twice"),
    ([[Slot(key="kpi"), Slot(key="trend")]], "rows of their own"),
    ([[Slot(key="trend", width=8), Slot(key="kpi", width=8)]], "wider"),
], ids=["unknown", "twice", "mixed", "too wide"])
def test_a_layout_that_cant_work_is_refused(rows, message):
    with pytest.raises(ValidationError, match=message):
        dashboard(rows=rows)


def test_unplaced_charts_are_reported():
    assert dashboard(rows=[[Slot(key="kpi")]]).unplaced() == ["trend"]


def test_filters_need_distinct_names():
    with pytest.raises(ValidationError, match="share a name"):
        dashboard(filters=[DashboardFilter(name="Region", member=REGION), DashboardFilter(name="Region", member=STATUS)])


def test_widths_share_the_row():
    assert widths([Slot(key="a"), Slot(key="b"), Slot(key="c")]) == [4, 4, 4]
    assert widths([Slot(key="a", width=8), Slot(key="b")]) == [8, 4]
    assert widths([Slot(key="a"), Slot(key="b"), Slot(key="c"), Slot(key="d"), Slot(key="e")]) == [3, 3, 2, 2, 2]
