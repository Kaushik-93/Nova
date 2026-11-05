"""Connecting a source from the API, for real: a new Trino catalog, a Superset database, and
encrypted credentials. Only the model is replaced, since profiling has its own tests in tests/llm."""

import os
import time
import uuid
from pathlib import Path
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from agent.api.main import create_app
from agent.api.runs import RunManager
from agent.config import get_settings
from agent.harness.audit import RunContext
from agent.tools.connectors import decrypt_credentials

pytestmark = pytest.mark.integration


def body(name: str, **changes) -> dict:
    return {"name": name, "kind": "postgres", "host": "demo-postgres", "port": 5432, "database": "shop", "username": "nova_ro",
            "password": os.environ["DEMO_RO_PASSWORD"]} | changes


@pytest.fixture(scope="module")
def client():
    settings = get_settings()
    with mock.patch.object(RunManager, "mark_abandoned"), TestClient(create_app()) as client:
        signed_in = client.post("/api/auth/login", json={"email": settings.nova_admin_email, "password": settings.nova_admin_password})
        assert signed_in.status_code == 200, signed_in.text
        yield client
        with client.app.state.services.pool.connection() as conn:
            conn.execute("DELETE FROM users WHERE email = %s", (settings.nova_admin_email,))


@pytest.fixture
def connected(client):
    """A source that's really connected, profiled by a stand-in, and removed afterwards."""
    services = client.app.state.services
    name = f"Connect test {uuid.uuid4().hex[:8]}"
    slug = name.lower().replace(" ", "-")
    catalog = "src_" + slug.replace("-", "_")
    profiled = []
    context = RunContext(run_id="test-connect", user_id="tester")

    def stand_in(*, source, context, sink):
        sink({"event": "profile_table", "agent": "profiler", "table": "shop.orders", "done": 1, "total": 1})
        profiled.append(source.slug)

    with mock.patch.object(services, "profile_source", stand_in):
        response = client.post("/api/sources", json=body(name))
        assert response.status_code == 202, response.text
        run_id = response.json()["run_id"]
        for _ in range(120):
            run = client.get(f"/api/runs/{run_id}").json()
            if run["status"] != "running":
                break
            time.sleep(0.5)
    try:
        assert run["status"] == "finished", run
        yield {"name": name, "slug": slug, "catalog": catalog, "run_id": run_id, "profiled": profiled, "response": response.json()}
    finally:
        services.executor.run_internal(f"DROP CATALOG IF EXISTS {catalog}", context=context, purpose="test cleanup")
        Path(services.settings.nova_trino_secrets_dir, f"{catalog}.properties").unlink(missing_ok=True)
        with services.pool.connection() as conn:
            row = conn.execute("DELETE FROM sources WHERE slug = %s RETURNING superset_database_id", (slug,)).fetchone()
            conn.execute("DELETE FROM runs WHERE run_id = %s", (run_id,))
        if row and row[0]:
            services.superset.delete_database(row[0])


def test_a_read_only_login_is_recognised(client):
    response = client.post("/api/sources/test", json=body("x"))
    assert response.status_code == 200, response.text
    assert response.json() == {"connected": True, "read_only": True, "warning": None}


def test_a_login_that_can_write_gets_a_warning(client):
    response = client.post("/api/sources/test", json=body("x", username="shop_owner", password=os.environ["DEMO_OWNER_PASSWORD"]))
    assert response.status_code == 200, response.text
    assert response.json()["read_only"] is False and response.json()["warning"]


def test_mysql_read_only_is_recognised_too(client):
    response = client.post("/api/sources/test", json=body("x", kind="mysql", host="demo-mysql", port=3306, database="marketing"))
    assert response.status_code == 200, response.text
    assert response.json()["read_only"] is True


def test_a_mysql_login_that_can_write_gets_a_warning(client):
    response = client.post("/api/sources/test", json=body("x", kind="mysql", host="demo-mysql", port=3306, database="marketing",
                                                          username="root", password=os.environ["DEMO_OWNER_PASSWORD"]))
    assert response.status_code == 200, response.text
    assert response.json()["read_only"] is False


def test_a_wrong_password_is_a_clear_error_that_doesnt_repeat_it(client):
    response = client.post("/api/sources/test", json=body("x", password="definitely-wrong-password"))
    assert response.status_code == 400
    assert "couldn't connect" in response.json()["error"]
    assert "definitely-wrong-password" not in response.text


def test_nothing_is_kept_after_a_failed_connection(client):
    name = f"Connect test {uuid.uuid4().hex[:8]}"
    assert client.post("/api/sources", json=body(name, password="definitely-wrong-password")).status_code == 400
    assert name.lower().replace(" ", "-") not in [source["slug"] for source in client.get("/api/sources").json()]


def test_a_connected_source_reads_through_its_own_trino_catalog(client, connected):
    services = client.app.state.services
    context = RunContext(run_id="test-connect", user_id="tester")
    rows = services.executor.run_internal(f"SELECT COUNT(*) FROM {connected['catalog']}.shop.regions", context=context, purpose="test")
    assert rows[0][0] == 8


def test_the_catalog_definition_holds_no_password(client, connected):
    services = client.app.state.services
    context = RunContext(run_id="test-connect", user_id="tester")
    # What Trino itself recorded, which is the text that reaches its logs and web UI.
    recorded = services.executor.run_internal(
        f"SELECT query FROM system.runtime.queries WHERE query LIKE 'CREATE CATALOG {connected['catalog']}%'",
        context=context, purpose="test")
    assert recorded
    assert os.environ["DEMO_RO_PASSWORD"] not in str(recorded)
    assert "connection-credential-file" in str(recorded)


