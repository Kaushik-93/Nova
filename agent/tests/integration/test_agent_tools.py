"""The agent's tools against the real stack, called directly, with no model. This is the
plumbing under the agent, so a failure in the LLM tests can be told apart from these."""

import contextlib
import json
import uuid

import pytest
from langchain_openai import ChatOpenAI
from pydantic import ValidationError
from langgraph.checkpoint.memory import InMemorySaver

from agent.config import get_settings
from agent.dashboard_agent import build_agent, dashboard_key_for
from agent.db import apply_schema, create_pool
from agent.harness.audit import RunContext
from agent.harness.errors import NotFound, QueryFailure, SqlRejected
from agent.harness.validators import check_dataset
from agent.runtime import make_runtime
from agent.tools.planning import make_planning_tools
from agent.tools.skill_tools import make_source_tools
from agent.tools.sources import register_source
from agent.tools.sql_tools import make_schema_tools
from agent.tools.superset_payloads import ChartSpec
from agent.tools.superset_tools import make_builder_tools

pytestmark = pytest.mark.integration

ITEMS_SQL = """
    SELECT i.order_item_id, CAST(o.order_ts AS timestamp(3)) AS order_ts, r.region_name,
           i.quantity * i.unit_price_cents / 100.0 AS revenue
    FROM demo_shop.shop.order_items i
    JOIN demo_shop.shop.orders o ON o.order_id = i.order_id
    JOIN demo_shop.shop.regions r ON r.region_id = o.region_id
    WHERE o.status <> 'cancelled'
"""
FAN_OUT_SQL = """
    SELECT o.order_id, o.shipping_cents / 100.0 AS shipping
    FROM demo_shop.shop.orders o
    JOIN demo_shop.shop.order_items i ON i.order_id = o.order_id
"""


@pytest.fixture(scope="module")
def pool():
    pool = create_pool(get_settings().database_url)
    apply_schema(pool)
    register_source(pool, slug="demo-shop", display_name="Demo shop", catalog="demo_shop", dialect="postgres")
    register_source(pool, slug="demo-marketing", display_name="Demo marketing", catalog="demo_marketing", dialect="mysql")
    yield pool
    pool.close()


@pytest.fixture
def events():
    return []


@pytest.fixture
def runtime(pool, events):
    thread = uuid.uuid4().hex
    context = RunContext(run_id=f"tools-{thread[:8]}", user_id="tester")
    # Constructing a client makes no network call, and these tests never reach the model.
    model = ChatOpenAI(model="gpt-6-luna", api_key="unused", use_responses_api=True)
    runtime = make_runtime(get_settings(), pool, model, context=context, dashboard_key=dashboard_key_for(thread),
                           thread_id=thread, sink=events.append)
    yield runtime
    state, superset = runtime.state, runtime.superset
    with pool.connection() as conn:
        conn.execute("DELETE FROM dashboards WHERE thread_id = %s", (thread,))

    def remove(delete, object_id):
        with contextlib.suppress(NotFound):  # finishing may already have removed it
            delete(object_id)

    if state.dashboard:
        remove(superset.delete_dashboard, state.dashboard.id)
    for chart_id in state.charts:
        remove(superset.delete_chart, chart_id)
    for dataset_id in state.datasets.values():
        remove(superset.delete_dataset, dataset_id)
    runtime.close()


@pytest.fixture
def tools(runtime):
    return make_builder_tools(runtime)


def harness(runtime):
    return dict(executor=runtime.executor, policy=runtime.policy, context=runtime.context, budget=runtime.budget)


REVENUE = {"label": "Revenue", "sql": "SUM(revenue)"}
KPI = {"key": "kpi", "title": "Revenue", "viz": "big_number_total", "metrics": [REVENUE]}
TREND = {"key": "trend", "title": "Revenue by month", "viz": "echarts_timeseries_line", "metrics": [REVENUE],
         "time_column": "order_ts", "time_grain": "P1M", "periods": 12}


def build(tools, *specs):
    dataset = tools["superset_create_dataset"].invoke({"key": "sales", "sql": ITEMS_SQL, "grain": ["order_item_id"]})
    dataset_id = int(dataset.split("dataset_id=")[1].split(",")[0])
    chart_ids = []
    for spec in specs:
        reply = tools["superset_create_chart"].invoke({"dataset_id": dataset_id, "spec": spec})
        assert "chart_id=" in reply and "no rows" not in reply, reply
        chart_ids.append(int(reply.split("chart_id=")[1].split(",")[0].split()[0]))
    return dataset_id, chart_ids


