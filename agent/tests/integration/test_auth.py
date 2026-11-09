"""Signing in, SSO through a real OpenID Connect provider, and who can see what through the API."""

import html
import re
import uuid
from unittest import mock
from urllib.parse import urljoin

import httpx
import jwt
import pytest
from fastapi.testclient import TestClient

from agent.api.auth import SsoRefused, provision_sso_user
from agent.api.main import create_app
from agent.api.runs import RunManager
from agent.config import get_settings
from agent.tools.superset_payloads import ChartSpec, Metric, Viz

pytestmark = pytest.mark.integration

PUBLIC = {"/api/health", "/api/auth/options", "/api/auth/login", "/api/auth/logout", "/api/auth/sso/login", "/api/auth/sso/callback"}
PASSWORD = "a-long-test-password"
DEX_PASSWORD = "dex-test-password"
CALLBACK = "http://localhost:18000/api/auth/sso/callback"
SSO_EMAILS = ("person@nova.test", "sso-admin@nova.test", "outsider@other.test")


@pytest.fixture(scope="module")
def app():
    settings = get_settings()
    with mock.patch.object(RunManager, "mark_abandoned"), TestClient(create_app()) as client:
        yield client.app
        with client.app.state.services.pool.connection() as conn:
            conn.execute("DELETE FROM users WHERE email = ANY(%s)", ([settings.nova_admin_email, *SSO_EMAILS],))


@pytest.fixture(scope="module")
def services(app):
    return app.state.services


@pytest.fixture(scope="module")
def admin(app):
    settings = get_settings()
    client = TestClient(app)
    signed_in = client.post("/api/auth/login", json={"email": settings.nova_admin_email, "password": settings.nova_admin_password})
    assert signed_in.status_code == 200, signed_in.text
    return client


@pytest.fixture
def make_user(app, admin, services):
    """Creates people for one test, signed in, and removes exactly those people afterwards."""
    created = []

    def make(role: str = "analyst") -> TestClient:
        email = f"user-{uuid.uuid4().hex[:10]}@nova.local"
        response = admin.post("/api/users", json={"email": email, "password": PASSWORD, "role": role})
        assert response.status_code == 201, response.text
        created.append(email)
        client = TestClient(app)
        assert client.post("/api/auth/login", json={"email": email, "password": PASSWORD}).status_code == 200
        client.email, client.user_id = email, response.json()["id"]
        return client

    yield make
    with services.pool.connection() as conn:
        conn.execute("DELETE FROM users WHERE email = ANY(%s)", (created,))


@pytest.fixture
def dashboard_row(services):
    """Adds rows to the dashboards table for one test. Only those rows are removed afterwards."""
    slugs = []

    def add(owner: str, *, shared: bool = False, catalogs: tuple[str, ...] = ("demo_rental",)) -> str:
        slug = f"auth-test-{uuid.uuid4().hex[:8]}"  # the project and its only sheet share a name, for brevity
        with services.pool.connection() as conn:
            project = conn.execute("INSERT INTO projects (slug, name, created_by, shared) VALUES (%s, 'Auth test', %s, %s) RETURNING id",
                                   (slug, owner, shared)).fetchone()[0]
            conn.execute("""INSERT INTO dashboards (slug, thread_id, superset_id, embed_uuid, title, created_by, catalogs, project_id)
                            VALUES (%s, %s, 0, %s, 'Auth test', %s, %s, %s)""",
                         (slug, slug, str(uuid.uuid4()), owner, list(catalogs), project))
        slugs.append(slug)
        return slug

    yield add
    with services.pool.connection() as conn:
        conn.execute("DELETE FROM dashboards WHERE slug = ANY(%s)", (slugs,))
        conn.execute("DELETE FROM projects WHERE slug = ANY(%s)", (slugs,))


def filled(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "1", path)


def api_routes(app):
    """From the OpenAPI schema, because `app.routes` doesn't list what's in included routers."""
    for path, operations in app.openapi()["paths"].items():
        if path not in PUBLIC:
            for method in operations:
                yield method.upper(), filled(path)


def test_every_route_needs_a_session(app):
    """Walks the app, so a route added later is covered without anyone remembering to."""
    anonymous = TestClient(app)
    routes = list(api_routes(app))
    assert {("GET", "/api/users"), ("POST", "/api/sources"), ("GET", "/api/runs")} <= set(routes)
    assert len(routes) > 25
    for method, path in routes:
        assert anonymous.request(method, path).status_code == 401, f"{method} {path} is open"


