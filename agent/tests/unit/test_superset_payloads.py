import json

import pytest
from pydantic import ValidationError

from agent.gallery import TILES
from agent.tools.superset_payloads import (ChartSlot, ChartSpec, Metric, SelectFilter, Stacking, TimeFilter, TimeGrain, Viz,
                                           build_chart_payload, build_form_data, build_json_metadata,
                                           build_native_filter, build_positions, build_query_context,
                                           complete_period_range, rows_from_positions, stable_uuid, wrap_row)

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


EXAMPLES = {viz: next(tile.spec for tile in TILES if tile.spec.viz is viz) for viz in Viz}
WITH_METRICS = [viz for viz in Viz if viz is not Viz.HISTOGRAM]


@pytest.mark.parametrize("viz", list(Viz))
def test_every_viz_points_at_its_dataset_and_type(viz):
    form = build_form_data(EXAMPLES[viz], dataset_id=7)
    assert form["datasource"] == "7__table"
    assert form["viz_type"] == viz.value
    assert json.dumps(form)


@pytest.mark.parametrize("viz", WITH_METRICS)
def test_every_viz_has_a_query_with_its_metric(viz):
    spec = EXAMPLES[viz]
    context = build_query_context(spec, dataset_id=7)
    assert context["datasource"] == {"id": 7, "type": "table"}
    assert context["queries"][0]["metrics"][0]["label"] == spec.metrics[0].label
    assert json.dumps(context)


def test_the_gallery_shows_every_chart_nova_can_build():
    assert {tile.spec.viz for tile in TILES} == set(Viz)


def test_chart_keys_in_the_gallery_are_unique():
    keys = [tile.spec.key for tile in TILES]
    assert len(keys) == len(set(keys))


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
    spec = chart(Viz.BAR, dimensions=["region"], time_column="order_date", time_grain=TimeGrain.MONTH, time_axis=True)
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


@pytest.mark.parametrize("value", ["2025-11-01 : 2026-10-31", "2025-11 : 2026-11", ": 2026-01-01"])
def test_fixed_dates_are_refused_unless_asked_for(value):
    with pytest.raises(ValidationError, match="fixed dates"):
        chart(Viz.LINE, time_range=value)
    assert chart(Viz.LINE, time_range=value, fixed_dates=True).effective_time_range == value


def test_relative_ranges_are_allowed():
    assert chart(Viz.LINE, time_range="Last quarter").effective_time_range == "Last quarter"


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


@pytest.mark.parametrize("viz_types, expected", [
    (["big_number_total"] * 4, [[0, 1, 2, 3]]),
    (["big_number_total"] * 5, [[0, 1, 2, 3], [4]]),
    (["echarts_timeseries_line"] * 4, [[0, 1], [2, 3]]),
    (["echarts_timeseries_line", "pie", "table"], [[0, 1], [2]]),
    (["big_number_total", "table"], [[0, 1]]),
    (["table"], [[0]]),
])
def test_crowded_rows_wrap(viz_types, expected):
    assert wrap_row(viz_types) == expected


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


def test_filters_sit_in_a_row_above_the_charts():
    assert build_json_metadata({}, [])["filter_bar_orientation"] == "HORIZONTAL"


def test_charts_use_the_validated_palette():
    assert build_form_data(chart(Viz.LINE), 1)["color_scheme"] == "novaColors"
    assert build_json_metadata({}, [])["color_scheme"] == "novaColors"


def test_json_metadata_carries_positions_so_superset_relinks_charts():
    positions = build_positions("t", [[ChartSlot(chart_id=1, title="A", width=12)]])
    metadata = build_json_metadata(positions, [build_native_filter(TimeFilter())])
    assert metadata["positions"] == positions
    assert len(metadata["native_filter_configuration"]) == 1


# --- The chart types added for Qlik parity ------------------------------------------------------

