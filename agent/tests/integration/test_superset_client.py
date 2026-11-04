"""Phase 2, against the real Superset and Trino."""

import json
import os
import uuid
from datetime import date, datetime, timezone

import httpx
import pytest

from agent.config import get_settings
from agent.harness.errors import NotFound, QueryFailure
from agent.tools.superset_client import SupersetClient
from agent.tools.superset_payloads import (ChartSlot, ChartSpec, Metric, SelectFilter, TimeFilter, TimeGrain, Viz,
                                           rows_from_positions)

pytestmark = pytest.mark.integration

SALES_SQL = """
    SELECT CAST(o.order_ts AS timestamp(3)) AS order_ts,
           r.region_name,
           p.category,
           i.quantity * i.unit_price_cents / 100.0 AS revenue
    FROM demo_shop.shop.orders o
    JOIN demo_shop.shop.order_items i ON i.order_id = o.order_id
    JOIN demo_shop.shop.products p ON p.product_id = i.product_id
    JOIN demo_shop.shop.regions r ON r.region_id = o.region_id
    WHERE o.status <> 'cancelled' AND o.deleted_at IS NULL
"""
REVENUE = Metric(label="Revenue", sql="SUM(revenue)")


@pytest.fixture(scope="module")
def superset():
    settings = get_settings()
    with SupersetClient(settings.superset_url, settings.superset_admin_user, settings.superset_admin_password) as client:
        yield client


@pytest.fixture(scope="module")
def database_id(superset):
    return superset.database_id("Nova Trino")


class Built:
    """Tracks what a test creates, and removes it even if the test fails."""

    def __init__(self, superset, database_id):
        self.superset = superset
        self.database_id = database_id
        self.key = f"test-{uuid.uuid4().hex[:8]}"
        self.dashboards, self.charts, self.datasets = [], [], []

    def dataset(self, sql=SALES_SQL, name="sales"):
        dataset = self.superset.ensure_dataset(key=f"{self.key}/{name}", name=f"{self.key}_{name}", sql=sql,
                                               database_id=self.database_id)
        if dataset.id not in self.datasets:
            self.datasets.append(dataset.id)
        return dataset

    def chart(self, spec, dataset):
        chart = self.superset.ensure_chart(spec, dataset_id=dataset.id, dashboard_key=self.key)
        if chart.id not in self.charts:
            self.charts.append(chart.id)
        return chart

    def dashboard(self, **kwargs):
        dashboard = self.superset.ensure_dashboard(slug=self.key, title=kwargs.pop("title", "Test"), **kwargs)
        if dashboard.id not in self.dashboards:
            self.dashboards.append(dashboard.id)
        return dashboard

    def clean_up(self):
        for dashboard_id in self.dashboards:
            self.superset.delete_dashboard(dashboard_id)
        for chart_id in self.charts:
            self.superset.delete_chart(chart_id)
        for dataset_id in self.datasets:
            self.superset.delete_dataset(dataset_id)


@pytest.fixture
def built(superset, database_id):
    tracker = Built(superset, database_id)
    yield tracker
    tracker.clean_up()


def test_finds_the_trino_database(database_id):
    assert database_id > 0


def test_an_unknown_database_is_not_found(superset):
    with pytest.raises(NotFound):
        superset.database_id("no such database")


def test_wrong_credentials_are_refused():
    settings = get_settings()
    with SupersetClient(settings.superset_url, "admin", "wrong") as client:
        with pytest.raises(Exception) as caught:
            client.login()
    assert settings.superset_admin_password not in str(caught.value)


def test_a_virtual_dataset_over_trino_gets_its_columns(superset, built):
    dataset = built.dataset()
    columns = {c["column_name"]: c for c in superset.dataset_columns(dataset.id)}

    assert set(columns) == {"order_ts", "region_name", "category", "revenue"}
    assert columns["order_ts"]["is_dttm"] is True


def test_ensure_dataset_updates_in_place(superset, built):
    first = built.dataset()
    second = built.dataset(sql=SALES_SQL.replace("r.region_name,", "r.region_name, r.country_code,"))

    assert second.id == first.id
    assert "country_code" in {c["column_name"] for c in superset.dataset_columns(first.id)}


def test_an_invalid_dataset_is_refused_and_leaves_nothing_behind(superset, built):
    with pytest.raises(QueryFailure, match="check its SQL"):
        built.dataset(sql="this is not sql", name="broken")
    assert superset.find_dataset(f"{built.key}/broken") is None


