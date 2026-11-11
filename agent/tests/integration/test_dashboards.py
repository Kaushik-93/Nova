"""Dashboards Nova draws, against real Cube, Trino and the UI. No model is involved: the charts are
the chart gallery's, whose small semantic model is written by hand (agent/gallery.py), so every
chart type is exercised, and what's shown in the gallery is what's tested here."""

import json
import shutil
import time
import uuid
from unittest import mock

import jwt
import pytest
from fastapi.testclient import TestClient
from langchain_openai import ChatOpenAI
from psycopg.types.json import Jsonb

from agent.api.main import create_app
from agent.api.runs import RunManager
from agent.config import get_settings
from agent.dashboard_agent import dashboard_key_for
from agent.dashboards import run_chart
from agent.db import apply_schema, create_pool
from agent.gallery import CHARTS, MODEL_SLUG, SHEETS, A, publish_model, sheet_spec
from agent.harness.audit import AuditLog, RunContext
from agent.harness.executor import SqlExecutor
from agent.runtime import make_runtime
from agent.semantic.access import tenancy_of
from agent.semantic.client import CubeIdentity
from agent.semantic.charts import Viz
from agent.tools.dashboard_tools import make_builder_tools, make_semantic_query
from agent.tools.renderer import render_dashboard, render_token
from agent.tools.skill_store import SkillStore
from agent.tools.sources import get_source_by_slug

pytestmark = pytest.mark.integration

TENANT = f"{A}.tenant_name"
RENTALS = f"{A}.rentals"


@pytest.fixture(scope="module")
def pool():
    pool = create_pool(get_settings().database_url)
    apply_schema(pool)
    yield pool
    pool.close()


@pytest.fixture(scope="module")
def gallery_model(pool):
    """The gallery's model, published like any other, and removed afterwards if it wasn't there before."""
    settings = get_settings()
    store = SkillStore(settings.nova_skills_dir, pool)
    existed = store.path(MODEL_SLUG).exists()
    source = get_source_by_slug(pool, "demo-rental")
    publish_model(store, SqlExecutor(settings.trino_url, AuditLog(pool)), tenancy_of(source))
    yield store
    if not existed:
        shutil.rmtree(store.path(MODEL_SLUG), ignore_errors=True)


@pytest.fixture
def runtime(pool, gallery_model):
    thread = uuid.uuid4().hex
    events = []
    model = ChatOpenAI(model="gpt-6-luna", api_key="unused", use_responses_api=True)  # never called here
    runtime = make_runtime(get_settings(), pool, model, context=RunContext(run_id=f"dash-{thread[:8]}", user_id="tester"),
                           dashboard_key=dashboard_key_for(thread), thread_id=thread, sink=events.append)
    runtime.events = events
    yield runtime
    with pool.connection() as conn:
        projects = [row[0] for row in conn.execute("SELECT project_id FROM dashboards WHERE thread_id = %s AND project_id IS NOT NULL",
                                                   (thread,)).fetchall()]
        conn.execute("DELETE FROM dashboards WHERE thread_id = %s", (thread,))
        conn.execute("DELETE FROM projects p WHERE p.id = ANY(%s) AND NOT EXISTS (SELECT 1 FROM dashboards d WHERE d.project_id = p.id)",
                     (projects,))
    runtime.close()


def chart(title: str):
    return next(spec for spec in CHARTS if spec.title == title)


def use(tools, name, **arguments) -> str:
    return tools[name].invoke(json.loads(json.dumps(arguments, default=lambda o: o.model_dump(mode="json"))))


# --- The agent's tools ---------------------------------------------------------------------------

def test_a_chart_runs_and_is_saved_and_its_key_updates_it(runtime):
    tools = make_builder_tools(runtime)
    reply = use(tools, "create_chart", spec=chart("Bar chart"))
    assert "rows" in reply and "window" in reply
    use(tools, "create_chart", spec=chart("Bar chart").model_copy(update={"title": "Rentals by class"}))
    assert [c.title for c in runtime.state.spec.charts] == ["Rentals by class"]
    assert [e["event"] for e in runtime.events].count("chart_ready") == 2


def test_a_chart_naming_something_that_doesnt_exist_is_refused(runtime):
    tools = make_builder_tools(runtime)
    reply = use(tools, "create_chart", spec=chart("Bar chart").model_copy(update={"dimensions": [f"{A}.colour"]}))
    assert reply.startswith("FAILED")
    assert runtime.state.spec is None or not runtime.state.spec.charts


