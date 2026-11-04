"""The dashboard plan, the one clarifying question, and the human checkpoints for both."""

from __future__ import annotations

from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from langgraph.types import interrupt
from pydantic import BaseModel, Field

from agent.runtime import Runtime
from agent.tools.superset_payloads import TimeGrain, Viz

MAX_QUESTIONS = 1


class PlannedChart(BaseModel):
    key: str = Field(description="Short stable id, such as revenue-trend")
    title: str
    viz: Viz
    metric: str = Field(description="Which metric, by its skill definition")
    dimensions: list[str] = Field(default_factory=list)
    time_grain: TimeGrain | None = None
    filters: str | None = None
    width: int = Field(default=6, ge=1, le=12, description="Columns out of 12")


class DashboardPlan(BaseModel):
    title: str
    goal: str = Field(description="The user's goal, restated")
    sources: list[str] = Field(min_length=1)
    charts: list[PlannedChart] = Field(min_length=1)
    filters: list[str] = Field(default_factory=list, description="Native filters to offer")
    assumptions: list[str] = Field(default_factory=list)


class AskUserInput(BaseModel):
    question: str


def make_planning_tools(runtime: Runtime, *, require_approval: bool) -> list[BaseTool]:
    state = runtime.state

    def submit_plan(**fields: Any) -> str:
        """Submit the dashboard plan before building anything. The user may edit it."""
        plan = DashboardPlan(**fields)
        if require_approval:
            answer = interrupt({"type": "plan_approval", "plan": plan.model_dump(mode="json")})
            if isinstance(answer, dict) and answer.get("rejected"):
                return f"The user rejected this plan: {answer.get('reason', 'no reason given')}. Revise it and resubmit."
            if isinstance(answer, dict) and answer.get("plan"):
                plan = DashboardPlan.model_validate(answer["plan"])
        state.plan = plan.model_dump(mode="json")
        return f"Plan approved. Build exactly this:\n{plan.model_dump_json(indent=1)}"

    def ask_user(question: str) -> str:
        """Ask the user one question, only if the answer would materially change the
        dashboard. Otherwise state an assumption in the plan."""
        if state.questions_asked >= MAX_QUESTIONS:
            return "You've used your question. Make a reasonable assumption and record it in the plan."
        state.questions_asked += 1
        return str(interrupt({"type": "question", "question": question}))

    return [
        StructuredTool.from_function(func=submit_plan, name="submit_plan", args_schema=DashboardPlan),
        StructuredTool.from_function(func=ask_user, name="ask_user", args_schema=AskUserInput),
    ]
