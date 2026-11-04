"""Connected sources. The agent knows a source by slug and catalog, never by its
credentials, which stay with Trino."""

from __future__ import annotations

from psycopg_pool import ConnectionPool
from pydantic import BaseModel


class Source(BaseModel):
    id: str
    slug: str
    display_name: str
    trino_catalog: str
    dialect: str


def register_source(pool: ConnectionPool, *, slug: str, display_name: str, catalog: str, dialect: str) -> str:
    with pool.connection() as conn:
        return str(conn.execute(
            """INSERT INTO sources (slug, display_name, trino_catalog, dialect) VALUES (%s, %s, %s, %s)
               ON CONFLICT (slug) DO UPDATE SET display_name = EXCLUDED.display_name RETURNING id""",
            (slug, display_name, catalog, dialect)).fetchone()[0])


def list_sources(pool: ConnectionPool) -> list[Source]:
    with pool.connection() as conn:
        rows = conn.execute("SELECT id::text, slug, display_name, trino_catalog, dialect FROM sources ORDER BY slug")
        return [Source(id=r[0], slug=r[1], display_name=r[2], trino_catalog=r[3], dialect=r[4]) for r in rows.fetchall()]


def get_source_by_slug(pool: ConnectionPool, slug: str) -> Source | None:
    return next((source for source in list_sources(pool) if source.slug == slug), None)
