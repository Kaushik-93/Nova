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

from agent.api import (auth, dashboards as dashboard_routes, projects as project_routes, semantic as semantic_routes,
                       sources as source_routes, users)
from agent.api.auth import LoginThrottle, User, check_secret, create_sso_client, current_user, ensure_admin, require_admin, require_builder
from agent.api.runs import RunManager
from agent.api.timing import summarise
from agent.config import Settings, get_settings
from agent.dashboard_agent import run_dashboard
from agent.db import apply_schema, create_checkpointer, create_langgraph_pool, create_pool, create_store
from agent.harness.audit import AuditLog
from agent.harness.access import UserAccess, load_access
from agent.harness.errors import ErrorCategory, NotFound, NovaError
from agent.projects import can_edit, project_of_sheet
from agent.harness.executor import SqlExecutor
from agent.harness.logs import configure_logging
from agent.llm import build_model
from agent.semantic.client import CubeClient
from agent.tools.skill_store import SkillStore
from agent.tools.sources import list_sources

POLL_SECONDS = 0.5
STATUS_BY_CATEGORY = {ErrorCategory.NOT_FOUND: 404, ErrorCategory.PERMISSION: 403, ErrorCategory.INVALID_REQUEST: 400}


class StartRun(BaseModel):
    prompt: str
    thread_id: str | None = None
    require_approval: bool = False
    project: str | None = None  # the project a new sheet goes in; without one it gets its own


class ResumeRun(BaseModel):
    plan: dict[str, Any] | None = None
    rejected: bool = False
    reason: str | None = None
    answer: str | None = None


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
        self.cube = CubeClient(settings.cube_url, settings.cubejs_api_secret)
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
        self.cube.close()
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
    app.include_router(project_routes.router)
    app.include_router(source_routes.router)
    app.include_router(semantic_routes.router)
    app.include_router(dashboard_routes.router)

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

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/config")
    def config(request: Request, user: User = Depends(current_user)) -> dict[str, Any]:
        settings = services(request).settings
        return {"model": settings.model_name, "provider": settings.model_provider}

    # --- Runs -----------------------------------------------------------------

    @app.post("/api/runs", status_code=202)
    def start_run(body: StartRun, request: Request, user: User = Depends(require_builder)) -> dict[str, str]:
        app_services = services(request)
        project_id = None
        if body.project:
            project_id = project_routes.editable_project(app_services, body.project, user).id
        if body.thread_id:
            owner = app_services.runs.owner_of_thread(body.thread_id)
            if owner and owner != user.email and user.role != "admin":
                raise NotFound("no such conversation")
            existing = project_of_sheet(app_services.pool, thread_id=body.thread_id)
            if existing and not can_edit(existing, email=user.email, role=user.role):
                raise HTTPException(403, "this sheet's project is shared, so only an administrator can change it")
        return app_services.runs.start(body.prompt, thread_id=body.thread_id, user_id=user.email, project_id=project_id,
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

    return app


app = create_app()
