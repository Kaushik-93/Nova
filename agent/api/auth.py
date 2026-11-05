"""Signing in, with a password or SSO, and working out who a request is from.

A session is a signed token in an HttpOnly cookie, so page scripts can't read it, and SameSite
stops other sites sending it. SSO is OpenID Connect with PKCE, and stays off until configured.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

from agent.config import Settings

logger = logging.getLogger(__name__)

COOKIE = "nova_session"
SESSION_SECONDS = 12 * 3600
ALGORITHM = "HS256"
MAX_FAILURES = 5
FAILURE_WINDOW_SECONDS = 60
SSO_DEFAULT_ROLE = "viewer"
MIN_SECRET_LENGTH = 32

hasher = PasswordHasher()
# Checked when an email isn't found, so a wrong email takes as long as a wrong password.
UNKNOWN_USER_HASH = hasher.hash("nova-unknown-user")


class User(BaseModel):
    id: str
    email: str
    display_name: str = ""
    role: str
    is_active: bool = True
    uses_sso: bool = False


class Login(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=1024)


class LoginThrottle:
    """Slows password guessing. Per process, which is fine for one API container."""

    def __init__(self) -> None:
        self._failures: dict[str, deque[float]] = defaultdict(deque)

    def blocked(self, email: str) -> bool:
        attempts = self._failures.get(email)
        if attempts is None:
            return False  # read without creating an entry, or made-up emails would fill memory
        while attempts and attempts[0] < time.monotonic() - FAILURE_WINDOW_SECONDS:
            attempts.popleft()
        if not attempts:
            del self._failures[email]
            return False
        return len(attempts) >= MAX_FAILURES

    def failed(self, email: str) -> None:
        self._failures[email].append(time.monotonic())

    def cleared(self, email: str) -> None:
        self._failures.pop(email, None)


USER_COLUMNS = "id::text, email, display_name, role, is_active, sso_subject IS NOT NULL"


def _to_user(row: tuple) -> User:
    return User(id=row[0], email=row[1], display_name=row[2], role=row[3], is_active=row[4], uses_sso=row[5])


def get_user(pool: ConnectionPool, user_id: str) -> User | None:
    with pool.connection() as conn:
        row = conn.execute(f"SELECT {USER_COLUMNS} FROM users WHERE id = %s::uuid", (user_id,)).fetchone()
    return _to_user(row) if row else None


def check_secret(secret: str) -> None:
    """Sessions are signed with this, so an empty one would let anyone forge a session."""
    if len(secret) < MIN_SECRET_LENGTH:
        raise RuntimeError(f"NOVA_JWT_SECRET must be at least {MIN_SECRET_LENGTH} characters; run `make env`")


def ensure_admin(pool: ConnectionPool, email: str, password: str) -> None:
    """Create the first admin. It never changes an existing account."""
    if not email or not password:
        return
    with pool.connection() as conn:
        conn.execute("""INSERT INTO users (email, display_name, password_hash, role) VALUES (%s, 'Administrator', %s, 'admin')
                        ON CONFLICT (email) DO NOTHING""", (email.lower(), hasher.hash(password)))


def issue_token(user_id: str, secret: str, *, lifetime: int = SESSION_SECONDS) -> str:
    now = int(time.time())
    return jwt.encode({"sub": user_id, "iat": now, "exp": now + lifetime}, secret, algorithm=ALGORITHM)


def set_session(response: Response, user_id: str, settings: Settings) -> None:
    response.set_cookie(COOKIE, issue_token(user_id, settings.nova_jwt_secret), max_age=SESSION_SECONDS, httponly=True,
                        samesite="lax", secure=settings.nova_ui_origin.startswith("https://"), path="/")


def current_user(request: Request) -> User:
    services = request.app.state.services
    token = request.cookies.get(COOKIE)
    if not token:
        raise HTTPException(401, "not signed in")
    try:
        claims = jwt.decode(token, services.settings.nova_jwt_secret, algorithms=[ALGORITHM],
                            options={"require": ["exp", "sub"]})
    except jwt.InvalidTokenError as error:
        raise HTTPException(401, "not signed in") from error
    # Read on every request, so disabling someone or changing their role applies at once.
    user = get_user(services.pool, claims["sub"])
    if user is None or not user.is_active:
        raise HTTPException(401, "not signed in")
    return user


def require_admin(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(403, "this needs an administrator")
    return user


def require_builder(user: User = Depends(current_user)) -> User:
    if user.role not in ("admin", "analyst"):
        raise HTTPException(403, "viewers can't build dashboards")
    return user


def create_sso_client(settings: Settings) -> Any | None:
    if not (settings.oidc_discovery_url and settings.oidc_client_id):
        return None
    oauth = OAuth()
    return oauth.register(name="sso", server_metadata_url=settings.oidc_discovery_url, client_id=settings.oidc_client_id,
                          client_secret=settings.oidc_client_secret or None,
                          client_kwargs={"scope": settings.oidc_scopes, "code_challenge_method": "S256"})


def _csv(value: str) -> set[str]:
    return {item.strip().lower() for item in value.split(",") if item.strip()}


class SsoRefused(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def sso_refusal(claims: dict[str, Any], settings: Settings) -> str | None:
    email = str(claims.get("email") or "").lower()
    if not email or not claims.get("sub"):
        return "missing_email"
    domains = _csv(settings.oidc_allowed_domains)
    if domains and email.rsplit("@", 1)[-1] not in domains:
        return "domain_not_allowed"
    return None


def provision_sso_user(pool: ConnectionPool, claims: dict[str, Any], settings: Settings) -> User:
    """Find or create someone signing in with SSO. A new user can see nothing until an admin
    grants them access, because being known to the identity provider isn't permission.

    An email only counts for anything if the provider says it verified it. Otherwise someone who
    can set their own email there could take over an existing account, or claim an admin's."""
    subject, email = str(claims["sub"]), str(claims["email"]).lower()
    name = str(claims.get("name") or claims.get("preferred_username") or "")
    verified = claims.get("email_verified") is True
    role = "admin" if verified and email in _csv(settings.oidc_admin_emails) else SSO_DEFAULT_ROLE
    with pool.connection() as conn:
        row = conn.execute(f"SELECT {USER_COLUMNS} FROM users WHERE sso_subject = %s", (subject,)).fetchone()
        if row is None:
            existing = conn.execute(f"SELECT {USER_COLUMNS} FROM users WHERE email = %s", (email,)).fetchone()
            if existing is None:
                row = conn.execute(
                    f"""INSERT INTO users (email, display_name, role, sso_subject) VALUES (%s, %s, %s, %s)
                        RETURNING {USER_COLUMNS}""", (email, name, role, subject)).fetchone()
            elif verified and not existing[5]:
                row = conn.execute(f"UPDATE users SET sso_subject = %s WHERE id = %s::uuid RETURNING {USER_COLUMNS}",
                                   (subject, existing[0])).fetchone()
            else:
                raise SsoRefused("account_conflict")
        conn.execute("UPDATE users SET last_login_at = now() WHERE id = %s::uuid", (row[0],))
    return _to_user(row)


