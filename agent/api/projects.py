"""Projects and their sheets."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from agent.api.auth import User, current_user, require_builder
from agent.harness.errors import NotFound
from agent.projects import Project, Sheet, can_edit, can_view_sheet, create_project, get_project, load_projects
from agent.tools.sources import list_sources

router = APIRouter(prefix="/api/projects", tags=["projects"])


class NewProject(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)


class ProjectChange(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    shared: bool | None = None


class SheetChange(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    project: str | None = Field(default=None, description="Move the sheet to this project")


class SheetOrder(BaseModel):
    sheets: list[str]


def granted_catalogs(services: Any, user: User) -> set[str]:
    return set(services.access_of(user).readable_catalogs(list_sources(services.pool)))


def describe(project: Project, user: User, granted: set[str]) -> dict[str, Any] | None:
    """A project as this person sees it, or None if they can't. Someone it's shared with doesn't
    see sheets they can't read, or the project at all if that leaves nothing."""
    sheets = [sheet for sheet in project.sheets
              if can_view_sheet(project, sheet, email=user.email, role=user.role, granted=granted)]
    if not sheets and not (user.role == "admin" or project.created_by == user.email):
        return None
    return {"slug": project.slug, "name": project.name, "description": project.description, "created_by": project.created_by,
            "shared": project.shared, "updated_at": project.updated_at, "can_edit": can_edit(project, email=user.email, role=user.role),
            "sheets": [{"slug": sheet.slug, "thread_id": sheet.thread_id, "name": sheet.name, "title": sheet.title,
                        "summary": sheet.summary, "updated_at": sheet.updated_at} for sheet in sheets]}


def visible_project(services: Any, slug: str, user: User) -> Project:
    project = get_project(services.pool, slug)
    if project is None or describe(project, user, granted_catalogs(services, user)) is None:
        raise NotFound(f"no project {slug}")  # not 403, which would confirm it exists
    return project


def editable_project(services: Any, slug: str, user: User) -> Project:
    project = visible_project(services, slug, user)
    if not can_edit(project, email=user.email, role=user.role):
        raise HTTPException(403, "this project is shared, so only an administrator can change it"
                            if project.shared else "only its owner can change this project")
    return project


def find_sheet(project: Project, slug: str) -> Sheet:
    sheet = next((item for item in project.sheets if item.slug == slug), None)
    if sheet is None:
        raise NotFound(f"no sheet {slug}")
    return sheet


def refuse_if_running(services: Any, sheets: list[Sheet]) -> None:
    with services.pool.connection() as conn:
        busy = conn.execute("SELECT 1 FROM runs WHERE thread_id = ANY(%s) AND status = 'running' LIMIT 1",
                            ([sheet.thread_id for sheet in sheets],)).fetchone()
    if busy:
        raise HTTPException(409, "the agent is working on this; wait for it to finish")


@router.get("")
def list_projects(request: Request, user: User = Depends(current_user)) -> list[dict[str, Any]]:
    services = request.app.state.services
    granted = granted_catalogs(services, user)
    found = (describe(project, user, granted) for project in load_projects(services.pool))
    return [project for project in found if project is not None]


@router.post("", status_code=201)
def new_project(body: NewProject, request: Request, user: User = Depends(require_builder)) -> dict[str, Any]:
    services = request.app.state.services
    slug = create_project(services.pool, name=body.name.strip(), description=body.description.strip(), owner=user.email)
    return describe(get_project(services.pool, slug), user, granted_catalogs(services, user))


@router.get("/{slug}")
def one_project(slug: str, request: Request, user: User = Depends(current_user)) -> dict[str, Any]:
    services = request.app.state.services
    return describe(visible_project(services, slug, user), user, granted_catalogs(services, user))


@router.patch("/{slug}")
def change_project(slug: str, body: ProjectChange, request: Request, user: User = Depends(current_user)) -> dict[str, Any]:
    services = request.app.state.services
    if body.shared is not None and user.role != "admin":
        raise HTTPException(403, "only an administrator can share a project")
    project = editable_project(services, slug, user)
    if body.shared and not any(sheet.catalogs for sheet in project.sheets):
        raise HTTPException(400, "there's nothing here to share yet: rebuild older sheets first")
    with services.pool.connection() as conn:
        conn.execute("""UPDATE projects SET name = COALESCE(%s, name), description = COALESCE(%s, description),
                               shared = COALESCE(%s, shared), updated_at = now() WHERE id = %s""",
                     (body.name, body.description, body.shared, project.id))
    return describe(get_project(services.pool, slug), user, granted_catalogs(services, user))


@router.delete("/{slug}")
def delete_project(slug: str, request: Request, user: User = Depends(current_user)) -> dict[str, bool]:
    services = request.app.state.services
    project = editable_project(services, slug, user)
    refuse_if_running(services, project.sheets)
    with services.pool.connection() as conn, conn.transaction():
        conn.execute("DELETE FROM dashboards WHERE project_id = %s", (project.id,))
        conn.execute("DELETE FROM projects WHERE id = %s", (project.id,))
    return {"deleted": True}


@router.put("/{slug}/order")
def reorder_sheets(slug: str, body: SheetOrder, request: Request, user: User = Depends(current_user)) -> dict[str, Any]:
    services = request.app.state.services
    project = editable_project(services, slug, user)
    if sorted(body.sheets) != sorted(sheet.slug for sheet in project.sheets):
        raise HTTPException(400, "that isn't the list of this project's sheets")
    with services.pool.connection() as conn, conn.transaction():
        for position, sheet in enumerate(body.sheets):
            conn.execute("UPDATE dashboards SET position = %s WHERE slug = %s", (position, sheet))
    return describe(get_project(services.pool, slug), user, granted_catalogs(services, user))


@router.patch("/{slug}/sheets/{sheet}")
def change_sheet(slug: str, sheet: str, body: SheetChange, request: Request, user: User = Depends(current_user)) -> dict[str, Any]:
    services = request.app.state.services
    project = editable_project(services, slug, user)
    found = find_sheet(project, sheet)
    target = editable_project(services, body.project, user) if body.project else None
    with services.pool.connection() as conn, conn.transaction():
        if body.name:
            conn.execute("UPDATE dashboards SET sheet_name = %s WHERE slug = %s", (body.name.strip(), found.slug))
        if target and target.id != project.id:
            conn.execute("""UPDATE dashboards SET project_id = %s,
                                   position = (SELECT COALESCE(MAX(position) + 1, 0) FROM dashboards WHERE project_id = %s)
                            WHERE slug = %s""", (target.id, target.id, found.slug))
    return describe(get_project(services.pool, target.slug if target else slug), user, granted_catalogs(services, user))


@router.delete("/{slug}/sheets/{sheet}")
def delete_sheet(slug: str, sheet: str, request: Request, user: User = Depends(current_user)) -> dict[str, bool]:
    services = request.app.state.services
    project = editable_project(services, slug, user)
    found = find_sheet(project, sheet)
    refuse_if_running(services, [found])
    with services.pool.connection() as conn:
        conn.execute("DELETE FROM dashboards WHERE slug = %s", (found.slug,))
    return {"deleted": True}
