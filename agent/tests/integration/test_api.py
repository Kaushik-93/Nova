"""The HTTP API on the real database and Superset. The model isn't involved: `run_dashboard`
is replaced by a stub for these tests, and the real thing is tested in tests/llm."""

import json
import threading
import uuid
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from agent.api.main import create_app
from agent.api.runs import RunManager
from agent.config import get_settings
from agent.dashboard_agent import RunResult, RunStatus
from agent.tools.superset_payloads import ChartSpec, Metric, Viz

pytestmark = pytest.mark.integration


class FakeRun:
    """Plays a run: emits steps, optionally waits, and returns a result."""

    def __init__(self):
        self.calls = []
        self.release = threading.Event()
        self.release.set()
        self.result = RunStatus.FINISHED

    def __call__(self, *, context, thread_id, sink, **options):
        self.calls.append({"thread_id": thread_id, **options})
        sink({"event": "model_start", "agent": "main"})
        self.release.wait(10)
        sink({"event": "model", "agent": "main", "seconds": 2, "tokens": 50, "tools": [], "text": "done"})
        return RunResult(status=self.result, thread_id=thread_id, run_id=context.run_id, title="T", guest_token="secret-token")


@pytest.fixture(scope="module")
def client():
    # Starting a server marks runs it finds in progress as abandoned. Here that would catch real
    # runs in the shared database, so these tests skip it, and remove their own runs afterwards.
    settings = get_settings()
    with mock.patch.object(RunManager, "mark_abandoned"), TestClient(create_app()) as client:
        signed_in = client.post("/api/auth/login", json={"email": settings.nova_admin_email, "password": settings.nova_admin_password})
        assert signed_in.status_code == 200, signed_in.text
        yield client
        with client.app.state.services.pool.connection() as conn:
            conn.execute("DELETE FROM runs WHERE prompt = 'p' OR prompt = 'Show sales'")
            conn.execute("DELETE FROM users WHERE email = %s", (settings.nova_admin_email,))


@pytest.fixture
def fake(client):
    runs = client.app.state.services.runs
    stub = FakeRun()
    original, runs._runner = runs._runner, stub
    yield stub
    stub.release.set()
    runs._runner = original


def wait_for(client, run_id):
    with client.stream("GET", f"/api/runs/{run_id}/events") as stream:
        return [line for line in stream.iter_lines() if line.startswith(("event:", "data:"))]


def test_health(client):
    assert client.get("/api/health").json() == {"status": "ok"}


def test_config_tells_the_ui_where_superset_is_and_no_secrets(client):
    config = client.get("/api/config").json()
    assert config["superset_url"].startswith("http://localhost")
    assert get_settings().superset_admin_password not in json.dumps(config)


def test_a_run_starts_and_finishes(client, fake):
    started = client.post("/api/runs", json={"prompt": "Show sales"})
    assert started.status_code == 202
    run_id = started.json()["run_id"]

    lines = wait_for(client, run_id)
    run = client.get(f"/api/runs/{run_id}").json()

    assert run["status"] == "finished"
    assert run["result"]["title"] == "T"
    assert "event: end" in lines
    assert sum(line == "event: step" for line in lines) == 2


def test_the_stream_replays_after_a_reconnect(client, fake):
    run_id = client.post("/api/runs", json={"prompt": "p"}).json()["run_id"]
    wait_for(client, run_id)
    first = client.app.state.services.runs.events(run_id)[0]["id"]
    with client.stream("GET", f"/api/runs/{run_id}/events", params={"after": first}) as stream:
        steps = [line for line in stream.iter_lines() if line == "event: step"]
    assert len(steps) == 1


def test_the_guest_token_is_never_stored_with_a_run(client, fake):
    run_id = client.post("/api/runs", json={"prompt": "p"}).json()["run_id"]
    wait_for(client, run_id)
    assert "secret-token" not in json.dumps(client.get(f"/api/runs/{run_id}").json())


def test_timing_comes_with_the_run(client, fake):
    run_id = client.post("/api/runs", json={"prompt": "p"}).json()["run_id"]
    wait_for(client, run_id)
    timing = client.get(f"/api/runs/{run_id}").json()["timing"]
    assert timing["turns"] == 1 and timing["model_seconds"] == 2


