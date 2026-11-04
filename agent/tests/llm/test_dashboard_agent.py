"""Prompt to dashboard, with the real model, on the real stack. Run with `make test-llm`.

These tests share one conversation, in order, because each build costs real money:
plan -> approve an edited plan -> build -> follow-up. `run_dashboard` builds a new agent on
every call, so each step also proves a run continues from its checkpoint alone.

Numbers are checked against SQL written here, independently of what the agent generated.
"""

import contextlib
import json
import uuid
from datetime import date

import httpx
import pytest

from agent.config import get_settings
from agent.dashboard_agent import RunStatus, build_agent, dashboard_key_for, run_dashboard
from agent.db import apply_schema, create_checkpointer, create_langgraph_pool, create_pool, create_store
from agent.harness.audit import AuditLog, RunContext
from agent.harness.budgets import BudgetLimits, RunBudget
from agent.harness.errors import NotFound
from agent.harness.executor import SqlExecutor
from agent.harness.guardrails import SqlPolicy
from agent.harness.validators import chart_time_window
from agent.llm import build_model
from agent.runtime import make_runtime
from agent.subagents.profiler import generate_skill
from agent.tools.renderer import render_dashboard
from agent.tools.skill_store import SkillStore
from agent.tools.sources import list_sources, register_source
from agent.tools.superset_client import SupersetClient

pytestmark = [pytest.mark.llm, pytest.mark.integration]

SALES_PROMPT = "Show me a sales overview for the last 12 months with revenue trend, top products and revenue by region"
THREAD = f"test-sales-{uuid.uuid4().hex[:10]}"
EDITED_TITLE = "Quarterly Business Review"


@pytest.fixture(scope="module")
def settings():
    return get_settings()


@pytest.fixture(scope="module")
def pool(settings):
    pool = create_pool(settings.database_url)
    apply_schema(pool)
    yield pool
    pool.close()


@pytest.fixture(scope="module")
def graph_pool(settings):
    pool = create_langgraph_pool(settings.database_url)
    yield pool
    pool.close()


@pytest.fixture(scope="module")
def checkpointer(graph_pool):
    return create_checkpointer(graph_pool)


@pytest.fixture(scope="module")
def store(graph_pool):
    return create_store(graph_pool)


@pytest.fixture(scope="module")
def model(settings):
    return build_model(settings)


@pytest.fixture(scope="module")
def superset(settings):
    with SupersetClient(settings.superset_url, settings.superset_admin_user, settings.superset_admin_password) as client:
        yield client


def context(name: str) -> RunContext:
    return RunContext(run_id=f"llm-{name}-{uuid.uuid4().hex[:8]}", user_id="tester")


@pytest.fixture(scope="module")
def skills(settings, pool, model):
    """The shared demo skills, generated once if they don't exist yet."""
    store = SkillStore(settings.nova_skills_dir, pool)
    executor = SqlExecutor(settings.trino_url, AuditLog(pool))
    for slug, catalog, dialect in (("demo-shop", "demo_shop", "postgres"), ("demo-marketing", "demo_marketing", "mysql")):
        source_id = register_source(pool, slug=slug, display_name=slug, catalog=catalog, dialect=dialect)
        if not store.exists(slug):
            generate_skill(model=model, store=store, executor=executor, source_id=source_id, slug=slug, catalog=catalog,
                           dialect=dialect, context=context("skill"),
                           budget=RunBudget(BudgetLimits(max_steps=60, max_tokens=500_000, max_sql_queries=300)))
    return store


@pytest.fixture(scope="module", autouse=True)
def clean_up(superset, pool):
    yield
    slug = dashboard_key_for(THREAD)
    dashboard = superset.find_dashboard(slug)
    if dashboard:
        superset.delete_dashboard(dashboard.id)
    for dataset in superset.datasets_named(f"{slug}__"):
        for chart in superset.charts_on_dataset(dataset.id):
            with contextlib.suppress(NotFound):
                superset.delete_chart(chart.id)
        superset.delete_dataset(dataset.id)
    with pool.connection() as conn:
        conn.execute("DELETE FROM dashboards WHERE slug = %s", (slug,))


@pytest.fixture(scope="module")
def run(settings, pool, model, checkpointer, store, skills):
    def go(thread=THREAD, *, name="run", overrides=None, **kwargs):
        return run_dashboard(settings=settings.model_copy(update=overrides or {}), pool=pool, model=model,
                             checkpointer=checkpointer, store=store, context=context(name), thread_id=thread, **kwargs)
    return go