@pytest.mark.parametrize("viz, keys", [
    (Viz.TREEMAP, {"metric", "groupby", "number_format"}),
    (Viz.SUNBURST, {"metric", "columns"}),
    (Viz.FUNNEL, {"metric", "groupby", "label_type"}),
    (Viz.GAUGE, {"metric", "min_val", "max_val", "number_format"}),
    (Viz.BULLET, {"metric", "ranges", "marker_lines"}),
    (Viz.WATERFALL, {"metric", "x_axis", "groupby", "increase_color"}),
    (Viz.HISTOGRAM, {"column", "bins"}),
    (Viz.BOX_PLOT, {"columns", "groupby", "metrics", "whiskerOptions"}),
    (Viz.SCATTER, {"entity", "x", "y", "size"}),
    (Viz.HEATMAP, {"metric", "x_axis", "groupby", "linear_color_scheme"}),
    (Viz.CALENDAR, {"metrics", "granularity_sqla", "domain_granularity", "subdomain_granularity"}),
    (Viz.RADAR, {"metrics", "groupby"}),
    (Viz.SANKEY, {"metric", "source", "target"}),
    (Viz.NETWORK, {"metric", "source", "target"}),
    (Viz.TREE, {"id", "parent", "name", "root_node_id"}),
    (Viz.WORD_CLOUD, {"metric", "series"}),
    (Viz.WORLD_MAP, {"metric", "entity", "country_fieldtype"}),
    (Viz.COUNTRY_MAP, {"metric", "entity", "select_country"}),
    (Viz.PIVOT, {"metrics", "groupbyRows", "groupbyColumns", "aggregateFunction"}),
])
def test_each_chart_sets_the_controls_its_plugin_reads(viz, keys):
    assert keys <= set(build_form_data(EXAMPLES[viz], 1))


def test_funnel_labels_use_the_frontends_numeric_enum():
    """A string here is saved without complaint, and then draws no labels."""
    assert build_form_data(EXAMPLES[Viz.FUNNEL], 1)["label_type"] == 5


def test_a_combo_chart_runs_one_query_for_bars_and_one_for_lines():
    spec = EXAMPLES[Viz.COMBO]
    form = build_form_data(spec, 1)
    queries = build_query_context(spec, 1)["queries"]
    assert (form["seriesType"], form["seriesType_b"]) == ("bar", "line")
    assert [query["metrics"][0]["label"] for query in queries] == ["Rentals", "Completed rentals"]
    assert form["adhoc_filters_b"] == form["adhoc_filters"]


def test_a_combo_shares_one_axis_unless_someone_asks_for_two():
    spec = ChartSpec(key="c", title="C", viz=Viz.COMBO, metrics=[REVENUE], line_metrics=[ORDERS], dimensions=["region"])
    assert build_form_data(spec, 1)["yAxisIndex_b"] == 0
    assert build_form_data(spec.model_copy(update={"secondary_axis": True}), 1)["yAxisIndex_b"] == 1


def test_a_histogram_reads_rows_rather_than_metrics():
    query = build_query_context(EXAMPLES[Viz.HISTOGRAM], 1)["queries"][0]
    assert "metrics" not in query
    assert query["columns"] == ["rental_days"]


@pytest.mark.parametrize("viz", [Viz.HISTOGRAM, Viz.BOX_PLOT])
def test_charts_that_read_rows_arent_cut_off_at_the_usual_limit(viz):
    assert EXAMPLES[viz].row_limit == 50000


def test_an_explicit_row_limit_is_respected():
    spec = ChartSpec(key="h", title="H", viz=Viz.HISTOGRAM, value_column="v", row_limit=500)
    assert spec.row_limit == 500


def test_a_scatter_without_a_size_draws_equal_points_in_both_places():
    spec = ChartSpec(key="s", title="S", viz=Viz.SCATTER, metrics=[REVENUE, ORDERS], dimensions=["region"])
    form = build_form_data(spec, 1)
    assert form["size"]["sqlExpression"] == "MAX(1)"
    assert form["size"] in build_query_context(spec, 1)["queries"][0]["metrics"]


def test_a_pivot_queries_rows_and_columns_once_each():
    spec = chart(Viz.PIVOT, dimensions=["region", "product"], pivot_columns=["product"])
    assert sorted(build_query_context(spec, 1)["queries"][0]["columns"]) == ["product", "region"]


def test_a_pivot_can_have_time_as_a_dimension_at_a_grain():
    spec = chart(Viz.PIVOT, dimensions=["region"], pivot_columns=["order_date"], time_column="order_date", time_grain=TimeGrain.MONTH)
    assert build_form_data(spec, 1)["temporal_columns_lookup"] == {"order_date": True}
    assert any(isinstance(c, dict) and c["timeGrain"] == "P1M" for c in build_query_context(spec, 1)["queries"][0]["columns"])


