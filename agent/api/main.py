"""The HTTP API. Run with: uvicorn agent.api.main:app"""

from __future__ import annotations

import asyncio
import json
import secrets
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse
from starlette.middleware.sessions import SessionMiddleware

from agent.api import auth, sources as source_routes, users
from agent.api.auth import LoginThrottle, User, check_secret, create_sso_client, current_user, ensure_admin, require_admin, require_builder
from agent.api.runs import RunManager
from agent.api.timing import summarise
from agent.config import Settings, get_settings
from agent.dashboard_agent import run_dashboard
from agent.db import apply_schema, create_checkpointer, create_langgraph_pool, create_pool, create_store
from agent.harness.access import UserAccess, guest_rules, load_access
from agent.harness.audit import AuditLog
from agent.harness.errors import ErrorCategory, NotFound, NovaError
from agent.harness.executor import SqlExecutor
from agent.harness.logs import configure_logging
from agent.harness.validators import chart_dataset_id
from agent.llm import build_model
from agent.tools.skill_store import SkillStore, parse_frontmatter
from agent.tools.sources import list_sources
from agent.tools.superset_client import SupersetClient
from agent.tools.superset_payloads import CHART_KEY_PREFIX

POLL_SECONDS = 0.5
PREVIEW_ROWS = 200
STATUS_BY_CATEGORY = {ErrorCategory.NOT_FOUND: 404, ErrorCategory.PERMISSION: 403, ErrorCategory.INVALID_REQUEST: 400}


class StartRun(BaseModel):
    prompt: str
    thread_id: str | None = None
    require_approval: bool = False


class ResumeRun(BaseModel):
    plan: dict[str, Any] | None = None
    rejected: bool = False
    reason: str | None = None
    answer: str | None = None


class DashboardChange(BaseModel):
    shared: bool