ALL_CHARTS = [
    ChartSpec(key="kpi", title="Revenue", viz=Viz.BIG_NUMBER, metrics=[REVENUE]),
    ChartSpec(key="kpi-trend", title="Revenue trend KPI", viz=Viz.BIG_NUMBER_TREND, metrics=[REVENUE],
              time_column="order_ts", time_grain=TimeGrain.MONTH),
    ChartSpec(key="line", title="Revenue by month", viz=Viz.LINE, metrics=[REVENUE], time_column="order_ts",
              time_grain=TimeGrain.MONTH),
    ChartSpec(key="area", title="Revenue by quarter", viz=Viz.AREA, metrics=[REVENUE], time_column="order_ts",
              time_grain=TimeGrain.QUARTER, dimensions=["region_name"]),
    ChartSpec(key="bar", title="Top categories", viz=Viz.BAR, metrics=[REVENUE], dimensions=["category"]),
    ChartSpec(key="pie", title="Revenue by region", viz=Viz.PIE, metrics=[REVENUE], dimensions=["region_name"]),
    ChartSpec(key="table", title="Category detail", viz=Viz.TABLE, metrics=[REVENUE],
              dimensions=["category", "region_name"], row_limit=20),
]


@pytest.mark.parametrize("spec", ALL_CHARTS, ids=lambda s: s.viz.value)
def test_every_chart_type_returns_data_from_superset(superset, built, spec):
    chart = built.chart(spec, built.dataset())
    data = superset.chart_data(chart.id)

    assert data.row_count > 0
    assert "Revenue" in data.columns
    assert all(row["Revenue"] is not None for row in data.rows)


def test_time_grain_really_groups_the_data(superset, built):
    dataset = built.dataset()
    span = "2024-01-01 : 2026-01-01"
    monthly = built.chart(ChartSpec(key="m", title="m", viz=Viz.LINE, metrics=[REVENUE], time_column="order_ts",
                                    time_grain=TimeGrain.MONTH, time_range=span, fixed_dates=True), dataset)
    quarterly = built.chart(ChartSpec(key="q", title="q", viz=Viz.LINE, metrics=[REVENUE], time_column="order_ts",
                                      time_grain=TimeGrain.QUARTER, time_range=span, fixed_dates=True), dataset)

    months, quarters = superset.chart_data(monthly.id), superset.chart_data(quarterly.id)
    assert months.row_count == 24
    assert quarters.row_count == 8
    month_total = sum(r["Revenue"] for r in months.rows)
    quarter_total = sum(r["Revenue"] for r in quarters.rows)
    assert month_total == pytest.approx(quarter_total, rel=1e-6)


def test_a_time_range_filter_narrows_the_query(superset, built):
    dataset = built.dataset()
    everything = built.chart(ChartSpec(key="all", title="all", viz=Viz.BIG_NUMBER, metrics=[REVENUE],
                                       time_column="order_ts"), dataset)
    one_year = built.chart(ChartSpec(key="yr", title="yr", viz=Viz.BIG_NUMBER, metrics=[REVENUE],
                                     time_column="order_ts", time_range="Last year"), dataset)

    total = superset.chart_data(everything.id).rows[0]["Revenue"]
    recent = superset.chart_data(one_year.id).rows[0]["Revenue"]
    assert 0 < recent < total


def test_a_chart_with_a_bad_metric_reports_a_query_failure(superset, built):
    chart = built.chart(ChartSpec(key="bad", title="bad", viz=Viz.BIG_NUMBER,
                                  metrics=[Metric(label="Bad", sql="SUM(no_such_column)")]), built.dataset())
    with pytest.raises(QueryFailure):
        superset.chart_data(chart.id)


@pytest.mark.parametrize("grain", list(TimeGrain))
def test_every_grain_ends_at_the_last_complete_period(superset, built, grain):
    chart = built.chart(ChartSpec(key=f"g{grain.value}", title="t", viz=Viz.LINE, metrics=[REVENUE],
                                  time_column="order_ts", time_grain=grain, periods=2), built.dataset())
    assert superset.chart_data(chart.id).row_count == 2


def test_a_monthly_trend_leaves_out_the_month_in_progress(superset, built):
    today = date.today()
    this_month = int(datetime(today.year, today.month, 1, tzinfo=timezone.utc).timestamp() * 1000)
    dataset = built.dataset()
    chart = built.chart(ChartSpec(key="trend", title="t", viz=Viz.LINE, metrics=[REVENUE], time_column="order_ts",
                                  time_grain=TimeGrain.MONTH), dataset)
    partial = built.chart(ChartSpec(key="partial", title="t", viz=Viz.LINE, metrics=[REVENUE], time_column="order_ts",
                                    time_grain=TimeGrain.MONTH, include_partial_period=True), dataset)

    assert max(r["order_ts"] for r in superset.chart_data(chart.id).rows) < this_month
    assert max(r["order_ts"] for r in superset.chart_data(partial.id).rows) == this_month


