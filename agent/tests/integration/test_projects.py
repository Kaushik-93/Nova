"""Projects and sheets through the API, on the real database and Superset. No model is involved."""

import re
import uuid
from pathlib import Path
from unittest import mock

import agent
import pytest
from fastapi.testclient import TestClient

from agent.api.main import create_app
from agent.api.runs import RunManager
from agent.config import get_settings
from agent.projects import describe_for_agent, project_for_new_sheet
from agent.tools.superset_payloads import ChartSlot, ChartSpec, Metric, Viz

pytestmark = pytest.mark.integration

PASSWORD = "a-long-test-password"
CATALOG = "demo_rental"


@pytest.fixture(scope="module")
def app():
    settings = get_settings()
    with mock.patch.object(RunManager, "mark_abandoned"), TestClient(create_app()) as client:
        yield client.app
        with client.app.state.services.pool.connection() as conn:
            conn.execute("DELETE FROM users WHERE email = %s", (settings.nova_admin_email,))


@pytest.fixture(scope="module")
def services(app):
    return app.state.services


@pytest.fixture(scope="module")
def admin(app):
    settings = get_settings()
    client = TestClient(app)
    assert client.post("/api/auth/login", json={"email": settings.nova_admin_email, "password": settings.nova_admin_password}).status_code == 200
    return client


@pytest.fixture
def people(app, admin, services):
    created = []

    def make(role: str = "analyst", *, access: list[dict] | None = None) -> TestClient:
        email = f"user-{uuid.uuid4().hex[:10]}@nova.local"
        response = admin.post("/api/users", json={"email": email, "password": PASSWORD, "role": role})
        assert response.status_code == 201, response.text
        created.append(email)
        if access is not None:
            assert admin.put(f"/api/users/{response.json()['id']}/access", json=access).status_code == 200
        client = TestClient(app)
        assert client.post("/api/auth/login", json={"email": email, "password": PASSWORD}).status_code == 200
        client.email = email
        return client

    yield make
    with services.pool.connection() as conn:
        conn.execute("DELETE FROM users WHERE email = ANY(%s)", (created,))


@pytest.fixture
def workspace(services):
    """Projects and sheets made for one test, and removed afterwards by exactly what was made."""
    projects, sheets = [], []

    def project(client: TestClient, name: str = "Test project") -> dict:
        response = client.post("/api/projects", json={"name": name})
        assert response.status_code == 201, response.text
        projects.append(response.json()["slug"])
        return response.json()

    def sheet(project_slug: str, owner: str, *, name: str = "Sheet", catalogs: tuple[str, ...] = (CATALOG,)) -> str:
        slug = f"nova-test{uuid.uuid4().hex[:7]}"
        with services.pool.connection() as conn:
            project_id = conn.execute("SELECT id FROM projects WHERE slug = %s", (project_slug,)).fetchone()[0]
            conn.execute(
                """INSERT INTO dashboards (slug, thread_id, superset_id, embed_uuid, title, created_by, catalogs, project_id, sheet_name, position)
                   VALUES (%s, %s, 0, %s, %s, %s, %s, %s, %s,
                           (SELECT COALESCE(MAX(position) + 1, 0) FROM dashboards WHERE project_id = %s))""",
                (slug, slug, str(uuid.uuid4()), name, owner, list(catalogs), project_id, name, project_id))
        sheets.append(slug)
        return slug

    yield project, sheet
    with services.pool.connection() as conn:
        conn.execute("DELETE FROM dashboards WHERE slug = ANY(%s)", (sheets,))
        conn.execute("DELETE FROM projects WHERE slug = ANY(%s)", (projects,))


GRANTED = [{"source": "demo-rental"}]


def test_a_project_starts_empty(admin, workspace):
    project, _ = workspace
    created = project(admin, "Fleet review")
    assert created["name"] == "Fleet review"
    assert created["sheets"] == []
    assert admin.get(f"/api/projects/{created['slug']}").json()["slug"] == created["slug"]


def test_two_projects_can_have_the_same_name(admin, workspace):
    project, _ = workspace
    assert project(admin, "Same")["slug"] != project(admin, "Same")["slug"]


def test_a_name_with_no_latin_letters_still_makes_a_project(admin, workspace):
    project, _ = workspace
    assert project(admin, "車両")["slug"].startswith("project-")


def test_viewers_cant_create_projects(people):
    assert people("viewer").post("/api/projects", json={"name": "x"}).status_code == 403


