from __future__ import annotations

import os
from typing import Iterator

import psycopg
import pymysql
import pytest
import trino
from datetime import date

from seed.generate import Dataset, generate


@pytest.fixture(scope="session")
def trino_cursor() -> Iterator[trino.dbapi.Cursor]:
    connection = trino.dbapi.connect(host="trino", port=8080, user="nova-tests")
    yield connection.cursor()
    connection.close()


def connect_shop(user: str = "nova_ro") -> psycopg.Connection:
    secret = "DEMO_RO_PASSWORD" if user == "nova_ro" else "DEMO_OWNER_PASSWORD"
    return psycopg.connect(host="demo-postgres", dbname="shop", user=user, password=os.environ[secret])


def connect_marketing(user: str = "nova_ro") -> pymysql.connections.Connection:
    secret = "DEMO_RO_PASSWORD" if user == "nova_ro" else "DEMO_OWNER_PASSWORD"
    return pymysql.connect(host="demo-mysql", database="marketing", user=user, password=os.environ[secret])


@pytest.fixture(scope="session")
def seeded_end_day() -> date:
    with connect_shop("shop_owner") as conn:
        return conn.execute("SELECT end_day FROM _seed.meta").fetchone()[0]


@pytest.fixture(scope="session")
def dataset(seeded_end_day: date) -> Dataset:
    """Regenerated from the same end date, so it must match what was loaded."""
    return generate(seeded_end_day)


def revenue_cents(data: Dataset, first: date, last: date) -> int:
    """Revenue as the seed README defines it: excludes every trap on the shop side."""
    test_customers = {c[0] for c in data.customers if c[7]}
    counted = {
        o[0] for o in data.orders
        if o[5] != "cancelled" and o[11] is None and o[2] not in test_customers and first <= o[4].date() <= last
    }
    return sum(q * p for _, order_id, _, q, p in data.order_items if order_id in counted)