def test_credentials_are_private_on_disk(client, connected):
    path = Path(client.app.state.services.settings.nova_trino_secrets_dir, f"{connected['catalog']}.properties")
    assert path.stat().st_mode & 0o077 == 0
    assert path.stat().st_uid == 1000  # Trino's user, so that only Trino reads it


def test_credentials_are_encrypted_in_the_database(client, connected):
    services = client.app.state.services
    with services.pool.connection() as conn:
        stored = conn.execute("SELECT encrypted_credentials FROM sources WHERE slug = %s", (connected["slug"],)).fetchone()[0]
    assert os.environ["DEMO_RO_PASSWORD"] not in stored
    assert decrypt_credentials(stored, services.settings.fernet_key)["username"] == "nova_ro"


def test_the_source_is_registered_in_superset(client, connected):
    services = client.app.state.services
    with services.pool.connection() as conn:
        database_id = conn.execute("SELECT superset_database_id FROM sources WHERE slug = %s", (connected["slug"],)).fetchone()[0]
    detail = services.superset._call("GET", f"/api/v1/database/{database_id}").json()["result"]
    assert detail["database_name"] == connected["name"]
    assert os.environ["DEMO_RO_PASSWORD"] not in str(detail)


def test_the_source_is_ready_and_nothing_secret_comes_back(client, connected):
    source = next(item for item in client.get("/api/sources").json() if item["slug"] == connected["slug"])
    assert (source["status"], source["read_only"], source["kind"]) == ("ready", True, "postgres")
    assert connected["profiled"] == [connected["slug"]]
    assert "encrypted_credentials" not in source
    assert os.environ["DEMO_RO_PASSWORD"] not in client.get("/api/sources").text


def test_progress_can_be_followed(client, connected):
    run = client.get(f"/api/runs/{connected['run_id']}").json()
    assert run["kind"] == "job"
    steps = [event.get("step") or event["event"] for event in client.app.state.services.runs.events(connected["run_id"])]
    assert steps == ["superset", "catalog", "profile", "profile_table", "done"]
    assert os.environ["DEMO_RO_PASSWORD"] not in str(client.app.state.services.runs.events(connected["run_id"]))


def test_a_job_isnt_listed_as_a_dashboard_run(client, connected):
    assert connected["run_id"] not in [run["run_id"] for run in client.get("/api/runs").json()]


def test_a_job_cant_be_resumed_as_a_dashboard_run(client, connected):
    assert client.post(f"/api/runs/{connected['run_id']}/resume", json={}).status_code == 400


def test_the_same_source_cant_be_connected_twice(client, connected):
    assert client.post("/api/sources", json=body(connected["name"])).status_code == 409


def test_a_failed_profile_marks_the_source_failed_and_can_be_retried(client):
    services = client.app.state.services
    name = f"Connect test {uuid.uuid4().hex[:8]}"
    slug = name.lower().replace(" ", "-")
    catalog = "src_" + slug.replace("-", "_")
    context = RunContext(run_id="test-connect", user_id="tester")

    def wait(run_id):
        for _ in range(120):
            run = client.get(f"/api/runs/{run_id}").json()
            if run["status"] != "running":
                return run
            time.sleep(0.5)
        raise AssertionError("the job didn't finish")

    def fails(**_):
        raise RuntimeError("the model is down")

    run_ids = []
    try:
        with mock.patch.object(services, "profile_source", fails):
            run_ids.append(client.post("/api/sources", json=body(name)).json()["run_id"])
            run = wait(run_ids[-1])
        assert run["status"] == "failed"
        assert "the model is down" not in str(run["result"])  # the reason stays in the server logs
        assert next(item for item in client.get("/api/sources").json() if item["slug"] == slug)["status"] == "failed"

        with mock.patch.object(services, "profile_source", lambda **_: None):
            retry = client.post("/api/sources", json=body(name))
            assert retry.status_code == 202, retry.text
            run_ids.append(retry.json()["run_id"])
            assert wait(run_ids[-1])["status"] == "finished"
        assert next(item for item in client.get("/api/sources").json() if item["slug"] == slug)["status"] == "ready"
    finally:
        services.executor.run_internal(f"DROP CATALOG IF EXISTS {catalog}", context=context, purpose="test cleanup")
        Path(services.settings.nova_trino_secrets_dir, f"{catalog}.properties").unlink(missing_ok=True)
        with services.pool.connection() as conn:
            row = conn.execute("DELETE FROM sources WHERE slug = %s RETURNING superset_database_id", (slug,)).fetchone()
            conn.execute("DELETE FROM runs WHERE run_id = ANY(%s)", (run_ids,))
        if row and row[0]:
            services.superset.delete_database(row[0])


def test_a_failed_source_is_kept_away_from_the_agent(client):
    """The agent only ever works with sources that are ready."""
    from agent.config import get_settings as settings_now
    from agent.runtime import make_runtime
    services = client.app.state.services
    slug = f"connect-test-{uuid.uuid4().hex[:8]}"
    with services.pool.connection() as conn:
        conn.execute("""INSERT INTO sources (slug, display_name, trino_catalog, dialect, status)
                        VALUES (%s, 'Failed', %s, 'postgres', 'failed')""", (slug, slug.replace("-", "_")))
    try:
        runtime = make_runtime(settings_now(), services.pool, None, context=RunContext(run_id="t", user_id="t"), dashboard_key="t")
        assert slug not in [source.slug for source in runtime.sources]
        assert slug.replace("-", "_") not in runtime.policy.allowed_catalogs
        runtime.close()
    finally:
        with services.pool.connection() as conn:
            conn.execute("DELETE FROM sources WHERE slug = %s", (slug,))