router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.get("/options")
def options(request: Request) -> dict[str, Any]:
    """What the sign-in page offers. Public, and holds nothing sensitive."""
    services = request.app.state.services
    return {"sso": {"enabled": services.sso is not None, "name": services.settings.oidc_provider_name}}


@router.post("/login")
def login(body: Login, request: Request, response: Response) -> User:
    services = request.app.state.services
    email = body.email.strip().lower()
    if services.throttle.blocked(email):
        raise HTTPException(429, "too many attempts; wait a minute and try again")
    with services.pool.connection() as conn:
        row = conn.execute(f"SELECT {USER_COLUMNS}, password_hash FROM users WHERE email = %s", (email,)).fetchone()
    stored = row[6] if row else None
    try:
        hasher.verify(stored or UNKNOWN_USER_HASH, body.password)
        valid = stored is not None and row[4]
    except (VerificationError, InvalidHashError):
        valid = False
    if not valid:
        services.throttle.failed(email)
        raise HTTPException(401, "that email and password don't match")
    services.throttle.cleared(email)
    with services.pool.connection() as conn:
        conn.execute("UPDATE users SET last_login_at = now() WHERE id = %s::uuid", (row[0],))
    set_session(response, row[0], services.settings)
    return _to_user(row[:6])


@router.post("/logout")
def logout(response: Response) -> dict[str, bool]:
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
def me(user: User = Depends(current_user)) -> User:
    return user


@router.get("/sso/login")
async def sso_login(request: Request) -> Response:
    services = request.app.state.services
    if services.sso is None:
        raise HTTPException(404, "SSO isn't configured")
    callback = f"{services.settings.nova_public_api_url.rstrip('/')}/api/auth/sso/callback"
    return await services.sso.authorize_redirect(request, callback)


@router.get("/sso/callback")
async def sso_callback(request: Request) -> Response:
    services = request.app.state.services
    settings: Settings = services.settings
    if services.sso is None:
        raise HTTPException(404, "SSO isn't configured")

    def back_to_login(reason: str) -> RedirectResponse:
        return RedirectResponse(f"{settings.nova_ui_origin}/login?error={reason}", status_code=302)

    try:
        token = await services.sso.authorize_access_token(request)
    except OAuthError as error:
        logger.warning("sso failed", extra={"reason": error.error})
        return back_to_login("sso_failed")
    claims = token.get("userinfo") or await services.sso.userinfo(token=token)
    refusal = sso_refusal(claims, settings)
    if refusal:
        logger.warning("sso refused", extra={"reason": refusal})
        return back_to_login(refusal)
    try:
        user = provision_sso_user(services.pool, claims, settings)
    except SsoRefused as error:
        logger.warning("sso refused", extra={"reason": error.reason})
        return back_to_login(error.reason)
    if not user.is_active:
        return back_to_login("account_disabled")
    response = RedirectResponse(f"{settings.nova_ui_origin}/builder", status_code=302)
    set_session(response, user.id, settings)
    return response
