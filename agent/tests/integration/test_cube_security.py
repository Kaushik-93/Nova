"""Per-tenant security in the semantic layer, against real Cube, Trino and the rental database.

A hand-written model is published the way Nova publishes one, and queried as different people.
The expected answers come straight from Postgres, without going through anything being tested.
"""

import os
import shutil
import time
import uuid

import httpx
import jwt
import psycopg
import pytest

from agent.config import get_settings
from agent.db import apply_schema, create_pool
from agent.harness.access import Grant, UserAccess
from agent.harness.audit import AuditLog, RunContext
from agent.harness.budgets import BudgetLimits, RunBudget
from agent.harness.errors import PermissionFailure, QueryFailure
from agent.harness.executor import SqlExecutor
from agent.semantic.access import Tenancy
from agent.semantic.client import CubeClient, CubeIdentity, identity_for
from agent.semantic.model import Cube, DataModel, Dimension, Join, Measure
from agent.tools.profiler import ColumnProfile, Profiler, SourceProfile, TableProfile
from agent.tools.skill_store import SkillStore
from agent.tools.sources import Source

pytestmark = pytest.mark.integration

CATALOG = "demo_rental"
TENANCY = Tenancy(column="tenant_id", shared_tables=["rental.fx_rates"])
RENTAL = Source(id="rental", slug="demo-rental", display_name="Rental", trino_catalog=CATALOG, dialect="postgres",
                tenant_column="tenant_id", tenant_table="rental.tenants", tenant_label_column="tenant_name",
                shared_tables=["rental.fx_rates"])
MINE = ["3", "7"]


def direct(sql: str, *params) -> list[tuple]:
    with psycopg.connect(host="demo-postgres", dbname="rental", user="nova_ro",
                         password=os.environ["DEMO_RO_PASSWORD"]) as conn:
        return conn.execute(sql, params).fetchall()


@pytest.fixture(scope="module")
def pool():
    pool = create_pool(get_settings().database_url)
    apply_schema(pool)
    yield pool
    pool.close()


@pytest.fixture(scope="module")
def cube():
    settings = get_settings()
    client = CubeClient(settings.cube_url, settings.cubejs_api_secret)
    yield client
    client.close()


@pytest.fixture(scope="module")
def names():
    """Cube names are global, so these can't collide with a real skill's."""
    prefix = f"{CATALOG}__t{uuid.uuid4().hex[:8]}"
    return {"invoices": f"{prefix}_invoices", "fx": f"{prefix}_fx", "lines": f"{prefix}_lines"}


@pytest.fixture(scope="module")
def published(pool, names):
    """A model published the way Nova publishes one: with the tenant dimension and manifest Nova adds."""
    settings = get_settings()
    store = SkillStore(settings.nova_skills_dir, pool)
    context = RunContext(run_id=f"test-{uuid.uuid4().hex[:10]}", user_id="tester")
    shape = Profiler(SqlExecutor(settings.trino_url, AuditLog(pool)), context=context,
                     budget=RunBudget(BudgetLimits())).read_shape(CATALOG)
    profile = SourceProfile(source_slug="test", catalog=CATALOG, dialect="postgres", fingerprint="f",
                            profiled_at="2026-11-09T00:00:00Z", tables=[
                                TableProfile(schema_name=name.split(".")[0], name=name.split(".")[1],
                                             columns=[ColumnProfile(name=c, type=t, nullable=True) for c, t in cols.items()])
                                for name, cols in shape.items()])
    invoices, fx, lines = names["invoices"], names["fx"], names["lines"]
    model = DataModel(cubes=[
        Cube(name=invoices, sql_table=f"{CATALOG}.rental.invoices",
             joins=[Join(name=fx, relationship="many_to_one",
                         sql=f"{{{fx}}}.currency = {{CUBE}}.currency AND "
                             f"{{{fx}}}.rate_month = CAST(date_trunc('month', {{CUBE}}.issued_on) AS date)")],
             dimensions=[Dimension(name="invoice_id", sql="{CUBE}.invoice_id", type="number", primary_key=True),
                         Dimension(name="tenant", sql="CAST({CUBE}.tenant_id AS varchar)", type="string"),
                         Dimension(name="status", sql="{CUBE}.status", type="string"),
                         Dimension(name="issued_on", sql="{CUBE}.issued_on", type="time")],
             measures=[Measure(name="count", type="count")]),
        Cube(name=fx, sql_table=f"{CATALOG}.rental.fx_rates",
             dimensions=[Dimension(name="key", sql="{CUBE}.currency || CAST({CUBE}.rate_month AS varchar)",
                                   type="string", primary_key=True),
                         Dimension(name="currency", sql="{CUBE}.currency", type="string")],
             measures=[Measure(name="rates", type="count")]),
        Cube(name=lines, sql_table=f"{CATALOG}.rental.invoice_lines",
             dimensions=[Dimension(name="id", sql="{CUBE}.invoice_line_id", type="number", primary_key=True)],
             measures=[Measure(name="count", type="count")]),
    ])
    slug = f"test-security-{uuid.uuid4().hex[:8]}"
    store.path(slug).mkdir(parents=True)
    try:
        store._write_model(slug, model, profile=profile, tenancy=TENANCY)
        yield store.read_access(slug)
    finally:
        shutil.rmtree(store.path(slug), ignore_errors=True)