def test_a_forged_session_is_refused(app):
    forged = jwt.encode({"sub": str(uuid.uuid4()), "exp": 9999999999}, "the-wrong-key-" * 4, algorithm="HS256")
    client = TestClient(app, cookies={"nova_session": forged})
    assert client.get("/api/auth/me").status_code == 401


@pytest.mark.parametrize("method, path", [
    ("GET", "/api/users"), ("POST", "/api/users"), ("PATCH", "/api/users/1"), ("PUT", "/api/users/1/access"),
    ("POST", "/api/sources"), ("POST", "/api/sources/test"), ("GET", "/api/sources/demos"), ("POST", "/api/sources/demos/demo-shop"),
    ("POST", "/api/sources/demo-shop/profile"), ("GET", "/api/sources/demo-shop/tenancy"), ("PUT", "/api/sources/demo-shop/tenancy"),
    ("GET", "/api/sources/demo-shop/tenants"), ("GET", "/api/skills/demo-shop"), ("POST", "/api/skills/demo-shop/approve"),
])
def test_administration_is_for_administrators(make_user, method, path):
    analyst = make_user("analyst")
    assert analyst.request(method, path, json={}).status_code == 403


def test_a_viewer_cant_start_a_run(make_user):
    assert make_user("viewer").post("/api/runs", json={"prompt": "p"}).status_code == 403


def test_sign_in_sets_a_protected_cookie_and_sign_out_clears_it(app, make_user):
    user = make_user()
    client = TestClient(app)
    response = client.post("/api/auth/login", json={"email": user.email.upper(), "password": PASSWORD})
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    assert "password" not in response.text
    assert client.get("/api/auth/me").json()["email"] == user.email
    client.post("/api/auth/logout")
    assert client.get("/api/auth/me").status_code == 401


def test_a_wrong_password_and_an_unknown_email_look_the_same(app, make_user):
    client = TestClient(app)
    wrong = client.post("/api/auth/login", json={"email": make_user().email, "password": "wrong-password"})
    unknown = client.post("/api/auth/login", json={"email": "nobody@nova.local", "password": "wrong-password"})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()


def test_repeated_failures_are_throttled(app):
    client = TestClient(app)
    email = f"guess-{uuid.uuid4().hex[:8]}@nova.local"
    codes = [client.post("/api/auth/login", json={"email": email, "password": "x"}).status_code for _ in range(6)]
    assert codes == [401] * 5 + [429]


def test_disabling_someone_ends_their_session_at_once(admin, make_user):
    user = make_user()
    assert user.get("/api/auth/me").status_code == 200
    assert admin.patch(f"/api/users/{user.user_id}", json={"is_active": False}).status_code == 200
    assert user.get("/api/auth/me").status_code == 401


def test_changing_a_role_applies_at_once(admin, make_user):
    user = make_user("analyst")
    assert user.get("/api/users").status_code == 403
    admin.patch(f"/api/users/{user.user_id}", json={"role": "admin"})
    assert user.get("/api/users").status_code == 200


def test_you_cant_remove_your_own_admin_access(admin):
    me = admin.get("/api/auth/me").json()
    assert admin.patch(f"/api/users/{me['id']}", json={"role": "viewer"}).status_code == 400
    assert admin.patch(f"/api/users/{me['id']}", json={"is_active": False}).status_code == 400


def test_a_refused_request_doesnt_repeat_the_password(admin):
    response = admin.post("/api/sources", json={"name": "x", "kind": "postgres", "password": "do-not-echo-me"})
    assert response.status_code == 422
    assert "do-not-echo-me" not in response.text
    assert "host" in response.json()["error"]


def test_no_secret_comes_back_from_sources(admin):
    assert "credentials" not in admin.get("/api/sources").text


def test_a_new_user_sees_no_sources(make_user):
    assert make_user().get("/api/sources").json() == []


def test_a_user_sees_only_their_granted_sources(admin, make_user, services):
    user = make_user()
    assert admin.put(f"/api/users/{user.user_id}/access", json=[{"source": "demo-shop"}]).status_code == 200
    listed = user.get("/api/sources").json()
    assert [source["slug"] for source in listed] == ["demo-shop"]
    assert set(listed[0]) <= {"slug", "display_name", "dialect", "status", "skill"}


def test_tenants_cant_be_granted_on_a_source_that_isnt_multi_tenant(admin, make_user):
    user = make_user()
    response = admin.put(f"/api/users/{user.user_id}/access", json=[{"source": "demo-shop", "tenant_ids": ["1"]}])
    assert response.status_code == 400


