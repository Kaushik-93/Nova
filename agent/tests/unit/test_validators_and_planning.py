import json
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from agent.dashboard_agent import dashboard_key_for, load_prompt
from agent.harness.validators import chart_time_window, last_point_warning, window_end_problem
from agent.tools.planning import DashboardPlan
from agent.tools.superset_client import ChartData
from agent.tools.superset_payloads import ChartSpec, Metric, TimeGrain, Viz, fill_row

REVENUE = Metric(label="Revenue", sql="SUM(revenue)")
TREND = ChartSpec(key="t", title="t", viz=Viz.LINE, metrics=[REVENUE], time_column="d", time_grain=TimeGrain.MONTH)


def data(*values) -> ChartData:
    return ChartData(columns=["d", "Revenue"], rows=[{"d": i, "Revenue": v} for i, v in enumerate(values)],
                     row_count=len(values))


def test_a_collapsing_last_point_is_flagged():
    warning = last_point_warning(data(100, 110, 105, 108, 12), TREND)
    assert "partial period" in warning


def test_the_last_point_is_found_whatever_order_superset_returns():
    newest_first = ChartData(columns=["d", "Revenue"], row_count=5, rows=[
        {"d": 5, "Revenue": 12}, {"d": 4, "Revenue": 108}, {"d": 3, "Revenue": 105}, {"d": 2, "Revenue": 110},
        {"d": 1, "Revenue": 100}])
    assert "partial period" in last_point_warning(newest_first, TREND)


def test_a_steady_trend_is_not_flagged():
    assert last_point_warning(data(100, 110, 105, 108, 95), TREND) is None


def test_a_rising_trend_is_not_flagged():
    assert last_point_warning(data(100, 110, 120, 130, 200), TREND) is None


def test_short_trends_are_left_alone():
    assert last_point_warning(data(100, 2), TREND) is None


def test_only_trends_are_checked():
    pie = ChartSpec(key="p", title="p", viz=Viz.PIE, metrics=[REVENUE], dimensions=["r"])
    assert last_point_warning(data(100, 110, 105, 108, 1), pie) is None


def test_split_trends_are_left_alone():
    # Rows are per series, so the order of values means nothing.
    split = TREND.model_copy(update={"dimensions": ["region"]})
    assert last_point_warning(data(100, 110, 105, 108, 1), split) is None


@pytest.mark.parametrize("end, grain", [
    ("2026-11-01 00:00:00", "P1M"), ("2026-10-01 00:00:00", "P3M"), ("2026-01-01 00:00:00", "P1Y"),
    ("2026-11-02 00:00:00", "P1W"), ("2026-10-31 00:00:00", "P1D"), (None, "P1M"), ("2026-10-31 00:00:00", None),
])
def test_windows_ending_on_a_boundary_pass(end, grain):
    assert window_end_problem(end, grain) is None


@pytest.mark.parametrize("end, grain", [
    ("2026-10-31 00:00:00", "P1M"), ("2026-11-01 00:00:00", "P3M"), ("2026-07-01 00:00:00", "P1Y"),
    ("2026-11-03 00:00:00", "P1W"),
])
def test_windows_ending_mid_period_are_a_problem(end, grain):
    assert "cut short" in window_end_problem(end, grain)


def test_every_prompt_knows_todays_date():
    today = datetime.now(timezone.utc).date().isoformat()
    for name in ("main", "sql_author", "chart_designer", "verifier"):
        assert today in load_prompt(name)


def test_a_thread_keeps_its_dashboard():
    assert dashboard_key_for("a") == dashboard_key_for("a")
    assert dashboard_key_for("a") != dashboard_key_for("b")


def test_a_plan_needs_charts_and_sources():
    with pytest.raises(ValidationError):
        DashboardPlan(title="t", goal="g", sources=[], charts=[])


def saved(*filters) -> dict:
    return {"params": json.dumps({"adhoc_filters": list(filters)})}


def test_a_charts_time_window_is_read_from_its_saved_params():
    window = {"operator": "TEMPORAL_RANGE", "comparator": "Last year"}
    assert chart_time_window(saved(window)) == "Last year"


@pytest.mark.parametrize("detail", [saved(), saved({"operator": "TEMPORAL_RANGE", "comparator": "No filter"}),
                                    saved({"operator": "==", "comparator": "x"}), {}])
def test_having_no_window_is_recognised(detail):
    assert chart_time_window(detail) is None


@pytest.mark.parametrize("widths, expected", [
    ([4, 8], [4, 8]), ([3, 3, 3, 3], [3, 3, 3, 3]), ([4], [12]), ([4, 4], [6, 6]), ([6, 3], [8, 4]),
    ([4, 4, 4], [4, 4, 4]), ([1, 1, 1, 1, 1], [3, 3, 2, 2, 2]),
])
def test_rows_are_stretched_to_fill_the_grid(widths, expected):
    assert fill_row(widths) == expected


@pytest.mark.parametrize("widths", [[1] * n for n in range(1, 13)] + [[2, 5], [7, 1], [1, 10, 1], [5, 5, 5]])
def test_a_row_always_comes_to_exactly_twelve(widths):
    assert sum(fill_row(widths)) == 12