def admin() -> CubeIdentity:
    return CubeIdentity(sub="admin@nova.test", role="admin", catalogs=[CATALOG])


def limited(tenants=MINE) -> CubeIdentity:
    return CubeIdentity(sub="tenant@nova.test", role="analyst", catalogs=[CATALOG], scopes={CATALOG: list(tenants)})


def by_tenant(cube: CubeClient, identity: CubeIdentity, names, **extra) -> dict[str, int]:
    invoices = names["invoices"]
    rows = cube.load(identity, {"measures": [f"{invoices}.count"], "dimensions": [f"{invoices}.tenant"], **extra}).data
    return {row[f"{invoices}.tenant"]: int(row[f"{invoices}.count"]) for row in rows}


def test_the_manifest_is_what_nova_derived(published, names):
    assert published["cubes"] == {names["invoices"]: {"tenant_dimension": "nova_tenant"},
                                  names["fx"]: {"shared": True}, names["lines"]: {}}


def test_an_administrator_sees_every_tenant(cube, published, names):
    expected = {str(t): n for t, n in direct("SELECT tenant_id, count(*) FROM rental.invoices GROUP BY 1")}
    assert by_tenant(cube, admin(), names) == expected


def test_a_limited_person_sees_only_their_tenants(cube, published, names):
    expected = {str(t): n for t, n in direct("SELECT tenant_id, count(*) FROM rental.invoices "
                                              "WHERE tenant_id IN (3, 7) GROUP BY 1")}
    assert by_tenant(cube, limited(), names) == expected


def test_a_total_is_only_over_their_rows(cube, published, names):
    [(expected,)] = direct("SELECT count(*) FROM rental.invoices WHERE tenant_id IN (3, 7)")
    rows = cube.load(limited(), {"measures": [f"{names['invoices']}.count"]}).data
    assert int(rows[0][f"{names['invoices']}.count"]) == expected


def test_an_or_filter_cant_widen_the_limit(cube, published, names):
    invoices = names["invoices"]
    sneaky = {"or": [{"member": f"{invoices}.tenant", "operator": "equals", "values": ["1"]},
                     {"member": f"{invoices}.status", "operator": "set"}]}
    assert set(by_tenant(cube, limited(), names, filters=[sneaky])) <= set(MINE)


def test_asking_for_another_tenant_finds_nothing(cube, published, names):
    other = {"member": f"{names['invoices']}.tenant", "operator": "equals", "values": ["5"]}
    assert by_tenant(cube, limited(), names, filters=[other]) == {}


def test_a_cube_that_cant_be_limited_is_refused(cube, published, names):
    with pytest.raises(PermissionFailure, match="can't be limited"):
        cube.load(limited(), {"measures": [f"{names['lines']}.count"]})
    assert cube.load(admin(), {"measures": [f"{names['lines']}.count"]}).data  # but not to everyone


