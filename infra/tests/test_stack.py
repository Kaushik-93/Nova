"""Phase 1: the real, running stack. Requires `make up`."""

import os
import time
import uuid
from pathlib import Path

import httpx
import psycopg
import pymysql
import pytest
import redis
import trino

from seed.generate import FIRST_DAY
from tests.conftest import connect_marketing, connect_shop, revenue_cents


# --- Superset ----------------------------------------------------------------

def test_superset_is_healthy(superset_url):
    assert httpx.get(f"{superset_url}/health", timeout=10).text == "OK"


def test_admin_can_log_in_and_is_admin(superset):
    me = superset.get("/api/v1/me/roles/")
    assert me.status_code == 200
    assert "Admin" in me.json()["result"]["roles"]


def test_wrong_password_is_rejected(superset_url):
    response = httpx.post(f"{superset_url}/api/v1/security/login", timeout=10, json={
        "username": os.environ["SUPERSET_ADMIN_USER"], "password": "wrong", "provider": "db",
    })
    assert response.status_code == 401


def test_trino_is_registered_read_only(superset, trino_database_id):
    database = superset.get(f"/api/v1/database/{trino_database_id}").json()["result"]
    assert database["backend"] == "trino"
    assert database["allow_dml"] is False
    assert database["allow_ctas"] is False
    assert database["allow_run_async"] is True


def _sqllab(superset, database_id, sql, *, run_async):
    return superset.post("/api/v1/sqllab/execute/", json={
        "database_id": database_id,
        "sql": sql,
        "runAsync": run_async,
        "client_id": uuid.uuid4().hex[:10],
    })


CROSS_SOURCE_SQL = """
    SELECT c.channel_code, COUNT(*) AS orders
    FROM demo_shop.shop.orders o
    JOIN demo_marketing.marketing.campaigns c
      ON UPPER(REPLACE(o.utm_campaign, '-', '_')) = c.campaign_code
    GROUP BY 1 ORDER BY 2 DESC
"""


def test_superset_runs_a_cross_source_query_through_trino(superset, trino_database_id):
    response = _sqllab(superset, trino_database_id, CROSS_SOURCE_SQL, run_async=False)
    assert response.status_code == 200, response.text
    assert {row["channel_code"] for row in response.json()["data"]} >= {"google_ads", "meta_ads"}


def test_async_queries_run_on_the_celery_worker(superset, trino_database_id):
    started = _sqllab(superset, trino_database_id, "SELECT COUNT(*) AS n FROM demo_shop.shop.orders", run_async=True)
    assert started.status_code == 202, started.text
    query_id = started.json()["query"]["queryId"]

    deadline = time.monotonic() + 60
    state = None
    while time.monotonic() < deadline:
        query = superset.get(f"/api/v1/query/{query_id}")
        assert query.status_code == 200, query.text
        state = query.json()["result"]["status"]
        if state in {"success", "failed", "stopped"}:
            break
        time.sleep(1)
    assert state == "success"
    assert query.json()["result"]["rows"] == 1


# --- Embedding ---------------------------------------------------------------

@pytest.fixture
def embedded_dashboard(superset, ui_origin):
    created = superset.post("/api/v1/dashboard/", json={
        "dashboard_title": f"phase1-embed-{uuid.uuid4().hex[:8]}", "published": True,
    })
    assert created.status_code == 201, created.text
    dashboard_id = created.json()["id"]
    embedded = superset.post(f"/api/v1/dashboard/{dashboard_id}/embedded", json={"allowed_domains": [ui_origin]})
    assert embedded.status_code == 200, embedded.text
    yield {"id": dashboard_id, "embed_uuid": embedded.json()["result"]["uuid"]}
    superset.delete(f"/api/v1/dashboard/{dashboard_id}")


def test_ui_origin_can_frame_an_embedded_dashboard(superset_url, ui_origin, embedded_dashboard):
    url = f"{superset_url}/embedded/{embedded_dashboard['embed_uuid']}"
    response = httpx.get(url, headers={"Referer": f"{ui_origin}/builder"}, timeout=30)

    assert response.status_code == 200
    assert "x-frame-options" not in response.headers
    directives = dict(
        part.strip().split(" ", 1)
        for part in response.headers["content-security-policy"].split(";") if " " in part.strip()
    )
    assert ui_origin in directives["frame-ancestors"].split()


def test_other_origins_cannot_load_an_embedded_dashboard(superset_url, embedded_dashboard):
    url = f"{superset_url}/embedded/{embedded_dashboard['embed_uuid']}"
    response = httpx.get(url, headers={"Referer": "https://evil.example/"}, timeout=30)
    assert response.status_code == 403