def test_a_project_needs_a_name(admin):
    assert admin.post("/api/projects", json={"name": ""}).status_code == 422


def test_sheets_come_back_in_order(admin, workspace):
    project, sheet = workspace
    slug = project(admin)["slug"]
    first, second = sheet(slug, "a@b.c", name="First"), sheet(slug, "a@b.c", name="Second")
    assert [item["slug"] for item in admin.get(f"/api/projects/{slug}").json()["sheets"]] == [first, second]


def test_sheets_can_be_reordered(admin, workspace):
    project, sheet = workspace
    slug = project(admin)["slug"]
    first, second = sheet(slug, "a@b.c"), sheet(slug, "a@b.c")
    assert admin.put(f"/api/projects/{slug}/order", json={"sheets": [second, first]}).status_code == 200
    assert [item["slug"] for item in admin.get(f"/api/projects/{slug}").json()["sheets"]] == [second, first]


def test_reordering_must_list_exactly_the_projects_sheets(admin, workspace):
    project, sheet = workspace
    slug = project(admin)["slug"]
    first = sheet(slug, "a@b.c")
    assert admin.put(f"/api/projects/{slug}/order", json={"sheets": [first, "someone-elses"]}).status_code == 400
    assert admin.put(f"/api/projects/{slug}/order", json={"sheets": []}).status_code == 400


def test_renaming_a_sheet_keeps_it_through_later_builds(admin, workspace, services):
    project, sheet = workspace
    slug = project(admin)["slug"]
    item = sheet(slug, "a@b.c", name="Original")
    assert admin.patch(f"/api/projects/{slug}/sheets/{item}", json={"name": "Renamed"}).status_code == 200
    with services.pool.connection() as conn:  # what a follow-up build's save does to the title
        conn.execute("UPDATE dashboards SET title = 'The agent''s title' WHERE slug = %s", (item,))
    assert admin.get(f"/api/projects/{slug}").json()["sheets"][0]["name"] == "Renamed"


def test_a_sheet_can_move_to_another_project_and_goes_last(admin, workspace):
    project, sheet = workspace
    source, target = project(admin)["slug"], project(admin)["slug"]
    moving, staying = sheet(source, "a@b.c"), sheet(target, "a@b.c")
    assert admin.patch(f"/api/projects/{source}/sheets/{moving}", json={"project": target}).status_code == 200
    assert admin.get(f"/api/projects/{source}").json()["sheets"] == []
    assert [item["slug"] for item in admin.get(f"/api/projects/{target}").json()["sheets"]] == [staying, moving]


def test_someone_elses_project_is_hidden(people, workspace):
    project, _ = workspace
    owner, other = people(), people()
    slug = project(owner)["slug"]
    assert other.get(f"/api/projects/{slug}").status_code == 404
    assert slug not in [item["slug"] for item in other.get("/api/projects").json()]


def test_only_the_owner_changes_a_project(people, workspace):
    project, _ = workspace
    owner, other = people(), people()
    slug = project(owner)["slug"]
    assert other.patch(f"/api/projects/{slug}", json={"name": "Mine now"}).status_code == 404
    assert other.delete(f"/api/projects/{slug}").status_code == 404
    assert owner.patch(f"/api/projects/{slug}", json={"name": "Renamed"}).json()["name"] == "Renamed"


def test_only_administrators_share(admin, people, workspace):
    project, sheet = workspace
    owner = people(access=GRANTED)
    slug = project(owner)["slug"]
    sheet(slug, owner.email)
    assert owner.patch(f"/api/projects/{slug}", json={"shared": True}).status_code == 403
    assert admin.patch(f"/api/projects/{slug}", json={"shared": True}).json()["shared"] is True


def test_sharing_an_empty_project_is_refused(admin, workspace):
    project, _ = workspace
    assert admin.patch(f"/api/projects/{project(admin)['slug']}", json={"shared": True}).status_code == 400


def test_sharing_shows_a_project_to_people_who_can_read_it(admin, people, workspace):
    project, sheet = workspace
    owner, reader, outsider = people(access=GRANTED), people("viewer", access=GRANTED), people("viewer")
    slug = project(owner)["slug"]
    item = sheet(slug, owner.email)
    assert reader.get(f"/api/projects/{slug}").status_code == 404
    admin.patch(f"/api/projects/{slug}", json={"shared": True})
    assert [entry["slug"] for entry in reader.get(f"/api/projects/{slug}").json()["sheets"]] == [item]
    assert outsider.get(f"/api/projects/{slug}").status_code == 404