def test_periods_are_honoured_when_the_partial_period_is_included(superset, built):
    chart = built.chart(ChartSpec(key="incl", title="t", viz=Viz.LINE, metrics=[REVENUE], time_column="order_ts",
                                  time_grain=TimeGrain.MONTH, periods=12, include_partial_period=True), built.dataset())
    assert superset.chart_data(chart.id).row_count == 12


def test_chart_data_reports_the_exact_window_it_covers(superset, built):
    chart = built.chart(ChartSpec(key="w", title="t", viz=Viz.LINE, metrics=[REVENUE], time_column="order_ts",
                                  time_grain=TimeGrain.MONTH, time_range="2025-01-01 : 2025-04-01", fixed_dates=True),
                        built.dataset())
    data = superset.chart_data(chart.id)
    assert data.window_start == "2025-01-01 00:00:00"
    assert data.window_end == "2025-04-01 00:00:00"


def test_a_kpi_can_cover_the_same_complete_periods_as_a_trend(superset, built):
    dataset = built.dataset()
    kpi = built.chart(ChartSpec(key="k", title="k", viz=Viz.BIG_NUMBER, metrics=[REVENUE], time_column="order_ts",
                                time_grain=TimeGrain.MONTH, periods=12), dataset)
    trend = built.chart(ChartSpec(key="t", title="t", viz=Viz.LINE, metrics=[REVENUE], time_column="order_ts",
                                  time_grain=TimeGrain.MONTH, periods=12), dataset)
    total = superset.chart_data(kpi.id).rows[0]["Revenue"]
    assert total == pytest.approx(sum(r["Revenue"] for r in superset.chart_data(trend.id).rows), rel=1e-6)


def test_ensure_chart_updates_in_place(superset, built):
    dataset = built.dataset()
    spec = ChartSpec(key="same", title="Before", viz=Viz.BIG_NUMBER, metrics=[REVENUE])
    first = built.chart(spec, dataset)
    second = built.chart(spec.model_copy(update={"title": "After", "viz": Viz.TABLE}), dataset)

    assert second.id == first.id
    assert second.slice_name == "After"
    assert second.viz_type == "table"


def _sales_dashboard(built):
    dataset = built.dataset()
    kpi = built.chart(ALL_CHARTS[0], dataset)
    line = built.chart(ALL_CHARTS[2], dataset)
    pie = built.chart(ALL_CHARTS[5], dataset)
    rows = [[ChartSlot(chart_id=kpi.id, title=kpi.slice_name, width=12)],
            [ChartSlot(chart_id=line.id, title=line.slice_name, width=8),
             ChartSlot(chart_id=pie.id, title=pie.slice_name, width=4)]]
    filters = [TimeFilter(), SelectFilter(name="Region", dataset_id=dataset.id, column="region_name")]
    return built.dashboard(title="Sales overview", rows=rows, filters=filters), dataset, (kpi, line, pie)


def test_builds_a_dashboard_with_layout_and_filters_then_deletes_it(superset, built):
    dashboard, dataset, charts = _sales_dashboard(built)

    on_dashboard = superset.dashboard_charts(dashboard.id)
    assert {c.id for c in on_dashboard} == {c.id for c in charts}

    detail = superset.dashboard_detail(dashboard.id)
    assert detail["published"] is True
    assert detail["dashboard_title"] == "Sales overview"
    positions = json.loads(detail["position_json"])
    assert positions["ROW-1"]["children"] == [f"CHART-{charts[1].id}", f"CHART-{charts[2].id}"]
    assert positions[f"CHART-{charts[1].id}"]["meta"]["uuid"], "Superset fills this in"
    filters = json.loads(detail["json_metadata"])["native_filter_configuration"]
    assert {f["filterType"] for f in filters} == {"filter_time", "filter_select"}

    superset.delete_dashboard(dashboard.id)
    built.dashboards.remove(dashboard.id)
    with pytest.raises(NotFound):
        superset.get_dashboard(dashboard.id)


def test_rebuilding_a_dashboard_duplicates_nothing(superset, built):
    first, dataset, charts = _sales_dashboard(built)
    second, dataset_again, charts_again = _sales_dashboard(built)

    assert second.id == first.id
    assert dataset_again.id == dataset.id
    assert [c.id for c in charts_again] == [c.id for c in charts]
    assert len(superset.dashboard_charts(first.id)) == 3