@pytest.fixture(scope="module")
def skill_versions_before(skills):
    return {source.slug: skills.read_frontmatter(source.slug).version for source in list_sources(skills._pool)
            if skills.exists(source.slug)}


@pytest.fixture(scope="module")
def awaiting(run, skill_versions_before):
    return run(prompt=SALES_PROMPT, require_approval=True, name="plan")


@pytest.fixture(scope="module")
def finished(run, awaiting):
    assert awaiting.status is RunStatus.AWAITING_APPROVAL, awaiting
    edited = {**awaiting.plan, "title": EDITED_TITLE}
    return run(resume={"plan": edited}, require_approval=True, name="build")


def chart_rows(superset, dashboard_id):
    return {chart.id: (chart, superset.chart_data(chart.id, force=True)) for chart in superset.dashboard_charts(dashboard_id)}


def numeric_total(data) -> float:
    """Metrics come after the group-by columns, so the last column is the value."""
    return sum(row[data.columns[-1]] for row in data.rows)


# --- Plan ----------------------------------------------------------------------

def test_the_agent_stops_for_approval_before_building_anything(awaiting, superset):
    assert awaiting.status is RunStatus.AWAITING_APPROVAL
    assert awaiting.plan["charts"]
    assert awaiting.dashboard_id is None
    assert superset.find_dashboard(dashboard_key_for(THREAD)) is None


def test_the_plan_covers_what_was_asked(awaiting):
    text = json.dumps(awaiting.plan).lower()
    assert "product" in text and "region" in text
    assert any(chart["time_grain"] for chart in awaiting.plan["charts"]), "a revenue trend needs a time grain"


# --- Build ---------------------------------------------------------------------

def test_the_run_finishes_with_a_dashboard(finished):
    assert finished.status is RunStatus.FINISHED, finished.error
    assert finished.dashboard_id and finished.embed_uuid and finished.guest_token
    assert finished.summary and finished.definitions


def test_the_users_edit_to_the_plan_was_honoured(finished):
    assert finished.title == EDITED_TITLE


def test_every_chart_returns_data(finished, superset):
    charts = chart_rows(superset, finished.dashboard_id)
    assert len(charts) >= 3
    for chart, data in charts.values():
        assert data.row_count > 0, chart.slice_name


def test_every_chart_covers_the_same_window(finished, superset):
    windows = {chart_time_window(superset.chart_detail(chart.id)) for chart in superset.dashboard_charts(finished.dashboard_id)}
    assert len(windows) == 1 and None not in windows, windows


def test_the_window_is_the_last_twelve_complete_months(finished, superset):
    today = date.today()
    this_month = today.replace(day=1)
    a_year_earlier = this_month.replace(year=this_month.year - 1)
    data = next(d for _, d in chart_rows(superset, finished.dashboard_id).values() if d.window_end)
    assert data.window_end.startswith(this_month.isoformat())
    assert data.window_start.startswith(a_year_earlier.isoformat())


def test_the_trend_has_twelve_points(finished, superset):
    assert any(data.row_count == 12 for _, data in chart_rows(superset, finished.dashboard_id).values())


def test_totals_match_an_independent_calculation(finished, superset, pool, settings):
    """Whatever revenue definition it chose from the skill, the trend must add up to a
    legitimate one, computed here by hand with every trap handled."""
    charts = chart_rows(superset, finished.dashboard_id)
    trend = next(data for _, data in charts.values() if data.row_count == 12)
    start, end = trend.window_start[:10], trend.window_end[:10]
    orders = f"""
        FROM demo_shop.shop.orders o
        JOIN demo_shop.shop.customers c ON c.customer_id = o.customer_id
        WHERE o.deleted_at IS NULL AND NOT c.is_test
          AND o.status IN ('delivered', 'refunded')
          AND o.order_ts >= TIMESTAMP '{start} 00:00:00 UTC' AND o.order_ts < TIMESTAMP '{end} 00:00:00 UTC'"""
    executor = SqlExecutor(settings.trino_url, AuditLog(pool))
    policy = SqlPolicy(allowed_catalogs=frozenset({"demo_shop"}), max_rows=10)
    merchandise = f"""SELECT SUM(i.quantity * i.unit_price_cents) / 100.0 FROM demo_shop.shop.order_items i
        JOIN (SELECT o.order_id {orders}) e ON e.order_id = i.order_id"""
    extras = f"SELECT SUM(o.shipping_cents - o.discount_cents) / 100.0 {orders}"

    def value(sql):
        return float(executor.run(sql, policy=policy, context=context("check"),
                                  budget=RunBudget(BudgetLimits())).rows[0][0])

    goods, extra = value(merchandise), value(extras)
    assert numeric_total(trend) == pytest.approx(goods, rel=1e-6) or numeric_total(trend) == pytest.approx(goods + extra, rel=1e-6)


