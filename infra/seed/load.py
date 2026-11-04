"""Load the demo data and (re)create the read-only users.

    python -m seed.load            load unless this seed version is already there
    python -m seed.load --force    drop everything and load again

Read-only users are reapplied on every run so they always match .env. The marker
lives in a `_seed` schema that nova_ro can't read, so the profiler never sees it.
"""

from __future__ import annotations

import argparse
import os
import time as clock
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pymysql
from psycopg import sql

from seed.generate import INJECTED_COLUMN_COMMENT, SEED_VERSION, Dataset, generate

HERE = Path(__file__).parent
RO_USER = "nova_ro"

SHOP_TABLES = {
    "regions": ("region_id", "region_code", "region_name", "country_code"),
    "products": ("product_id", "sku", "product_name", "category", "description", "list_price_cents",
                 "unit_cost_cents", "launched_on", "discontinued_on"),
    "customers": ("customer_id", "email", "first_name", "last_name", "region_id", "signup_ts", "email_opt_in",
                  "is_test"),
    "orders": ("order_id", "order_number", "customer_id", "region_id", "order_ts", "status", "channel",
               "utm_campaign", "shipping_cents", "tax_cents", "discount_cents", "deleted_at"),
    "order_items": ("order_item_id", "order_id", "product_id", "quantity", "unit_price_cents"),
    "refunds": ("refund_id", "order_id", "refund_ts", "amount_cents", "reason"),
}
MARKETING_TABLES = {
    "channels": ("channel_code", "channel_name", "is_paid"),
    "campaigns": ("campaign_id", "campaign_code", "campaign_name", "channel_code", "target_region_code",
                  "start_date", "end_date", "daily_budget_usd", "status"),
    "ad_spend_daily": ("id", "spend_date", "campaign_id", "revision", "is_latest", "impressions", "clicks",
                       "spend_usd", "platform_conversions", "synced_at"),
}


def connect_shop() -> psycopg.Connection:
    return psycopg.connect(host="demo-postgres", dbname="shop", user="shop_owner",
                           password=os.environ["DEMO_OWNER_PASSWORD"], autocommit=False)


def connect_marketing() -> pymysql.connections.Connection:
    return pymysql.connect(host="demo-mysql", database="marketing", user="root",
                           password=os.environ["DEMO_OWNER_PASSWORD"], autocommit=False)


def shop_is_seeded(conn: psycopg.Connection) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('_seed.meta')")
        if cur.fetchone()[0] is None:
            return False
        cur.execute("SELECT seed_version FROM _seed.meta")
        row = cur.fetchone()
    conn.rollback()
    return row is not None and row[0] == SEED_VERSION


def marketing_is_seeded(conn: pymysql.connections.Connection) -> bool:
    with conn.cursor() as cur:
        cur.execute("CREATE DATABASE IF NOT EXISTS _seed")
        cur.execute("CREATE TABLE IF NOT EXISTS _seed.meta (seed_version VARCHAR(16), end_day DATE)")
        cur.execute("SELECT seed_version FROM _seed.meta")
        row = cur.fetchone()
    conn.commit()
    return row is not None and row[0] == SEED_VERSION


def load_shop(conn: psycopg.Connection, data: Dataset) -> None:
    rows = {"regions": data.regions, "products": data.products, "customers": data.customers,
            "orders": data.orders, "order_items": data.order_items, "refunds": data.refunds}
    with conn.cursor() as cur:
        cur.execute((HERE / "shop_schema.sql").read_text())
        for table, columns in SHOP_TABLES.items():
            copy_sql = sql.SQL("COPY shop.{} ({}) FROM STDIN").format(
                sql.Identifier(table), sql.SQL(", ").join(map(sql.Identifier, columns)))
            with cur.copy(copy_sql) as copy:
                for row in rows[table]:
                    copy.write_row(row)
        cur.execute((HERE / "shop_indexes.sql").read_text())
        cur.execute(sql.SQL("COMMENT ON COLUMN shop.products.description IS {}").format(
            sql.Literal(INJECTED_COLUMN_COMMENT)))
        cur.execute("ANALYZE")
        cur.execute("DROP SCHEMA IF EXISTS _seed CASCADE")
        cur.execute("CREATE SCHEMA _seed")
        cur.execute("CREATE TABLE _seed.meta (seed_version text NOT NULL, end_day date NOT NULL)")
        cur.execute("INSERT INTO _seed.meta VALUES (%s, %s)", (SEED_VERSION, data.end_day))
    conn.commit()


