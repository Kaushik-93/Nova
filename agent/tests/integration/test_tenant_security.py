"""Per-tenant security, against real Trino and Superset on the rental database.

The expected answers come straight from Postgres, without going through anything being tested.
"""

import json
import os
import uuid
from types import SimpleNamespace

import httpx
import psycopg
import pytest

from agent.config import get_settings
from agent.db import apply_schema, create_pool
from agent.harness.access import Grant, UserAccess, build_policy, guest_rules
from agent.harness.audit import AuditLog, RunContext
from agent.harness.budgets import BudgetLimits, RunBudget
from agent.harness.errors import QueryFailure, SqlRejected
from agent.harness.executor import SqlExecutor
from agent.harness.validators import check_dataset
from agent.tools.profiler import ColumnProfile, Profiler, TableProfile
from agent.tools.sources import Source
from agent.tools.sql_tools import make_schema_tools
from agent.tools.superset_client import SupersetClient
from agent.tools.superset_payloads import ChartSlot, ChartSpec, Metric, Viz

pytestmark = pytest.mark.integration

CATALOG = "demo_rental"
AGREEMENTS = f"{CATALOG}.rental.rental_agreements"
INVOICES = f"{CATALOG}.rental.invoices"
VEHICLES = f"{CATALOG}.rental.vehicles"
TENANTS = f"{CATALOG}.rental.tenants"
RENTAL = Source(id="rental", slug="demo-rental", display_name="Rental", trino_catalog=CATALOG, dialect="postgres",
                tenant_column="tenant_id", tenant_table="rental.tenants", tenant_label_column="tenant_name",
                shared_tables=["rental.fx_rates"])
SHOP = Source(id="shop", slug="demo-shop", display_name="Shop", trino_catalog="demo_shop", dialect="postgres")


def direct(sql: str, *params) -> list[tuple]:
    with psycopg.connect(host="demo-postgres", dbname="rental", user="nova_ro", password=os.environ["DEMO_RO_PASSWORD"]) as conn:
        return conn.execute(sql, params).fetchall()


def access_to(*tenants: int, grants: dict | None = None) -> UserAccess:
    limited = {"rental": Grant(source_id="rental", tenant_ids=[str(tenant) for tenant in tenants])}
    return UserAccess(email="tenant@nova.test", role="viewer", grants=limited if grants is None else grants)


@pytest.fixture(scope="module")
def pool():
    pool = create_pool(get_settings().database_url)
    apply_schema(pool)
    yield pool
    pool.close()


@pytest.fixture(scope="module")
def executor(pool):
    return SqlExecutor(get_settings().trino_url, AuditLog(pool))


@pytest.fixture
def context():
    return RunContext(run_id=f"test-{uuid.uuid4().hex[:10]}", user_id="tester")


@pytest.fixture
def budget():
    return RunBudget(BudgetLimits())


@pytest.fixture(scope="module")
def store(executor):
    """The real table layout, read live. Skills aren't in the repo, so these can't rely on one."""
    context = RunContext(run_id=f"test-{uuid.uuid4().hex[:10]}", user_id="tester")
    shape = Profiler(executor, context=context, budget=RunBudget(BudgetLimits())).read_shape(CATALOG)
    tables = [TableProfile(schema_name=name.split(".")[0], name=name.split(".")[1],
                           columns=[ColumnProfile(name=column, type=kind, nullable=True) for column, kind in columns.items()])
              for name, columns in shape.items()]
    return SimpleNamespace(exists=lambda slug: True, read_profile=lambda slug: SimpleNamespace(tables=tables))


@pytest.fixture(scope="module")
def tenants():
    """Two tenants, busiest first, so a leak of either into the other would show."""
    return [row[0] for row in direct("""SELECT tenant_id FROM rental.rental_agreements GROUP BY 1
                                        ORDER BY COUNT(*) DESC LIMIT 2""")]


@pytest.fixture(scope="module")
def policy(store, tenants):
    return build_policy([RENTAL, SHOP], access_to(tenants[0]), store)


@pytest.fixture
def query(executor, context, budget, policy):
    def run(sql, *, using=None):
        return executor.run(sql, policy=using or policy, context=context, budget=budget).rows
    return run


def test_a_count_matches_postgres_for_that_tenant(query, tenants):
    expected = direct("SELECT COUNT(*) FROM rental.rental_agreements WHERE tenant_id = %s", tenants[0])[0][0]
    everyone = direct("SELECT COUNT(*) FROM rental.rental_agreements")[0][0]
    assert query(f"SELECT COUNT(*) FROM {AGREEMENTS}")[0][0] == expected
    assert 0 < expected < everyone


