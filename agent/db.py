"""The application database."""

from __future__ import annotations

from pathlib import Path

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.store.postgres import PostgresStore
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

SCHEMA = Path(__file__).with_name("schema.sql")


def create_pool(database_url: str, *, max_size: int = 8) -> ConnectionPool:
    return ConnectionPool(database_url, min_size=1, max_size=max_size, open=True, kwargs={"autocommit": True})


def apply_schema(pool: ConnectionPool) -> None:
    with pool.connection() as conn:
        conn.execute(SCHEMA.read_text())


def create_langgraph_pool(database_url: str, *, max_size: int = 8) -> ConnectionPool:
    """LangGraph's Postgres classes need dict rows and no prepared statements."""
    return ConnectionPool(database_url, min_size=1, max_size=max_size, open=True,
                          kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row})


def create_checkpointer(pool: ConnectionPool) -> PostgresSaver:
    """What makes a run durable: an interrupted one resumes from its last checkpoint."""
    saver = PostgresSaver(pool)
    saver.setup()
    return saver


def create_store(pool: ConnectionPool) -> PostgresStore:
    """Long-term memory, for org preferences."""
    store = PostgresStore(pool)
    store.setup()
    return store
