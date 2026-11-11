"""Projects hold sheets, and each sheet is one dashboard.

A project is what people share and find things by. The rules are here rather than in the API
because the agent's runs depend on them too.
"""

from __future__ import annotations

import contextlib
import uuid
from typing import Any

from psycopg import Connection
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

from agent.harness.errors import InvalidRequest
from agent.tools.skill_store import skill_slug

ADMIN = "admin"


class Sheet(BaseModel):
    slug: str
    thread_id: str
    name: str
    title: str
    summary: str = ""
    position: int = 0
    created_by: str
    catalogs: list[str] = Field(default_factory=list)
    updated_at: Any = None


class Project(BaseModel):
    id: str
    slug: str
    name: str
    description: str = ""
    created_by: str
    shared: bool = False
    created_at: Any = None
    updated_at: Any = None
    sheets: list[Sheet] = Field(default_factory=list)


def new_slug(name: str) -> str:
    """A readable address, unique whatever the name is. Names don't have to be unique."""
    base = "project"
    with contextlib.suppress(InvalidRequest):
        base = skill_slug(name)[:48].strip("-")
    return f"{base}-{uuid.uuid4().hex[:6]}"


def can_edit(project: Project, *, email: str, role: str) -> bool:
    """Sharing is an administrator's decision about what others see, so once a project is shared
    only an administrator can change it. Otherwise its owner could add to what they approved."""
    return role == ADMIN or (project.created_by == email and not project.shared)


def can_view_sheet(project: Project, sheet: Sheet, *, email: str, role: str, granted: set[str]) -> bool:
    """Whether someone can see that a sheet exists. Which rows they then get is Cube's job."""
    if role == ADMIN:
        return True
    owner = project.created_by == email
    if not (owner or project.shared):
        return False
    # A sheet with no record of its sources can only be vouched for to whoever owns it.
    return (owner or bool(sheet.catalogs)) and set(sheet.catalogs) <= granted


def create_project(pool: ConnectionPool, *, name: str, description: str, owner: str) -> str:
    with pool.connection() as conn:
        return conn.execute("INSERT INTO projects (slug, name, description, created_by) VALUES (%s, %s, %s, %s) RETURNING slug",
                            (new_slug(name), name, description, owner)).fetchone()[0]


def load_projects(pool: ConnectionPool, *, slug: str | None = None) -> list[Project]:
    where, values = ("WHERE p.slug = %s", (slug,)) if slug else ("", ())
    with pool.connection() as conn:
        projects = conn.execute(
            f"""SELECT p.id::text, p.slug, p.name, p.description, p.created_by, p.shared, p.created_at, p.updated_at
                FROM projects p {where} ORDER BY p.updated_at DESC""", values).fetchall()
        wanted = [row[0] for row in projects]
        sheets = conn.execute(
            """SELECT project_id::text, slug, thread_id, COALESCE(sheet_name, title), title, summary, position,
                      created_by, catalogs, updated_at
               FROM dashboards WHERE project_id = ANY(%s::uuid[]) ORDER BY position, created_at""", (wanted,)).fetchall()
    keys = ("id", "slug", "name", "description", "created_by", "shared", "created_at", "updated_at")
    found = {row[0]: Project(**dict(zip(keys, row))) for row in projects}
    sheet_keys = ("slug", "thread_id", "name", "title", "summary", "position", "created_by", "catalogs", "updated_at")
    for row in sheets:
        found[row[0]].sheets.append(Sheet(**dict(zip(sheet_keys, row[1:]))))
    return list(found.values())


def get_project(pool: ConnectionPool, slug: str) -> Project | None:
    return next(iter(load_projects(pool, slug=slug)), None)


def project_of_sheet(pool: ConnectionPool, *, thread_id: str | None = None, slug: str | None = None) -> Project | None:
    column, value = ("thread_id", thread_id) if thread_id else ("slug", slug)
    with pool.connection() as conn:
        row = conn.execute(f"""SELECT p.slug FROM dashboards d JOIN projects p ON p.id = d.project_id
                               WHERE d.{column} = %s""", (value,)).fetchone()
    return get_project(pool, row[0]) if row else None


def project_for_new_sheet(conn: Connection, *, sheet_slug: str, requested: str | None, title: str, owner: str) -> str:
    """Where a dashboard belongs. Every dashboard is in a project, so one built without choosing
    a project gets its own, named after it."""
    existing = conn.execute("SELECT project_id FROM dashboards WHERE slug = %s", (sheet_slug,)).fetchone()
    if existing and existing[0]:
        return existing[0]
    if requested:
        return requested
    return conn.execute("INSERT INTO projects (slug, name, created_by) VALUES (%s, %s, %s) RETURNING id",
                        (new_slug(title), title, owner)).fetchone()[0]


def describe_for_agent(pool: ConnectionPool, *, project_id: str | None, sheet_slug: str) -> str:
    """Tells the agent what a new sheet sits alongside, so that a project reads as one piece of work."""
    with pool.connection() as conn:
        if project_id is None:
            row = conn.execute("SELECT project_id FROM dashboards WHERE slug = %s", (sheet_slug,)).fetchone()
            project_id = row[0] if row else None
        if project_id is None:
            return ""
        name = conn.execute("SELECT name FROM projects WHERE id = %s", (project_id,)).fetchone()
        others = conn.execute("""SELECT COALESCE(sheet_name, title) FROM dashboards WHERE project_id = %s AND slug <> %s
                                 ORDER BY position""", (project_id, sheet_slug)).fetchall()
    if name is None:
        return ""
    sheets = "; ".join(row[0] for row in others) or "none yet"
    return (f'This dashboard is a sheet in the project "{name[0]}". Its other sheets: {sheets}. Keep to the project\'s '
            "subject, and don't repeat what those sheets show unless asked.\n\n")
