"""Runs in the background, with every step saved as it happens. The API reads steps from
Postgres, so a browser that refreshes or reconnects loses nothing."""

from __future__ import annotations

import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from agent.dashboard_agent import RunResult, RunStatus, dashboard_key_for
from agent.harness.access import UserAccess
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
            # Connecting a source dies with its process too. It can't be resumed, but can be retried.
            conn.execute("UPDATE sources SET status = 'failed' WHERE status IN ('connecting', 'profiling')")

    def _insert(self, run_id: str, thread_id: str, prompt: str, user_id: str, *, kind: str = "dashboard",
                project_id: str | None = None) -> None:
        with self._pool.connection() as conn:
            active = conn.execute("SELECT 1 FROM runs WHERE thread_id = %s AND status = %s", (thread_id, RUNNING)).fetchone()
            if active:
                raise InvalidRequest("this conversation already has a run in progress")
            conn.execute("""INSERT INTO runs (run_id, thread_id, prompt, status, created_by, kind, dashboard_key, project_id)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                         (run_id, thread_id, prompt, RUNNING, user_id, kind, dashboard_key_for(thread_id), project_id))

    def owner_of_thread(self, thread_id: str) -> str | None:
        with self._pool.connection() as conn:
            row = conn.execute("SELECT created_by FROM runs WHERE thread_id = %s ORDER BY started_at LIMIT 1", (thread_id,)).fetchone()
        return row[0] if row else None

    def owns_dashboard_key(self, key: str, user_id: str) -> bool:
        with self._pool.connection() as conn:
            return conn.execute("SELECT 1 FROM runs WHERE dashboard_key = %s AND created_by = %s LIMIT 1", (key, user_id)).fetchone() is not None

    def start(self, prompt: str, *, thread_id: str | None, user_id: str, require_approval: bool,
              access: UserAccess | None = None, project_id: str | None = None) -> dict[str, str]:
        if not prompt.strip():
            raise InvalidRequest("a run needs a prompt")
        run_id = f"run-{uuid.uuid4().hex[:12]}"
        thread = thread_id or f"thread-{uuid.uuid4().hex[:12]}"
        self._insert(run_id, thread, prompt, user_id, project_id=project_id)
        self._executor.submit(self._execute, run_id, thread, user_id, prompt=prompt, require_approval=require_approval,
                              access=access, project_id=project_id)
        return {"run_id": run_id, "thread_id": thread}

    def start_job(self, *, label: str, thread_id: str, user_id: str, job: Callable[[RunContext, Any], dict[str, Any]]) -> dict[str, str]:
        """Run something other than the dashboard agent, such as connecting a source, with its
        steps stored and streamed the same way."""
        run_id = f"run-{uuid.uuid4().hex[:12]}"
        self._insert(run_id, thread_id, label, user_id, kind="job")
        self._executor.submit(self._execute_job, run_id, user_id, job)
        return {"run_id": run_id, "thread_id": thread_id}

    def _execute_job(self, run_id: str, user_id: str, job: Callable[[RunContext, Any], dict[str, Any]]) -> None:
        context = RunContext(run_id=run_id, user_id=user_id)
        try:
            status, stored = RunStatus.FINISHED.value, job(context, lambda event: self.record(run_id, event))
        except Exception as error:
            logger.exception("job crashed", extra={"run_id": run_id})
            safe = getattr(error, "message", "something went wrong; see the server logs")
            status, stored = RunStatus.FAILED.value, {"error": safe}
        with self._pool.connection() as conn:
            conn.execute("UPDATE runs SET status = %s, result = %s, finished_at = now() WHERE run_id = %s",
                         (status, Jsonb(stored), run_id))

    def resume(self, run_id: str, answer: Any, *, user_id: str, access: UserAccess | None = None) -> dict[str, str]:
        """Continue a run that paused for a person or was interrupted, as a new run on the
        same conversation."""
        previous = self.get(run_id)
        if previous["kind"] != "dashboard":
            raise InvalidRequest("only a dashboard run can be resumed; connect the source again instead")
        if previous["status"] in (RunStatus.AWAITING_APPROVAL.value, RunStatus.AWAITING_ANSWER.value):
            if answer is None:
                raise InvalidRequest("this run is waiting for an answer")
        elif previous["status"] != INTERRUPTED and previous["status"] != RunStatus.FAILED.value:
            raise InvalidRequest(f"a run that is {previous['status']} can't be resumed")
        new_run = f"run-{uuid.uuid4().hex[:12]}"
        project_id = previous.get("project_id")
        self._insert(new_run, previous["thread_id"], previous["prompt"], user_id, project_id=project_id)
        self._executor.submit(self._execute, new_run, previous["thread_id"], user_id, resume=answer, access=access,
                              project_id=project_id,
                              require_approval=previous["status"] == RunStatus.AWAITING_APPROVAL.value)
        return {"run_id": new_run, "thread_id": previous["thread_id"]}

    def record(self, run_id: str, event: dict[str, Any]) -> None:
        with self._pool.connection() as conn:
            conn.execute("INSERT INTO run_events (run_id, event) VALUES (%s, %s)", (run_id, Jsonb(event)))

    def _execute(self, run_id: str, thread_id: str, user_id: str, **options: Any) -> None:
        context = RunContext(run_id=run_id, user_id=user_id)
        try:
            result = self._runner(context=context, thread_id=thread_id, sink=lambda e: self.record(run_id, e), **options)
            status, stored = result.status.value, result.model_dump(mode="json")
        except Exception:  # a run must never die silently: the UI is waiting on it
            logger.exception("run crashed", extra={"run_id": run_id})
            status, stored = RunStatus.FAILED.value, {"error": "the run crashed; it can be resumed"}
        with self._pool.connection() as conn:
            conn.execute("UPDATE runs SET status = %s, result = %s, finished_at = now() WHERE run_id = %s",
                         (status, Jsonb(stored), run_id))

    def get(self, run_id: str) -> dict[str, Any]:
        with self._pool.connection() as conn:
            cursor = conn.execute(
                """SELECT run_id, thread_id, prompt, status, created_by, kind, project_id::text, started_at, finished_at, result,
                          EXTRACT(EPOCH FROM COALESCE(finished_at, now()) - started_at) AS wall_seconds
                   FROM runs WHERE run_id = %s""", (run_id,))
            row = cursor.fetchone()
            if row is None:
                raise NotFound(f"no run {run_id}")
            return dict(zip([c.name for c in cursor.description], row))

    def list(self, *, thread_id: str | None = None, user_id: str | None = None, limit: int = 30) -> list[dict[str, Any]]:
        """`user_id` limits it to one person's runs. Only administrators see everyone's."""
        conditions, values = ["kind = 'dashboard'"], []
        for column, value in (("thread_id", thread_id), ("created_by", user_id)):
            if value:
                conditions.append(f"{column} = %s")
                values.append(value)
        where = f"WHERE {' AND '.join(conditions)}"
        with self._pool.connection() as conn:
            cursor = conn.execute(
                f"""SELECT run_id, thread_id, prompt, status, started_at, finished_at, result->>'title' AS title,
                           result->>'summary' AS summary
                    FROM runs {where} ORDER BY started_at DESC LIMIT %s""", (*values, limit))
            columns = [c.name for c in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def events(self, run_id: str, *, after: int = 0) -> list[dict[str, Any]]:
        with self._pool.connection() as conn:
            rows = conn.execute("SELECT id, at, event FROM run_events WHERE run_id = %s AND id > %s ORDER BY id",
                                (run_id, after)).fetchall()
        return [{**event, "id": event_id, "at": at.isoformat()} for event_id, at, event in rows]
