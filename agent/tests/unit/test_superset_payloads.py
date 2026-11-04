import json

import pytest
from pydantic import ValidationError

from agent.tools.superset_payloads import (ChartSlot, ChartSpec, Metric, SelectFilter, TimeFilter, TimeGrain, Viz,
                                           build_chart_payload, build_form_data, build_json_metadata,
                                           build_native_filter, build_positions, build_query_context,
                                           complete_period_range, stable_uuid)

REVENUE = Metric(label="Revenue", sql="SUM(revenue)")
ORDERS = Metric(label="Orders", sql="COUNT(*)")


def chart(viz: Viz, **overrides) -> ChartSpec:
    defaults = dict(key="c", title="Chart", viz=viz, metrics=[REVENUE])
    if viz in {Viz.LINE, Viz.AREA, Viz.BIG_NUMBER_TREND}:
        defaults |= dict(time_column="order_date", time_grain=TimeGrain.MONTH)
    if viz is Viz.PIE:
        defaults |= dict(dimensions=["region"])
    if viz is Viz.BAR:
        defaults |= dict(dimensions=["product"])
    return ChartSpec(**(defaults | overrides))


@pytest.mark.parametrize("viz", list(Viz))
def test_every_viz_points_at_its_dataset_and_type(viz):
    form = build_form_data(chart(viz), dataset_id=7)
    assert form["datasource"] == "7__table"
    assert form["viz_type"] == viz.value


@pytest.mark.parametrize("viz", list(Viz))
def test_every_viz_has_a_query_with_the_metric(viz):
    context = build_query_context(chart(viz), dataset_id=7)
    assert context["datasource"] == {"id": 7, "type": "table"}
    assert context["queries"][0]["metrics"][0]["sqlExpression"] == "SUM(revenue)"
    assert context["queries"][0]["metrics"][0]["label"] == "Revenue"


@pytest.mark.parametrize("viz", [Viz.BIG_NUMBER, Viz.BIG_NUMBER_TREND, Viz.PIE])
def test_single_metric_charts_use_the_singular_control(viz):
    form = build_form_data(chart(viz), 1)
    assert "metric" in form and "metrics" not in form


@pytest.mark.parametrize("viz", [Viz.LINE, Viz.AREA, Viz.BAR, Viz.TABLE])
def test_multi_metric_charts_use_the_plural_control(viz):
    form = build_form_data(chart(viz, metrics=[REVENUE, ORDERS]), 1)
    assert [m["label"] for m in form["metrics"]] == ["Revenue", "Orders"]


def test_time_series_put_time_on_the_x_axis_at_the_chosen_grain():
    spec = chart(Viz.LINE, time_grain=TimeGrain.QUARTER)
    form = build_form_data(spec, 1)
    column = build_query_context(spec, 1)["queries"][0]["columns"][0]

    assert form["x_axis"] == "order_date"
    assert form["time_grain_sqla"] == "P3M"
    assert column["columnType"] == "BASE_AXIS"
    assert column["timeGrain"] == "P3M"


@pytest.mark.parametrize("grain, expected", [(TimeGrain.MONTH, "%b %Y"), (TimeGrain.QUARTER, "%Y Q%q"),
                                             (TimeGrain.YEAR, "%Y"), (TimeGrain.DAY, "%b %d")])
def test_time_axes_label_every_point_unambiguously(grain, expected):
    assert build_form_data(chart(Viz.LINE, time_grain=grain), 1)["x_axis_time_format"] == expected


def test_time_filter_is_applied_in_both_form_data_and_query():
    spec = chart(Viz.LINE, time_range="Last year")
    assert build_form_data(spec, 1)["adhoc_filters"] == [{
        "clause": "WHERE", "subject": "order_date", "operator": "TEMPORAL_RANGE", "comparator": "Last year",
        "expressionType": "SIMPLE",
    }]
    assert build_query_context(spec, 1)["queries"][0]["filters"] == [
        {"col": "order_date", "op": "TEMPORAL_RANGE", "val": "Last year"}]


def test_categorical_bars_rank_by_value_and_series_split_by_dimension():
    ranked = build_form_data(chart(Viz.BAR, dimensions=["product", "region"]), 1)
    assert ranked["x_axis"] == "product"
    assert ranked["groupby"] == ["region"]
    assert ranked["x_axis_sort"] == "Revenue"
    assert ranked["x_axis_sort_asc"] is False