def test_runs_belong_to_whoever_started_them(make_user, services):
    owner, other = make_user(), make_user()
    run = f"run-{uuid.uuid4().hex[:12]}"
    thread = f"thread-{uuid.uuid4().hex[:12]}"
    with services.pool.connection() as conn:
        conn.execute("INSERT INTO runs (run_id, thread_id, prompt, status, created_by) VALUES (%s, %s, 'p', 'failed', %s)",
                     (run, thread, owner.email))
    try:
        assert owner.get(f"/api/runs/{run}").status_code == 200
        assert [item["run_id"] for item in owner.get("/api/runs").json()] == [run]
        assert other.get(f"/api/runs/{run}").status_code == 404
        assert other.get(f"/api/runs/{run}/events").status_code == 404
        assert other.post(f"/api/runs/{run}/resume", json={}).status_code == 404
        assert other.get("/api/runs").json() == []
        assert other.post("/api/runs", json={"prompt": "p", "thread_id": thread}).status_code == 404
    finally:
        with services.pool.connection() as conn:
            conn.execute("DELETE FROM runs WHERE run_id = %s", (run,))


def test_dashboards_are_private_until_shared(admin, make_user, dashboard_row):
    owner, other = make_user(), make_user()
    admin.put(f"/api/users/{other.user_id}/access", json=[{"source": "demo-rental"}])
    slug = dashboard_row(owner.email)
    assert slug not in [row["slug"] for row in other.get("/api/dashboards").json()]
    assert other.get(f"/api/dashboards/{slug}/embed").status_code == 404
    assert admin.patch(f"/api/projects/{slug}", json={"shared": True}).status_code == 200
    assert slug in [row["slug"] for row in other.get("/api/dashboards").json()]


def test_a_shared_dashboard_stays_hidden_without_access_to_its_data(make_user, dashboard_row):
    outsider = make_user()
    slug = dashboard_row("someone@nova.local", shared=True)
    assert slug not in [row["slug"] for row in outsider.get("/api/dashboards").json()]
    assert outsider.get(f"/api/dashboards/{slug}/embed").status_code == 404


def test_losing_access_hides_your_own_dashboards(admin, make_user, dashboard_row):
    owner = make_user()
    admin.put(f"/api/users/{owner.user_id}/access", json=[{"source": "demo-rental"}])
    slug = dashboard_row(owner.email)
    assert slug in [row["slug"] for row in owner.get("/api/dashboards").json()]
    admin.put(f"/api/users/{owner.user_id}/access", json=[])
    assert slug not in [row["slug"] for row in owner.get("/api/dashboards").json()]
    assert owner.get(f"/api/dashboards/{slug}/embed").status_code == 404


def test_a_dashboard_with_no_record_of_its_sources_cant_be_shared(admin, dashboard_row):
    slug = dashboard_row("someone@nova.local", catalogs=())
    assert admin.patch(f"/api/projects/{slug}", json={"shared": True}).status_code == 400


def test_a_chart_preview_is_only_for_whoever_built_it(admin, make_user, services):
    """Previews reach Superset as an admin, so nothing else would stop this."""
    owner, other = make_user(), make_user()
    key = f"preview-{uuid.uuid4().hex[:8]}"
    superset = services.superset
    dataset = superset.ensure_dataset(key=key, name=f"{key}__sales", database_id=superset.database_id("Nova Trino"),
                                      sql="SELECT status FROM demo_shop.shop.orders")
    spec = ChartSpec(key="count", title="Orders", viz=Viz.BIG_NUMBER, metrics=[Metric(label="Orders", sql="COUNT(*)")])
    chart = superset.ensure_chart(spec, dataset_id=dataset.id, dashboard_key=key)
    run = f"run-{uuid.uuid4().hex[:12]}"
    with services.pool.connection() as conn:
        conn.execute("""INSERT INTO runs (run_id, thread_id, prompt, status, created_by, dashboard_key)
                        VALUES (%s, %s, 'p', 'failed', %s, %s)""", (run, run, owner.email, key))
    try:
        assert owner.get(f"/api/charts/{chart.id}/preview").status_code == 200
        assert other.get(f"/api/charts/{chart.id}/preview").status_code == 404
    finally:
        with services.pool.connection() as conn:
            conn.execute("DELETE FROM runs WHERE run_id = %s", (run,))
        superset.delete_chart(chart.id)
        superset.delete_dataset(dataset.id)


# --- SSO, through Dex ---------------------------------------------------------------------------