@pytest.mark.parametrize("sql", [
    f"SELECT DISTINCT tenant_id FROM {AGREEMENTS}",
    f"SELECT tenant_id, COUNT(*) FROM {AGREEMENTS} GROUP BY 1",
    f"SELECT DISTINCT tenant_id FROM {AGREEMENTS} WHERE tenant_id <> -1 OR 1 = 1",
    f"SELECT DISTINCT a.tenant_id FROM {AGREEMENTS} AS a",
    f"SELECT DISTINCT tenant_id FROM (SELECT * FROM {AGREEMENTS}) AS inner_query",
    f"WITH rental_agreements AS (SELECT * FROM {AGREEMENTS}) SELECT DISTINCT tenant_id FROM rental_agreements",
    f"SELECT tenant_id FROM {AGREEMENTS} UNION SELECT tenant_id FROM {INVOICES}",
    f"SELECT tenant_id FROM {AGREEMENTS} UNION ALL SELECT tenant_id FROM {VEHICLES}",
    f"SELECT DISTINCT i.tenant_id FROM {INVOICES} i JOIN {AGREEMENTS} a ON a.agreement_id = i.agreement_id",
    f"SELECT DISTINCT a.tenant_id FROM {AGREEMENTS} a LEFT JOIN {VEHICLES} v ON v.tenant_id <> a.tenant_id",
    f"SELECT DISTINCT tenant_id FROM {VEHICLES} WHERE tenant_id IN (SELECT tenant_id FROM {TENANTS})",
    f"SELECT DISTINCT (SELECT MAX(tenant_id) FROM {TENANTS}) FROM {VEHICLES}",
    f'SELECT DISTINCT tenant_id FROM "demo_rental"."rental"."rental_agreements"',
    f"SELECT DISTINCT tenant_id FROM DEMO_RENTAL.RENTAL.RENTAL_AGREEMENTS",
])
def test_every_way_of_reading_stays_inside_the_tenant(query, tenants, sql):
    assert {row[0] for row in query(sql)} == {tenants[0]}


def test_the_other_side_of_a_join_is_limited_too(query, tenants):
    """Even a join built to reach other tenants' rows only finds this tenant's."""
    rows = query(f"""SELECT COUNT(*) FROM {AGREEMENTS} a JOIN {VEHICLES} v ON v.tenant_id <> a.tenant_id""")
    assert rows[0][0] == 0


def test_shared_tables_stay_readable(query):
    assert query(f"SELECT COUNT(*) FROM {CATALOG}.rental.fx_rates")[0][0] == direct("SELECT COUNT(*) FROM rental.fx_rates")[0][0]


def test_a_table_that_cant_be_limited_is_refused(query):
    """invoice_lines has no tenant column, so it can't be shown safely."""
    with pytest.raises(SqlRejected, match="can't be limited"):
        query(f"SELECT COUNT(*) FROM {CATALOG}.rental.invoice_lines")


@pytest.mark.parametrize("sql", [
    f"SELECT table_name FROM {CATALOG}.information_schema.tables",
    "SELECT COUNT(*) FROM demo_shop.shop.orders",
    f"SELECT * FROM TABLE({CATALOG}.system.query(query => 'SELECT * FROM rental.tenants'))",
])
def test_ways_around_the_scope_are_refused(query, sql):
    with pytest.raises(SqlRejected):
        query(sql)


def test_two_tenants_sees_both_and_only_those(query, store, tenants):
    both = build_policy([RENTAL], access_to(*tenants), store)
    assert {row[0] for row in query(f"SELECT DISTINCT tenant_id FROM {AGREEMENTS}", using=both)} == set(tenants)


def test_no_tenants_sees_nothing(query, store):
    nothing = build_policy([RENTAL], access_to(), store)
    assert query(f"SELECT COUNT(*) FROM {AGREEMENTS}", using=nothing)[0][0] == 0


def test_a_whole_source_grant_sees_everyone(query, store):
    whole = build_policy([RENTAL], access_to(grants={"rental": Grant(source_id="rental")}), store)
    assert query(f"SELECT COUNT(*) FROM {AGREEMENTS}", using=whole)[0][0] == direct("SELECT COUNT(*) FROM rental.rental_agreements")[0][0]


def test_sampling_a_table_shows_only_that_tenants_rows(executor, policy, context, budget, tenants):
    tools = {tool.name: tool for tool in make_schema_tools(executor, policy=policy, context=context, budget=budget)}
    names = dict(direct("SELECT tenant_id, tenant_name FROM rental.tenants WHERE tenant_id = ANY(%s)", tenants))
    shown = tools["sample_table"].invoke({"table": TENANTS, "rows": 20})
    assert names[tenants[0]] in shown
    assert names[tenants[1]] not in shown


def test_a_dataset_on_a_multi_tenant_source_must_carry_the_tenant_column(executor, policy, context, budget):
    """Otherwise a dashboard's row rule would have nothing to apply to."""
    with pytest.raises(QueryFailure, match="multi-tenant"):
        check_dataset(f"SELECT status, COUNT(*) AS n FROM {VEHICLES} GROUP BY 1", grain=["status"], executor=executor,
                      policy=policy, context=context, budget=budget, tenant_columns={CATALOG: "tenant_id"})


