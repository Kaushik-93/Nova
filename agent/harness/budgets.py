"""Per-run limits. When one runs out the run stops gracefully and returns what it
has, rather than spinning or running up a bill."""

from __future__ import annotations

import threading
import time
from typing import Callable

from pydantic import BaseModel

from agent.harness.errors import BudgetExceeded


class BudgetLimits(BaseModel):
    max_steps: int = 150
    max_tokens: int = 2_000_000
    max_wall_seconds: float = 900
    max_sql_queries: int = 120


class RunBudget:
    """Counts a run's spending. Thread-safe: tools run in a pool."""

    def __init__(self, limits: BudgetLimits, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.limits = limits
        self._clock = clock
        self._started = clock()
        self._lock = threading.Lock()
        self.steps = 0
        self.tokens = 0
        self.sql_queries = 0

    @property
    def elapsed_seconds(self) -> float:
        return self._clock() - self._started

    def check(self) -> None:
        """Raise if any limit has been reached, without spending anything."""
        if self.elapsed_seconds >= self.limits.max_wall_seconds:
            raise BudgetExceeded("wall time in seconds", self.limits.max_wall_seconds)
        if self.steps >= self.limits.max_steps:
            raise BudgetExceeded("steps", self.limits.max_steps)
        if self.tokens >= self.limits.max_tokens:
            raise BudgetExceeded("tokens", self.limits.max_tokens)

    def spend_step(self) -> None:
        self.check()
        with self._lock:
            self.steps += 1

    def spend_tokens(self, count: int) -> None:
        """Tokens are only known after a call, so this records first and the next
        check stops the run."""
        with self._lock:
            self.tokens += count

    def spend_query(self) -> None:
        self.check()
        with self._lock:
            if self.sql_queries >= self.limits.max_sql_queries:
                raise BudgetExceeded("SQL queries", self.limits.max_sql_queries)
            self.sql_queries += 1

    def snapshot(self) -> dict[str, float]:
        return {"steps": self.steps, "tokens": self.tokens, "sql_queries": self.sql_queries,
                "elapsed_seconds": round(self.elapsed_seconds, 1)}