def test_a_shared_project_hides_sheets_someone_cant_read(admin, people, workspace):
    project, sheet = workspace
    reader = people("viewer", access=GRANTED)
    slug = project(admin)["slug"]
    readable, hidden = sheet(slug, "a@b.c", catalogs=(CATALOG,)), sheet(slug, "a@b.c", catalogs=("demo_shop",))
    admin.patch(f"/api/projects/{slug}", json={"shared": True})
    assert [entry["slug"] for entry in reader.get(f"/api/projects/{slug}").json()["sheets"]] == [readable]
    assert reader.get(f"/api/dashboards/{hidden}/embed").status_code == 404
    assert hidden not in [row["slug"] for row in reader.get("/api/dashboards").json()]


def test_a_sheet_spanning_sources_needs_all_of_them(admin, people, workspace):
    project, sheet = workspace
    reader = people("viewer", access=GRANTED)
    slug = project(admin)["slug"]
    both = sheet(slug, "a@b.c", catalogs=(CATALOG, "demo_shop"))
    sheet(slug, "a@b.c")
    admin.patch(f"/api/projects/{slug}", json={"shared": True})
    assert both not in [entry["slug"] for entry in reader.get(f"/api/projects/{slug}").json()["sheets"]]


def test_a_sheet_with_no_record_of_its_sources_isnt_shown_to_others(admin, people, workspace):
    project, sheet = workspace
    reader = people("viewer", access=GRANTED)
    slug = project(admin)["slug"]
    unknown = sheet(slug, "a@b.c", catalogs=())
    sheet(slug, "a@b.c")
    admin.patch(f"/api/projects/{slug}", json={"shared": True})
    assert unknown not in [entry["slug"] for entry in reader.get(f"/api/projects/{slug}").json()["sheets"]]


def test_losing_access_hides_a_project_you_own_the_sheets_of(admin, people, services, workspace):
    project, sheet = workspace
    owner = people(access=GRANTED)
    slug = project(owner)["slug"]
    item = sheet(slug, owner.email)
    assert owner.get(f"/api/projects/{slug}").json()["sheets"]
    me = owner.get("/api/auth/me").json()
    admin.put(f"/api/users/{me['id']}/access", json=[])
    assert owner.get(f"/api/projects/{slug}").json()["sheets"] == []
    assert owner.get(f"/api/dashboards/{item}/embed").status_code == 404


def test_a_shared_project_can_only_be_changed_by_an_administrator(admin, people, workspace):
    """Otherwise its owner could change what an administrator approved for everyone."""
    project, sheet = workspace
    owner = people(access=GRANTED)
    slug = project(owner)["slug"]
    item = sheet(slug, owner.email)
    admin.patch(f"/api/projects/{slug}", json={"shared": True})

    assert owner.get(f"/api/projects/{slug}").json()["can_edit"] is False
    assert owner.patch(f"/api/projects/{slug}", json={"name": "Changed"}).status_code == 403
    assert owner.patch(f"/api/projects/{slug}/sheets/{item}", json={"name": "Changed"}).status_code == 403
    assert owner.delete(f"/api/projects/{slug}/sheets/{item}").status_code == 403
    assert owner.put(f"/api/projects/{slug}/order", json={"sheets": [item]}).status_code == 403
    assert owner.post("/api/runs", json={"prompt": "p", "project": slug}).status_code == 403
    assert owner.post("/api/runs", json={"prompt": "p", "thread_id": item}).status_code == 403


def test_a_sheet_cant_be_moved_into_a_shared_project_by_its_owner(admin, people, workspace):
    project, sheet = workspace
    owner = people(access=GRANTED)
    source, shared = project(owner)["slug"], project(owner)["slug"]
    item = sheet(source, owner.email)
    sheet(shared, owner.email)
    admin.patch(f"/api/projects/{shared}", json={"shared": True})
    assert owner.patch(f"/api/projects/{source}/sheets/{item}", json={"project": shared}).status_code == 403


def test_a_run_cant_add_a_sheet_to_someone_elses_project(people, workspace):
    project, _ = workspace
    owner, other = people(), people()
    assert other.post("/api/runs", json={"prompt": "p", "project": project(owner)["slug"]}).status_code == 404


