"""Harness behaviour that has to run around every model and tool call."""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, ToolMessage

from agent.harness.budgets import RunBudget

logger = logging.getLogger("nova.trace")

EventSink = Callable[[dict[str, Any]], None]


class RunMiddleware(AgentMiddleware):
    """Enforces the budget and traces every step.

    Each model call is one step, and its real token count is recorded. A spent budget
    raises BudgetExceeded, which stops the graph; the run boundary catches it and returns
    what was finished, with the last checkpoint intact.

    Each step also goes to the JSON logs and to `sink`, which is how the API streams a
    run's progress. Arguments and results aren't traced: they hold data from sources.
    """

    def __init__(self, budget: RunBudget, *, agent: str = "main", run_id: str = "", sink: EventSink | None = None) -> None:
        super().__init__()
        self._budget = budget
        self._agent = agent
        self._run_id = run_id
        self._sink = sink

    def _emit(self, **event: Any) -> None:
        event = {"run_id": self._run_id, "agent": self._agent, **event}
        logger.info("step", extra=event)
        if self._sink:
            self._sink(event)

    def wrap_model_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        self._budget.spend_step()
        started = time.monotonic()
        response = handler(request)
        message = next((m for m in reversed(getattr(response, "result", None) or [])
                        if isinstance(m, AIMessage)), None)
        usage = (message.usage_metadata or {}) if message else {}
        self._budget.spend_tokens(usage.get("total_tokens", 0))
        self._emit(event="model", seconds=round(time.monotonic() - started, 1), tokens=usage.get("total_tokens", 0),
                   reasoning_tokens=(usage.get("output_token_details") or {}).get("reasoning", 0),
                   tools=[call["name"] for call in (message.tool_calls if message else [])])
        return response

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        started = time.monotonic()
        name = request.tool_call["name"]
        self._emit(event="tool_start", tool=name)
        result = handler(request)
        failed = isinstance(result, ToolMessage) and (
            result.status == "error" or str(result.content).startswith(("FAILED", "Not accepted", "Not finished")))
        self._emit(event="tool_end", tool=name, seconds=round(time.monotonic() - started, 1), ok=not failed)
        return result