def test_totals_can_be_turned_off_for_rates():
    form = build_form_data(chart(Viz.PIVOT, dimensions=["region"], totals=False), 1)
    assert form["rowTotals"] is False and form["colTotals"] is False


def test_a_bar_chart_can_lie_on_its_side():
    assert build_form_data(chart(Viz.BAR, horizontal=True), 1)["orientation"] == "horizontal"


def test_stacking():
    plain = build_form_data(chart(Viz.BAR), 1)
    stacked = build_form_data(chart(Viz.BAR, stacking=Stacking.STACKED), 1)
    percent = build_form_data(chart(Viz.BAR, stacking=Stacking.PERCENT), 1)
    assert "stack" not in plain
    assert stacked["stack"] == "Stack" and "contributionMode" not in stacked
    assert percent["contributionMode"] == "row" and percent["y_axis_format"] == ".0%"


def test_a_pie_can_be_solid():
    assert build_form_data(chart(Viz.PIE), 1)["donut"] is True
    assert build_form_data(chart(Viz.PIE, donut=False), 1)["donut"] is False


def test_magnitude_uses_one_hue_and_change_uses_opposite_colours():
    assert build_form_data(EXAMPLES[Viz.HEATMAP], 1)["linear_color_scheme"] == "novaBlues"
    form = build_form_data(EXAMPLES[Viz.WATERFALL], 1)
    assert form["increase_color"] != form["decrease_color"]


def test_a_calendar_shows_the_months_a_daily_window_covers():
    assert build_form_data(EXAMPLES[Viz.CALENDAR], 1)["steps"] == 12


def test_a_year_of_days_is_a_valid_window():
    assert chart(Viz.LINE, time_grain=TimeGrain.DAY, periods=365).periods == 365


@pytest.mark.parametrize("viz, fields, message", [
    (Viz.GAUGE, dict(metrics=[REVENUE, ORDERS]), "exactly one metric"),
    (Viz.SANKEY, dict(dimensions=["from"]), "exactly 2 dimensions"),
    (Viz.NETWORK, dict(dimensions=["a", "b", "c"]), "exactly 2 dimensions"),
    (Viz.TREE, dict(dimensions=["id"]), "2 to 3 dimensions"),
    (Viz.RADAR, dict(), "at least 3|3 to 9"),
    (Viz.SCATTER, dict(dimensions=["a"]), "2 to 3 metrics"),
    (Viz.FUNNEL, dict(), "exactly one dimension"),
    (Viz.HEATMAP, dict(dimensions=["a"]), "two axes"),
    (Viz.HEATMAP, dict(dimensions=["a", "b"], time_column="t", time_grain=TimeGrain.MONTH, time_axis=True), "two axes"),
    (Viz.BAR, dict(dimensions=["a"], time_axis=True), "time_axis needs time_column"),
    (Viz.BAR, dict(time_column="t", time_grain=TimeGrain.MONTH, time_axis=False), "needs a dimension"),
    (Viz.COMBO, dict(dimensions=["a"]), "line_metrics"),
    (Viz.HISTOGRAM, dict(metrics=[]), "value_column"),
    (Viz.HISTOGRAM, dict(metrics=[REVENUE], value_column="v"), "0 to 0|exactly 0"),
    (Viz.BOX_PLOT, dict(dimensions=["a"]), "distribute_across"),
    (Viz.CALENDAR, dict(), "time_column"),
    (Viz.COUNTRY_MAP, dict(dimensions=["a"], map_country="narnia"), "isn't a country map"),
    (Viz.COUNTRY_MAP, dict(dimensions=["a"]), "isn't a country map"),
    (Viz.PIVOT, dict(), "needs dimensions"),
    (Viz.WATERFALL, dict(), "x axis"),
    (Viz.BAR, dict(dimensions=["a"], metrics=[REVENUE, REVENUE]), "own label"),
])
def test_charts_that_couldnt_draw_are_refused_with_a_reason(viz, fields, message):
    with pytest.raises(ValidationError, match=message):
        ChartSpec(**(dict(key="c", title="C", viz=viz, metrics=[REVENUE]) | fields))


# --- What each chart looked like when someone actually opened it ---------------------------------

WINDOW = dict(time_column="order_date", time_grain=TimeGrain.MONTH, periods=12)