def test_a_run_goes_into_the_chosen_project(admin, services, workspace):
    project, _ = workspace
    slug = project(admin)["slug"]
    runs = services.runs
    calls = []

    def stand_in(**options):
        calls.append(options)
        raise RuntimeError("no model in this test")

    original, runs._runner = runs._runner, stand_in
    try:
        response = admin.post("/api/runs", json={"prompt": "project-run-test", "project": slug})
        assert response.status_code == 202, response.text
        for _ in range(100):
            if calls:
                break
            import time
            time.sleep(0.05)
    finally:
        runs._runner = original
        with services.pool.connection() as conn:
            stored = conn.execute("SELECT project_id::text FROM runs WHERE run_id = %s", (response.json()["run_id"],)).fetchone()[0]
            expected = conn.execute("SELECT id::text FROM projects WHERE slug = %s", (slug,)).fetchone()[0]
            conn.execute("DELETE FROM runs WHERE run_id = %s", (response.json()["run_id"],))
    assert calls[0]["project_id"] == expected
    assert stored == expected


def test_a_dashboard_built_without_a_project_gets_its_own(services, workspace):
    with services.pool.connection() as conn:
        project_id = project_for_new_sheet(conn, sheet_slug=f"nova-{uuid.uuid4().hex[:8]}", requested=None,
                                           title="Rental review", owner="a@b.c")
        name, owner = conn.execute("SELECT name, created_by FROM projects WHERE id = %s", (project_id,)).fetchone()
        conn.execute("DELETE FROM projects WHERE id = %s", (project_id,))
    assert (name, owner) == ("Rental review", "a@b.c")


def test_a_chosen_project_is_used_and_an_existing_sheet_stays_put(admin, services, workspace):
    project, sheet = workspace
    home = project(admin)["slug"]
    item = sheet(home, "a@b.c")
    with services.pool.connection() as conn:
        home_id = conn.execute("SELECT id FROM projects WHERE slug = %s", (home,)).fetchone()[0]
        assert project_for_new_sheet(conn, sheet_slug=f"nova-{uuid.uuid4().hex[:8]}", requested=home_id, title="T", owner="o") == home_id
        assert project_for_new_sheet(conn, sheet_slug=item, requested=uuid.uuid4(), title="T", owner="o") == home_id


def test_the_agent_is_told_what_a_new_sheet_sits_beside(admin, services, workspace):
    project, sheet = workspace
    slug = project(admin, "Fleet review")["slug"]
    current = sheet(slug, "a@b.c", name="This one")
    sheet(slug, "a@b.c", name="Damage overview")
    note = describe_for_agent(services.pool, project_id=None, sheet_slug=current)
    assert "Fleet review" in note and "Damage overview" in note and "This one" not in note


def test_nothing_is_said_about_a_sheet_with_no_project(services):
    assert describe_for_agent(services.pool, project_id=None, sheet_slug="nova-nothing") == ""


def superset_sheet(services, key: str) -> tuple[int, int, int]:
    superset = services.superset
    dataset = superset.ensure_dataset(key=key, name=f"{key}__sales", database_id=superset.database_id("Nova Trino"),
                                      sql="SELECT status FROM demo_shop.shop.orders")
    spec = ChartSpec(key="count", title="Orders", viz=Viz.BIG_NUMBER, metrics=[Metric(label="Orders", sql="COUNT(*)")])
    chart = superset.ensure_chart(spec, dataset_id=dataset.id, dashboard_key=key)
    board = superset.ensure_dashboard(slug=key, title="Test", rows=[[ChartSlot(chart_id=chart.id, title="Orders", width=12)]])
    return board.id, chart.id, dataset.id


def test_deleting_a_sheet_removes_what_it_built_and_nothing_else(admin, services, workspace):
    project, sheet = workspace
    slug = project(admin)["slug"]
    key = f"nova-test{uuid.uuid4().hex[:7]}"
    bystander = f"nova-test{uuid.uuid4().hex[:7]}"
    board, chart, dataset = superset_sheet(services, key)
    kept = superset_sheet(services, bystander)
    item = sheet(slug, "a@b.c")
    with services.pool.connection() as conn:
        conn.execute("UPDATE dashboards SET slug = %s, thread_id = %s, superset_id = %s WHERE slug = %s", (key, key, board, item))
    try:
        assert admin.delete(f"/api/projects/{slug}/sheets/{key}").status_code == 200
        assert admin.get(f"/api/projects/{slug}").json()["sheets"] == []
        assert services.superset.datasets_named(f"{key}__") == []
        assert [found.id for found in services.superset.datasets_named(f"{bystander}__")] == [kept[2]]
    finally:
        services.superset.delete_dashboard(kept[0])
        services.superset.delete_chart(kept[1])
        services.superset.delete_dataset(kept[2])