def layout(chart_ids, width=6):
    return [[{"chart_id": chart_id, "width": width} for chart_id in chart_ids]]


# --- Datasets ----------------------------------------------------------------

def test_a_dataset_is_checked_and_returned_without_a_limit(runtime):
    checked = check_dataset(ITEMS_SQL, grain=["order_item_id"], **harness(runtime))
    assert checked.rows > 100_000
    assert "LIMIT" not in checked.sql


def test_a_join_that_multiplies_rows_is_caught(runtime):
    with pytest.raises(QueryFailure, match="multiplies rows"):
        check_dataset(FAN_OUT_SQL, grain=["order_id"], **harness(runtime))


def test_grain_columns_must_exist(runtime):
    with pytest.raises(QueryFailure, match="aren't in the query's output"):
        check_dataset(ITEMS_SQL, grain=["nope"], **harness(runtime))


def test_an_empty_dataset_is_caught(runtime):
    with pytest.raises(QueryFailure, match="no rows"):
        check_dataset(ITEMS_SQL + " AND o.status = 'nothing'", grain=["order_item_id"], **harness(runtime))


def test_dataset_sql_cannot_bypass_the_guard(runtime):
    with pytest.raises(SqlRejected):
        check_dataset("SELECT * FROM TABLE(demo_shop.system.query(query => 'select 1'))", grain=["x"],
                      **harness(runtime))


def test_the_tool_reports_failures_for_the_model_to_fix(tools):
    reply = tools["superset_create_dataset"].invoke({"key": "bad", "sql": FAN_OUT_SQL, "grain": ["order_id"]})
    assert reply.startswith("FAILED [sql]:")


# --- Building ----------------------------------------------------------------

def test_a_dashboard_can_be_built_read_changed_and_finished(tools, runtime):
    _, (kpi, trend) = build(tools, KPI, TREND)
    reply = tools["superset_create_dashboard"].invoke({"title": "Sales", "rows": layout([kpi, trend])})
    assert "2 charts" in reply

    current = json.loads(tools["superset_get_dashboard"].invoke({}))
    assert [[s["chart_id"] for s in row] for row in current["rows"]] == [[kpi, trend]]

    tools["superset_update_dashboard"].invoke({"rows": layout([trend], width=12), "title": "Sales v2"})
    assert runtime.superset.get_dashboard(runtime.state.dashboard.id).dashboard_title == "Sales v2"
    assert [c.id for c in runtime.superset.dashboard_charts(runtime.state.dashboard.id)] == [trend]

    assert tools["finish_dashboard"].invoke({"summary": "s", "definitions": ["d"]}) == "Finished."
    assert runtime.state.finished


def test_rebuilding_with_the_same_keys_updates_in_place(tools, runtime):
    _, first = build(tools, KPI, TREND)
    _, second = build(tools, KPI, TREND)
    assert first == second


def test_finish_refuses_a_chart_that_fails_then_gives_up_after_the_limit(tools, runtime):
    _, (good,) = build(tools, KPI)
    broken = {**KPI, "key": "broken", "title": "Broken", "metrics": [{"label": "Bad", "sql": "SUM(nothing_here)"}]}
    dataset_id = next(iter(runtime.state.datasets.values()))
    chart = runtime.superset.ensure_chart(ChartSpec(**broken), dataset_id=dataset_id,
                                          dashboard_key=runtime.state.dashboard_key)
    runtime.state.charts[chart.id] = ChartSpec(**broken)
    tools["superset_create_dashboard"].invoke({"title": "t", "rows": layout([good, chart.id])})

    for _ in range(runtime.settings.nova_chart_repair_rounds):
        reply = tools["finish_dashboard"].invoke({"summary": "s", "definitions": []})
        assert reply.startswith("Not finished") and "Broken" in reply
    assert tools["finish_dashboard"].invoke({"summary": "s", "definitions": []}) == "Finished."
    assert any("Broken failed" in warning for warning in runtime.state.warnings)