class Services:
    def __init__(self, settings: Settings) -> None:
        check_secret(settings.nova_jwt_secret)  # first, so that nothing starts without it
        self.settings = settings
        self.pool = create_pool(settings.database_url)
        apply_schema(self.pool)
        self.graph_pool = create_langgraph_pool(settings.database_url)
        self.checkpointer = create_checkpointer(self.graph_pool)
        self.long_term = create_store(self.graph_pool)
        self.skills = SkillStore(settings.nova_skills_dir, self.pool)
        self.superset = SupersetClient(settings.superset_url, settings.superset_admin_user,
                                       settings.superset_admin_password)
        self.audit = AuditLog(self.pool)
        self.executor = SqlExecutor(settings.trino_url, self.audit)
        self.model = build_model(settings)
        self.throttle = LoginThrottle()
        self.sso = create_sso_client(settings)
        ensure_admin(self.pool, settings.nova_admin_email, settings.nova_admin_password)
        self.runs = RunManager(self.pool, self._run)
        self.runs.mark_abandoned()

    def _run(self, **options: Any) -> Any:
        return run_dashboard(settings=self.settings, pool=self.pool, model=self.model, checkpointer=self.checkpointer,
                             store=self.long_term, **options)

    def profile_source(self, **options: Any) -> None:
        source_routes.profile_source(self, **options)

    def access_of(self, user: User) -> UserAccess:
        return load_access(self.pool, user_id=user.id, email=user.email, role=user.role)

    def close(self) -> None:
        self.runs.close()
        self.superset.close()
        self.pool.close()
        self.graph_pool.close()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    app.state.services = Services(get_settings())
    yield
    app.state.services.close()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Nova", lifespan=lifespan)
    # Holds only the SSO state and nonce, for the moments while someone is at the provider.
    app.add_middleware(SessionMiddleware, secret_key=settings.nova_jwt_secret or secrets.token_hex(32), session_cookie="nova_sso",
                       same_site="lax", https_only=settings.nova_ui_origin.startswith("https://"), max_age=600)
    app.add_middleware(CORSMiddleware, allow_origins=[settings.nova_ui_origin], allow_methods=["*"], allow_headers=["*"],
                       allow_credentials=True)
    app.include_router(auth.router)
    app.include_router(users.router)
    app.include_router(source_routes.router)

    @app.exception_handler(NovaError)
    async def nova_error(request: Request, error: NovaError) -> JSONResponse:
        status = STATUS_BY_CATEGORY.get(error.category, 502 if error.retryable else 500)
        return JSONResponse(status_code=status, content={"error": error.message, "category": error.category.value})

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, error: RequestValidationError) -> JSONResponse:
        # FastAPI's default answer repeats the input, which could be the body with a password in it.
        problems = [f"{'.'.join(str(part) for part in item['loc'] if part != 'body')}: {item['msg']}" for item in error.errors()]
        return JSONResponse(status_code=422, content={"error": "; ".join(problems)})

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, error: HTTPException) -> JSONResponse:
        return JSONResponse(status_code=error.status_code, content={"error": error.detail})

    def services(request: Request) -> Services:
        return request.app.state.services

    def own_run(app_services: Services, run_id: str, user: User) -> dict[str, Any]:
        run = app_services.runs.get(run_id)
        if user.role != "admin" and run["created_by"] != user.email:
            raise NotFound(f"no run {run_id}")  # not 403, which would confirm it exists
        return run

    def visible_dashboard(app_services: Services, slug: str, user: User) -> tuple:
        with app_services.pool.connection() as conn:
            row = conn.execute("SELECT superset_id, embed_uuid, title, created_by, shared, catalogs FROM dashboards WHERE slug = %s",
                               (slug,)).fetchone()
        if row is None or not row[1] or not (user.role == "admin" or row[3] == user.email or row[4]):
            raise NotFound(f"no dashboard {slug}")
        granted = app_services.access_of(user).readable_catalogs(list_sources(app_services.pool))
        if user.role != "admin" and row[5] and not set(row[5]) <= set(granted):
            raise NotFound(f"no dashboard {slug}")  # not 403, which would confirm it exists
        return row

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/config")
    def config(request: Request, user: User = Depends(current_user)) -> dict[str, Any]:
        settings = services(request).settings
        return {"superset_url": settings.superset_public_url if user.role == "admin" else None, "model": settings.model_name,
                "provider": settings.model_provider}

    # --- Runs -----------------------------------------------------------------

    @app.post("/api/runs", status_code=202)
    def start_run(body: StartRun, request: Request, user: User = Depends(require_builder)) -> dict[str, str]:
        app_services = services(request)
        if body.thread_id:
            owner = app_services.runs.owner_of_thread(body.thread_id)
            if owner and owner != user.email and user.role != "admin":
                raise NotFound("no such conversation")
        return app_services.runs.start(body.prompt, thread_id=body.thread_id, user_id=user.email,
                                       require_approval=body.require_approval, access=app_services.access_of(user))

    @app.get("/api/runs")
    def list_runs(request: Request, thread_id: str | None = None, user: User = Depends(current_user)) -> list[dict[str, Any]]:
        return services(request).runs.list(thread_id=thread_id, user_id=None if user.role == "admin" else user.email)

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str, request: Request, user: User = Depends(current_user)) -> dict[str, Any]:
        app_services = services(request)
        run = own_run(app_services, run_id, user)
        return {**run, "wall_seconds": float(run["wall_seconds"]),
                "timing": summarise(app_services.runs.events(run_id), float(run["wall_seconds"]))}

    @app.post("/api/runs/{run_id}/resume", status_code=202)
    def resume_run(run_id: str, body: ResumeRun, request: Request, user: User = Depends(require_builder)) -> dict[str, str]:
        app_services = services(request)
        own_run(app_services, run_id, user)
        answer: Any = body.answer
        if body.plan is not None or body.rejected:
            answer = body.model_dump(exclude_none=True)
        return app_services.runs.resume(run_id, answer, user_id=user.email, access=app_services.access_of(user))

    @app.get("/api/runs/{run_id}/events")
    async def stream_events(run_id: str, request: Request, after: int = 0, user: User = Depends(current_user)) -> EventSourceResponse:
        app_services = services(request)
        own_run(app_services, run_id, user)  # a 404 here beats an empty stream
        runs = app_services.runs

        async def generate() -> AsyncIterator[dict[str, str]]:
            last = after
            while not await request.is_disconnected():
                events = await asyncio.to_thread(runs.events, run_id, after=last)
                for event in events:
                    last = event["id"]
                    yield {"id": str(last), "event": "step", "data": json.dumps(event)}
                status = (await asyncio.to_thread(runs.get, run_id))["status"]
                if status != "running" and not events:
                    yield {"event": "end", "data": json.dumps({"status": status})}
                    return
                await asyncio.sleep(POLL_SECONDS)

        return EventSourceResponse(generate())

    # --- Dashboards -----------------------------------------------------------

    @app.get("/api/dashboards")
    def list_dashboards(request: Request, user: User = Depends(current_user)) -> list[dict[str, Any]]:
        app_services = services(request)
        where, values = "", ()
        if user.role != "admin":
            # Someone who lost access to a source no longer sees even the titles built on it.
            granted = app_services.access_of(user).readable_catalogs(list_sources(app_services.pool))
            where, values = "WHERE (created_by = %s OR shared) AND catalogs <@ %s::text[]", (user.email, granted)
        with app_services.pool.connection() as conn:
            cursor = conn.execute(f"""SELECT slug, thread_id, superset_id, title, summary, created_by, shared, created_at, updated_at
                                      FROM dashboards {where} ORDER BY updated_at DESC""", values)
            columns = [c.name for c in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]

    @app.patch("/api/dashboards/{slug}")
    def change_dashboard(slug: str, body: DashboardChange, request: Request, user: User = Depends(require_admin)) -> dict[str, bool]:
        """Sharing is for administrators: it decides what other people can see, and an owner with
        wide access could otherwise show a dashboard's data to someone with narrower access."""
        app_services = services(request)
        row = visible_dashboard(app_services, slug, user)
        if body.shared and not row[5]:
            raise HTTPException(400, "this dashboard was built before access controls; rebuild it to share it")
        with app_services.pool.connection() as conn:
            conn.execute("UPDATE dashboards SET shared = %s WHERE slug = %s", (body.shared, slug))
        return {"shared": body.shared}

    @app.get("/api/dashboards/{slug}/embed")
    def embed_dashboard(slug: str, request: Request, user: User = Depends(current_user)) -> dict[str, Any]:
        """What the browser needs to show a dashboard. The token is short-lived and made for this
        user, with their row rules in it, so a shared dashboard shows each person their own data.
        The SDK asks for a new one here whenever it needs to."""
        app_services = services(request)
        superset_id, embed_uuid, title, _, _, catalogs = visible_dashboard(app_services, slug, user)
        rules = guest_rules(app_services.access_of(user), list_sources(app_services.pool), list(catalogs), app_services.skills)
        metadata = json.loads(app_services.superset.dashboard_detail(superset_id).get("json_metadata") or "{}")
        filters = [{"id": native["id"], "name": native["name"],
                    "column": ((native.get("targets") or [{}])[0].get("column") or {}).get("name")}
                   for native in metadata.get("native_filter_configuration") or []]
        return {"slug": slug, "title": title, "embed_uuid": embed_uuid, "filters": filters,
                "superset_url": app_services.settings.superset_public_url,
                "guest_token": app_services.superset.guest_token(embed_uuid, username=user.email, rls=rules)}

    @app.get("/api/charts/{chart_id}/preview")
    def chart_preview(chart_id: int, request: Request, user: User = Depends(require_builder)) -> dict[str, Any]:
        """A chart's data, for the live preview while a dashboard is being built. It's read from
        Superset here rather than kept in the run's events, so no data is copied. Superset is
        reached as an admin, so this only serves the charts of someone's own build, whose
        datasets already hold their row scope."""
        app_services = services(request)
        superset = app_services.superset
        detail = superset.chart_detail(chart_id)
        dataset_id = chart_dataset_id(detail)
        if not (detail.get("description") or "").startswith(CHART_KEY_PREFIX) or dataset_id is None:
            raise NotFound(f"chart {chart_id} wasn't built by Nova")
        key = superset.get_dataset(dataset_id).table_name.split("__")[0]
        if user.role != "admin" and not app_services.runs.owns_dashboard_key(key, user.email):
            raise NotFound(f"chart {chart_id} wasn't built by Nova")
        params = json.loads(detail.get("params") or "{}")
        data = superset.chart_data(chart_id)
        return {"chart_id": chart_id, "title": detail["slice_name"], "viz": params.get("viz_type"),
                "columns": data.columns, "rows": data.rows[:PREVIEW_ROWS], "row_count": data.row_count,
                "x_axis": params.get("x_axis"), "groupby": params.get("groupby") or [],
                "time_grain": params.get("time_grain_sqla"),
                "format": params.get("y_axis_format") or params.get("number_format")}

    # --- Skills ---------------------------------------------------------------

    @app.get("/api/skills/{slug}")
    def skill(slug: str, request: Request, user: User = Depends(require_admin)) -> dict[str, Any]:
        store: SkillStore = services(request).skills
        if not store.exists(slug):
            raise NotFound(f"no skill named {slug}")
        directory = store.path(slug)
        front, body = parse_frontmatter((directory / "SKILL.md").read_text())
        layer = store.read_semantic_layer(slug)
        profile = store.read_profile(slug)
        return {"name": front.name, "description": front.description, "version": front.version, "status": front.status.value,
                "generated_at": front.generated_at, "reviewed_by": front.reviewed_by, "guide": body,
                "metrics": {key: metric.model_dump(mode="json") for key, metric in layer.metrics.items()},
                "proposed": {key: metric.model_dump(mode="json") for key, metric in layer.proposed.items()},
                "gotchas": [gotcha.model_dump(mode="json") for gotcha in profile.gotchas],
                "tables": [{"name": t.qualified, "rows": t.row_count} for t in profile.tables],
                "examples": (directory / "examples.sql").read_text(),
                "changelog": (directory / "CHANGELOG.md").read_text()}

    @app.post("/api/skills/{slug}/approve")
    def approve_skill(slug: str, request: Request, user: User = Depends(require_admin)) -> dict[str, Any]:
        store: SkillStore = services(request).skills
        if not store.exists(slug):
            raise NotFound(f"no skill named {slug}")
        front = store.approve(slug, reviewed_by=user.email)
        return {"name": front.name, "version": front.version, "status": front.status.value}

    return app


app = create_app()
