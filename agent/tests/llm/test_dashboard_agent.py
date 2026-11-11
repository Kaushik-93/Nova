"""Prompt to dashboard, with the real model, on the real stack. Run with `make test-llm`.

These tests share one conversation, in order, because each build costs real money:
plan -> approve an edited plan -> build -> follow-up. `run_dashboard` builds a new agent on
every call, so each step also proves a run continues from its checkpoint alone.

Numbers are checked against SQL written here, independently of the semantic model the agent used.
"""

import json
import uuid
from datetime import date

import pytest

from agent.config import get_settings
from agent.dashboard_agent import RunStatus, dashboard_key_for, run_dashboard
from agent.dashboards import load_spec, run_chart
from agent.db import apply_schema, create_checkpointer, create_langgraph_pool, create_pool, create_store
from agent.harness.audit import AuditLog, RunContext
from agent.harness.budgets import BudgetLimits, RunBudget
from agent.harness.executor import SqlExecutor
from agent.harness.guardrails import SqlPolicy
from agent.llm import build_model
from agent.semantic.charts import KPIS
from agent.semantic.client import CubeClient, CubeIdentity
from agent.subagents.profiler import generate_skill
from agent.tools.renderer import render_dashboard
from agent.tools.skill_store import SkillStore
from agent.tools.sources import list_sources, register_source

pytestmark = [pytest.mark.llm, pytest.mark.integration]

SALES_PROMPT = "Show me a sales overview for the last 12 months with revenue trend, top products and revenue by region"
THREAD = f"test-sales-{uuid.uuid4().hex[:10]}"
EDITED_TITLE = "Quarterly Business Review"
ADMIN = CubeIdentity(sub="tester", role="admin", catalogs=["demo_shop", "demo_marketing", "demo_rental"])


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
def cube(settings):
    client = CubeClient(settings.cube_url, settings.cubejs_api_secret)
    yield client
    client.close()


def context(name: str) -> RunContext:
    return RunContext(run_id=f"llm-{name}-{uuid.uuid4().hex[:8]}", user_id="tester")


@pytest.fixture(scope="module")
def skills(settings, pool, model, cube):
    """The shared demo skills, generated once if they don't have a semantic model yet."""
    skill_store = SkillStore(settings.nova_skills_dir, pool)
    executor = SqlExecutor(settings.trino_url, AuditLog(pool))
    for slug, catalog, dialect in (("demo-shop", "demo_shop", "postgres"), ("demo-marketing", "demo_marketing", "mysql")):
        source_id = register_source(pool, slug=slug, display_name=slug, catalog=catalog, dialect=dialect)
        if skill_store.read_model(slug) is None:
            generate_skill(model=model, store=skill_store, executor=executor, cube=cube, source_id=source_id, slug=slug,
                           catalog=catalog, dialect=dialect, context=context("skill"),
                           budget=RunBudget(BudgetLimits(max_steps=80, max_tokens=800_000, max_sql_queries=600)))
    return skill_store


@pytest.fixture(scope="module", autouse=True)
def clean_up(pool):
    yield
    with pool.connection() as conn:
        projects = [row[0] for row in conn.execute(
            "SELECT project_id FROM dashboards WHERE thread_id LIKE 'test-%%' AND project_id IS NOT NULL").fetchall()]
        conn.execute("DELETE FROM dashboards WHERE thread_id LIKE 'test-%%'")
        conn.execute("DELETE FROM projects p WHERE p.id = ANY(%s) AND NOT EXISTS (SELECT 1 FROM dashboards d WHERE d.project_id = p.id)",
                     (projects,))


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


def charts_of(pool, cube, slug):
    spec, _ = load_spec(pool, slug)
    return spec, {chart.key: (chart, run_chart(cube, ADMIN, spec, chart.key)) for chart in spec.charts}


# --- Plan ----------------------------------------------------------------------

def test_the_agent_stops_for_approval_before_building_anything(awaiting, pool):
    assert awaiting.status is RunStatus.AWAITING_APPROVAL
    assert awaiting.plan["charts"]
    assert awaiting.dashboard_slug is None
    with pool.connection() as conn:
        assert conn.execute("SELECT 1 FROM dashboards WHERE thread_id = %s", (THREAD,)).fetchone() is None


def test_the_plan_covers_what_was_asked(awaiting):
    text = json.dumps(awaiting.plan).lower()
    assert "product" in text and "region" in text
    assert any(chart["grain"] for chart in awaiting.plan["charts"]), "a revenue trend needs a grain"
    assert all(member.count(".") == 1 for chart in awaiting.plan["charts"] for member in chart["measures"])


# --- Build ---------------------------------------------------------------------

def test_the_run_finishes_with_a_dashboard(finished):
    assert finished.status is RunStatus.FINISHED, finished.error
    assert finished.dashboard_slug == dashboard_key_for(THREAD)
    assert finished.summary and finished.definitions


def test_the_users_edit_to_the_plan_was_honoured(finished):
    assert finished.title == EDITED_TITLE