def test_a_hard_coded_date_cannot_become_a_dataset(runtime):
    with pytest.raises(QueryFailure, match="fixed date"):
        check_dataset(ITEMS_SQL + " AND o.order_ts >= TIMESTAMP '2025-11-01 00:00:00 UTC'", grain=["order_item_id"],
                      **harness(runtime))


def test_a_snapshot_on_its_own_dataset_needs_no_time_window(tools, runtime):
    """Regression: refusing this once pushed an agent to hard-code dates into its SQL."""
    _, (trend,) = build(tools, TREND)
    fleet = tools["superset_create_dataset"].invoke({"key": "products", "grain": ["product_id"], "sql": """
        SELECT product_id, category, list_price_cents / 100.0 AS revenue FROM demo_shop.shop.products"""})
    fleet_id = int(fleet.split("dataset_id=")[1].split(",")[0])
    snapshot = tools["superset_create_chart"].invoke({"dataset_id": fleet_id, "spec": {
        "key": "snapshot", "title": "Now", "viz": "big_number_total", "metrics": [{"label": "N", "sql": "COUNT(*)"}]}})
    snapshot_id = int(snapshot.split("chart_id=")[1].split(",")[0])
    tools["superset_create_dashboard"].invoke({"title": "t", "rows": layout([trend, snapshot_id])})
    assert tools["finish_dashboard"].invoke({"summary": "s", "definitions": []}) == "Finished."


def test_finish_refuses_a_window_that_cuts_off_the_last_day(tools, runtime):
    """Regression: an agent wrote 2025-11-01 : 2026-10-31, and Superset excludes the end."""
    cut_short = {**TREND, "periods": None, "fixed_dates": True, "time_range": "2025-11-01 : 2026-10-31"}
    _, (chart,) = build(tools, cut_short)
    tools["superset_create_dashboard"].invoke({"title": "t", "rows": layout([chart])})

    reply = tools["finish_dashboard"].invoke({"summary": "s", "definitions": []})

    assert reply.startswith("Not finished") and "cut short" in reply


def test_the_chart_tool_refuses_fixed_dates_from_the_model(tools):
    dataset, _ = build(tools, TREND)
    with pytest.raises(ValidationError, match="fixed dates"):
        tools["superset_create_chart"].invoke({"dataset_id": dataset, "spec": {**TREND, "periods": None,
                                                                               "time_range": "2025-11-01 : 2026-10-31"}})


def test_a_crowded_layout_wraps(tools, runtime):
    specs = [{**TREND, "key": f"t{n}", "title": f"Trend {n}"} for n in range(4)]
    _, charts = build(tools, *specs)
    tools["superset_create_dashboard"].invoke({"title": "t", "rows": layout(charts, width=3)})
    rows = json.loads(tools["superset_get_dashboard"].invoke({}))["rows"]
    assert [[slot["width"] for slot in row] for row in rows] == [[6, 6], [6, 6]]


def test_a_row_of_kpis_stays_compact(tools, runtime):
    _, (kpi, other) = build(tools, KPI, {**KPI, "key": "k2", "title": "Second"})
    tools["superset_create_dashboard"].invoke({"title": "t", "rows": [[{"chart_id": kpi, "width": 6, "height": 50},
                                                                       {"chart_id": other, "width": 6, "height": 50}]]})
    assert [slot["height"] for slot in json.loads(tools["superset_get_dashboard"].invoke({}))["rows"][0]] == [20, 20]


def test_finish_refuses_a_chart_covering_a_different_period(tools, runtime):
    _, (trend, all_time) = build(tools, TREND, {**KPI, "key": "all-time", "title": "All time"})
    tools["superset_create_dashboard"].invoke({"title": "t", "rows": layout([trend, all_time])})

    reply = tools["finish_dashboard"].invoke({"summary": "s", "definitions": []})

    assert reply.startswith("Not finished") and "All time" in reply and "different period" in reply


def test_a_dashboard_with_matching_windows_finishes(tools, runtime):
    kpi = {**KPI, "time_column": "order_ts", "time_grain": "P1M", "periods": 12}
    _, (trend, total) = build(tools, TREND, kpi)
    tools["superset_create_dashboard"].invoke({"title": "t", "rows": layout([trend, total])})
    assert tools["finish_dashboard"].invoke({"summary": "s", "definitions": []}) == "Finished."


