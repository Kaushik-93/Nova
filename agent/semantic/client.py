"""Nova's client for Cube's REST API.

Only Nova's server talks to Cube. Each request carries a token signed here, valid for a minute,
holding the identity Cube's `queryRewrite` enforces (infra/cube/cube.js). The identity is built
from the same grants as everything else (`agent/harness/access.py`), so a limited user's query
through Cube sees exactly the rows their SQL would.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx
import jwt
from pydantic import BaseModel, Field

from agent.harness.access import UserAccess
from agent.harness.audit import AuditKind, AuditLog, AuditStatus, RunContext
from agent.harness.errors import (ConnectionFailure, InvalidRequest, NovaError, PermissionFailure, QueryFailure,
                                  QueryTimedOut)
from agent.tools.sources import Source

TOKEN_SECONDS = 60
CONTINUE_WAIT = "Continue wait"
REFUSALS = ("isn't available to you", "can't be limited to your data", "you have no tenants",
            "is not a member Nova can answer for", "member expression", "carries no usable identity")


class CubeIdentity(BaseModel):
    """Who is asking, as Cube sees it. `scopes` maps a catalog to the tenants this user is limited
    to there; a catalog that isn't in it isn't limited."""

    sub: str
    role: str
    catalogs: list[str]
    scopes: dict[str, list[str]] = Field(default_factory=dict)
    model: str = "live"


def identity_for(access: UserAccess | None, sources: list[Source], *, user_id: str) -> CubeIdentity:
    """`access=None` is the command line and tests, which are trusted with every source."""
    if access is None or access.is_admin:
        return CubeIdentity(sub=user_id, role="admin", catalogs=sorted(s.trino_catalog for s in sources))
    readable = [source for source in sources if access.can_read(source)]
    scopes = {}
    for source in readable:
        tenants = access.tenants_of(source)
        if tenants is not None:
            scopes[source.trino_catalog] = [str(tenant) for tenant in tenants]
    return CubeIdentity(sub=user_id, role=access.role, catalogs=sorted(s.trino_catalog for s in readable), scopes=scopes)


class CubeResult(BaseModel):
    data: list[dict[str, Any]]
    annotation: dict[str, Any] = Field(default_factory=dict)


class CubeClient:
    def __init__(self, url: str, secret: str, *, timeout_seconds: float = 60) -> None:
        if not secret:
            raise InvalidRequest("CUBEJS_API_SECRET isn't set")
        self._secret = secret
        self._timeout_seconds = timeout_seconds
        self._http = httpx.Client(base_url=f"{url.rstrip('/')}/cubejs-api/v1", timeout=30)

    def close(self) -> None:
        self._http.close()

    def token(self, identity: CubeIdentity) -> str:
        now = int(time.time())
        return jwt.encode({**identity.model_dump(), "iat": now, "exp": now + TOKEN_SECONDS}, self._secret,
                          algorithm="HS256")

    def meta(self, identity: CubeIdentity) -> dict[str, Any]:
        return self._get("meta", identity)

    def load(self, identity: CubeIdentity, query: dict[str, Any]) -> CubeResult:
        body = self._get("load", identity, query)
        return CubeResult(data=body.get("data") or [], annotation=body.get("annotation") or {})

    def sql(self, identity: CubeIdentity, query: dict[str, Any]) -> str:
        """The SQL Cube would run for a query, with this user's limits applied."""
        body = self._get("sql", identity, query)
        sql = body.get("sql") or {}
        return sql.get("sql", ["", []])[0] if isinstance(sql.get("sql"), list) else str(sql.get("sql", ""))

    def _get(self, path: str, identity: CubeIdentity, query: dict[str, Any] | None = None) -> dict[str, Any]:
        params = {"query": json.dumps(query)} if query is not None else None
        headers = {"Authorization": self.token(identity)}
        deadline = time.monotonic() + self._timeout_seconds
        while True:
            try:
                response = self._http.get(path, params=params, headers=headers)
            except httpx.HTTPError as error:
                raise ConnectionFailure(f"Cube is unreachable: {type(error).__name__}") from error
            try:
                body = response.json()
            except ValueError:
                raise QueryFailure(f"Cube answered {response.status_code} with no JSON") from None
            error = body.get("error") if isinstance(body, dict) else None
            if error == CONTINUE_WAIT:
                if time.monotonic() > deadline:
                    raise QueryTimedOut(f"Cube took longer than {self._timeout_seconds:g}s")
                time.sleep(0.25)
                headers = {"Authorization": self.token(identity)}
                continue
            if error:
                raise _translate(str(error), response.status_code)
            if response.status_code >= 400:
                raise QueryFailure(f"Cube answered {response.status_code}")
            return body



def cubes_of(query: dict[str, Any]) -> list[str]:
    """The cubes a query in Cube's format reads, from every member it names."""
    members = [*query.get("measures", []), *query.get("dimensions", []), *query.get("segments", []),
               *(item.get("dimension", "") for item in query.get("timeDimensions", [])),
               *(item.get("member", "") for item in query.get("filters", []))]
    return sorted({member.split(".")[0] for member in members if member})


class AuditedCubeClient(CubeClient):
    """Cube, as an agent run uses it: every query it loads goes in the audit log, like its SQL does,
    with what was asked, as whom, and how many rows came back or why it was refused."""

    def __init__(self, url: str, secret: str, *, audit: AuditLog, context: RunContext, timeout_seconds: float = 60) -> None:
        super().__init__(url, secret, timeout_seconds=timeout_seconds)
        self._audit = audit
        self._context = context

    def load(self, identity: CubeIdentity, query: dict[str, Any]) -> CubeResult:
        started = time.monotonic()
        record = dict(target=",".join(cubes_of(query)), statement=json.dumps(query, sort_keys=True))
        who = {"model": identity.model, "catalogs": identity.catalogs, "scopes": identity.scopes}
        try:
            result = super().load(identity, query)
        except NovaError as error:
            status = AuditStatus.REJECTED if isinstance(error, PermissionFailure) else AuditStatus.FAILED
            self._audit.record(self._context, AuditKind.SEMANTIC_QUERY, status, **record,
                               elapsed_ms=int((time.monotonic() - started) * 1000), detail={**who, "reason": error.message})
            raise
        self._audit.record(self._context, AuditKind.SEMANTIC_QUERY, AuditStatus.OK, **record, rows=len(result.data),
                           elapsed_ms=int((time.monotonic() - started) * 1000), detail=who)
        return result

def _translate(message: str, status: int) -> Exception:
    text = message.removeprefix("Error: ").strip()
    if any(phrase in text for phrase in REFUSALS):
        return PermissionFailure(text, status=status)
    return QueryFailure(text[:2000], status=status)
