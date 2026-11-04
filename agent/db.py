"""The application database."""

from __future__ import annotations

from pathlib import Path

from psycopg_pool import ConnectionPool

SCHEMA = Path(__file__).with_name("schema.sql")


def create_pool(database_url: str, *, max_size: int = 8) -> ConnectionPool:
    return ConnectionPool(database_url, min_size=1, max_size=max_size, open=True, kwargs={"autocommit": True})


def apply_schema(pool: ConnectionPool) -> None:
    with pool.connection() as conn:
        conn.execute(SCHEMA.read_text())