def test_a_row_with_gaps_is_stretched_to_fill_the_grid(tools, runtime):
    _, (trend,) = build(tools, TREND)
    tools["superset_create_dashboard"].invoke({"title": "t", "rows": layout([trend], width=4)})
    assert json.loads(tools["superset_get_dashboard"].invoke({}))["rows"][0][0]["width"] == 12


def test_finish_refuses_a_filter_that_cannot_reach_a_chart(tools, runtime):
    with_region, (kpi,) = build(tools, TREND)
    no_region = tools["superset_create_dataset"].invoke({"key": "other", "grain": ["order_id"], "sql": """
        SELECT o.order_id, CAST(o.order_ts AS timestamp(3)) AS order_ts, o.tax_cents / 100.0 AS revenue
        FROM demo_shop.shop.orders o"""})
    other_id = int(no_region.split("dataset_id=")[1].split(",")[0])
    other = tools["superset_create_chart"].invoke({"dataset_id": other_id, "spec": {**TREND, "key": "o", "title": "No region"}})
    other_chart = int(other.split("chart_id=")[1].split(",")[0])
    tools["superset_create_dashboard"].invoke({"title": "t", "rows": layout([kpi, other_chart]),
                                               "filters": [{"name": "Region", "dataset_id": with_region, "column": "region_name"}]})

    reply = tools["finish_dashboard"].invoke({"summary": "s", "definitions": []})

    assert reply.startswith("Not finished")
    assert "No region" in reply and "can't reach this chart" in reply and "region_name" in reply


def test_a_filter_on_a_column_every_dataset_has_is_accepted(tools, runtime):
    dataset, (trend,) = build(tools, TREND)
    tools["superset_create_dashboard"].invoke({"title": "t", "rows": layout([trend]),
                                               "filters": [{"name": "Region", "dataset_id": dataset, "column": "region_name"}]})
    assert tools["finish_dashboard"].invoke({"summary": "s", "definitions": []}) == "Finished."


def test_each_finished_chart_is_announced_without_its_data(tools, events):
    _, (trend,) = build(tools, TREND)
    ready = [e for e in events if e["event"] == "chart_ready"]
    assert ready == [{"event": "chart_ready", "agent": "main", "run_id": ready[0]["run_id"], "chart_id": trend,
                      "key": "trend", "title": "Revenue by month", "viz": "echarts_timeseries_line"}]


def test_the_dashboard_can_be_shown_as_soon_as_it_exists(tools, runtime, events, pool):
    _, (trend,) = build(tools, TREND)
    tools["superset_create_dashboard"].invoke({"title": "Sales", "rows": layout([trend])})

    ready = next(e for e in events if e["event"] == "dashboard_ready")
    assert ready["slug"] == runtime.state.dashboard_key
    with pool.connection() as conn:
        row = conn.execute("SELECT title, embed_uuid FROM dashboards WHERE slug = %s", (ready["slug"],)).fetchone()
    assert row[0] == "Sales" and row[1], "ready to embed before verification has run"


def test_a_failed_chart_is_not_announced(tools, events):
    dataset, _ = build(tools, KPI)
    events.clear()
    tools["superset_create_chart"].invoke({"dataset_id": dataset, "spec": {
        **KPI, "key": "bad", "metrics": [{"label": "Bad", "sql": "SUM(no_such_column)"}]}})
    assert not any(e["event"] == "chart_ready" for e in events)


def test_inspecting_shows_filters_windows_and_sql(tools, runtime):
    dataset, (trend,) = build(tools, TREND)
    tools["superset_create_dashboard"].invoke({"title": "t", "rows": layout([trend]),
                                               "filters": [{"name": "Region", "dataset_id": dataset, "column": "region_name"}]})

    report = json.loads(tools["superset_inspect_dashboard"].invoke({}))

    region = next(f for f in report["filters"] if f["name"] == "Region")
    assert region == {"name": "Region", "type": "filter_select", "column": "region_name", "missing_from": []}
    assert report["charts"][0]["time_window"].endswith("DATETRUNC(DATETIME('today'), month)")
    assert "demo_shop.shop.order_items" in report["datasets"][0]["sql"]


