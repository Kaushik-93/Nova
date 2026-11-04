"""The audit log: every SQL statement and every Superset write, including the ones
refused, with who and which run. Written to Postgres and to the JSON logs."""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import Any

from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from pydantic import BaseModel

logger = logging.getLogger("nova.audit")


class AuditKind(StrEnum):
    SQL = "sql"
    SUPERSET_WRITE = "superset_write"


class AuditStatus(StrEnum):
    OK = "ok"
    REJECTED = "rejected"
    FAILED = "failed"


class RunContext(BaseModel):
    run_id: str
    user_id: str


class AuditLog:
    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def record(self, context: RunContext, kind: AuditKind, status: AuditStatus, *, target: str = "",
               statement: str = "", rows: int | None = None, elapsed_ms: int | None = None,
               detail: dict[str, Any] | None = None) -> None:
        """Failures here propagate on purpose: an action that can't be audited
        shouldn't appear to have succeeded."""
        logger.info("audit", extra={"run_id": context.run_id, "user_id": context.user_id, "kind": kind.value,
                                    "status": status.value, "target": target, "statement": statement, "rows": rows,
                                    "elapsed_ms": elapsed_ms, "detail": detail})
        with self._pool.connection() as conn:
            conn.execute(
                """INSERT INTO audit_log (run_id, user_id, kind, status, target, statement, rows, elapsed_ms, detail)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (context.run_id, context.user_id, kind.value, status.value, target, statement, rows, elapsed_ms,
                 Jsonb(detail or {})))

    def for_run(self, run_id: str) -> list[dict[str, Any]]:
        with self._pool.connection() as conn:
            cursor = conn.execute(
                """SELECT occurred_at, run_id, user_id, kind, status, target, statement, rows, elapsed_ms, detail
                   FROM audit_log WHERE run_id = %s ORDER BY id""", (run_id,))
            columns = [c.name for c in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]