def test_bars_over_time_keep_time_order():
    spec = chart(Viz.BAR, dimensions=["region"], time_column="order_date", time_grain=TimeGrain.MONTH)
    form = build_form_data(spec, 1)
    assert form["x_axis"] == "order_date"
    assert form["groupby"] == ["region"]
    assert "x_axis_sort" not in form


def test_ranked_charts_order_their_query_by_the_metric():
    query = build_query_context(chart(Viz.PIE), 1)["queries"][0]
    metric, ascending = query["orderby"][0]
    assert metric["label"] == "Revenue"
    assert ascending is False


def test_a_table_groups_by_its_dimensions_in_aggregate_mode():
    form = build_form_data(chart(Viz.TABLE, dimensions=["region", "category"]), 1)
    assert form["query_mode"] == "aggregate"
    assert form["groupby"] == ["region", "category"]


@pytest.mark.parametrize("viz, overrides, complaint", [
    (Viz.BIG_NUMBER, {"metrics": [REVENUE, ORDERS]}, "exactly one metric"),
    (Viz.PIE, {"metrics": [REVENUE, ORDERS]}, "exactly one metric"),
    (Viz.PIE, {"dimensions": ["a", "b"]}, "exactly one dimension"),
    (Viz.LINE, {"time_grain": None}, "time_column and time_grain"),
    (Viz.LINE, {"time_column": None}, "time_column and time_grain"),
    (Viz.BAR, {"dimensions": [], "time_column": None}, "x axis"),
])
def test_specs_that_cannot_render_are_rejected_up_front(viz, overrides, complaint):
    with pytest.raises(ValidationError, match=complaint):
        chart(viz, **overrides)


def test_a_chart_needs_a_metric():
    with pytest.raises(ValidationError):
        ChartSpec(key="c", title="t", viz=Viz.TABLE, metrics=[])


def test_chart_payload_serialises_params_and_query_context_as_strings():
    payload = build_chart_payload(chart(Viz.PIE), 3, "sales")
    assert payload["slice_name"] == "Chart"
    assert payload["datasource_id"] == 3
    assert json.loads(payload["params"])["datasource"] == "3__table"
    assert json.loads(payload["query_context"])["datasource"]["id"] == 3


def test_identity_is_stable_and_scoped():
    first = build_chart_payload(chart(Viz.PIE), 3, "sales")["uuid"]
    assert first == build_chart_payload(chart(Viz.PIE, title="Renamed"), 99, "sales")["uuid"]
    assert first != build_chart_payload(chart(Viz.PIE), 3, "marketing")["uuid"]
    assert stable_uuid("chart", "x") != stable_uuid("dataset", "x")


def test_payloads_are_deterministic():
    assert build_chart_payload(chart(Viz.LINE), 1, "d") == build_chart_payload(chart(Viz.LINE), 1, "d")


# --- Complete periods --------------------------------------------------------

def test_a_range_ends_at_the_start_of_the_current_period():
    assert complete_period_range(TimeGrain.MONTH, 12) == (
        "DATEADD(DATETRUNC(DATETIME('today'), month), -12, month) : DATETRUNC(DATETIME('today'), month)")


@pytest.mark.parametrize("viz", [Viz.LINE, Viz.AREA, Viz.BIG_NUMBER_TREND])
def test_trends_leave_out_the_period_in_progress_by_default(viz):
    assert chart(viz).effective_time_range.endswith(": DATETRUNC(DATETIME('today'), month)")


def test_bars_over_time_are_trends_too():
    spec = chart(Viz.BAR, dimensions=[], time_column="order_date", time_grain=TimeGrain.WEEK)
    assert spec.effective_time_range.endswith(": DATETRUNC(DATETIME('today'), week)")


def test_periods_bound_the_start():
    assert "-6, month" in chart(Viz.LINE, periods=6).effective_time_range


def test_a_kpi_with_periods_matches_its_trend():
    kpi = chart(Viz.BIG_NUMBER, time_column="order_date", time_grain=TimeGrain.MONTH, periods=12)
    assert kpi.effective_time_range == chart(Viz.LINE, periods=12).effective_time_range


