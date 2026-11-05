"""Managing who can sign in and what they can read. Administrators only."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from agent.api.auth import USER_COLUMNS, User, _to_user, hasher, require_admin
from agent.tools.sources import get_source_by_slug

router = APIRouter(prefix="/api/users", tags=["users"], dependencies=[Depends(require_admin)])

Role = Literal["admin", "analyst", "viewer"]


class NewUser(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    display_name: str = ""
    password: str | None = Field(default=None, min_length=10, max_length=1024)
    role: Role = "viewer"


class UserChange(BaseModel):
    role: Role | None = None
    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=10, max_length=1024)


class AccessEntry(BaseModel):
    source: str = Field(description="The source's slug")
    tenant_ids: list[str] | None = Field(default=None, description="Null for the whole source")


def _access_of(pool: Any, user_id: str) -> list[dict[str, Any]]:
    with pool.connection() as conn:
        rows = conn.execute("""SELECT s.slug, a.tenant_ids FROM source_access a JOIN sources s ON s.id = a.source_id
                               WHERE a.user_id = %s::uuid ORDER BY s.slug""", (user_id,)).fetchall()
    return [{"source": row[0], "tenant_ids": row[1]} for row in rows]


@router.get("")
def list_users(request: Request) -> list[dict[str, Any]]:
    pool = request.app.state.services.pool
    with pool.connection() as conn:
        rows = conn.execute(f"SELECT {USER_COLUMNS} FROM users ORDER BY email").fetchall()
    return [{**_to_user(row).model_dump(), "access": _access_of(pool, row[0])} for row in rows]


@router.post("", status_code=201)
def create_user(body: NewUser, request: Request) -> User:
    pool = request.app.state.services.pool
    secret = hasher.hash(body.password) if body.password else None
    with pool.connection() as conn:
        row = conn.execute(f"""INSERT INTO users (email, display_name, password_hash, role) VALUES (%s, %s, %s, %s)
                               ON CONFLICT (email) DO NOTHING RETURNING {USER_COLUMNS}""",
                           (body.email.strip().lower(), body.display_name, secret, body.role)).fetchone()
    if row is None:
        raise HTTPException(409, "someone with that email already exists")
    return _to_user(row)


@router.patch("/{user_id}")
def change_user(user_id: str, body: UserChange, request: Request, admin: User = Depends(require_admin)) -> User:
    # So that nobody can lock everyone out by accident.
    if user_id == admin.id and (body.is_active is False or (body.role and body.role != "admin")):
        raise HTTPException(400, "you can't remove your own administrator access")
    pool = request.app.state.services.pool
    with pool.connection() as conn:
        row = conn.execute(
            f"""UPDATE users SET role = COALESCE(%s, role), is_active = COALESCE(%s, is_active),
                    password_hash = COALESCE(%s, password_hash)
                WHERE id = %s::uuid RETURNING {USER_COLUMNS}""",
            (body.role, body.is_active, hasher.hash(body.password) if body.password else None, user_id)).fetchone()
    if row is None:
        raise HTTPException(404, "no such user")
    return _to_user(row)


@router.put("/{user_id}/access")
def set_access(user_id: str, entries: list[AccessEntry], request: Request) -> list[dict[str, Any]]:
    """Replace everything a user can read."""
    pool = request.app.state.services.pool
    resolved = []
    for entry in entries:
        source = get_source_by_slug(pool, entry.source)
        if source is None:
            raise HTTPException(404, f"no source named {entry.source}")
        if entry.tenant_ids is not None and not source.tenant_column:
            raise HTTPException(400, f"{entry.source} isn't set up as multi-tenant yet")
        resolved.append((source.id, entry.tenant_ids))
    with pool.connection() as conn, conn.transaction():
        conn.execute("DELETE FROM source_access WHERE user_id = %s::uuid", (user_id,))
        for source_id, tenant_ids in resolved:
            conn.execute("INSERT INTO source_access (user_id, source_id, tenant_ids) VALUES (%s::uuid, %s, %s)",
                         (user_id, source_id, tenant_ids))
    return _access_of(pool, user_id)
