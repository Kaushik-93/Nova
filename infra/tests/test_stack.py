"""Phase 1: the real, running stack. Requires `make up`."""

import os
from pathlib import Path

import httpx
import psycopg
import pymysql
import pytest
import trino

from seed.generate import FIRST_DAY
from tests.conftest import connect_marketing, connect_shop, revenue_cents


# --- Semantic layer ----------------------------------------------------------

def test_cube_is_up_and_only_reachable_inside_the_network():
    """Only Nova's API talks to Cube, so it publishes no port to the host."""
    assert httpx.get("http://cube:4000/readyz", timeout=10).status_code == 200


def test_cube_refuses_a_request_without_a_token_from_nova():
    response = httpx.get("http://cube:4000/cubejs-api/v1/meta", timeout=10)
    assert response.status_code >= 400 and "cubes" not in response.text


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
