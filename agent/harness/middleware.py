"""Harness behaviour that has to run around every model and tool call."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, ToolMessage

from agent.harness.budgets import RunBudget

logger = logging.getLogger("nova.trace")

EventSink = Callable[[dict[str, Any]], None]

MAX_TEXT = 3000
MAX_DETAIL = 400
# Which argument says what a tool call is doing, for the live view.
DETAIL_FIELDS = {
    "load_skill": "name", "check_skill_freshness": "name", "introspect_schema": "catalog", "sample_table": "table",
    "get_chart_data": "key", "remove_chart": "key", "set_layout": "title", "add_definition": "cube",
    "run_readonly_sql": "sql", "write_file": "file_path", "read_file": "file_path", "submit_plan": "title",
    "ask_user": "question", "finish_dashboard": "summary",
}


def describe_call(name: str, args: dict[str, Any]) -> str:
    """One line saying what a tool call is doing."""
    if name == "task":
        return f"{args.get('subagent_type', '')}: {args.get('description', '')}"[:MAX_DETAIL]
    if name == "create_chart":
        return str((args.get("spec") or {}).get("title", ""))[:MAX_DETAIL]
    if name == "semantic_query":
        query = args.get("query") or {}
        return ", ".join([*query.get("measures", []), *query.get("dimensions", [])])[:MAX_DETAIL]
    field = DETAIL_FIELDS.get(name)
    return " ".join(str(args.get(field, "")).split())[:MAX_DETAIL] if field else ""


class SubagentLimits:
    """How many times each subagent may be used in a run. Shared by a run's middleware.

    A prompt can only ask an agent to stop repairing. In practice one kept going for nine
    rounds over label overlap, so the limit is here.
    """

    DEFAULTS = {"verifier": 2, "sql_author": 3, "chart_designer": 3}

    def __init__(self, limits: dict[str, int] | None = None) -> None:
        self._limits = limits or self.DEFAULTS
        self._used: dict[str, int] = {}
        self._lock = threading.Lock()

    def allow(self, subagent: str) -> bool:
        with self._lock:
            if self._used.get(subagent, 0) >= self._limits.get(subagent, 99):
                return False
            self._used[subagent] = self._used.get(subagent, 0) + 1
            return True


class RunMiddleware(AgentMiddleware):
    """Enforces the budget and traces every step.

    Each model call is one step, and its real token count is recorded. A spent budget
    raises BudgetExceeded, which stops the graph; the run boundary catches it and returns
    what was finished, with the last checkpoint intact.

    Each step goes to the JSON logs and to `sink`, which is how the API shows a run live.
    The logs get timings and names only. Arguments and text can hold data from sources, so
    those go only to `sink`, for the person who started the run.
    """

    def __init__(self, budget: RunBudget, *, agent: str = "main", run_id: str = "", sink: EventSink | None = None,
                 limits: SubagentLimits | None = None) -> None:
        super().__init__()
        self._budget = budget
        self._agent = agent
        self._run_id = run_id
        self._sink = sink
        self._limits = limits

    def _emit(self, *, private: dict[str, Any] | None = None, **event: Any) -> None:
        event = {"run_id": self._run_id, "agent": self._agent, **event}
        logger.info("step", extra=event)
        if self._sink:
            self._sink(event | (private or {}))

    def wrap_model_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        self._budget.spend_step()
        self._emit(event="model_start")
        started = time.monotonic()
        response = handler(request)
        message = next((m for m in reversed(getattr(response, "result", None) or [])
                        if isinstance(m, AIMessage)), None)
        usage = (message.usage_metadata or {}) if message else {}
        self._budget.spend_tokens(usage.get("total_tokens", 0))
        calls = message.tool_calls if message else []
        self._emit(event="model", seconds=round(time.monotonic() - started, 1), tokens=usage.get("total_tokens", 0),
                   reasoning_tokens=(usage.get("output_token_details") or {}).get("reasoning", 0),
                   tools=[call["name"] for call in calls],
                   private={"text": (message.text or "")[:MAX_TEXT] if message else ""})
        return response

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        started = time.monotonic()
        call = request.tool_call
        name, args = call["name"], call.get("args") or {}
        if name == "task" and self._limits and not self._limits.allow(args.get("subagent_type", "")):
            self._emit(event="tool_blocked", tool=name, subagent=args.get("subagent_type", ""))
            return ToolMessage(f"{args.get('subagent_type')} has been used as often as a run allows. Don't delegate "
                               "any more. Finish now, and say in your summary what's still open.",
                               tool_call_id=call.get("id") or "")
        self._emit(event="tool_start", tool=name, private={"detail": describe_call(name, args)})
        result = handler(request)
        failed = isinstance(result, ToolMessage) and (
            result.status == "error" or str(result.content).startswith(("FAILED", "Not accepted", "Not finished")))
        self._emit(event="tool_end", tool=name, seconds=round(time.monotonic() - started, 1), ok=not failed,
                   private={"result": str(result.content)[:300]} if failed else None)
        return result
