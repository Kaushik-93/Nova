"""The HTTP API. Run with: uvicorn agent.api.main:app"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from agent.api.runs import TERMINAL, RunManager
from agent.api.timing import summarise
from agent.config import Settings, get_settings
from agent.dashboard_agent import run_dashboard
from agent.db import apply_schema, create_checkpointer, create_langgraph_pool, create_pool, create_store
from agent.harness.errors import ErrorCategory, InvalidRequest, NotFound, NovaError
from agent.harness.logs import configure_logging
from agent.llm import build_model
from agent.tools.skill_store import SkillStore, parse_frontmatter
from agent.tools.sources import list_sources
from agent.tools.superset_client import SupersetClient
from agent.tools.superset_payloads import CHART_KEY_PREFIX

POLL_SECONDS = 0.5
PREVIEW_ROWS = 200
# Authentication comes with the rest of Phase 6. Until then everything acts as one local user.
LOCAL_USER = "local"
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


class ApproveSkill(BaseModel):
    reviewed_by: str = Field(default=LOCAL_USER)


class Services:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.pool = create_pool(settings.database_url)
        apply_schema(self.pool)
        self.graph_pool = create_langgraph_pool(settings.database_url)
        self.checkpointer = create_checkpointer(self.graph_pool)
        self.long_term = create_store(self.graph_pool)
        self.skills = SkillStore(settings.nova_skills_dir, self.pool)
        self.superset = SupersetClient(settings.superset_url, settings.superset_admin_user,
                                       settings.superset_admin_password)
        self.model = build_model(settings)
        self.runs = RunManager(self.pool, self._run)
        self.runs.mark_abandoned()

    def _run(self, **options: Any) -> Any:
        return run_dashboard(settings=self.settings, pool=self.pool, model=self.model, checkpointer=self.checkpointer,
                             store=self.long_term, **options)

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
    app = FastAPI(title="Nova", lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=[get_settings().nova_ui_origin], allow_methods=["*"],
                       allow_headers=["*"])

    @app.exception_handler(NovaError)
    async def nova_error(request: Request, error: NovaError) -> JSONResponse:
        status = STATUS_BY_CATEGORY.get(error.category, 502 if error.retryable else 500)
        return JSONResponse(status_code=status, content={"error": error.message, "category": error.category.value})

    def services(request: Request) -> Services:
        return request.app.state.services

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/config")
    def config(request: Request) -> dict[str, Any]:
        settings = services(request).settings
        return {"superset_url": settings.superset_public_url, "model": settings.model_name,
                "provider": settings.model_provider}

    # --- Runs -----------------------------------------------------------------

    @app.post("/api/runs", status_code=202)
    def start_run(body: StartRun, request: Request) -> dict[str, str]:
        return services(request).runs.start(body.prompt, thread_id=body.thread_id, user_id=LOCAL_USER,
                                            require_approval=body.require_approval)

    @app.get("/api/runs")
    def list_runs(request: Request, thread_id: str | None = None) -> list[dict[str, Any]]:
        return services(request).runs.list(thread_id=thread_id)

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str, request: Request) -> dict[str, Any]:
        runs = services(request).runs
        run = runs.get(run_id)
        return {**run, "wall_seconds": float(run["wall_seconds"]),
                "timing": summarise(runs.events(run_id), float(run["wall_seconds"]))}

    @app.post("/api/runs/{run_id}/resume", status_code=202)
    def resume_run(run_id: str, body: ResumeRun, request: Request) -> dict[str, str]:
        answer: Any = body.answer
        if body.plan is not None or body.rejected:
            answer = body.model_dump(exclude_none=True)
        return services(request).runs.resume(run_id, answer, user_id=LOCAL_USER)

    @app.get("/api/runs/{run_id}/events")
    async def stream_events(run_id: str, request: Request, after: int = 0) -> EventSourceResponse:
        runs = services(request).runs
        runs.get(run_id)  # a 404 here beats an empty stream

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
    def list_dashboards(request: Request) -> list[dict[str, Any]]:
        with services(request).pool.connection() as conn:
            cursor = conn.execute("SELECT slug, thread_id, superset_id, title, summary, created_by, created_at, updated_at "
                                  "FROM dashboards ORDER BY updated_at DESC")
            columns = [c.name for c in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]

    @app.get("/api/dashboards/{slug}/embed")
    def embed_dashboard(slug: str, request: Request) -> dict[str, Any]:
        """What the browser needs to show a dashboard. The token is short-lived, so the SDK
        asks for a new one here whenever it needs to."""
        app_services = services(request)
        with app_services.pool.connection() as conn:
            row = conn.execute("SELECT superset_id, embed_uuid, title FROM dashboards WHERE slug = %s", (slug,)).fetchone()
        if row is None or not row[1]:
            raise NotFound(f"no embeddable dashboard {slug}")
        superset_id, embed_uuid, title = row
        metadata = json.loads(app_services.superset.dashboard_detail(superset_id).get("json_metadata") or "{}")
        filters = [{"id": native["id"], "name": native["name"],
                    "column": ((native.get("targets") or [{}])[0].get("column") or {}).get("name")}
                   for native in metadata.get("native_filter_configuration") or []]
        return {"slug": slug, "title": title, "embed_uuid": embed_uuid, "filters": filters,
                "superset_url": app_services.settings.superset_public_url,
                "guest_token": app_services.superset.guest_token(embed_uuid, username=LOCAL_USER)}

    @app.get("/api/charts/{chart_id}/preview")
    def chart_preview(chart_id: int, request: Request) -> dict[str, Any]:
        """A chart's data, for the live preview while a dashboard is being built. It's read
        from Superset here rather than kept in the run's events, so no data is copied."""
        superset = services(request).superset
        detail = superset.chart_detail(chart_id)
        if not (detail.get("description") or "").startswith(CHART_KEY_PREFIX):
            raise NotFound(f"chart {chart_id} wasn't built by Nova")
        params = json.loads(detail.get("params") or "{}")
        data = superset.chart_data(chart_id)
        return {"chart_id": chart_id, "title": detail["slice_name"], "viz": params.get("viz_type"),
                "columns": data.columns, "rows": data.rows[:PREVIEW_ROWS], "row_count": data.row_count,
                "x_axis": params.get("x_axis"), "groupby": params.get("groupby") or [],
                "time_grain": params.get("time_grain_sqla"),
                "format": params.get("y_axis_format") or params.get("number_format")}

    # --- Sources and skills ---------------------------------------------------

    @app.get("/api/sources")
    def sources(request: Request) -> list[dict[str, Any]]:
        app_services = services(request)
        listed = []
        for source in list_sources(app_services.pool):
            entry = source.model_dump(exclude={"id"})
            if app_services.skills.exists(source.slug):
                front = app_services.skills.read_frontmatter(source.slug)
                entry["skill"] = {"name": front.name, "version": front.version, "status": front.status.value}
            listed.append(entry)
        return listed

    @app.get("/api/skills/{slug}")
    def skill(slug: str, request: Request) -> dict[str, Any]:
        store = services(request).skills
        front, body = parse_frontmatter((store.path(slug) / "SKILL.md").read_text()) if store.exists(slug) else (None, "")
        if front is None:
            raise NotFound(f"no skill named {slug}")
        layer = store.read_semantic_layer(slug)
        profile = store.read_profile(slug)
        directory = store.path(slug)
        return {"name": front.name, "description": front.description, "version": front.version, "status": front.status.value,
                "generated_at": front.generated_at, "reviewed_by": front.reviewed_by, "guide": body,
                "metrics": {key: metric.model_dump(mode="json") for key, metric in layer.metrics.items()},
                "proposed": {key: metric.model_dump(mode="json") for key, metric in layer.proposed.items()},
                "gotchas": [gotcha.model_dump(mode="json") for gotcha in profile.gotchas],
                "tables": [{"name": t.qualified, "rows": t.row_count} for t in profile.tables],
                "examples": (directory / "examples.sql").read_text(),
                "changelog": (directory / "CHANGELOG.md").read_text()}

    @app.post("/api/skills/{slug}/approve")
    def approve_skill(slug: str, body: ApproveSkill, request: Request) -> dict[str, Any]:
        store = services(request).skills
        if not store.exists(slug):
            raise NotFound(f"no skill named {slug}")
        front = store.approve(slug, reviewed_by=body.reviewed_by)
        return {"name": front.name, "version": front.version, "status": front.status.value}

    return app


app = create_app()