def test_shared_data_is_readable_and_joins_from_limited_data(cube, published, names):
    [(rates,)] = direct("SELECT count(*) FROM rental.fx_rates")
    assert int(cube.load(limited(), {"measures": [f"{names['fx']}.rates"]}).data[0][f"{names['fx']}.rates"]) == rates
    rows = cube.load(limited(), {"measures": [f"{names['invoices']}.count"], "dimensions": [f"{names['fx']}.currency"]})
    [(expected,)] = direct("SELECT count(*) FROM rental.invoices WHERE tenant_id IN (3, 7)")
    assert sum(int(row[f"{names['invoices']}.count"]) for row in rows.data) == expected


def test_the_hidden_tenant_dimension_cant_be_asked_for(cube, published, names):
    with pytest.raises(QueryFailure, match="hidden member"):
        cube.load(limited(), {"dimensions": [f"{names['invoices']}.nova_tenant"]})


@pytest.mark.parametrize("identity, reason", [
    (CubeIdentity(sub="x", role="analyst", catalogs=[]), "isn't available"),
    (limited(tenants=[]), "no tenants"),
], ids=["no grant", "no tenants"])
def test_no_grant_means_no_data(cube, published, names, identity, reason):
    with pytest.raises(PermissionFailure, match=reason):
        cube.load(identity, {"measures": [f"{names['invoices']}.count"]})


@pytest.mark.parametrize("member", ["{invoices}.status.oops", "{invoices}.{fx}.currency"])
def test_paths_through_joins_are_refused(cube, published, names, member):
    # Cube itself refuses most of these before queryRewrite sees them; queryRewrite is the second line.
    with pytest.raises((PermissionFailure, QueryFailure)):
        cube.load(limited(), {"measures": [f"{names['invoices']}.count"],
                              "dimensions": [member.format(invoices=names["invoices"], fx=names["fx"])]})


def test_the_sql_shown_carries_the_limit(cube, published, names):
    sql = cube.sql(limited(), {"measures": [f"{names['invoices']}.count"]})
    assert "tenant_id AS varchar" in sql and " IN (" in sql


def test_a_draft_token_reaches_only_its_draft(cube, published, names):
    with pytest.raises((PermissionFailure, QueryFailure)):
        cube.load(admin().model_copy(update={"model": f"draft-{uuid.uuid4().hex[:16]}"}),
                  {"measures": [f"{names['invoices']}.count"]})


@pytest.mark.parametrize("token", [
    lambda claims: jwt.encode(claims, "not-the-secret", algorithm="HS256"),
    lambda claims: jwt.encode({**claims, "exp": int(time.time()) - 5}, os.environ["CUBEJS_API_SECRET"], algorithm="HS256"),
    lambda claims: jwt.encode({k: v for k, v in claims.items() if k != "exp"}, os.environ["CUBEJS_API_SECRET"],
                              algorithm="HS256"),
    lambda claims: jwt.encode(claims, "", algorithm="none"),
    lambda claims: "",
], ids=["forged", "expired", "no expiry", "unsigned", "missing"])
def test_only_a_valid_token_from_nova_is_accepted(published, names, token):
    claims = {**admin().model_dump(), "exp": int(time.time()) + 60}
    response = httpx.get(f"{get_settings().cube_url}/cubejs-api/v1/load",
                         params={"query": f'{{"measures": ["{names["invoices"]}.count"]}}'},
                         headers={"Authorization": token(claims)})
    assert response.status_code >= 400
    assert "data" not in response.json()


def test_the_identity_is_built_from_grants():
    grants = {"rental": Grant(source_id="rental", tenant_ids=["3", "7"])}
    identity = identity_for(UserAccess(email="t@nova.test", role="analyst", grants=grants), [RENTAL], user_id="t@nova.test")
    assert (identity.catalogs, identity.scopes) == ([CATALOG], {CATALOG: MINE})
    nobody = identity_for(UserAccess(email="n@nova.test", role="analyst"), [RENTAL], user_id="n@nova.test")
    assert (nobody.catalogs, nobody.scopes) == ([], {})


