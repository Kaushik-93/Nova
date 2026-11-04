"""Runs in the background, with every step saved as it happens. The API reads steps from
Postgres, so a browser that refreshes or reconnects loses nothing."""

from __future__ import annotations

import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from agent.dashboard_agent import RunResult, RunStatus
from agent.harness.audit import RunContext
from agent.harness.errors import InvalidRequest, NotFound

logger = logging.getLogger(__name__)

RUNNING = "running"
INTERRUPTED = "interrupted"
TERMINAL = {RunStatus.FINISHED.value, RunStatus.PARTIAL.value, RunStatus.FAILED.value, INTERRUPTED}
Runner = Callable[..., RunResult]


class RunManager:
    def __init__(self, pool: ConnectionPool, runner: Runner, *, max_workers: int = 2) -> None:
        self._pool = pool
        self._runner = runner
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="nova-run")

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    def mark_abandoned(self) -> None:
        """A run can't survive its process, but its checkpoint does, so it can be resumed."""
        with self._pool.connection() as conn:
            conn.execute("UPDATE runs SET status = %s, finished_at = now() WHERE status = %s", (INTERRUPTED, RUNNING))

    def _insert(self, run_id: str, thread_id: str, prompt: str, user_id: str) -> None:
        with self._pool.connection() as conn:
            active = conn.execute("SELECT 1 FROM runs WHERE thread_id = %s AND status = %s", (thread_id, RUNNING)).fetchone()
            if active:
                raise InvalidRequest("this conversation already has a run in progress")
            conn.execute("INSERT INTO runs (run_id, thread_id, prompt, status, created_by) VALUES (%s, %s, %s, %s, %s)",
                         (run_id, thread_id, prompt, RUNNING, user_id))

    def start(self, prompt: str, *, thread_id: str | None, user_id: str, require_approval: bool) -> dict[str, str]:
        if not prompt.strip():
            raise InvalidRequest("a run needs a prompt")
        run_id = f"run-{uuid.uuid4().hex[:12]}"
        thread = thread_id or f"thread-{uuid.uuid4().hex[:12]}"
        self._insert(run_id, thread, prompt, user_id)
        self._executor.submit(self._execute, run_id, thread, user_id, prompt=prompt, require_approval=require_approval)
        return {"run_id": run_id, "thread_id": thread}

    def resume(self, run_id: str, answer: Any, *, user_id: str) -> dict[str, str]:
        """Continue a run that paused for a person or was interrupted, as a new run on the
        same conversation."""
        previous = self.get(run_id)
        if previous["status"] in (RunStatus.AWAITING_APPROVAL.value, RunStatus.AWAITING_ANSWER.value):
            if answer is None:
                raise InvalidRequest("this run is waiting for an answer")
        elif previous["status"] != INTERRUPTED and previous["status"] != RunStatus.FAILED.value:
            raise InvalidRequest(f"a run that is {previous['status']} can't be resumed")
        new_run = f"run-{uuid.uuid4().hex[:12]}"
        self._insert(new_run, previous["thread_id"], previous["prompt"], user_id)
        self._executor.submit(self._execute, new_run, previous["thread_id"], user_id, resume=answer,
                              require_approval=previous["status"] == RunStatus.AWAITING_APPROVAL.value)
        return {"run_id": new_run, "thread_id": previous["thread_id"]}

    def record(self, run_id: str, event: dict[str, Any]) -> None:
        with self._pool.connection() as conn:
            conn.execute("INSERT INTO run_events (run_id, event) VALUES (%s, %s)", (run_id, Jsonb(event)))

    def _execute(self, run_id: str, thread_id: str, user_id: str, **options: Any) -> None:
        context = RunContext(run_id=run_id, user_id=user_id)
        try:
            result = self._runner(context=context, thread_id=thread_id, sink=lambda e: self.record(run_id, e), **options)
            status, stored = result.status.value, result.model_dump(mode="json", exclude={"guest_token"})
        except Exception:  # a run must never die silently: the UI is waiting on it
            logger.exception("run crashed", extra={"run_id": run_id})
            status, stored = RunStatus.FAILED.value, {"error": "the run crashed; it can be resumed"}
        with self._pool.connection() as conn:
            conn.execute("UPDATE runs SET status = %s, result = %s, finished_at = now() WHERE run_id = %s",
                         (status, Jsonb(stored), run_id))

    def get(self, run_id: str) -> dict[str, Any]:
        with self._pool.connection() as conn:
            cursor = conn.execute(
                """SELECT run_id, thread_id, prompt, status, created_by, started_at, finished_at, result,
                          EXTRACT(EPOCH FROM COALESCE(finished_at, now()) - started_at) AS wall_seconds
                   FROM runs WHERE run_id = %s""", (run_id,))
            row = cursor.fetchone()
            if row is None:
                raise NotFound(f"no run {run_id}")
            return dict(zip([c.name for c in cursor.description], row))

    def list(self, *, thread_id: str | None = None, limit: int = 30) -> list[dict[str, Any]]:
        where = "WHERE thread_id = %s" if thread_id else ""
        with self._pool.connection() as conn:
            cursor = conn.execute(
                f"""SELECT run_id, thread_id, prompt, status, started_at, finished_at, result->>'title' AS title,
                           result->>'summary' AS summary
                    FROM runs {where} ORDER BY started_at DESC LIMIT %s""", (*([thread_id] if thread_id else []), limit))
            columns = [c.name for c in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def events(self, run_id: str, *, after: int = 0) -> list[dict[str, Any]]:
        with self._pool.connection() as conn:
            rows = conn.execute("SELECT id, at, event FROM run_events WHERE run_id = %s AND id > %s ORDER BY id",
                                (run_id, after)).fetchall()
        return [{**event, "id": event_id, "at": at.isoformat()} for event_id, at, event in rows]