def test_guest_token_is_issued_and_opens_the_dashboard(superset, superset_url, embedded_dashboard):
    response = superset.post("/api/v1/security/guest_token/", json={
        "user": {"username": "phase1-guest"},
        "resources": [{"type": "dashboard", "id": embedded_dashboard["embed_uuid"]}],
        "rls": [],
    })
    assert response.status_code == 200, response.text
    token = response.json()["token"]

    guest = httpx.get(f"{superset_url}/api/v1/dashboard/{embedded_dashboard['id']}",
                      headers={"X-GuestToken": token}, timeout=30)
    assert guest.status_code == 200, guest.text


def test_guest_token_gives_no_access_to_other_dashboards(superset, superset_url, embedded_dashboard):
    other = superset.post("/api/v1/dashboard/", json={"dashboard_title": f"phase1-private-{uuid.uuid4().hex[:8]}"})
    assert other.status_code == 201
    other_id = other.json()["id"]
    try:
        token = superset.post("/api/v1/security/guest_token/", json={
            "user": {"username": "phase1-guest"},
            "resources": [{"type": "dashboard", "id": embedded_dashboard["embed_uuid"]}],
            "rls": [],
        }).json()["token"]
        guest = httpx.get(f"{superset_url}/api/v1/dashboard/{other_id}", headers={"X-GuestToken": token}, timeout=30)
        assert guest.status_code in {403, 404}
    finally:
        superset.delete(f"/api/v1/dashboard/{other_id}")


def test_cors_allows_only_the_ui_origin(superset_url, ui_origin):
    url = f"{superset_url}/api/v1/security/csrf_token/"
    allowed = httpx.options(url, headers={"Origin": ui_origin, "Access-Control-Request-Method": "GET"}, timeout=10)
    blocked = httpx.options(url, headers={"Origin": "https://evil.example",
                                          "Access-Control-Request-Method": "GET"}, timeout=10)
    assert allowed.headers.get("access-control-allow-origin") == ui_origin
    assert "access-control-allow-origin" not in blocked.headers


# --- Demo sources ------------------------------------------------------------

def test_shop_matches_the_generator(dataset):
    with connect_shop() as conn:
        count = lambda table: conn.execute(f"SELECT COUNT(*) FROM shop.{table}").fetchone()[0]  # noqa: E731
        assert count("orders") == len(dataset.orders)
        assert count("order_items") == len(dataset.order_items)
        assert count("customers") == len(dataset.customers)
        assert count("products") == len(dataset.products)
        assert count("refunds") == len(dataset.refunds)


def test_marketing_matches_the_generator(dataset):
    conn = connect_marketing()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM ad_spend_daily")
            assert cur.fetchone()[0] == len(dataset.ad_spend)
            cur.execute("SELECT COUNT(*) FROM campaigns")
            assert cur.fetchone()[0] == len(dataset.campaigns)
    finally:
        conn.close()


@pytest.mark.parametrize("statement", [
    "INSERT INTO shop.regions VALUES (99, 'X', 'X', 'XX')",
    "UPDATE shop.orders SET status = 'x' WHERE order_id = 1",
    "DELETE FROM shop.refunds",
    "DROP TABLE shop.refunds",
    "CREATE TABLE shop.nope (id int)",
    "CREATE TABLE public.nope (id int)",
])
def test_postgres_read_only_user_cannot_write(statement):
    with connect_shop() as conn:
        with pytest.raises(psycopg.errors.Error):
            conn.execute(statement)


def test_postgres_read_only_user_cannot_turn_off_read_only():
    with connect_shop() as conn:
        conn.execute("SET TRANSACTION READ WRITE")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("INSERT INTO shop.regions VALUES (99, 'X', 'X', 'XX')")


def test_postgres_read_only_user_has_a_statement_timeout():
    with connect_shop() as conn:
        assert conn.execute("SHOW statement_timeout").fetchone()[0] == "30s"


def test_seed_marker_is_invisible_to_the_read_only_user():
    with connect_shop() as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT * FROM _seed.meta")


@pytest.mark.parametrize("statement", [
    "INSERT INTO channels VALUES ('x', 'x', 0)",
    "UPDATE campaigns SET status = 'x'",
    "DELETE FROM ad_spend_daily",
    "DROP TABLE ad_spend_daily",
    "CREATE TABLE nope (id INT)",
])
def test_mysql_read_only_user_cannot_write(statement):
    conn = connect_marketing()
    try:
        with conn.cursor() as cur, pytest.raises(pymysql.err.OperationalError):
            cur.execute(statement)
    finally:
        conn.close()


# --- Trino -------------------------------------------------------------------

def test_only_the_demo_catalogs_are_exposed(trino_cursor):
    trino_cursor.execute("SHOW CATALOGS")
    assert {row[0] for row in trino_cursor.fetchall()} == {"demo_marketing", "demo_rental", "demo_shop", "system"}