# --- Through the API, as a signed-in person -----------------------------------------------------

@pytest.fixture(scope="module")
def api():
    from unittest import mock

    from fastapi.testclient import TestClient

    from agent.api.main import create_app
    from agent.api.runs import RunManager

    settings = get_settings()
    with mock.patch.object(RunManager, "mark_abandoned"), TestClient(create_app()) as client:
        signed_in = client.post("/api/auth/login", json={"email": settings.nova_admin_email,
                                                         "password": settings.nova_admin_password})
        assert signed_in.status_code == 200, signed_in.text
        yield client


@pytest.fixture
def tenant_user(api):
    from fastapi.testclient import TestClient

    email, password = f"tenant-{uuid.uuid4().hex[:8]}@nova.local", "a-long-test-password"
    created = api.post("/api/users", json={"email": email, "password": password, "role": "analyst"})
    assert created.status_code == 201, created.text
    granted = api.put(f"/api/users/{created.json()['id']}/access", json=[{"source": "demo-rental", "tenant_ids": MINE}])
    assert granted.status_code == 200, granted.text
    client = TestClient(api.app)
    assert client.post("/api/auth/login", json={"email": email, "password": password}).status_code == 200
    yield client
    with api.app.state.services.pool.connection() as conn:
        conn.execute("DELETE FROM users WHERE email = %s", (email,))


def test_the_query_api_answers_with_the_persons_own_rows(api, tenant_user, published, names):
    query = {"query": {"measures": [f"{names['invoices']}.count"], "dimensions": [f"{names['invoices']}.tenant"]}}
    mine = tenant_user.post("/api/semantic/query", json=query)
    everyone = api.post("/api/semantic/query", json=query)
    assert mine.status_code == 200, mine.text
    assert {row[f"{names['invoices']}.tenant"] for row in mine.json()["rows"]} == set(MINE)
    assert len(everyone.json()["rows"]) > len(MINE)
    assert mine.json()["certified"] is False  # nothing in this model has been reviewed


def test_the_api_refuses_what_cant_be_limited_and_exports_only_their_rows(tenant_user, published, names):
    refused = tenant_user.post("/api/semantic/query", json={"query": {"measures": [f"{names['lines']}.count"]}})
    assert refused.status_code == 403
    exported = tenant_user.post("/api/semantic/csv", json={"query": {"dimensions": [f"{names['invoices']}.tenant"],
                                                                       "measures": [f"{names['invoices']}.count"]}})
    assert exported.status_code == 200
    assert {line.split(",")[0] for line in exported.text.strip().splitlines()[1:]} == set(MINE)


def test_a_runs_queries_through_cube_are_audited_with_their_refusals(pool, published, names):
    from agent.harness.audit import AuditLog, RunContext
    from agent.semantic.client import AuditedCubeClient

    settings = get_settings()
    context = RunContext(run_id=f"test-audit-{uuid.uuid4().hex[:10]}", user_id="tenant@nova.test")
    audit = AuditLog(pool)
    client = AuditedCubeClient(settings.cube_url, settings.cubejs_api_secret, audit=audit, context=context)
    try:
        assert by_tenant(client, limited(), names)
        with pytest.raises(PermissionFailure):
            client.load(limited(), {"measures": [f"{names['lines']}.count"]})
    finally:
        client.close()
    ok, refused = audit.for_run(context.run_id)
    assert (ok["kind"], ok["status"], ok["target"], ok["user_id"]) == ("semantic_query", "ok", names["invoices"],
                                                                        "tenant@nova.test")
    assert ok["rows"] == 2 and f"{names['invoices']}.count" in ok["statement"]
    assert ok["detail"]["scopes"] == {CATALOG: MINE}
    assert (refused["kind"], refused["status"], refused["target"]) == ("semantic_query", "rejected", names["lines"])
    assert refused["detail"]["reason"]
