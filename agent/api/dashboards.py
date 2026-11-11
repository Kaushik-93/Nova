"""Dashboards over HTTP. Nova draws them, and each chart's numbers are fetched from Cube as the
person looking, so a shared dashboard shows everyone their own rows without anyone having to set
that up.

A dashboard is reached by someone who can see its sheet (agent/projects.py), or by the renderer
with a short-lived token for that one dashboard (agent/tools/renderer.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import jwt
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from agent.api.auth import ALGORITHM, User, current_user
from agent.dashboards import filter_options, load_spec, published_model, run_chart
from agent.harness.access import UserAccess, load_access
from agent.harness.errors import NotFound, PermissionFailure
from agent.projects import can_edit, can_view_sheet, load_projects, project_of_sheet
from agent.semantic.client import CubeIdentity, identity_for
from agent.tools.renderer import RENDER_PURPOSE
from agent.tools.sources import list_sources

router = APIRouter(prefix="/api/dashboards", tags=["dashboards"])


class ChartRequest(BaseModel):
    filters: dict[str, list[str]] = Field(default_factory=dict, description="Chosen values, by filter name")
    date_range: list[str] | None = Field(default=None, description="The time filter: [from, to], both included")


@dataclass
class Viewer:
    identity: CubeIdentity
    user: User | None  # None for the renderer


def _services(request: Request) -> Any:
    return request.app.state.services


def _render_viewer(request: Request, slug: str) -> Viewer | None:
    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer "):
        return None
    services = _services(request)
    try:
        claims = jwt.decode(header.removeprefix("Bearer "), services.settings.nova_jwt_secret, algorithms=[ALGORITHM],
                            options={"require": ["exp", "purpose", "slug", "sub"]})
    except jwt.InvalidTokenError as error:
        raise PermissionFailure("that link has expired") from error
    if claims["purpose"] != RENDER_PURPOSE or claims["slug"] != slug:
        raise PermissionFailure("that link is for something else")
    sources = [source for source in list_sources(services.pool) if source.status == "ready"]
    with services.pool.connection() as conn:
        row = conn.execute("SELECT id::text, email, role FROM users WHERE email = %s AND is_active", (claims["sub"],)).fetchone()
    # A run from the command line has no account, and the command line is trusted with everything.
    access: UserAccess | None = load_access(services.pool, user_id=row[0], email=row[1], role=row[2]) if row else None
    readable = [s for s in sources if access is None or access.can_read(s)]
    return Viewer(identity=identity_for(access, readable, user_id=claims["sub"]), user=None)


def viewer_of(slug: str, request: Request) -> Viewer:
    """Whoever is looking at this dashboard, if they may. A dependency, so it's settled before the
    request's body is even read."""
    rendered = _render_viewer(request, slug)
    if rendered is not None:
        return rendered
    user = current_user(request)
    services = _services(request)
    access = services.access_of(user)
    sources = [source for source in list_sources(services.pool) if source.status == "ready"]
    readable = [source for source in sources if access.can_read(source)]
    project = project_of_sheet(services.pool, slug=slug)
    sheet = next((item for item in project.sheets if item.slug == slug), None) if project else None
    granted = {source.trino_catalog for source in readable}
    if sheet is None or not can_view_sheet(project, sheet, email=user.email, role=user.role, granted=granted):
        raise NotFound(f"no dashboard {slug}")  # not 403, which would confirm it exists
    return Viewer(identity=identity_for(access, readable, user_id=user.email), user=user)


@router.get("")
def list_dashboards(request: Request, user: User = Depends(current_user)) -> list[dict[str, Any]]:
    """Every sheet this person can see, newest first, whichever project it's in."""
    services = _services(request)
    granted = set(services.access_of(user).readable_catalogs(list_sources(services.pool)))
    rows = [{"slug": sheet.slug, "thread_id": sheet.thread_id, "title": sheet.title, "summary": sheet.summary,
             "created_by": sheet.created_by, "updated_at": sheet.updated_at, "sheet_name": sheet.name,
             "project_slug": project.slug, "project_name": project.name}
            for project in load_projects(services.pool) for sheet in project.sheets
            if can_view_sheet(project, sheet, email=user.email, role=user.role, granted=granted)]
    return sorted(rows, key=lambda row: row["updated_at"], reverse=True)


@router.get("/{slug}")
def dashboard(slug: str, request: Request, viewer: Viewer = Depends(viewer_of)) -> dict[str, Any]:
    services = _services(request)
    spec, record = load_spec(services.pool, slug)
    project = project_of_sheet(services.pool, slug=slug)
    editable = viewer.user is not None and project is not None and can_edit(project, email=viewer.user.email,
                                                                           role=viewer.user.role)
    prompt = None
    if spec is None:  # built in Superset, before Nova drew its own: it can be rebuilt from what was asked
        with services.pool.connection() as conn:
            row = conn.execute("SELECT prompt FROM runs WHERE thread_id = %s ORDER BY started_at LIMIT 1",
                               (record["thread_id"],)).fetchone()
        prompt = row[0] if row else None
    return {"slug": slug, "thread_id": record["thread_id"], "title": record["title"], "summary": record["summary"],
            "updated_at": record["updated_at"], "spec": spec.model_dump(mode="json") if spec else None,
            "needs_rebuild": spec is None, "prompt": prompt, "can_edit": editable,
            "project": {"slug": project.slug, "name": project.name} if project else None}


@router.post("/{slug}/charts/{key}/data")
def chart_data(slug: str, key: str, body: ChartRequest, request: Request, viewer: Viewer = Depends(viewer_of)) -> dict[str, Any]:
    services = _services(request)
    spec, _ = load_spec(services.pool, slug)
    if spec is None:
        raise NotFound("this dashboard needs rebuilding before it can be shown")
    return run_chart(services.cube, viewer.identity, spec, key, chosen=body.filters, date_range=body.date_range,
                     model=published_model(services.skills))


@router.get("/{slug}/filters/{name}/options")
def options(slug: str, name: str, request: Request, viewer: Viewer = Depends(viewer_of)) -> list[str]:
    services = _services(request)
    spec, _ = load_spec(services.pool, slug)
    if spec is None:
        raise NotFound("this dashboard needs rebuilding before it can be shown")
    return filter_options(services.cube, viewer.identity, spec, name)
