"""The main agent, and running it: prompt in, dashboard out.

The run boundary is here. Budgets stop a run gracefully, and an interrupted run resumes from its
last checkpoint. The dashboard is a spec Nova draws, and every chart in it is a semantic query that
Cube answers for whoever opens it.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any

from deepagents import FilesystemPermission, SubAgent, create_deep_agent
from deepagents.backends import CompositeBackend, FilesystemBackend, StateBackend
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.store.base import BaseStore
from langgraph.types import Command
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

from agent.config import Settings
from agent.harness.access import UserAccess
from agent.projects import describe_for_agent
from agent.harness.audit import RunContext
from agent.harness.errors import BudgetExceeded, InvalidRequest, NotFound, NovaError
from agent.dashboards import load_spec
from agent.harness.middleware import EventSink, RunMiddleware, SubagentLimits
from agent.runtime import Runtime, make_runtime
from agent.tools.dashboard_tools import make_add_definition, make_builder_tools, make_semantic_query
from agent.tools.planning import make_planning_tools
from agent.tools.skill_tools import make_source_tools
from agent.tools.sql_tools import make_run_readonly_sql

logger = logging.getLogger(__name__)

PROMPTS = Path(__file__).parent / "prompts"
RECURSION_LIMIT = 300
PREFERENCES_NAMESPACE = ("org", "preferences")


class RunStatus(StrEnum):
    FINISHED = "finished"
    AWAITING_APPROVAL = "awaiting_approval"
    AWAITING_ANSWER = "awaiting_answer"
    PARTIAL = "partial"
    FAILED = "failed"


class RunResult(BaseModel):
    status: RunStatus
    thread_id: str
    run_id: str
    dashboard_slug: str | None = None
    title: str | None = None
    summary: str = ""
    definitions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    plan: dict[str, Any] | None = None
    question: str | None = None
    error: str | None = None
    usage: dict[str, float] = Field(default_factory=dict)


def dashboard_key_for(thread_id: str) -> str:
    """One conversation is one dashboard, so a retry or a follow-up finds what it built."""
    return f"nova-{hashlib.sha256(thread_id.encode()).hexdigest()[:12]}"


def load_prompt(name: str) -> str:
    """A system prompt, with today's date. Models don't know it. Without this a verifier
    assumed an earlier month, "disproved" a correct dashboard, and the repair broke it."""
    today = datetime.now(timezone.utc).date().isoformat()
    return f"{(PROMPTS / f'{name}.md').read_text()}\n\nToday is {today} (UTC)."


def build_agent(runtime: Runtime, *, require_approval: bool, checkpointer: BaseCheckpointSaver,
                store: BaseStore | None = None):
    limits = SubagentLimits()

    def middleware(agent: str) -> RunMiddleware:
        return RunMiddleware(runtime.budget, agent=agent, run_id=runtime.context.run_id, sink=runtime.sink,
                             limits=limits if agent == "main" else None)

    run_sql = make_run_readonly_sql(runtime.executor, policy=runtime.policy, context=runtime.context,
                                    budget=runtime.budget)
    source_tools = {tool.name: tool for tool in make_source_tools(runtime)}
    semantic_query = make_semantic_query(runtime)
    builder = make_builder_tools(runtime)

    def pick(*names: str) -> list:
        return [builder[name] for name in names]

    subagents: list[SubAgent] = [
        SubAgent(name="modeler", system_prompt=load_prompt("modeler"), middleware=[middleware("modeler")],
                 description="Adds a measure or dimension the plan needs and the semantic model lacks.",
                 tools=[source_tools["load_skill"], semantic_query, run_sql, make_add_definition(runtime)]),
        SubAgent(name="chart_designer", system_prompt=load_prompt("chart_designer"), middleware=[middleware("chart_designer")],
                 description="Creates the charts from the semantic model, and the layout and filters.",
                 tools=[semantic_query, *pick("create_chart", "remove_chart", "set_layout", "get_dashboard",
                                              "inspect_dashboard", "get_chart_data")]),
        SubAgent(name="verifier", system_prompt=load_prompt("verifier"), middleware=[middleware("verifier")],
                 description="Checks a built dashboard's numbers against the definitions, and that it renders.",
                 tools=[source_tools["load_skill"], run_sql, semantic_query,
                        *pick("inspect_dashboard", "get_chart_data", "render_dashboard_screenshot")]),
    ]
    main_tools = [*source_tools.values(), *make_planning_tools(runtime, require_approval=require_approval), semantic_query,
                  *pick("get_dashboard", "inspect_dashboard", "finish_dashboard")]

    skills_backend = FilesystemBackend(root_dir=runtime.skills_view or runtime.settings.nova_skills_dir, virtual_mode=True)
    backend = CompositeBackend(default=StateBackend(), routes={"/skills/": skills_backend})
    return create_deep_agent(
        model=runtime.model, tools=main_tools, system_prompt=load_prompt("main"), subagents=subagents,
        skills=["/skills/"], backend=backend, middleware=[middleware("main")], checkpointer=checkpointer, store=store,
        permissions=[FilesystemPermission(operations=["write"], paths=["/skills/**"], mode="deny")],
    )


def with_existing(runtime: Runtime) -> None:
    """A follow-up in a conversation changes the dashboard it already built."""
    try:
        spec, _ = load_spec(runtime.pool, runtime.state.dashboard_key)
    except NotFound:
        return
    if spec is not None:
        runtime.state.spec, runtime.state.laid_out = spec, bool(spec.rows)


def load_preferences(store: BaseStore | None) -> str:
    if store is None:
        return ""
    items = store.search(PREFERENCES_NAMESPACE)
    return "\n".join(f"- {item.key}: {item.value.get('value')}" for item in items)


def interrupt_payload(result: dict[str, Any]) -> dict[str, Any] | None:
    interrupts = result.get("__interrupt__") or ()
    return interrupts[0].value if interrupts else None


def result_from(runtime: Runtime, status: RunStatus, thread_id: str, *, error: str | None = None,
                pending: dict[str, Any] | None = None) -> RunResult:
    state = runtime.state
    pending = pending or {}
    return RunResult(status=status, thread_id=thread_id, run_id=runtime.context.run_id,
                     dashboard_slug=state.dashboard_key if state.laid_out else None,
                     title=state.spec.title if state.spec else None, summary=state.summary,
                     definitions=state.definitions, warnings=state.warnings, plan=pending.get("plan") or state.plan,
                     question=pending.get("question"), error=error, usage=runtime.budget.snapshot())


def run_dashboard(*, settings: Settings, pool: ConnectionPool, model: BaseChatModel, checkpointer: BaseCheckpointSaver,
                  context: RunContext, thread_id: str, prompt: str | None = None, resume: Any = None,
                  require_approval: bool = False, store: BaseStore | None = None,
                  sink: EventSink | None = None, access: UserAccess | None = None,
                  project_id: str | None = None) -> RunResult:
    """Start a run, continue a conversation, or resume one paused for a person.

    Pass `prompt` to start or continue, or `resume` to answer an interrupt. Calling with
    neither picks up an interrupted run from its last checkpoint.
    """
    if not thread_id:
        raise InvalidRequest("a run needs a thread_id: it's what identifies its dashboard")
    runtime = make_runtime(settings, pool, model, context=context, dashboard_key=dashboard_key_for(thread_id),
                           thread_id=thread_id, sink=sink, access=access, project_id=project_id)
    try:
        with_existing(runtime)
        agent = build_agent(runtime, require_approval=require_approval, checkpointer=checkpointer, store=store)
        config = {"configurable": {"thread_id": thread_id}, "recursion_limit": RECURSION_LIMIT}

        if resume is not None:
            payload: Any = Command(resume=resume)
        elif prompt is not None:
            preferences = load_preferences(store)
            intro = f"Org preferences, set by an administrator:\n{preferences}\n\n" if preferences else ""
            intro += describe_for_agent(pool, project_id=project_id, sheet_slug=runtime.state.dashboard_key)
            payload = {"messages": [HumanMessage(f"{intro}{prompt}")]}
        else:
            payload = None

        result = agent.invoke(payload, config)
        pending = interrupt_payload(result)
        if pending is None and not runtime.state.finished and runtime.state.laid_out:
            reminder = HumanMessage("You stopped before finish_dashboard accepted the dashboard. Finish it now.")
            result = agent.invoke({"messages": [reminder]}, config)
            pending = interrupt_payload(result)

        if pending is not None:
            status = (RunStatus.AWAITING_APPROVAL if pending.get("type") == "plan_approval"
                      else RunStatus.AWAITING_ANSWER)
            return result_from(runtime, status, thread_id, pending=pending)

        if not runtime.state.finished:
            return result_from(runtime, RunStatus.FAILED, thread_id, error="the agent stopped without a dashboard")

        return result_from(runtime, RunStatus.FINISHED, thread_id)

    except BudgetExceeded as error:
        logger.warning("run stopped by budget", extra={"run_id": context.run_id, "resource": error.resource})
        return result_from(runtime, RunStatus.PARTIAL, thread_id, error=error.message)
    except NovaError as error:
        logger.error("run failed", extra={"run_id": context.run_id, "reason": error.message})
        return result_from(runtime, RunStatus.FAILED, thread_id, error=error.message)
    except Exception:
        # The checkpoint survives this, so the run can still be resumed once it's fixed.
        logger.exception("run crashed", extra={"run_id": context.run_id})
        return result_from(runtime, RunStatus.FAILED, thread_id, error="the run crashed; it can be resumed")
    finally:
        runtime.close()