def test_every_chart_returns_data(finished, pool, cube):
    _, charts = charts_of(pool, cube, finished.dashboard_slug)
    assert len(charts) >= 3
    for chart, data in charts.values():
        assert data["row_count"] > 0, chart.title


def test_every_chart_of_activity_covers_the_last_twelve_complete_months(finished, pool, cube):
    _, charts = charts_of(pool, cube, finished.dashboard_slug)
    this_month = date.today().replace(day=1)
    windows = {tuple(data["window"]) for _, data in charts.values() if data["window"]}
    assert len(windows) == 1, windows
    start, end = next(iter(windows))
    assert start == this_month.replace(year=this_month.year - 1).isoformat()
    assert end < this_month.isoformat()


def test_the_trend_has_twelve_points_and_matches_an_independent_calculation(finished, pool, cube, settings):
    """Whatever revenue definition it chose from the model, the trend must add up to a legitimate
    one, computed here by hand with every trap handled."""
    _, charts = charts_of(pool, cube, finished.dashboard_slug)
    chart, trend = next((c, d) for c, d in charts.values() if d["row_count"] == 12 and c.viz not in KPIS)
    start, end = trend["window"]
    executor = SqlExecutor(settings.trino_url, AuditLog(pool))
    policy = SqlPolicy(allowed_catalogs=frozenset({"demo_shop"}), max_rows=10)

    def merchandise(statuses: str) -> float:
        sql = f"""SELECT SUM(i.quantity * i.unit_price_cents) / 100.0 FROM demo_shop.shop.order_items i
                  JOIN demo_shop.shop.orders o ON o.order_id = i.order_id
                  JOIN demo_shop.shop.customers c ON c.customer_id = o.customer_id
                  WHERE o.deleted_at IS NULL AND NOT c.is_test AND o.status IN ({statuses})
                    AND o.order_ts >= TIMESTAMP '{start} 00:00:00 UTC'
                    AND o.order_ts < TIMESTAMP '{end} 00:00:00 UTC' + INTERVAL '1' DAY"""
        return float(executor.run(sql, policy=policy, context=context("check"), budget=RunBudget(BudgetLimits())).rows[0][0])

    total = sum(float(row[chart.measures[0]]) for row in trend["rows"])
    # The model may reasonably decide which statuses count as a sale; the arithmetic and traps are fixed here.
    candidates = [merchandise("'delivered', 'refunded'"), merchandise("'delivered', 'refunded', 'shipped', 'paid'"),
                  merchandise("'delivered', 'shipped', 'paid'"), merchandise("'delivered'")]
    assert any(total == pytest.approx(candidate, rel=1e-6) for candidate in candidates), (total, candidates)


def test_it_draws_in_a_real_browser(finished, pool, settings):
    spec, _ = load_spec(pool, finished.dashboard_slug)
    placed = sum(len(row) for row in spec.rows)
    rendered = render_dashboard(settings, finished.dashboard_slug, user_id="tester", expected_charts=placed)
    assert rendered.charts == placed and rendered.errors == []


def test_every_chart_is_laid_out(finished, pool):
    spec, _ = load_spec(pool, finished.dashboard_slug)
    assert spec.unplaced() == []


def test_it_was_recorded_for_the_projects_page(finished, pool):
    with pool.connection() as conn:
        row = conn.execute("SELECT title, project_id IS NOT NULL FROM dashboards WHERE slug = %s", (finished.dashboard_slug,)).fetchone()
    assert row == (EDITED_TITLE, True)


def test_a_run_stays_inside_its_budget(finished):
    assert finished.usage["tokens"] < 800_000
    assert finished.usage["steps"] <= 100


def test_it_reused_the_skill_instead_of_profiling_again(finished, skills, skill_versions_before):
    assert skill_versions_before
    for slug, version in skill_versions_before.items():
        assert skills.read_frontmatter(slug).version == version


# --- Follow-up -----------------------------------------------------------------

@pytest.fixture(scope="module")
def followed_up(run, finished, pool):
    before, _ = load_spec(pool, finished.dashboard_slug)
    return {chart.key for chart in before.charts}, run(prompt="Add a chart of revenue by sales channel to this dashboard.",
                                                        name="followup")


def test_a_follow_up_changes_the_same_dashboard(finished, followed_up):
    _, result = followed_up
    assert result.status is RunStatus.FINISHED, result.error
    assert result.dashboard_slug == finished.dashboard_slug


def test_a_follow_up_adds_to_what_was_there(finished, followed_up, pool, cube):
    before, _ = followed_up
    spec, charts = charts_of(pool, cube, finished.dashboard_slug)
    after = {chart.key for chart in spec.charts}
    assert before < after, "existing charts stay, and one is added"
    assert any(charts[key][1]["row_count"] == 3 for key in after - before), "web, mobile_app and marketplace"


# --- Budgets -------------------------------------------------------------------

def test_a_spent_budget_stops_the_run_gracefully(run):
    result = run(f"test-budget-{uuid.uuid4().hex[:8]}", prompt=SALES_PROMPT, name="budget", overrides={"nova_max_steps": 3})
    assert result.status is RunStatus.PARTIAL
    assert "steps" in result.error
    assert result.usage["steps"] == 3
