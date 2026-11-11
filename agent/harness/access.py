"""Who may read what. The rule is the same everywhere: an admin reads everything, anyone else
reads only what they've been granted, and anything unclear is refused.

Rows are limited in two places, from the same grant. Everything shown through the semantic layer
is limited by Cube (agent/semantic/client.py builds the identity it enforces), and the agent's own
SQL is rewritten by the guardrails. So the agent can never show someone a number their own
dashboard wouldn't.
"""

from __future__ import annotations

import re
import shutil
import tempfile
from pathlib import Path

from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

from agent.harness.guardrails import RowScope, SqlPolicy
from agent.tools.skill_store import SkillStore
from agent.tools.sources import Source

NUMERIC = re.compile(r"^(tinyint|smallint|integer|bigint|decimal)")
ADMIN = "admin"


class Grant(BaseModel):
    source_id: str
    tenant_ids: list[str] | None = None  # None: the whole source


class UserAccess(BaseModel):
    email: str
    role: str
    grants: dict[str, Grant] = Field(default_factory=dict)  # by source id

    @property
    def is_admin(self) -> bool:
        return self.role == ADMIN

    def can_read(self, source: Source) -> bool:
        if self.is_admin:
            return True
        grant = self.grants.get(source.id)
        if grant is None:
            return False
        # If tenancy is later removed from a source, a limit can't be applied. That has to mean
        # no access: the alternative is that someone limited to one tenant now reads all of them.
        return grant.tenant_ids is None or bool(source.tenant_column)

    def tenants_of(self, source: Source) -> list[str] | None:
        """The tenants a user is limited to, or None if they aren't limited."""
        return None if self.is_admin else self.grants[source.id].tenant_ids

    def readable_catalogs(self, sources: list[Source]) -> list[str]:
        return [source.trino_catalog for source in sources if self.can_read(source)]


def load_access(pool: ConnectionPool, *, user_id: str, email: str, role: str) -> UserAccess:
    with pool.connection() as conn:
        rows = conn.execute("SELECT source_id::text, tenant_ids FROM source_access WHERE user_id = %s", (user_id,)).fetchall()
    return UserAccess(email=email, role=role, grants={row[0]: Grant(source_id=row[0], tenant_ids=row[1]) for row in rows})


def tenant_tables(source: Source, store: SkillStore) -> dict[str, str]:
    """The tables of a source that carry its tenant column, with that column's type."""
    if not source.tenant_column or not store.exists(source.slug):
        return {}
    found = {}
    for table in store.read_profile(source.slug).tables:
        for column in table.columns:
            if column.name == source.tenant_column:
                found[table.qualified] = column.type
    return found


def row_scope(source: Source, tenant_ids: list[str], store: SkillStore) -> RowScope:
    tables = tenant_tables(source, store)
    catalog = source.trino_catalog
    return RowScope(column=source.tenant_column or "", values=tuple(tenant_ids),
                    numeric=any(NUMERIC.match(kind) for kind in tables.values()),
                    scoped_tables=frozenset(f"{catalog}.{table}" for table in tables),
                    shared_tables=frozenset(f"{catalog}.{table}" for table in source.shared_tables))


def build_policy(sources: list[Source], access: UserAccess | None, store: SkillStore, *, max_rows: int = 1000) -> SqlPolicy:
    """What one user's run may touch. `access=None` is the command line, which is trusted."""
    readable = [source for source in sources if access is None or access.can_read(source)]
    scopes = {}
    for source in readable:
        tenants = access.tenants_of(source) if access else None
        if tenants is not None:
            scopes[source.trino_catalog] = row_scope(source, tenants, store)
    return SqlPolicy(allowed_catalogs=frozenset(source.trino_catalog for source in readable), row_scopes=scopes,
                     max_rows=max_rows)


def make_skills_view(sources: list[Source], access: UserAccess | None, skills_dir: str) -> str | None:
    """A folder holding only the skills one user may see, or None if they can see them all.

    The agent's file tools read this folder, and they aren't subject to the SQL guard, so limiting
    the SQL alone wouldn't be enough. `schema.json` is left out for anyone limited to some tenants,
    because its measured values come from every tenant's rows. `load_skill` doesn't need it."""
    if access is None or access.is_admin:
        return None
    view = Path(tempfile.mkdtemp(prefix="nova-skills-"))
    for source in sources:
        origin = Path(skills_dir) / source.slug
        if access.can_read(source) and origin.is_dir():
            limited = access.tenants_of(source) is not None
            shutil.copytree(origin, view / source.slug, ignore=shutil.ignore_patterns("schema.json") if limited else None)
    return str(view)