def test_dropping_a_chart_from_the_layout_removes_it_from_the_dashboard(superset, built):
    dashboard, _, (kpi, line, pie) = _sales_dashboard(built)
    built.dashboard(title="Sales overview", rows=[[ChartSlot(chart_id=kpi.id, title=kpi.slice_name, width=12)]])
    assert {c.id for c in superset.dashboard_charts(dashboard.id)} == {kpi.id}


def test_changing_the_layout_keeps_the_filters_unless_told_otherwise(superset, built):
    dashboard, _, (kpi, _, _) = _sales_dashboard(built)
    built.dashboard(title="Sales overview", rows=[[ChartSlot(chart_id=kpi.id, title=kpi.slice_name, width=12)]])
    metadata = json.loads(superset.dashboard_detail(dashboard.id)["json_metadata"])
    assert len(metadata["native_filter_configuration"]) == 2


def test_a_layout_reads_back_the_same(superset, built):
    dashboard, _, (kpi, line, pie) = _sales_dashboard(built)
    positions = json.loads(superset.dashboard_detail(dashboard.id)["position_json"])
    rows = rows_from_positions(positions)
    assert [[slot.chart_id for slot in row] for row in rows] == [[kpi.id], [line.id, pie.id]]
    assert [slot.width for slot in rows[1]] == [8, 4]


def test_an_embedded_dashboard_is_readable_with_a_guest_token_only(superset, built):
    dashboard, dataset, (kpi, _, _) = _sales_dashboard(built)
    embed_uuid = superset.enable_embedding(dashboard.id, [os.environ["NOVA_UI_ORIGIN"]])
    token = superset.guest_token(embed_uuid, username="nova-test")
    url = get_settings().superset_url

    guest = httpx.get(f"{url}/api/v1/dashboard/{dashboard.id}/charts", headers={"X-GuestToken": token}, timeout=30)
    assert guest.status_code == 200, guest.text
    assert len(guest.json()["result"]) == 3

    anonymous = httpx.get(f"{url}/api/v1/dashboard/{dashboard.id}/charts", timeout=30)
    assert anonymous.status_code in {401, 403, 404}


def _chart_query(superset, chart_id, dashboard_id):
    """The request the embedded frontend makes to load one chart."""
    context = json.loads(superset.chart_detail(chart_id)["query_context"])
    context["form_data"] |= {"slice_id": chart_id, "dashboardId": dashboard_id}
    return context


def _load_as_guest(context, token, dashboard_id):
    return httpx.post(f"{get_settings().superset_url}/api/v1/chart/data", json=context,
                      params={"dashboard_id": dashboard_id}, headers={"X-GuestToken": token}, timeout=60)


def test_a_guest_can_load_the_data_for_their_dashboard(superset, built):
    # Listing charts isn't enough: without this, an embedded dashboard shows only
    # errors. Found by loading one in a browser; this keeps it covered.
    dashboard, _, (kpi, _, _) = _sales_dashboard(built)
    token = superset.guest_token(superset.enable_embedding(dashboard.id, []), username="nova-test")

    response = _load_as_guest(_chart_query(superset, kpi.id, dashboard.id), token, dashboard.id)

    assert response.status_code == 200, response.text
    assert response.json()["result"][0]["data"][0]["Revenue"] > 0


def test_a_guest_cannot_load_a_chart_from_another_dashboard(superset, built):
    dashboard, dataset, _ = _sales_dashboard(built)
    token = superset.guest_token(superset.enable_embedding(dashboard.id, []), username="nova-test")
    private = built.chart(ChartSpec(key="private", title="Private", viz=Viz.BIG_NUMBER, metrics=[REVENUE]), dataset)

    response = _load_as_guest(_chart_query(superset, private.id, dashboard.id), token, dashboard.id)

    assert response.status_code == 403, response.text


def test_rls_in_a_guest_token_really_filters_the_data(superset, built):
    dashboard, dataset, (kpi, _, _) = _sales_dashboard(built)
    embed = superset.enable_embedding(dashboard.id, [])
    context = _chart_query(superset, kpi.id, dashboard.id)

    def revenue(rls):
        token = superset.guest_token(embed, username="nova-test", rls=rls)
        context["force"] = True
        response = _load_as_guest(context, token, dashboard.id)
        assert response.status_code == 200, response.text
        return response.json()["result"][0]["data"][0]["Revenue"]

    everyone = revenue([])
    germany = revenue([{"dataset": dataset.id, "clause": "region_name = 'Germany'"}])
    assert 0 < germany < everyone * 0.2


def test_enable_embedding_keeps_the_same_uuid(superset, built):
    dashboard, _, _ = _sales_dashboard(built)
    origin = os.environ["NOVA_UI_ORIGIN"]
    assert superset.enable_embedding(dashboard.id, [origin]) == superset.enable_embedding(dashboard.id, [origin])