def through_dex(client: TestClient, email: str, password: str = DEX_PASSWORD) -> httpx.Response:
    """Do what a browser does: start at Nova, sign in on the provider's page, come back."""
    started = client.get("/api/auth/sso/login", follow_redirects=False)
    assert started.status_code == 302, started.text
    with httpx.Client(follow_redirects=False, timeout=30) as provider:
        response = provider.get(started.headers["location"])
        while response.is_redirect:
            response = provider.get(urljoin(str(response.url), response.headers["location"]))
        action = html.unescape(re.search(r'<form[^>]*action="([^"]+)"', response.text).group(1))
        response = provider.post(urljoin(str(response.url), action), data={"login": email, "password": password})
        while response.is_redirect:
            target = urljoin(str(response.url), response.headers["location"])
            if target.startswith(CALLBACK):
                return client.get(target.removeprefix("http://localhost:18000"), follow_redirects=False)
            response = provider.get(target)
    raise AssertionError(f"the provider didn't sign in: {response.status_code}")


@pytest.fixture
def forget_sso(services):
    with services.pool.connection() as conn:
        conn.execute("DELETE FROM users WHERE email = ANY(%s)", (list(SSO_EMAILS),))


def test_sso_is_offered_once_configured(app):
    assert TestClient(app).get("/api/auth/options").json() == {"sso": {"enabled": True, "name": "Test SSO"}}


def test_sso_signs_someone_in_with_no_access(app, forget_sso):
    client = TestClient(app)
    response = through_dex(client, "person@nova.test")
    assert response.status_code == 302
    assert response.headers["location"].endswith("/builder")
    me = client.get("/api/auth/me").json()
    assert (me["email"], me["role"], me["uses_sso"]) == ("person@nova.test", "viewer", True)
    assert client.get("/api/sources").json() == []
    assert client.get("/api/dashboards").json() == []
    assert client.post("/api/runs", json={"prompt": "p"}).status_code == 403


def test_signing_in_again_finds_the_same_person(app, forget_sso):
    first, second = TestClient(app), TestClient(app)
    through_dex(first, "person@nova.test")
    through_dex(second, "person@nova.test")
    assert first.get("/api/auth/me").json()["id"] == second.get("/api/auth/me").json()["id"]


def test_listed_emails_become_administrators(app, forget_sso):
    client = TestClient(app)
    through_dex(client, "sso-admin@nova.test")
    assert client.get("/api/auth/me").json()["role"] == "admin"


def test_a_domain_outside_the_allow_list_is_refused(app, forget_sso):
    client = TestClient(app)
    response = through_dex(client, "outsider@other.test")
    assert response.headers["location"].endswith("/login?error=domain_not_allowed")
    assert client.get("/api/auth/me").status_code == 401


def test_sso_links_to_an_account_made_earlier(admin, app, forget_sso):
    created = admin.post("/api/users", json={"email": "person@nova.test", "password": PASSWORD, "role": "analyst"}).json()
    client = TestClient(app)
    through_dex(client, "person@nova.test")
    me = client.get("/api/auth/me").json()
    assert (me["id"], me["role"]) == (created["id"], "analyst")


def test_a_disabled_person_cant_sign_in_with_sso(admin, app, forget_sso):
    client = TestClient(app)
    through_dex(client, "person@nova.test")
    admin.patch(f"/api/users/{client.get('/api/auth/me').json()['id']}", json={"is_active": False})
    again = TestClient(app)
    assert through_dex(again, "person@nova.test").headers["location"].endswith("/login?error=account_disabled")
    assert again.get("/api/auth/me").status_code == 401


def test_a_callback_with_a_forged_state_is_refused(app):
    client = TestClient(app)
    client.get("/api/auth/sso/login", follow_redirects=False)
    response = client.get("/api/auth/sso/callback?code=stolen&state=forged", follow_redirects=False)
    assert response.headers["location"].endswith("/login?error=sso_failed")
    assert client.get("/api/auth/me").status_code == 401


def test_an_unverified_email_cant_take_over_an_account(admin, services):
    """The provider didn't say it verified this email, so it proves nothing."""
    email = f"victim-{uuid.uuid4().hex[:8]}@nova.local"
    created = admin.post("/api/users", json={"email": email, "password": PASSWORD, "role": "admin"}).json()
    try:
        for claims in ({"sub": "attacker", "email": email}, {"sub": "attacker", "email": email, "email_verified": False}):
            with pytest.raises(SsoRefused):
                provision_sso_user(services.pool, claims, services.settings)
        with services.pool.connection() as conn:
            assert conn.execute("SELECT sso_subject FROM users WHERE id = %s::uuid", (created["id"],)).fetchone()[0] is None
    finally:
        with services.pool.connection() as conn:
            conn.execute("DELETE FROM users WHERE email = %s", (email,))


def test_an_unverified_email_cant_claim_administrator(services, forget_sso):
    claims = {"sub": f"sub-{uuid.uuid4().hex}", "email": "sso-admin@nova.test"}
    assert provision_sso_user(services.pool, claims, services.settings).role == "viewer"