def test_a_filter_reaches_the_charts_its_dimension_joins_to(runtime):
    tools = make_builder_tools(runtime)
    use(tools, "create_chart", spec=chart("Bar chart"))
    use(tools, "create_chart", spec=chart("Org chart"))
    reply = use(tools, "set_layout", title="T", rows=[[{"key": "bar_chart"}], [{"key": "org_chart"}]],
                filters=[{"name": "Tenant", "member": TENANT}])
    assert "Tenant reaches 1 of 2 charts" in reply
    inspected = json.loads(use(tools, "inspect_dashboard"))
    assert inspected["filters"][0]["missing_from"] == ["org_chart"]
    assert inspected["charts"][0]["window"][1] < time.strftime("%Y-%m-01")  # ends at the last complete month


def test_a_filter_that_reaches_nothing_is_refused(runtime):
    tools = make_builder_tools(runtime)
    use(tools, "create_chart", spec=chart("Org chart"))
    reply = use(tools, "set_layout", title="T", rows=[[{"key": "org_chart"}]], filters=[{"name": "Tenant", "member": TENANT}])
    assert reply.startswith("FAILED") and "reach no chart" in reply


def test_finishing_runs_every_chart_and_refuses_an_empty_one(runtime):
    tools = make_builder_tools(runtime)
    empty = chart("Bar chart").model_copy(update={"key": "empty", "filters": [{"member": f"{A}.status", "operator": "equals",
                                                                                 "values": ["no such status"]}]})
    use(tools, "create_chart", spec=chart("KPI"))
    use(tools, "create_chart", spec=empty)
    use(tools, "create_chart", spec=chart("Pie chart"))  # left out of the layout
    use(tools, "set_layout", title="T", rows=[[{"key": "kpi"}], [{"key": "empty"}]])
    assert "Not finished" in use(tools, "finish_dashboard", summary="s", definitions=[])
    use(tools, "set_layout", title="T", rows=[[{"key": "kpi"}]])
    assert use(tools, "finish_dashboard", summary="s", definitions=[]) == "Finished."
    assert {c.key for c in runtime.state.spec.charts} == {"kpi"}  # charts not shown are dropped
    assert any("reviewed" in warning for warning in runtime.state.warnings)  # the gallery's model isn't reviewed


def test_semantic_query_answers_as_untrusted_data(runtime):
    reply = make_semantic_query(runtime).invoke({"query": {"measures": [RENTALS], "dimensions": [f"{A}.booking_channel"]}})
    assert "<untrusted_data>" in reply and "rows" in reply


# --- Every chart in the gallery returns data ---------------------------------------------------------

@pytest.mark.parametrize("name", list(SHEETS))
def test_every_gallery_chart_returns_data(gallery_model, name):
    settings = get_settings()
    from agent.semantic.client import CubeClient
    with_cube = CubeClient(settings.cube_url, settings.cubejs_api_secret)
    try:
        spec = sheet_spec(name)
        for item in spec.charts:
            result = run_chart(with_cube, CubeIdentity(sub="tester", role="admin", catalogs=["demo_rental"]), spec, item.key)
            assert result["row_count"] > 0, item.title
            if item.viz in {Viz.BAR, Viz.LINE} and item.time_dimension:
                assert result["window"] is not None
    finally:
        with_cube.close()


# --- Through the API, as people ----------------------------------------------------------------------

@pytest.fixture(scope="module")
def api(gallery_model):
    settings = get_settings()
    with mock.patch.object(RunManager, "mark_abandoned"), TestClient(create_app()) as client:
        assert client.post("/api/auth/login", json={"email": settings.nova_admin_email,
                                                    "password": settings.nova_admin_password}).status_code == 200
        yield client


@pytest.fixture
def sheet(api, pool):
    """A shared sheet of the gallery's Numbers and Comparisons charts, with its tenant filter."""
    thread = f"dash-test-{uuid.uuid4().hex[:8]}"
    slug = dashboard_key_for(thread)
    spec = sheet_spec("Comparisons")
    with pool.connection() as conn:
        project = conn.execute("INSERT INTO projects (slug, name, created_by, shared) VALUES (%s, 'Test', %s, true) RETURNING id",
                               (slug, get_settings().nova_admin_email)).fetchone()[0]
        conn.execute("""INSERT INTO dashboards (slug, thread_id, title, created_by, catalogs, project_id, sheet_name, spec)
                        VALUES (%s, %s, 'Test', %s, '{demo_rental}', %s, 'Test', %s)""",
                     (slug, thread, get_settings().nova_admin_email, project, Jsonb(spec.model_dump(mode="json"))))
    yield slug
    with pool.connection() as conn:
        conn.execute("DELETE FROM dashboards WHERE slug = %s", (slug,))
        conn.execute("DELETE FROM projects WHERE slug = %s", (slug,))


@pytest.fixture
def tenant_user(api):
    email, password = f"tenant-{uuid.uuid4().hex[:8]}@nova.local", "a-long-test-password"
    created = api.post("/api/users", json={"email": email, "password": password, "role": "viewer"})
    assert created.status_code == 201, created.text
    assert api.put(f"/api/users/{created.json()['id']}/access", json=[{"source": "demo-rental", "tenant_ids": ["3"]}]).status_code == 200
    client = TestClient(api.app)
    assert client.post("/api/auth/login", json={"email": email, "password": password}).status_code == 200
    yield client
    with api.app.state.services.pool.connection() as conn:
        conn.execute("DELETE FROM users WHERE email = %s", (email,))


