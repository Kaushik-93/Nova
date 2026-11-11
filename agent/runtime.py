"""What one agent run works with. Tools close over these, so the model never handles a
connection, credential or user id, and can't name a different user or catalog."""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from agent.config import Settings
from agent.harness.access import UserAccess, build_policy, make_skills_view
from agent.harness.audit import AuditLog, RunContext
from agent.harness.budgets import BudgetLimits, RunBudget
from agent.harness.executor import CostLimits, SqlExecutor
from agent.harness.guardrails import SqlPolicy
from agent.harness.middleware import EventSink
from agent.projects import project_for_new_sheet
from agent.semantic.client import AuditedCubeClient, CubeClient, CubeIdentity, identity_for
from agent.semantic.charts import DashboardSpec
from agent.tools.skill_store import SkillStore
from agent.tools.sources import Source, list_sources


@dataclass
class BuildState:
    """What this run has built so far. It survives a budget stop, so the run can report
    what exists."""

    dashboard_key: str
    thread_id: str = ""
    spec: DashboardSpec | None = None  # the charts so far, and once laid out, the dashboard
    laid_out: bool = False
    project_id: str | None = None
    plan: dict[str, Any] | None = None
    questions_asked: int = 0
    finish_attempts: int = 0
    finished: bool = False
    summary: str = ""
    definitions: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class Runtime:
    settings: Settings
    pool: ConnectionPool
    model: BaseChatModel
    audit: AuditLog
    executor: SqlExecutor
    cube: CubeClient
    cube_identity: CubeIdentity
    store: SkillStore
    context: RunContext
    budget: RunBudget
    policy: SqlPolicy
    sources: list[Source]
    state: BuildState
    sink: EventSink | None = None
    access: UserAccess | None = None
    skills_view: str | None = None  # this user's own view of the skills; see make_skills_view

    def close(self) -> None:
        self.cube.close()
        if self.skills_view:
            shutil.rmtree(self.skills_view, ignore_errors=True)

    def emit(self, event: str, **fields: Any) -> None:
        """Tell the live view something happened. Only ids and labels go here: a chart's data
        is fetched when it's shown, so no source data is copied into the event log."""
        if self.sink:
            self.sink({"event": event, "agent": "main", "run_id": self.context.run_id, **fields})

    def save_dashboard(self, catalogs: list[str]) -> None:
        """Record the dashboard whenever it changes, so it can be shown while it's being built and
        checked. Its charts draw from Cube for whoever opens it."""
        state = self.state
        spec = state.spec
        with self.pool.connection() as conn:
            project = project_for_new_sheet(conn, sheet_slug=state.dashboard_key, requested=state.project_id,
                                            title=spec.title, owner=self.context.user_id)
            conn.execute(
                """INSERT INTO dashboards (slug, thread_id, title, summary, created_by, catalogs, project_id, sheet_name,
                                          position, spec)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                           (SELECT COALESCE(MAX(position) + 1, 0) FROM dashboards WHERE project_id = %s), %s)
                   ON CONFLICT (slug) DO UPDATE SET title = EXCLUDED.title, spec = EXCLUDED.spec, updated_at = now(),
                       summary = COALESCE(NULLIF(EXCLUDED.summary, ''), dashboards.summary), catalogs = EXCLUDED.catalogs""",
                (state.dashboard_key, state.thread_id, spec.title, state.summary, self.context.user_id, catalogs, project,
                 spec.title, project, Jsonb(spec.model_dump(mode="json"))))


def make_runtime(settings: Settings, pool: ConnectionPool, model: BaseChatModel, *, context: RunContext,
                 dashboard_key: str, thread_id: str = "", sink: EventSink | None = None,
                 access: UserAccess | None = None, project_id: str | None = None) -> Runtime:
    """`access=None` is the command line and tests. The API always passes the user's access."""
    audit = AuditLog(pool)
    store = SkillStore(settings.nova_skills_dir, pool)

    sources = [source for source in list_sources(pool) if source.status == "ready"]
    visible = [source for source in sources if access is None or access.can_read(source)]
    return Runtime(
        settings=settings, pool=pool, model=model, audit=audit, context=context, sources=visible, sink=sink, access=access,
        skills_view=make_skills_view(visible, access, settings.nova_skills_dir),
        executor=SqlExecutor(settings.trino_url, audit, timeout_seconds=settings.nova_query_timeout_seconds,
                             cost_limits=CostLimits(max_estimated_rows=settings.nova_max_estimated_rows,
                                                    max_estimated_bytes=settings.nova_max_estimated_bytes)),
        cube=AuditedCubeClient(settings.cube_url, settings.cubejs_api_secret, audit=audit, context=context),
        cube_identity=identity_for(access, visible, user_id=context.user_id),
        store=store,
        budget=RunBudget(BudgetLimits(max_steps=settings.nova_max_steps, max_tokens=settings.nova_max_tokens,
                                      max_wall_seconds=settings.nova_max_wall_seconds,
                                      max_sql_queries=settings.nova_max_sql_queries)),
        policy=build_policy(visible, access, store),
        state=BuildState(dashboard_key=dashboard_key, thread_id=thread_id, project_id=project_id),
    )