def test_a_sheet_the_agent_is_working_on_cant_be_deleted(admin, services, workspace):
    project, sheet = workspace
    slug = project(admin)["slug"]
    item = sheet(slug, "a@b.c")
    run = f"run-{uuid.uuid4().hex[:12]}"
    with services.pool.connection() as conn:
        conn.execute("INSERT INTO runs (run_id, thread_id, prompt, status, created_by) VALUES (%s, %s, 'p', 'running', 'a@b.c')", (run, item))
    try:
        assert admin.delete(f"/api/projects/{slug}/sheets/{item}").status_code == 409
        assert admin.delete(f"/api/projects/{slug}").status_code == 409
    finally:
        with services.pool.connection() as conn:
            conn.execute("DELETE FROM runs WHERE run_id = %s", (run,))


def test_deleting_a_project_removes_its_sheets(admin, services, workspace):
    project, sheet = workspace
    slug = project(admin)["slug"]
    item = sheet(slug, "a@b.c")
    assert admin.delete(f"/api/projects/{slug}").status_code == 200
    assert admin.get(f"/api/projects/{slug}").status_code == 404
    with services.pool.connection() as conn:
        assert conn.execute("SELECT 1 FROM dashboards WHERE slug = %s", (item,)).fetchone() is None


def test_the_dashboard_list_says_where_each_sheet_lives(admin, workspace):
    project, sheet = workspace
    created = project(admin, "Where it lives")
    item = sheet(created["slug"], "a@b.c", name="A sheet")
    row = next(entry for entry in admin.get("/api/dashboards").json() if entry["slug"] == item)
    assert (row["project_slug"], row["project_name"], row["sheet_name"]) == (created["slug"], "Where it lives", "A sheet")


def test_moving_to_projects_doesnt_share_anything_new(services):
    """Sharing moved from dashboards to projects. Putting an owner's dashboards in one project
    would share the unshared ones along with the shared ones."""
    migration = re.search(r"DO \$\$.*?END \$\$;", (Path(agent.__file__).parent / "schema.sql").read_text(), re.S).group()
    schema = f"migration_test_{uuid.uuid4().hex[:8]}"
    # Everything happens in a scratch schema, in a transaction that's always rolled back. The pool
    # autocommits, so without the transaction SET LOCAL would do nothing.
    with services.pool.connection() as conn, conn.transaction(force_rollback=True):
        conn.execute(f"CREATE SCHEMA {schema}")
        conn.execute(f"SET LOCAL search_path TO {schema}, public")
        conn.execute("""CREATE TABLE projects (id uuid PRIMARY KEY DEFAULT gen_random_uuid(), slug text UNIQUE, name text,
                                              created_by text, shared boolean NOT NULL DEFAULT false)""")
        conn.execute("""CREATE TABLE dashboards (slug text PRIMARY KEY, title text, created_by text, shared boolean,
                                                created_at timestamptz DEFAULT now(), project_id uuid, sheet_name text, position integer)""")
        conn.execute("""INSERT INTO dashboards (slug, title, created_by, shared) VALUES
                        ('a-shared', 'Shared', 'a@b.c', true), ('a-private', 'Private', 'a@b.c', false),
                        ('a-private-2', 'Private 2', 'a@b.c', false), ('b-private', 'Hers', 'b@b.c', false)""")
        conn.execute(migration)
        rows = conn.execute("""SELECT d.slug, p.shared, p.created_by, d.position FROM dashboards d
                               JOIN projects p ON p.id = d.project_id ORDER BY d.slug""").fetchall()
        columns = conn.execute("SELECT 1 FROM information_schema.columns WHERE table_schema = %s AND table_name = 'dashboards' "
                               "AND column_name = 'shared'", (schema,)).fetchone()
    assert {row[0]: row[1] for row in rows} == {"a-private": False, "a-private-2": False, "a-shared": True, "b-private": False}
    assert {row[0]: row[2] for row in rows}["b-private"] == "b@b.c"
    assert sorted(row[3] for row in rows if row[0].startswith("a-private")) == [0, 1]
    assert columns is None