def load_marketing(conn: pymysql.connections.Connection, data: Dataset) -> None:
    rows = {"channels": data.channels, "campaigns": data.campaigns, "ad_spend_daily": data.ad_spend}
    with conn.cursor() as cur:
        # pymysql runs one statement at a time. Comments go first: they contain semicolons.
        lines = (HERE / "marketing_schema.sql").read_text().splitlines()
        script = "\n".join(line for line in lines if not line.lstrip().startswith("--"))
        for statement in filter(str.strip, script.split(";")):
            cur.execute(statement)
        for table, columns in MARKETING_TABLES.items():
            placeholders = ", ".join(["%s"] * len(columns))
            statement = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"
            batch = rows[table]
            for start in range(0, len(batch), 5000):
                cur.executemany(statement, batch[start:start + 5000])
        cur.execute("DELETE FROM _seed.meta")
        cur.execute("INSERT INTO _seed.meta VALUES (%s, %s)", (SEED_VERSION, data.end_day))
    conn.commit()


def apply_shop_readonly(conn: psycopg.Connection, password: str) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (RO_USER,))
        verb = "ALTER" if cur.fetchone() else "CREATE"
        cur.execute(sql.SQL("{} ROLE {} LOGIN PASSWORD {}").format(
            sql.SQL(verb), sql.Identifier(RO_USER), sql.Literal(password)))
        cur.execute(sql.SQL("""
            ALTER ROLE {ro} SET default_transaction_read_only = on;
            ALTER ROLE {ro} SET statement_timeout = '30s';
            REVOKE ALL ON DATABASE shop FROM PUBLIC;
            REVOKE ALL ON SCHEMA public FROM PUBLIC;
            GRANT CONNECT ON DATABASE shop TO {ro};
            GRANT USAGE ON SCHEMA shop TO {ro};
            GRANT SELECT ON ALL TABLES IN SCHEMA shop TO {ro};
        """).format(ro=sql.Identifier(RO_USER)))
    conn.commit()


def apply_marketing_readonly(conn: pymysql.connections.Connection, password: str) -> None:
    with conn.cursor() as cur:
        cur.execute(f"CREATE USER IF NOT EXISTS '{RO_USER}'@'%%' IDENTIFIED BY %s", (password,))
        cur.execute(f"ALTER USER '{RO_USER}'@'%%' IDENTIFIED BY %s", (password,))
        # No params here, so pymysql does no %-formatting and this is a literal %.
        cur.execute(f"GRANT SELECT, SHOW VIEW ON marketing.* TO '{RO_USER}'@'%'")
    conn.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="drop and reload even if already seeded")
    args = parser.parse_args()

    ro_password = os.environ["DEMO_RO_PASSWORD"]
    shop = connect_shop()
    marketing = connect_marketing()
    try:
        if not args.force and shop_is_seeded(shop) and marketing_is_seeded(marketing):
            print(f"[seed] version {SEED_VERSION} already loaded; use --force to rebuild")
        else:
            started = clock.monotonic()
            # Whole days only: today is still in progress.
            end_day = datetime.now(timezone.utc).date() - timedelta(days=1)
            data = generate(end_day)
            print(f"[seed] generated through {end_day}: {len(data.orders):,} orders, "
                  f"{len(data.order_items):,} items, {len(data.customers):,} customers, "
                  f"{len(data.ad_spend):,} spend rows in {clock.monotonic() - started:.1f}s")
            marketing_is_seeded(marketing)  # also creates the marker table
            load_shop(shop, data)
            load_marketing(marketing, data)
            print(f"[seed] loaded in {clock.monotonic() - started:.1f}s")
        apply_shop_readonly(shop, ro_password)
        apply_marketing_readonly(marketing, ro_password)
        print(f"[seed] read-only user '{RO_USER}' ready on both sources")
    finally:
        shop.close()
        marketing.close()


if __name__ == "__main__":
    main()