def test_inspecting_names_the_charts_a_filter_misses(tools, runtime):
    with_region, (trend,) = build(tools, TREND)
    other = tools["superset_create_dataset"].invoke({"key": "other", "grain": ["order_id"], "sql": """
        SELECT o.order_id, CAST(o.order_ts AS timestamp(3)) AS order_ts, o.tax_cents / 100.0 AS revenue
        FROM demo_shop.shop.orders o"""})
    other_chart = tools["superset_create_chart"].invoke({"dataset_id": int(other.split("dataset_id=")[1].split(",")[0]),
                                                         "spec": {**TREND, "key": "o", "title": "No region"}})
    tools["superset_create_dashboard"].invoke({
        "title": "t", "rows": layout([trend, int(other_chart.split("chart_id=")[1].split(",")[0])]),
        "filters": [{"name": "Region", "dataset_id": with_region, "column": "region_name"}]})

    report = json.loads(tools["superset_inspect_dashboard"].invoke({}))

    assert next(f for f in report["filters"] if f["name"] == "Region")["missing_from"] == ["No region"]


def test_a_guessed_skill_name_resolves_through_the_plan(runtime):
    if not runtime.store.exists("demo-shop"):
        pytest.skip("needs the demo skill")
    runtime.state.plan = {"sources": ["demo-shop"]}
    tools = {tool.name: tool for tool in make_source_tools(runtime)}
    assert not tools["load_skill"].invoke({"name": "vehicle_rentals"}).startswith("FAILED")


def test_build_status_shows_what_exists_with_keys(tools, runtime):
    dataset_id, (trend,) = build(tools, TREND)
    tools["superset_create_dashboard"].invoke({"title": "t", "rows": layout([trend])})

    status = json.loads(tools["superset_build_status"].invoke({}))

    assert status["datasets"][0]["key"] == "sales"
    assert status["datasets"][0]["dataset_id"] == dataset_id
    assert "order_ts" in status["datasets"][0]["columns"]
    assert status["charts"] == [{"key": "trend", "chart_id": trend, "title": "Revenue by month",
                                 "dataset_id": dataset_id, "on_dashboard": True}]


def test_build_status_survives_a_new_run(tools, runtime):
    """It reads Superset, so a follow-up or a resumed run sees the same thing."""
    build(tools, TREND)
    fresh = make_builder_tools(runtime)
    assert json.loads(fresh["superset_build_status"].invoke({}))["charts"][0]["key"] == "trend"


def test_a_runaway_number_of_datasets_is_refused(tools, runtime, monkeypatch):
    monkeypatch.setattr("agent.tools.superset_tools.MAX_DATASETS", 1)
    build(tools, KPI)
    reply = tools["superset_create_dataset"].invoke({"key": "another", "sql": ITEMS_SQL, "grain": ["order_item_id"]})
    assert reply.startswith("FAILED") and "sales" in reply
    assert "dataset_id=" in tools["superset_create_dataset"].invoke(
        {"key": "sales", "sql": ITEMS_SQL, "grain": ["order_item_id"]}), "updating is still fine"


def test_finishing_removes_what_the_dashboard_does_not_use(tools, runtime):
    _, (kept, dropped) = build(tools, TREND, {**TREND, "key": "other", "title": "Other"})
    spare = tools["superset_create_dataset"].invoke({"key": "spare", "sql": ITEMS_SQL, "grain": ["order_item_id"]})
    spare_id = int(spare.split("dataset_id=")[1].split(",")[0])
    tools["superset_create_dashboard"].invoke({"title": "t", "rows": layout([kept])})

    assert tools["finish_dashboard"].invoke({"summary": "s", "definitions": []}) == "Finished."

    remaining = {d.id for d in runtime.superset.datasets_named(runtime.state.dashboard_key)}
    assert spare_id not in remaining
    with pytest.raises(NotFound):
        runtime.superset.get_chart(dropped)
    assert runtime.superset.get_chart(kept).id == kept


def test_finish_needs_a_dashboard(tools):
    assert tools["finish_dashboard"].invoke({"summary": "s", "definitions": []}).startswith("FAILED")


def test_chart_data_marks_values_as_untrusted(tools):
    _, (kpi,) = build(tools, KPI)
    assert "<untrusted_data>" in tools["superset_get_chart_data"].invoke({"chart_id": kpi})