def test_trino_revenue_matches_the_generator(trino_cursor, dataset, seeded_end_day):
    trino_cursor.execute("""
        SELECT SUM(i.quantity * i.unit_price_cents)
        FROM demo_shop.shop.order_items i
        JOIN demo_shop.shop.orders o ON o.order_id = i.order_id
        JOIN demo_shop.shop.customers c ON c.customer_id = o.customer_id
        WHERE o.status <> 'cancelled' AND o.deleted_at IS NULL AND NOT c.is_test
    """)
    assert trino_cursor.fetchone()[0] == revenue_cents(dataset, FIRST_DAY, seeded_end_day)


def test_trino_joins_the_two_sources_on_the_campaign_key(trino_cursor):
    trino_cursor.execute("""
        SELECT COUNT(*) AS tagged, COUNT(c.campaign_code) AS matched
        FROM demo_shop.shop.orders o
        LEFT JOIN demo_marketing.marketing.campaigns c
          ON UPPER(REPLACE(o.utm_campaign, '-', '_')) = c.campaign_code
        WHERE o.utm_campaign IS NOT NULL
    """)
    tagged, matched = trino_cursor.fetchone()
    assert tagged > 10_000
    assert 0.9 < matched / tagged < 1.0


def test_trino_compares_monthly_spend_and_revenue(trino_cursor):
    trino_cursor.execute("""
        WITH revenue AS (
            SELECT date_trunc('month', CAST(o.order_ts AS date)) AS month,
                   SUM(i.quantity * i.unit_price_cents) / 100.0 AS revenue
            FROM demo_shop.shop.orders o JOIN demo_shop.shop.order_items i ON i.order_id = o.order_id
            WHERE o.status <> 'cancelled' GROUP BY 1
        ), spend AS (
            SELECT date_trunc('month', spend_date) AS month, SUM(spend_usd) AS spend
            FROM demo_marketing.marketing.ad_spend_daily WHERE is_latest = 1 GROUP BY 1
        )
        SELECT r.month, r.revenue, s.spend FROM revenue r JOIN spend s ON r.month = s.month ORDER BY 1
    """)
    rows = trino_cursor.fetchall()
    assert len(rows) >= 24
    assert all(float(revenue) > float(spend) > 0 for _, revenue, spend in rows)


@pytest.mark.parametrize("statement, refusal", [
    ("INSERT INTO demo_shop.shop.regions VALUES (99, 'X', 'X', 'XX')", "read-only transaction"),
    ("DROP TABLE demo_shop.shop.refunds", "read-only transaction"),
    ("CREATE TABLE demo_shop.shop.nope (id integer)", "read-only transaction"),
    ("INSERT INTO demo_marketing.marketing.channels VALUES ('x', 'x', 0)", "denied"),
    ("DROP TABLE demo_marketing.marketing.ad_spend_daily", "denied"),
])
def test_writes_through_trino_are_refused_by_the_source(trino_cursor, statement, refusal):
    # Trino lets these through; the catalogs connect as nova_ro, so the source refuses.
    with pytest.raises(trino.exceptions.TrinoQueryError, match=f"(?i){refusal}"):
        trino_cursor.execute(statement)
        trino_cursor.fetchall()


def test_a_source_can_be_added_with_no_secret_in_the_sql_and_no_restart(trino_cursor):
    """The design in D-003: CREATE CATALOG names a credentials file, never a password."""
    password = os.environ["DEMO_RO_PASSWORD"]
    trino_container_file = "/data/trino/secrets/phase1_probe.properties"
    secrets_dir = Path("/trino-data/secrets")
    secret_file = secrets_dir / "phase1_probe.properties"
    secret_file.write_text(f"connection-user=nova_ro\nconnection-password={password}\n")
    secret_file.chmod(0o600)
    statement = f"""
        CREATE CATALOG phase1_probe USING postgresql WITH (
            "connection-url" = 'jdbc:postgresql://demo-postgres:5432/shop',
            "credential-provider.type" = 'FILE',
            "connection-credential-file" = '{trino_container_file}'
        )
    """
    assert password not in statement
    try:
        trino_cursor.execute(statement)
        trino_cursor.fetchall()

        trino_cursor.execute("SELECT COUNT(*) FROM phase1_probe.shop.regions")
        assert trino_cursor.fetchone()[0] == 8
        trino_cursor.execute("""
            SELECT COUNT(*) FROM system.runtime.queries
            WHERE query LIKE '%connection-password%' AND query NOT LIKE '%system.runtime%'
        """)
        assert trino_cursor.fetchone()[0] == 0
    finally:
        trino_cursor.execute("DROP CATALOG IF EXISTS phase1_probe")
        trino_cursor.fetchall()
        secret_file.unlink(missing_ok=True)


# --- Redis -------------------------------------------------------------------

def test_superset_uses_redis_for_results_and_rate_limits():
    client = redis.Redis.from_url(os.environ["REDIS_URL"])
    assert client.ping()
    used = [db for db in (4, 5, 6, 7) if redis.Redis.from_url(f"redis://redis:6379/{db}").dbsize()]
    assert used, "expected Superset to have written to at least one of its Redis databases"