def test_it_draws_in_a_real_browser(finished, superset, settings):
    expected = len(superset.dashboard_charts(finished.dashboard_id))
    rendered = render_dashboard(settings.superset_url, settings.superset_admin_user, settings.superset_admin_password,
                                finished.dashboard_slug, expected_charts=expected)
    assert rendered.charts == expected
    assert rendered.errors == []


def test_a_guest_can_open_it_with_the_token(finished, settings):
    response = httpx.get(f"{settings.superset_url}/api/v1/dashboard/{finished.dashboard_id}/charts",
                         headers={"X-GuestToken": finished.guest_token}, timeout=30)
    assert response.status_code == 200


def test_the_token_never_reached_the_model(finished, settings, pool, model, checkpointer):
    runtime = make_runtime(settings, pool, model, context=context("read"), dashboard_key=dashboard_key_for(THREAD))
    agent = build_agent(runtime, require_approval=False, checkpointer=checkpointer)
    state = agent.get_state({"configurable": {"thread_id": THREAD}})
    runtime.close()
    assert state.values["messages"]
    assert finished.guest_token not in str(state.values["messages"])


def test_the_build_left_nothing_unused_in_superset(finished, superset):
    on_dashboard = {chart.id for chart in superset.dashboard_charts(finished.dashboard_id)}
    built = [chart.id for dataset in superset.datasets_named(f"{finished.dashboard_slug}__")
             for chart in superset.charts_on_dataset(dataset.id)]
    assert set(built) == on_dashboard


def test_it_was_recorded_for_the_dashboards_page(finished, pool):
    with pool.connection() as conn:
        row = conn.execute("SELECT title, embed_uuid FROM dashboards WHERE slug = %s", (finished.dashboard_slug,)).fetchone()
    assert row == (EDITED_TITLE, finished.embed_uuid)


def test_every_write_was_audited(finished, pool):
    with pool.connection() as conn:
        kinds = dict(conn.execute("SELECT kind, COUNT(*) FROM audit_log WHERE run_id = %s GROUP BY 1",
                                  (finished.run_id,)).fetchall())
    assert kinds["sql"] > 0 and kinds["superset_write"] > 0


def test_a_run_stays_inside_its_budget(finished):
    assert finished.usage["tokens"] < 800_000
    assert finished.usage["steps"] <= 100


def test_it_reused_the_skill_instead_of_profiling_again(finished, skills, skill_versions_before):
    assert skill_versions_before
    for slug, version in skill_versions_before.items():
        assert skills.read_frontmatter(slug).version == version


# --- Follow-up -----------------------------------------------------------------

@pytest.fixture(scope="module")
def followed_up(run, finished, superset):
    before = {chart.id for chart in superset.dashboard_charts(finished.dashboard_id)}
    return before, run(prompt="Add a chart of revenue by sales channel to this dashboard.", name="followup")


def test_a_follow_up_changes_the_same_dashboard(finished, followed_up):
    _, result = followed_up
    assert result.status is RunStatus.FINISHED, result.error
    assert result.dashboard_id == finished.dashboard_id


def test_a_follow_up_adds_to_what_was_there(superset, finished, followed_up):
    before, _ = followed_up
    after = {chart.id for chart in superset.dashboard_charts(finished.dashboard_id)}
    assert before < after, "existing charts stay, and one is added"


def test_the_new_chart_splits_by_channel_and_returns_data(superset, finished, followed_up):
    before, _ = followed_up
    new = [chart for chart in superset.dashboard_charts(finished.dashboard_id) if chart.id not in before]
    data = superset.chart_data(new[0].id, force=True)
    assert data.row_count == 3, "web, mobile_app and marketplace"


# --- Budgets -------------------------------------------------------------------

def test_a_spent_budget_stops_the_run_gracefully(run):
    result = run(f"test-budget-{uuid.uuid4().hex[:8]}", prompt=SALES_PROMPT, name="budget",
                 overrides={"nova_max_steps": 3})
    assert result.status is RunStatus.PARTIAL
    assert "steps" in result.error
    assert result.usage["steps"] == 3