def test_a_dataset_is_saved_with_the_builders_scope_inside_it(executor, policy, context, budget, tenants):
    """A tenant user's dataset can't later show more than they can see, whoever opens it."""
    checked = check_dataset(f"SELECT tenant_id, status, COUNT(*) AS n FROM {VEHICLES} GROUP BY 1, 2", grain=["tenant_id", "status"],
                            executor=executor, policy=policy, context=context, budget=budget,
                            tenant_columns={CATALOG: "tenant_id"})
    assert f"tenant_id IN ({tenants[0]})" in checked.sql


# --- The same rule in a dashboard ---------------------------------------------------------------

@pytest.fixture(scope="module")
def dashboard():
    settings = get_settings()
    key = f"tenant-test-{uuid.uuid4().hex[:8]}"
    with SupersetClient(settings.superset_url, settings.superset_admin_user, settings.superset_admin_password) as superset:
        dataset = superset.ensure_dataset(key=key, name=key, database_id=superset.database_id("Nova Trino"),
                                          sql=f"SELECT tenant_id, agreement_id FROM {AGREEMENTS}")
        spec = ChartSpec(key="count", title="Agreements", viz=Viz.BIG_NUMBER, metrics=[Metric(label="Agreements", sql="COUNT(*)")])
        chart = superset.ensure_chart(spec, dataset_id=dataset.id, dashboard_key=key)
        board = superset.ensure_dashboard(slug=key, title="Tenant test", rows=[[ChartSlot(chart_id=chart.id, title="Agreements", width=12)]])
        try:
            yield SimpleNamespace(superset=superset, chart=chart, board=board, embed=superset.enable_embedding(board.id, []))
        finally:
            superset.delete_dashboard(board.id)
            superset.delete_chart(chart.id)
            superset.delete_dataset(dataset.id)


def as_guest(dashboard, rules, change=None):
    """The request the embedded dashboard makes, optionally edited as an attacker would."""
    context = json.loads(dashboard.superset.chart_detail(dashboard.chart.id)["query_context"])
    context["form_data"] |= {"slice_id": dashboard.chart.id, "dashboardId": dashboard.board.id}
    context["force"] = True
    if change:
        change(context["queries"][0])
    token = dashboard.superset.guest_token(dashboard.embed, username="tenant@nova.test", rls=rules)
    return httpx.post(f"{get_settings().superset_url}/api/v1/chart/data", json=context, params={"dashboard_id": dashboard.board.id},
                      headers={"X-GuestToken": token}, timeout=60)


def agreements_seen(dashboard, rules):
    response = as_guest(dashboard, rules)
    assert response.status_code == 200, response.text
    return response.json()["result"][0]["data"][0]["Agreements"]


def test_a_dashboard_shows_a_tenant_the_same_number_as_postgres(dashboard, store, tenants):
    for tenant in tenants:
        rules = guest_rules(access_to(tenant), [RENTAL], [CATALOG], store)
        expected = direct("SELECT COUNT(*) FROM rental.rental_agreements WHERE tenant_id = %s", tenant)[0][0]
        assert agreements_seen(dashboard, rules) == expected


def test_a_dashboard_for_no_tenants_is_empty(dashboard, store):
    assert agreements_seen(dashboard, guest_rules(access_to(), [RENTAL], [CATALOG], store)) == 0


@pytest.mark.parametrize("change", [
    lambda query: query.update(columns=["tenant_id"], metrics=["count"]),
    lambda query: query.update(columns=["tenant_id"], metrics=["count"], filters=[{"col": "tenant_id", "op": "!=", "val": -1}]),
    lambda query: query.update(columns=["tenant_id"], metrics=["count"], extras={"where": "1=1) OR (1=1"}),
    lambda query: query.update(columns=["tenant_id"], metrics=["count"], extras={"having": "1=1) OR (1=1"}),
], ids=["group-by-tenant", "exclude-nothing", "break-out-of-where", "break-out-of-having"])
def test_a_guest_cant_widen_their_rows_by_editing_the_request(dashboard, store, tenants, change):
    """The browser is the attacker's, so what it asks for can't be trusted. The rule is in the
    signed token, and Superset adds it to whatever query arrives."""
    response = as_guest(dashboard, guest_rules(access_to(tenants[0]), [RENTAL], [CATALOG], store), change)
    if response.status_code == 200:
        rows = response.json()["result"][0]["data"]
        assert {row["tenant_id"] for row in rows} <= {tenants[0]}
    else:
        assert response.status_code in (400, 403, 422), response.text


def test_a_dashboard_and_the_agent_agree(dashboard, query, store, tenants):
    """The same grant gives the same number by both routes."""
    rules = guest_rules(access_to(tenants[0]), [RENTAL], [CATALOG], store)
    assert agreements_seen(dashboard, rules) == query(f"SELECT COUNT(*) FROM {AGREEMENTS}")[0][0]
