"""Harness behaviour that has to run around every model call."""

from __future__ import annotations

import logging
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage

from agent.harness.budgets import RunBudget

logger = logging.getLogger(__name__)


class BudgetMiddleware(AgentMiddleware):
    """Counts each model call as a step and records the tokens it actually used.

    A spent budget raises BudgetExceeded, which stops the graph. The run boundary
    catches it and returns what was finished. The last checkpoint is intact.
    """

    def __init__(self, budget: RunBudget) -> None:
        super().__init__()
        self._budget = budget

    def before_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        self._budget.spend_step()
        return None

    def after_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        messages = state.get("messages") or []
        if messages and isinstance(messages[-1], AIMessage) and messages[-1].usage_metadata:
            self._budget.spend_tokens(messages[-1].usage_metadata.get("total_tokens", 0))
        return None