def test_a_shared_dashboard_shows_each_person_their_own_tenants(api, sheet, tenant_user):
    everyone = api.get(f"/api/dashboards/{sheet}/filters/Tenant/options").json()
    mine = tenant_user.get(f"/api/dashboards/{sheet}/filters/Tenant/options").json()
    assert len(everyone) > 5 and len(mine) == 1 and mine[0] in everyone
    body = {"filters": {}, "date_range": None}
    total = lambda client: sum(float(r[RENTALS]) for r in client.post(f"/api/dashboards/{sheet}/charts/bar_chart/data", json=body).json()["rows"])
    assert 0 < total(tenant_user) < total(api)


def test_choosing_another_tenant_cant_widen_a_limit(api, sheet, tenant_user):
    other = next(name for name in api.get(f"/api/dashboards/{sheet}/filters/Tenant/options").json()
                 if name not in tenant_user.get(f"/api/dashboards/{sheet}/filters/Tenant/options").json())
    rows = tenant_user.post(f"/api/dashboards/{sheet}/charts/bar_chart/data", json={"filters": {"Tenant": [other]}}).json()["rows"]
    assert rows == []


def test_the_time_filter_replaces_the_window(api, sheet):
    data = api.post(f"/api/dashboards/{sheet}/charts/bar_chart/data", json={"filters": {}, "date_range": ["2025-01-01", "2025-03-31"]}).json()
    assert data["window"] == ["2025-01-01", "2025-03-31"]


def test_someone_without_access_cant_see_a_dashboard(api, sheet):
    email = f"none-{uuid.uuid4().hex[:8]}@nova.local"
    api.post("/api/users", json={"email": email, "password": "a-long-test-password", "role": "viewer"})
    try:
        client = TestClient(api.app)
        client.post("/api/auth/login", json={"email": email, "password": "a-long-test-password"})
        assert client.get(f"/api/dashboards/{sheet}").status_code == 404
        assert client.post(f"/api/dashboards/{sheet}/charts/bar_chart/data", json={}).status_code == 404
    finally:
        with api.app.state.services.pool.connection() as conn:
            conn.execute("DELETE FROM users WHERE email = %s", (email,))


def test_a_render_token_opens_only_its_own_dashboard(api, sheet):
    settings = get_settings()
    anonymous = TestClient(api.app)
    good = {"Authorization": f"Bearer {render_token(settings, sheet, user_id='system')}"}
    assert anonymous.get(f"/api/dashboards/{sheet}", headers=good).status_code == 200
    other = {"Authorization": f"Bearer {render_token(settings, 'nova-elsewhere', user_id='system')}"}
    assert anonymous.get(f"/api/dashboards/{sheet}", headers=other).status_code == 403
    expired = jwt.encode({"purpose": "render", "slug": sheet, "sub": "system", "exp": int(time.time()) - 1},
                         settings.nova_jwt_secret, algorithm="HS256")
    assert anonymous.get(f"/api/dashboards/{sheet}", headers={"Authorization": f"Bearer {expired}"}).status_code == 403
    assert anonymous.get(f"/api/dashboards/{sheet}").status_code == 401


def test_an_old_superset_sheet_asks_to_be_rebuilt(api, pool, sheet):
    with pool.connection() as conn:
        conn.execute("UPDATE dashboards SET spec = NULL WHERE slug = %s", (sheet,))
    detail = api.get(f"/api/dashboards/{sheet}").json()
    assert detail["needs_rebuild"] is True and detail["spec"] is None


# --- In a real browser -------------------------------------------------------------------------------

@pytest.mark.parametrize("name", list(SHEETS))
def test_every_gallery_sheet_draws_in_a_real_browser(api, pool, name):
    thread = f"dash-render-{uuid.uuid4().hex[:8]}"
    slug = dashboard_key_for(thread)
    spec = sheet_spec(name)
    with pool.connection() as conn:
        conn.execute("""INSERT INTO dashboards (slug, thread_id, title, created_by, catalogs, sheet_name, spec)
                        VALUES (%s, %s, %s, 'system', '{demo_rental}', %s, %s)""",
                     (slug, thread, name, name, Jsonb(spec.model_dump(mode="json"))))
    try:
        placed = sum(len(row) for row in spec.rows)
        rendered = render_dashboard(get_settings(), slug, user_id="system", expected_charts=placed)
        assert rendered.errors == [] and rendered.charts == placed, rendered.errors
    finally:
        with pool.connection() as conn:
            conn.execute("DELETE FROM dashboards WHERE slug = %s", (slug,))