def test_superset_writes_are_audited(tools, runtime):
    build(tools, KPI)
    targets = [entry["target"] for entry in runtime.audit.for_run(runtime.context.run_id)]
    assert "POST /api/v1/dataset/" in targets
    assert "POST /api/v1/chart/" in targets


def test_preparing_embedding_keeps_the_token_out_of_the_model(tools, runtime):
    _, (kpi,) = build(tools, KPI)
    tools["superset_create_dashboard"].invoke({"title": "t", "rows": layout([kpi])})
    assert tools["superset_guest_token"].invoke({}) == "Embedding is ready."
    assert runtime.state.embed_uuid


# --- Sources and schema ------------------------------------------------------

def test_sources_are_listed_without_connection_details(runtime):
    tools = {tool.name: tool for tool in make_source_tools(runtime)}
    listing = tools["list_sources"].invoke({})
    assert "demo_shop" in listing
    assert "password" not in listing.lower() and "jdbc" not in listing.lower()


@pytest.mark.parametrize("name", ["demo-shop", "demo_shop", "Demo Shop"])
def test_a_skill_is_found_by_any_of_its_likely_names(runtime, name):
    tools = {tool.name: tool for tool in make_source_tools(runtime)}
    if runtime.store.exists("demo-shop"):
        assert not tools["load_skill"].invoke({"name": name}).startswith("FAILED")
    else:
        assert "no skill named" in tools["load_skill"].invoke({"name": name})


def test_an_unknown_skill_lists_the_real_ones(runtime):
    tools = {tool.name: tool for tool in make_source_tools(runtime)}
    reply = tools["load_skill"].invoke({"name": "nope"})
    assert reply.startswith("FAILED") and "demo-shop" in reply


def test_chart_data_shows_the_window_to_the_verifier(tools):
    _, (trend,) = build(tools, TREND)
    assert "time window: 20" in tools["superset_get_chart_data"].invoke({"chart_id": trend})


def test_sampling_leaves_out_personal_columns(runtime):
    tools = {tool.name: tool for tool in make_schema_tools(runtime.executor, policy=runtime.policy,
                                                           context=runtime.context, budget=runtime.budget)}
    reply = tools["sample_table"].invoke({"table": "demo_shop.shop.customers", "rows": 2})
    assert "customer_id" in reply
    assert "email" not in reply and "@" not in reply and "first_name" not in reply


@pytest.mark.parametrize("table", ["system.runtime.queries", "demo_shop.information_schema.tables", "evil; DROP TABLE x"])
def test_sampling_refuses_anything_but_a_source_table(runtime, table):
    tools = {tool.name: tool for tool in make_schema_tools(runtime.executor, policy=runtime.policy,
                                                           context=runtime.context, budget=runtime.budget)}
    assert tools["sample_table"].invoke({"table": table}).startswith("FAILED")


def test_introspecting_a_source_that_is_not_connected_is_refused(runtime):
    tools = {tool.name: tool for tool in make_schema_tools(runtime.executor, policy=runtime.policy,
                                                           context=runtime.context, budget=runtime.budget)}
    assert tools["introspect_schema"].invoke({"catalog": "system"}).startswith("FAILED")


# --- Plans -------------------------------------------------------------------

PLAN = {"title": "Sales", "goal": "g", "sources": ["demo-shop"],
        "charts": [{"key": "k", "title": "t", "viz": "big_number_total", "metric": "revenue"}]}


def test_with_approval_off_a_plan_goes_straight_through(runtime):
    tools = {tool.name: tool for tool in make_planning_tools(runtime, require_approval=False)}
    assert "Plan approved" in tools["submit_plan"].invoke(PLAN)
    assert runtime.state.plan["title"] == "Sales"


@pytest.mark.parametrize("name", ["superset_get_dashboard", "render_dashboard_screenshot", "superset_guest_token"])
def test_tools_with_no_arguments_offer_none_to_the_model(tools, name):
    assert tools[name].args == {}


def test_list_sources_offers_no_arguments(runtime):
    tools = {tool.name: tool for tool in make_source_tools(runtime)}
    assert tools["list_sources"].args == {}


def test_the_agent_builds_with_skills_and_subagents(runtime):
    agent = build_agent(runtime, require_approval=False, checkpointer=InMemorySaver())
    assert agent is not None