def test_a_conversation_runs_one_thing_at_a_time(client, fake):
    fake.release.clear()
    thread = f"t-{uuid.uuid4().hex[:8]}"
    first = client.post("/api/runs", json={"prompt": "p", "thread_id": thread}).json()["run_id"]

    second = client.post("/api/runs", json={"prompt": "p", "thread_id": thread})

    assert second.status_code == 400
    fake.release.set()
    wait_for(client, first)


def test_an_empty_prompt_is_refused(client):
    assert client.post("/api/runs", json={"prompt": "  "}).status_code == 400


def test_an_unknown_run_is_404(client):
    assert client.get("/api/runs/nope").status_code == 404
    assert client.get("/api/runs/nope/events").status_code == 404


def test_a_paused_run_needs_an_answer_then_continues_on_its_thread(client, fake):
    fake.result = RunStatus.AWAITING_APPROVAL
    started = client.post("/api/runs", json={"prompt": "p", "require_approval": True}).json()
    wait_for(client, started["run_id"])
    assert client.get(f"/api/runs/{started['run_id']}").json()["status"] == "awaiting_approval"
    assert client.post(f"/api/runs/{started['run_id']}/resume", json={}).status_code == 400

    fake.result = RunStatus.FINISHED
    resumed = client.post(f"/api/runs/{started['run_id']}/resume", json={"plan": {"title": "Edited"}})

    assert resumed.status_code == 202
    assert resumed.json()["thread_id"] == started["thread_id"]
    wait_for(client, resumed.json()["run_id"])
    assert fake.calls[-1]["resume"] == {"plan": {"title": "Edited"}, "rejected": False}


def test_a_finished_run_cannot_be_resumed(client, fake):
    run_id = client.post("/api/runs", json={"prompt": "p"}).json()["run_id"]
    wait_for(client, run_id)
    assert client.post(f"/api/runs/{run_id}/resume", json={"answer": "x"}).status_code == 400


def test_a_crashing_run_is_reported_not_lost(client, fake):
    def explode(**options):
        raise RuntimeError("boom")

    client.app.state.services.runs._runner = explode
    run_id = client.post("/api/runs", json={"prompt": "p"}).json()["run_id"]
    wait_for(client, run_id)
    run = client.get(f"/api/runs/{run_id}").json()
    assert run["status"] == "failed"
    assert "boom" not in json.dumps(run)


def test_sources_list_their_skills_and_no_credentials(client):
    sources = client.get("/api/sources").json()
    assert {source["slug"] for source in sources} >= {"demo-shop"}
    assert "password" not in json.dumps(sources).lower()


def test_an_unknown_skill_is_404(client):
    assert client.get("/api/skills/nope").status_code == 404


def test_a_skill_is_served_with_its_definitions(client):
    if not client.app.state.services.skills.exists("demo-rental"):
        pytest.skip("generate it with make skill")
    skill = client.get("/api/skills/demo-rental").json()
    assert skill["metrics"] and skill["gotchas"] and skill["guide"]


def test_a_chart_built_by_nova_can_be_previewed(client):
    services = client.app.state.services
    key = f"preview-{uuid.uuid4().hex[:8]}"
    database = services.superset.database_id("Nova Trino")
    dataset = services.superset.ensure_dataset(key=key, name=key, database_id=database, sql="""
        SELECT r.region_name, CAST(o.order_ts AS timestamp(3)) AS order_ts, o.tax_cents / 100.0 AS revenue
        FROM demo_shop.shop.orders o JOIN demo_shop.shop.regions r ON r.region_id = o.region_id""")
    spec = ChartSpec(key="by-region", title="By region", viz=Viz.BAR, dimensions=["region_name"], y_axis_format="$,.0f",
                     metrics=[Metric(label="Revenue", sql="SUM(revenue)")])
    chart = services.superset.ensure_chart(spec, dataset_id=dataset.id, dashboard_key=key)
    try:
        preview = client.get(f"/api/charts/{chart.id}/preview").json()
        assert preview["viz"] == "echarts_timeseries_bar"
        assert preview["x_axis"] == "region_name"
        assert preview["format"] == "$,.0f"
        assert preview["row_count"] == 8
        assert preview["columns"] == ["region_name", "Revenue"]
    finally:
        services.superset.delete_chart(chart.id)
        services.superset.delete_dataset(dataset.id)


def test_only_charts_nova_built_can_be_previewed(client):
    assert client.get("/api/charts/999999/preview").status_code == 404


def test_an_unknown_dashboard_is_404(client):
    assert client.get("/api/dashboards/nope/embed").status_code == 404
