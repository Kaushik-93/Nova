"""Connected sources. The agent knows a source by slug and catalog, never by its
credentials, which stay with Trino and, encrypted, in the application database."""

from __future__ import annotations

from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

COLUMNS = """id::text, slug, display_name, trino_catalog, dialect, kind, status, read_only, tenant_column, tenant_table,
             tenant_label_column, shared_tables"""


class Source(BaseModel):
    id: str
    slug: str
    display_name: str
    trino_catalog: str
    dialect: str
    kind: str = "postgres"
    status: str = "ready"
    read_only: bool | None = None
    tenant_column: str | None = None
    tenant_table: str | None = None
    tenant_label_column: str | None = None
    shared_tables: list[str] = Field(default_factory=list)


def _to_source(row: tuple) -> Source:
    keys = ("id", "slug", "display_name", "trino_catalog", "dialect", "kind", "status", "read_only", "tenant_column",
            "tenant_table", "tenant_label_column", "shared_tables")
    return Source(**dict(zip(keys, row)))


def register_source(pool: ConnectionPool, *, slug: str, display_name: str, catalog: str, dialect: str,
                    kind: str | None = None, status: str = "ready") -> str:
    with pool.connection() as conn:
        return str(conn.execute(
            """INSERT INTO sources (slug, display_name, trino_catalog, dialect, kind, status) VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (slug) DO UPDATE SET display_name = EXCLUDED.display_name RETURNING id""",
            (slug, display_name, catalog, dialect, kind or dialect, status)).fetchone()[0])


def list_sources(pool: ConnectionPool) -> list[Source]:
    with pool.connection() as conn:
        return [_to_source(row) for row in conn.execute(f"SELECT {COLUMNS} FROM sources ORDER BY slug").fetchall()]


def get_source_by_slug(pool: ConnectionPool, slug: str) -> Source | None:
    return next((source for source in list_sources(pool) if source.slug == slug), None)
