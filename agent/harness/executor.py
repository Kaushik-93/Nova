"""The one path by which the agent's SQL reaches data.

    budget -> guardrails -> EXPLAIN cost check -> run with a timeout -> audit

Everything goes through Trino, whose catalogs hold the read-only credentials. The
agent only ever names a catalog, so it has no connection string to see or leak.
"""

from __future__ import annotations

import json
import logging
import math
import time
from typing import Any, Iterator
from urllib.parse import urlsplit

import trino
from pydantic import BaseModel, Field

from agent.harness.audit import AuditKind, AuditLog, AuditStatus, RunContext
from agent.harness.budgets import RunBudget
from agent.harness.errors import NovaError, QueryFailure, QueryTimedOut, QueryTooExpensive, SqlRejected
from agent.harness.guardrails import GuardedQuery, SqlPolicy, guard

logger = logging.getLogger(__name__)

TIMEOUT_ERRORS = {"EXCEEDED_TIME_LIMIT", "EXCEEDED_CPU_LIMIT"}


class CostLimits(BaseModel):
    """EXPLAIN's estimates, taken at the largest step of the plan."""

    max_estimated_rows: float = 50_000_000
    max_estimated_bytes: float = 5_000_000_000


class CostEstimate(BaseModel):
    rows: float | None = None
    bytes: float | None = None


class QueryResult(BaseModel):
    columns: list[str]
    rows: list[list[Any]] = Field(default_factory=list)
    row_count: int
    truncated: bool
    sql: str
    elapsed_ms: int
    estimate: CostEstimate


def _plan_nodes(plan: Any) -> Iterator[dict[str, Any]]:
    if isinstance(plan, dict):
        if "estimates" in plan:
            yield plan
        for value in plan.values():
            yield from _plan_nodes(value)
    elif isinstance(plan, list):
        for value in plan:
            yield from _plan_nodes(value)


def estimate_from_plan(plan: Any) -> CostEstimate:
    """The largest row and byte counts at any step of the plan.

    Only the largest step is meaningful. The plan's final output is useless for this:
    `SELECT count(*)` over a cross join of two huge tables outputs one row. NaN means
    Trino has no statistics, and those steps are skipped; the timeout covers them.
    """
    rows, sizes = [], []
    for node in _plan_nodes(plan):
        for estimate in node.get("estimates") or []:
            for key, into in (("outputRowCount", rows), ("outputSizeInBytes", sizes)):
                value = estimate.get(key)
                if isinstance(value, (int, float)) and not math.isnan(value):
                    into.append(value)
    return CostEstimate(rows=max(rows, default=None), bytes=max(sizes, default=None))


class SqlExecutor:
    def __init__(self, trino_url: str, audit: AuditLog, *, timeout_seconds: int = 30,
                 cost_limits: CostLimits | None = None) -> None:
        url = urlsplit(trino_url)
        self._host, self._port, self._scheme = url.hostname, url.port or 8080, url.scheme or "http"
        self._audit = audit
        self._timeout_seconds = timeout_seconds
        self._cost_limits = cost_limits or CostLimits()

    def _connect(self) -> trino.dbapi.Connection:
        return trino.dbapi.connect(
            host=self._host, port=self._port, http_scheme=self._scheme, user="nova-agent", source="nova-agent",
            session_properties={"query_max_execution_time": f"{self._timeout_seconds}s"},
        )

    def run(self, sql: str, *, policy: SqlPolicy, context: RunContext, budget: RunBudget) -> QueryResult:
        """Run one query. Everything that happens, including refusals, is audited."""
        budget.spend_query()
        started = time.monotonic()
        guarded: GuardedQuery | None = None
        estimate = CostEstimate()
        try:
            guarded = guard(sql, policy)
            connection = self._connect()
            try:
                cursor = connection.cursor()
                estimate = self._estimate(cursor, guarded)
                self._enforce_cost(estimate)
                result = self._execute(cursor, guarded, estimate, started)
            finally:
                connection.close()
        except (SqlRejected, QueryTooExpensive) as error:
            self._record(context, AuditStatus.REJECTED, guarded.sql if guarded else sql, guarded, started,
                         detail={"reason": error.message, "estimate": estimate.model_dump()})
            raise
        except NovaError as error:
            self._record(context, AuditStatus.FAILED, guarded.sql if guarded else sql, guarded, started,
                         detail={"reason": error.message})
            raise
        self._record(context, AuditStatus.OK, guarded.sql, guarded, started, rows=result.row_count,
                     detail={"estimate": estimate.model_dump(), "truncated": result.truncated})
        return result

    def _record(self, context: RunContext, status: AuditStatus, statement: str, guarded: GuardedQuery | None,
                started: float, *, rows: int | None = None, detail: dict[str, Any]) -> None:
        self._audit.record(context, AuditKind.SQL, status, target=",".join(guarded.tables) if guarded else "",
                           statement=statement, rows=rows, elapsed_ms=int((time.monotonic() - started) * 1000),
                           detail=detail)

    def _estimate(self, cursor: trino.dbapi.Cursor, guarded: GuardedQuery) -> CostEstimate:
        try:
            cursor.execute(f"EXPLAIN (TYPE LOGICAL, FORMAT JSON) {guarded.sql}")
            return estimate_from_plan(json.loads(cursor.fetchall()[0][0]))
        except trino.exceptions.TrinoQueryError as error:
            raise self._translate(error) from error

    def _enforce_cost(self, estimate: CostEstimate) -> None:
        limits = self._cost_limits
        if estimate.rows is not None and estimate.rows > limits.max_estimated_rows:
            raise QueryTooExpensive(f"estimated to process {estimate.rows:,.0f} rows, over the limit of "
                                    f"{limits.max_estimated_rows:,.0f}; check the joins and add filters")
        if estimate.bytes is not None and estimate.bytes > limits.max_estimated_bytes:
            raise QueryTooExpensive(f"estimated to process {estimate.bytes:,.0f} bytes, over the limit of "
                                    f"{limits.max_estimated_bytes:,.0f}; check the joins and add filters")

    def _execute(self, cursor: trino.dbapi.Cursor, guarded: GuardedQuery, estimate: CostEstimate,
                 started: float) -> QueryResult:
        try:
            cursor.execute(guarded.sql)
            rows = cursor.fetchall()
        except trino.exceptions.TrinoQueryError as error:
            raise self._translate(error) from error
        columns = [column[0] for column in cursor.description or []]
        return QueryResult(columns=columns, rows=[list(row) for row in rows], row_count=len(rows),
                           truncated=len(rows) >= guarded.limit, sql=guarded.sql, estimate=estimate,
                           elapsed_ms=int((time.monotonic() - started) * 1000))

    @staticmethod
    def _translate(error: trino.exceptions.TrinoQueryError) -> NovaError:
        if error.error_name in TIMEOUT_ERRORS:
            return QueryTimedOut("the query ran past its time limit; narrow it or add filters")
        return QueryFailure(f"{error.error_name}: {(error.message or '')[:500]}")