def test_a_bar_with_a_dimension_compares_it_across_the_window():
    """Rentals by class over 12 months has a bar per class, not a bar per month."""
    spec = chart(Viz.BAR, dimensions=["product"], **WINDOW)
    form = build_form_data(spec, 1)
    assert form["x_axis"] == "product"
    assert build_query_context(spec, 1)["queries"][0]["columns"] == ["product"]
    assert form["adhoc_filters"][0]["comparator"].startswith("DATEADD")  # still only the last 12 months


def test_a_ranking_over_a_window_is_ordered_by_its_value():
    spec = chart(Viz.BAR, dimensions=["product"], **WINDOW)
    assert build_form_data(spec, 1)["x_axis_sort"] == "Revenue"
    assert "orderby" in build_query_context(spec, 1)["queries"][0]


def test_time_can_be_asked_for_along_the_bottom():
    spec = chart(Viz.BAR, dimensions=["product"], time_axis=True, **WINDOW)
    form = build_form_data(spec, 1)
    assert (form["x_axis"], form["groupby"]) == ("order_date", ["product"])


def test_a_bar_with_no_dimension_is_a_trend():
    assert build_form_data(chart(Viz.BAR, dimensions=[], **WINDOW), 1)["x_axis"] == "order_date"


def test_a_line_always_has_time_along_the_bottom():
    assert build_form_data(chart(Viz.LINE, dimensions=["product"]), 1)["x_axis"] == "order_date"


def test_a_waterfall_steps_through_a_dimension_over_the_window():
    spec = ChartSpec(key="w", title="W", viz=Viz.WATERFALL, metrics=[REVENUE], dimensions=["channel"], **WINDOW)
    assert build_form_data(spec, 1)["x_axis"] == "channel"


def test_a_heatmap_of_two_dimensions_can_have_a_time_window():
    spec = ChartSpec(key="h", title="H", viz=Viz.HEATMAP, metrics=[REVENUE], dimensions=["a", "b"], **WINDOW)
    form = build_form_data(spec, 1)
    assert (form["x_axis"], form["groupby"]) == ("a", "b")


def test_a_heatmap_of_one_dimension_puts_time_on_the_other_axis():
    spec = ChartSpec(key="h", title="H", viz=Viz.HEATMAP, metrics=[REVENUE], dimensions=["a"], **WINDOW)
    assert build_form_data(spec, 1)["x_axis"] == "order_date"


def test_a_country_map_shades_by_value_rather_than_by_category():
    """Superset prefers a categorical scheme if there is one, which gives each region a colour
    that means nothing."""
    assert "color_scheme" not in build_form_data(EXAMPLES[Viz.COUNTRY_MAP], 1)


def test_a_table_uses_the_number_format_it_was_given():
    form = build_form_data(chart(Viz.TABLE, dimensions=["product"], y_axis_format=",d"), 1)
    assert form["column_config"] == {"Revenue": {"d3NumberFormat": ",d"}}


def test_a_kpi_can_carry_a_caption():
    assert build_form_data(chart(Viz.BIG_NUMBER, subtitle="Last 12 months"), 1)["subtitle"] == "Last 12 months"


def test_a_sheet_can_hold_a_block_of_text_next_to_a_chart():
    positions = build_positions("t", [[ChartSlot(chart_id=1, title="A", width=6), ChartSlot(text="## Note", width=6)]])
    block = next(item for item in positions.values() if isinstance(item, dict) and item.get("type") == "MARKDOWN")
    assert block["meta"]["code"] == "## Note"
    assert block["id"] in positions["ROW-0"]["children"]


@pytest.mark.parametrize("fields", [dict(width=6), dict(width=6, chart_id=1, text="both")])
def test_a_slot_holds_one_thing(fields):
    with pytest.raises(ValidationError, match="either"):
        ChartSlot(**fields)


def test_nothing_needs_javascript_compiled_in_the_browser():
    """The Handlebars chart does, which Superset's content security policy rightly blocks, so it
    only ever showed an error. It saves without complaint, so this keeps it out."""
    assert "handlebars" not in {viz.value for viz in Viz}


def test_text_on_a_sheet_survives_being_read_back():
    """A follow-up request rebuilds the layout from what's there, so anything not read back is lost."""
    positions = build_positions("t", [[ChartSlot(chart_id=1, title="A", width=6), ChartSlot(text="## Note", width=6, height=20)]])
    rows = rows_from_positions(positions)
    assert rows[0][0].chart_id == 1
    assert (rows[0][1].text, rows[0][1].height) == ("## Note", 20)