def test_a_kpi_without_periods_covers_all_time():
    assert chart(Viz.BIG_NUMBER, time_column="order_date").effective_time_range == "No filter"


def test_an_explicit_range_wins():
    assert chart(Viz.LINE, time_range="Last week").effective_time_range == "Last week"


def test_the_partial_period_can_be_asked_for():
    assert chart(Viz.LINE, include_partial_period=True).effective_time_range == "No filter"


def test_asking_for_the_partial_period_still_honours_periods():
    # Regression: this once dropped `periods`, so "12 months" showed all of history.
    spec = chart(Viz.LINE, periods=12, include_partial_period=True)
    assert spec.effective_time_range == (
        "DATEADD(DATETRUNC(DATETIME('today'), month), -11, month) : "
        "DATEADD(DATETRUNC(DATETIME('today'), month), 1, month)")


def test_a_chart_payload_records_its_key():
    assert build_chart_payload(chart(Viz.PIE, key="by-region"), 1, "d")["description"] == "Nova chart key: by-region"


def test_periods_need_a_time_column():
    with pytest.raises(ValidationError, match="periods needs"):
        chart(Viz.TABLE, periods=12)


# --- Layout ------------------------------------------------------------------

def test_positions_form_a_tree_with_every_chart_on_a_row():
    rows = [[ChartSlot(chart_id=1, title="A", width=6), ChartSlot(chart_id=2, title="B", width=6)],
            [ChartSlot(chart_id=3, title="C", width=12)]]
    positions = build_positions("Sales", rows)

    assert positions["DASHBOARD_VERSION_KEY"] == "v2"
    assert positions["ROOT_ID"]["children"] == ["GRID_ID"]
    assert positions["GRID_ID"]["children"] == ["ROW-0", "ROW-1"]
    assert positions["HEADER_ID"]["meta"]["text"] == "Sales"
    assert positions["ROW-0"]["children"] == ["CHART-1", "CHART-2"]
    assert positions["CHART-3"]["parents"] == ["ROOT_ID", "GRID_ID", "ROW-1"]
    assert positions["CHART-2"]["meta"] == {"chartId": 2, "width": 6, "height": 50, "sliceName": "B"}


def test_a_row_cannot_exceed_the_grid():
    too_wide = [[ChartSlot(chart_id=1, title="A", width=8), ChartSlot(chart_id=2, title="B", width=5)]]
    with pytest.raises(ValueError, match="13 columns wide"):
        build_positions("t", too_wide)


# --- Filters -----------------------------------------------------------------

def test_select_filter_targets_its_column_and_spans_the_dashboard():
    native = build_native_filter(SelectFilter(name="Region", dataset_id=4, column="region"))
    assert native["filterType"] == "filter_select"
    assert native["targets"] == [{"datasetId": 4, "column": {"name": "region"}}]
    assert native["scope"] == {"rootPath": ["ROOT_ID"], "excluded": []}
    assert native["type"] == "NATIVE_FILTER"
    assert native["id"].startswith("NATIVE_FILTER-")


def test_time_filter_carries_its_default_range():
    native = build_native_filter(TimeFilter(default="Last year"))
    assert native["filterType"] == "filter_time"
    assert native["defaultDataMask"]["extraFormData"] == {"time_range": "Last year"}
    assert native["defaultDataMask"]["filterState"] == {"value": "Last year"}


def test_filter_ids_are_stable_so_rebuilds_keep_user_selections():
    first = build_native_filter(TimeFilter())["id"]
    assert first == build_native_filter(TimeFilter())["id"]
    assert first != build_native_filter(TimeFilter(name="Other"))["id"]


def test_a_time_filter_with_no_default_leaves_chart_ranges_alone():
    native = build_native_filter(TimeFilter())
    assert native["defaultDataMask"]["extraFormData"] == {}


def test_json_metadata_carries_positions_so_superset_relinks_charts():
    positions = build_positions("t", [[ChartSlot(chart_id=1, title="A", width=12)]])
    metadata = build_json_metadata(positions, [build_native_filter(TimeFilter())])
    assert metadata["positions"] == positions
    assert len(metadata["native_filter_configuration"]) == 1
